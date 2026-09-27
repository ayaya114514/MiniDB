"""Serialization of rows (lists of SQL values) to bytes and back.

Layout: a u16 value count, then for every value a one-byte type tag and payload:

    0  NULL     no payload
    1  INTEGER  8-byte signed big-endian
    2  REAL     8-byte IEEE double
    3  TEXT     u32 byte length + UTF-8 bytes
"""

import struct

NULL, INTEGER, REAL, TEXT = 0, 1, 2, 3

_count = struct.Struct(">H")
_int = struct.Struct(">q")
_real = struct.Struct(">d")
_len = struct.Struct(">I")


class RecordError(Exception):
    pass


def encode_value(value, out):
    if value is None:
        out.append(NULL)
    elif isinstance(value, bool):
        raise RecordError("booleans are not SQL values")
    elif isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise RecordError("integer overflow")
        out.append(INTEGER)
        out += _int.pack(value)
    elif isinstance(value, float):
        out.append(REAL)
        out += _real.pack(value)
    elif isinstance(value, str):
        data = value.encode("utf-8")
        out.append(TEXT)
        out += _len.pack(len(data))
        out += data
    else:
        raise RecordError(f"cannot store value of type {type(value).__name__}")


def decode_value(data, pos):
    tag = data[pos]
    pos += 1
    if tag == NULL:
        return None, pos
    if tag == INTEGER:
        return _int.unpack_from(data, pos)[0], pos + 8
    if tag == REAL:
        return _real.unpack_from(data, pos)[0], pos + 8
    if tag == TEXT:
        (length,) = _len.unpack_from(data, pos)
        pos += 4
        return bytes(data[pos:pos + length]).decode("utf-8"), pos + length
    raise RecordError(f"bad type tag {tag}")


def encode_record(values):
    out = bytearray(_count.pack(len(values)))
    for value in values:
        encode_value(value, out)
    return bytes(out)


def decode_record(data, pos=0):
    """Decode a record starting at ``pos``; returns (values, end position)."""
    (count,) = _count.unpack_from(data, pos)
    pos += 2
    values = []
    for _ in range(count):
        value, pos = decode_value(data, pos)
        values.append(value)
    return values, pos
