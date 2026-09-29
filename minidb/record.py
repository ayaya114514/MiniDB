"""Serialization of rows (lists of SQL values) to bytes and back.

A record is ``header size | header | body``.  The header size is one byte,
or 0xFF followed by a u32 for very wide rows.  The header holds one type
code per value:

    0          NULL
    1, 2, 3, 4 INTEGER stored in 1, 2, 4 or 8 bytes (signed, big-endian)
    5          REAL, 8-byte IEEE double
    6, 7       the INTEGERs 0 and 1 (no body bytes)
    8          TEXT whose UTF-8 length follows in the header as a u32
    9          BLOB whose length follows in the header as a u32
    16..255    TEXT of (code - 16) UTF-8 bytes

The body holds the payloads in order.  Because the header determines the
exact layout, decoding compiles each distinct header once into a
``struct.Struct`` plus a small assembly function and caches it: decoding a
row is then a single ``unpack_from`` call.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

from minidb.values import SQLValue

NULL_CODE, REAL_CODE, ZERO_CODE, ONE_CODE, LONG_TEXT_CODE, BLOB_CODE, SHORT_TEXT_BASE = 0, 5, 6, 7, 8, 9, 16
MAX_SHORT_TEXT = 255 - SHORT_TEXT_BASE
_INT_FORMATS = {1: "b", 2: "h", 3: "i", 4: "q"}
_INT_LIMITS = [(1, 2**7), (2, 2**15), (3, 2**31), (4, 2**63)]
_u32 = struct.Struct(">I")
_real = struct.Struct(">d")
_CACHE_LIMIT = 20_000


class RecordError(Exception):
    pass


def encode_record(values: list[SQLValue]) -> bytes:
    header = bytearray()
    body = bytearray()
    for value in values:
        if value is None:
            header.append(NULL_CODE)
        elif isinstance(value, bool):
            raise RecordError("booleans are not SQL values")
        elif isinstance(value, int):
            if value == 0:
                header.append(ZERO_CODE)
            elif value == 1:
                header.append(ONE_CODE)
            else:
                for code, limit in _INT_LIMITS:
                    if -limit <= value < limit:
                        header.append(code)
                        body += value.to_bytes(1 << (code - 1), "big", signed=True)
                        break
                else:
                    raise RecordError("integer overflow")
        elif isinstance(value, float):
            header.append(REAL_CODE)
            body += _real.pack(value)
        elif isinstance(value, str):
            data = value.encode("utf-8", "surrogateescape")
            if len(data) <= MAX_SHORT_TEXT:
                header.append(SHORT_TEXT_BASE + len(data))
            else:
                header.append(LONG_TEXT_CODE)
                header += _u32.pack(len(data))
            body += data
        elif isinstance(value, bytes):
            header.append(BLOB_CODE)
            header += _u32.pack(len(value))
            body += value
        else:
            raise RecordError(f"cannot store value of type {type(value).__name__}")
    if len(header) < 0xFF:
        return bytes([len(header)]) + bytes(header) + bytes(body)
    return b"\xff" + _u32.pack(len(header)) + bytes(header) + bytes(body)


def encoded_size(values: list[SQLValue]) -> int:
    """``len(encode_record(values))`` without building the bytes."""
    header = body = 0
    for value in values:
        header += 1
        if value is None or value is True or value is False:
            continue
        if isinstance(value, int):
            if value in (0, 1):
                continue
            for code, limit in _INT_LIMITS:
                if -limit <= value < limit:
                    body += 1 << (code - 1)
                    break
        elif isinstance(value, float):
            body += 8
        elif isinstance(value, bytes):
            header += 4
            body += len(value)
        else:
            length = len(value.encode("utf-8", "surrogateescape")) if not value.isascii() else len(value)
            if length > MAX_SHORT_TEXT:
                header += 4
            body += length
    return (1 if header < 0xFF else 5) + header + body


_decoders = {}


def _compile(header: bytes) -> tuple[struct.Struct, Callable[[tuple], list[SQLValue]]]:
    """(Struct, assemble function) for records with this header."""
    fmt = [">"]
    parts = []  # Python expressions building the value list from unpacked fields ``f``
    field = 0
    i = 0
    while i < len(header):
        code = header[i]
        i += 1
        if code == NULL_CODE:
            parts.append("None")
        elif code == ZERO_CODE:
            parts.append("0")
        elif code == ONE_CODE:
            parts.append("1")
        elif code in _INT_FORMATS:
            fmt.append(_INT_FORMATS[code])
            parts.append(f"f[{field}]")
            field += 1
        elif code == REAL_CODE:
            fmt.append("d")
            parts.append(f"f[{field}]")
            field += 1
        elif code == BLOB_CODE:
            if i + 4 > len(header):
                raise RecordError("truncated record header")
            length = _u32.unpack_from(header, i)[0]
            i += 4
            fmt.append(f"{length}s")
            parts.append(f"f[{field}]")
            field += 1
        elif code == LONG_TEXT_CODE or code >= SHORT_TEXT_BASE:
            if code == LONG_TEXT_CODE:
                if i + 4 > len(header):
                    raise RecordError("truncated record header")
                length = _u32.unpack_from(header, i)[0]
                i += 4
            else:
                length = code - SHORT_TEXT_BASE
            fmt.append(f"{length}s")
            parts.append(f"f[{field}].decode('utf-8', 'surrogateescape')")
            field += 1
        else:
            raise RecordError(f"bad type code {code}")
    layout = struct.Struct("".join(fmt))
    assemble = eval(f"lambda f: [{', '.join(parts)}]")  # noqa: S307 - built from type codes only
    return layout, assemble


def decode_row(data: bytes) -> list[SQLValue]:
    """The values of a record that makes up all of ``data`` (a table row):
    decode_record without the end position, for speed."""
    size = data[0]
    if size != 0xFF:
        decoder = _decoders.get(data[1:size + 1])
        if decoder is not None:
            layout, assemble = decoder
            try:
                return assemble(layout.unpack_from(data, size + 1))
            except struct.error:
                pass  # reported by decode_record
    return decode_record(data)[0]


def decode_record(data: bytes | bytearray | memoryview, pos: int = 0) -> tuple[list[SQLValue], int]:
    """Decode a record starting at ``pos``; returns (values, end position)."""
    size = data[pos]
    pos += 1
    if size == 0xFF:
        size = _u32.unpack_from(data, pos)[0]
        pos += 4
    header = bytes(data[pos:pos + size])
    decoder = _decoders.get(header)
    if decoder is None:
        decoder = _compile(header)
        if len(_decoders) < _CACHE_LIMIT:
            _decoders[header] = decoder
    layout, assemble = decoder
    pos += size
    try:
        fields = layout.unpack_from(data, pos)
    except struct.error as exc:
        raise RecordError(f"truncated record: {exc}") from None
    return assemble(fields), pos + layout.size
