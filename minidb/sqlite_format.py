"""SQLite's file format: the pieces that are pure byte layout.

(https://www.sqlite.org/fileformat2.html)  Used by ``minidb.sqlite_pager``
and ``minidb.sqlite_btree`` for databases in SQLite's own format.

* varints: 1 to 9 bytes, big-endian, 7 bits per byte with the high bit
  saying "more"; the 9th byte carries 8 bits.  Row ids are signed 64-bit.
* records: a header (its own size as a varint, then one serial type per
  value) and the values.  Serial types: 0 NULL, 1-6 integers of 1, 2, 3,
  4, 6, 8 bytes, 7 a big-endian double, 8 and 9 the integers 0 and 1,
  even N >= 12 a BLOB of (N - 12) / 2 bytes, odd N >= 13 a text of
  (N - 13) / 2 bytes (UTF-8).
* page 1 starts with the 100-byte database header; every B-tree page has
  an 8-byte (leaf) or 12-byte (interior) header, an array of 2-byte cell
  offsets in key order, and the cells packed at the end of the page.
* a cell's payload is stored in the page up to a limit that depends on the
  page kind; the rest goes to a chain of overflow pages (4 bytes: next
  page, then data).
* pages are 512 to 65536 bytes (a power of two), the same size in a whole
  database; ``Geometry`` holds the limits that follow from it.
"""

from __future__ import annotations

import struct

from minidb.errors import DatabaseError
from minidb.values import SQLValue

MAGIC = b"SQLite format 3\x00"
DEFAULT_PAGE_SIZE = 4096  # for new databases (SQLite's default)
HEADER_SIZE = 100
PENDING_BYTE = 0x40000000  # SQLite's locks live here; the page holding it is never used
SQLITE_VERSION_NUMBER = 3053004  # written as "last writer" (the format version MiniDB follows)

# B-tree page kinds (the first byte of the page header)
INDEX_INTERIOR, TABLE_INTERIOR, INDEX_LEAF, TABLE_LEAF = 2, 5, 10, 13

_u16 = struct.Struct(">H")
_u32 = struct.Struct(">I")
_double = struct.Struct(">d")


def corrupt(detail: str) -> DatabaseError:
    return DatabaseError(f"database disk image is malformed ({detail})")


# ---- varints ----------------------------------------------------------------------


def put_varint(value: int) -> bytes:
    """The varint for ``value`` (taken as unsigned 64-bit)."""
    value &= 0xFFFFFFFFFFFFFFFF
    if value < 0x80:
        return bytes((value,))
    if value >> 56:
        out = [value & 0xFF]
        value >>= 8
        for _ in range(8):
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        return bytes(reversed(out))
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def get_varint(data: bytes, pos: int) -> tuple[int, int]:
    """(unsigned value, position after it)"""
    value = 0
    for i in range(8):
        byte = data[pos + i]
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, pos + i + 1
    return (value << 8) | data[pos + 8], pos + 9


def get_signed_varint(data: bytes, pos: int) -> tuple[int, int]:
    value, pos = get_varint(data, pos)
    return (value - (1 << 64) if value >> 63 else value), pos


def varint_size(value: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    if value >> 56:
        return 9
    size = 1
    while value >= 0x80:
        value >>= 7
        size += 1
    return size


# ---- records ------------------------------------------------------------------------

_INT_TYPES = ((1, -0x80, 0x7F), (2, -0x8000, 0x7FFF), (3, -0x800000, 0x7FFFFF),
              (4, -0x80000000, 0x7FFFFFFF), (5, -0x800000000000, 0x7FFFFFFFFFFF))
_INT_SIZES = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}


def _serial(value: SQLValue) -> tuple[int, bytes]:
    if value is None:
        return 0, b""
    if isinstance(value, int):
        if value == 0 or value == 1:
            return 8 + value, b""
        for serial, low, high in _INT_TYPES:
            if low <= value <= high:
                return serial, value.to_bytes(_INT_SIZES[serial], "big", signed=True)
        return 6, value.to_bytes(8, "big", signed=True)
    if isinstance(value, float):
        return 7, _double.pack(value)
    if isinstance(value, str):
        data = value.encode("utf-8", "surrogateescape")
        return 13 + 2 * len(data), data
    if isinstance(value, bytes):
        return 12 + 2 * len(value), value
    raise TypeError(f"cannot store {type(value).__name__}")


def encode_record(values: list[SQLValue]) -> bytes:
    serials, bodies = [], []
    for value in values:
        serial, body = _serial(value)
        serials.append(put_varint(serial))
        bodies.append(body)
    types = b"".join(serials)
    size = len(types) + 1
    if size >= 0x80:  # the header size counts its own varint
        size = len(types) + varint_size(len(types) + 2)
    return put_varint(size) + types + b"".join(bodies)


def decode_record(data: bytes) -> list[SQLValue]:
    """The values of a record.  As in minidb.record, each distinct header is
    compiled once into a ``struct.Struct`` and an assembly function."""
    try:
        header_size = data[0]
        if header_size >= 0x80:
            header_size = get_varint(data, 0)[0]
        header = bytes(data[:header_size])
        decoder = _decoders.get(header)
        if decoder is None:
            decoder = _compile(header)
            if len(_decoders) < _CACHE_LIMIT:
                _decoders[header] = decoder
        layout, assemble = decoder
        return assemble(layout.unpack_from(data, header_size))
    except (IndexError, struct.error):
        raise corrupt("bad record") from None


_decoders: dict = {}
_CACHE_LIMIT = 20_000
_FIXED = {1: "b", 2: "h", 4: "i", 6: "q", 7: "d"}


def _compile(header: bytes) -> tuple[struct.Struct, object]:
    pos = get_varint(header, 0)[1]
    fmt, parts, field = [">"], [], 0
    while pos < len(header):
        serial, pos = get_varint(header, pos)
        if serial == 0:
            parts.append("None")
            continue
        if serial in (8, 9):
            parts.append(str(serial - 8))
            continue
        if serial in _FIXED:
            fmt.append(_FIXED[serial])
            parts.append(f"f[{field}]")
        elif serial in (3, 5):  # 24- and 48-bit integers
            fmt.append(f"{_INT_SIZES[serial]}s")
            parts.append(f"int.from_bytes(f[{field}], 'big', signed=True)")
        elif serial >= 12:
            fmt.append(f"{(serial - 12) >> 1}s")
            parts.append(f"f[{field}].decode('utf-8', 'surrogateescape')" if serial & 1 else f"f[{field}]")
        else:
            raise corrupt(f"bad serial type {serial}")
        field += 1
    if pos != len(header):
        raise corrupt("bad record header")
    assemble = eval(f"lambda f: [{', '.join(parts)}]")  # noqa: S307 - built from serial types only
    return struct.Struct("".join(fmt)), assemble


# ---- the database header ------------------------------------------------------------


class DbHeader:
    """The first 100 bytes of page 1, kept as page 0 of the pager's cache."""

    _format = struct.Struct(">16sHBBBBBBIIIIIIIIIIII20sII")

    def __init__(self, data: bytes | None = None, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        self.pgno = 0
        if data is None:  # a new database
            data = self._format.pack(
                MAGIC, 1 if page_size == 65536 else page_size, 1, 1, 0, 64, 32, 32, 0, 1, 0, 0, 0, 4, 0, 0, 1, 0, 0, 0,
                b"\x00" * 20, 0, SQLITE_VERSION_NUMBER)
        fields = list(self._format.unpack_from(data))
        (self.magic, page_size, self.write_version, self.read_version, self.reserved, self.max_fraction,
         self.min_fraction, self.leaf_fraction, self.change_counter, self.page_count, self.freelist_trunk,
         self.freelist_count, self.schema_cookie, self.schema_format, self.cache_size, self.autovacuum_root,
         self.encoding, self.user_version, self.incremental_vacuum, self.application_id, self.padding,
         self.version_valid_for, self.version_number) = fields
        self.page_size = 65536 if page_size == 1 else page_size

    @classmethod
    def from_bytes(cls, pgno: int, data: bytes, geometry: Geometry | None = None) -> DbHeader:
        return cls(data)

    def check(self, file_size: int) -> None:
        """Refuse what MiniDB does not handle (and say why)."""
        if self.magic != MAGIC:
            raise DatabaseError("file is not a database")
        if not valid_page_size(self.page_size):
            raise DatabaseError("file is not a database")
        file_pages = file_size // self.page_size
        if self.write_version == 2 or self.read_version == 2:
            raise DatabaseError("SQLite databases in WAL mode are not supported: "
                                "PRAGMA journal_mode = DELETE with sqlite3 first")
        if self.write_version > 2 or self.read_version > 2:
            raise DatabaseError("unsupported file format")
        if self.reserved:
            raise DatabaseError("SQLite databases with reserved bytes per page are not supported")
        if self.encoding not in (0, 1):
            raise DatabaseError("SQLite databases in UTF-16 are not supported")
        if self.autovacuum_root:
            raise DatabaseError("SQLite databases with auto_vacuum are not supported")
        if not 1 <= self.schema_format <= 4:
            raise DatabaseError("unsupported schema format")
        if self.version_valid_for != self.change_counter or self.page_count == 0:
            self.page_count = file_pages  # written by a version that did not keep it (as SQLite)

    def to_bytes(self) -> bytes:
        self.version_valid_for = self.change_counter
        self.version_number = SQLITE_VERSION_NUMBER
        return self._format.pack(
            MAGIC, 1 if self.page_size == 65536 else self.page_size, self.write_version, self.read_version,
            self.reserved, self.max_fraction, self.min_fraction, self.leaf_fraction, self.change_counter,
            self.page_count, self.freelist_trunk, self.freelist_count, self.schema_cookie, self.schema_format,
            self.cache_size, self.autovacuum_root, self.encoding or 1, self.user_version,
            self.incremental_vacuum, self.application_id, self.padding, self.version_valid_for,
            self.version_number)

    def copy(self) -> DbHeader:
        return DbHeader(self.to_bytes())


# ---- payloads and cells -------------------------------------------------------------

def valid_page_size(size: int) -> bool:
    return 512 <= size <= 65536 and size & (size - 1) == 0


class Geometry:
    """The sizes that follow from a database's page size (no reserved bytes
    at the end of pages, so all of a page is usable)."""

    def __init__(self, page_size: int) -> None:
        self.page_size = page_size
        self.usable = usable = page_size
        self.min_local = (usable - 12) * 32 // 255 - 23
        self.table_max_local = usable - 35
        self.index_max_local = (usable - 12) * 64 // 255 - 23
        self.overflow_data = usable - 4  # payload bytes per overflow page
        self.lock_page = PENDING_BYTE // page_size + 1
        self.max_leaves = usable // 4 - 8  # leaves SQLite writes on a freelist trunk
        self.read_leaves = usable // 4 - 2  # leaves it accepts there

    def local_size(self, payload: int, table: bool) -> int:
        """How much of a payload of ``payload`` bytes is stored in the cell."""
        max_local = self.table_max_local if table else self.index_max_local
        if payload <= max_local:
            return payload
        size = self.min_local + (payload - self.min_local) % self.overflow_data
        return size if size <= max_local else self.min_local


DEFAULT = Geometry(DEFAULT_PAGE_SIZE)


class Cell:
    """A cell: for table leaves the row id and the payload (a record), for
    table interiors a child page and the row id that bounds it, for index
    pages the key record (plus the child page on interior pages).

    ``local`` is the part of the payload stored in the page and
    ``overflow`` the first overflow page (0 if none); ``size`` is the total
    payload size.  ``key`` caches the decoded key of an index cell.

    A cell on a page is never changed (the B-tree code copies one before
    setting its child), so page copies share cells, and a cell caches its
    size on a page of the last kind asked for."""

    __slots__ = ("child", "rowid", "local", "size", "overflow", "key", "sized_for", "bytes")

    def __init__(self, child: int = 0, rowid: int = 0, local: bytes = b"", size: int = 0, overflow: int = 0) -> None:
        self.child = child
        self.rowid = rowid
        self.local = local
        self.size = size
        self.overflow = overflow
        self.key = None
        self.sized_for = None

    def copy(self) -> Cell:
        cell = Cell(self.child, self.rowid, self.local, self.size, self.overflow)
        cell.key = self.key
        return cell

    def byte_size(self, kind: int) -> int:
        """Bytes of this cell on a page of ``kind``, plus its 2-byte pointer."""
        if self.sized_for == kind:
            return self.bytes
        if kind == TABLE_INTERIOR:
            size = 6 + varint_size(self.rowid)
        else:
            size = varint_size(self.size) + len(self.local) + (4 if self.overflow else 0) + 2
            if kind == TABLE_LEAF:
                size += varint_size(self.rowid)
            elif kind == INDEX_INTERIOR:
                size += 4
        self.sized_for, self.bytes = kind, size
        return size

    def to_bytes(self, kind: int) -> bytes:
        if kind == TABLE_INTERIOR:
            return _u32.pack(self.child) + put_varint(self.rowid)
        parts = [put_varint(self.size)]
        if kind == TABLE_LEAF:
            parts.append(put_varint(self.rowid))
        elif kind == INDEX_INTERIOR:
            parts.insert(0, _u32.pack(self.child))
        parts.append(self.local)
        if self.overflow:
            parts.append(_u32.pack(self.overflow))
        return b"".join(parts)


def parse_cell(data: bytes, pos: int, kind: int, geometry: Geometry) -> Cell:
    cell = Cell()
    if kind in (TABLE_INTERIOR, INDEX_INTERIOR):
        cell.child = _u32.unpack_from(data, pos)[0]
        pos += 4
        if kind == TABLE_INTERIOR:
            cell.rowid = get_signed_varint(data, pos)[0]
            return cell
    cell.size, pos = get_varint(data, pos)
    if kind == TABLE_LEAF:
        cell.rowid, pos = get_signed_varint(data, pos)
    local = geometry.local_size(cell.size, kind == TABLE_LEAF)
    cell.local = bytes(data[pos:pos + local])
    if local < cell.size:
        cell.overflow = _u32.unpack_from(data, pos + local)[0]
    if len(cell.local) != local:
        raise corrupt("cell runs past the page")
    return cell


# ---- pages ---------------------------------------------------------------------------


class BtreePage:
    """A decoded B-tree page.  On page 1 the B-tree part starts after the
    database header (``offset`` 100)."""

    def __init__(self, pgno: int, kind: int, cells: list[Cell] | None = None, right: int = 0, *,
                 geometry: Geometry) -> None:
        self.pgno = pgno
        self.kind = kind
        self.cells = cells if cells is not None else []
        self.right = right
        self.geometry = geometry

    @property
    def offset(self) -> int:
        return HEADER_SIZE if self.pgno == 1 else 0

    @property
    def is_leaf(self) -> bool:
        return self.kind in (TABLE_LEAF, INDEX_LEAF)

    @property
    def header_size(self) -> int:
        return 8 if self.is_leaf else 12

    @property
    def capacity(self) -> int:
        """Bytes available for cells and their pointers."""
        return self.geometry.usable - self.offset - self.header_size

    def used(self) -> int:
        kind = self.kind
        total = 0
        for cell in self.cells:
            total += cell.bytes if cell.sized_for == kind else cell.byte_size(kind)
        return total

    @classmethod
    def from_bytes(cls, pgno: int, data: bytes, geometry: Geometry) -> BtreePage:
        offset = HEADER_SIZE if pgno == 1 else 0
        kind = data[offset]
        if kind not in (INDEX_INTERIOR, TABLE_INTERIOR, INDEX_LEAF, TABLE_LEAF):
            raise corrupt(f"page {pgno} is not a B-tree page")
        count = _u16.unpack_from(data, offset + 3)[0]
        leaf = kind in (TABLE_LEAF, INDEX_LEAF)
        right = 0 if leaf else _u32.unpack_from(data, offset + 8)[0]
        pointers = offset + (8 if leaf else 12)
        try:
            cells = [parse_cell(data, _u16.unpack_from(data, pointers + 2 * i)[0], kind, geometry)
                     for i in range(count)]
        except (IndexError, struct.error):
            raise corrupt(f"bad cell on page {pgno}") from None
        return cls(pgno, kind, cells, right, geometry=geometry)

    def to_bytes(self) -> bytes:
        kind, offset = self.kind, self.offset
        page = bytearray(self.geometry.page_size)
        top = self.geometry.usable
        pointers = []
        for cell in self.cells:
            data = cell.to_bytes(kind)
            top -= len(data)
            page[top:top + len(data)] = data
            pointers.append(top)
        start = offset + self.header_size
        if start + 2 * len(pointers) > top:
            raise AssertionError(f"page {self.pgno} overfull")
        page[offset] = kind
        page[offset + 1:offset + 3] = b"\x00\x00"  # no freeblocks
        page[offset + 3:offset + 5] = _u16.pack(len(self.cells))
        page[offset + 5:offset + 7] = _u16.pack(top % 65536)
        page[offset + 7] = 0  # no fragmented bytes
        if not self.is_leaf:
            page[offset + 8:offset + 12] = _u32.pack(self.right)
        for i, pointer in enumerate(pointers):
            page[start + 2 * i:start + 2 * i + 2] = _u16.pack(pointer)
        return bytes(page)

    def copy(self) -> BtreePage:
        return BtreePage(self.pgno, self.kind, list(self.cells), self.right,
                         geometry=self.geometry)  # (cells are not changed)


class OverflowPage:
    def __init__(self, pgno: int, next_page: int = 0, data: bytes = b"", *, geometry: Geometry) -> None:
        self.pgno = pgno
        self.next_page = next_page
        self.data = data
        self.geometry = geometry

    @classmethod
    def from_bytes(cls, pgno: int, data: bytes, geometry: Geometry) -> OverflowPage:
        return cls(pgno, _u32.unpack_from(data)[0], bytes(data[4:geometry.usable]), geometry=geometry)

    def to_bytes(self) -> bytes:
        return (_u32.pack(self.next_page) + self.data).ljust(self.geometry.page_size, b"\x00")

    def copy(self) -> OverflowPage:
        return OverflowPage(self.pgno, self.next_page, self.data, geometry=self.geometry)


class TrunkPage:
    """A freelist trunk page: the next trunk and a list of free leaf pages."""

    def __init__(self, pgno: int, next_trunk: int = 0, leaves: list[int] | None = None, *,
                 geometry: Geometry) -> None:
        self.pgno = pgno
        self.next_trunk = next_trunk
        self.leaves = leaves if leaves is not None else []
        self.geometry = geometry

    @classmethod
    def from_bytes(cls, pgno: int, data: bytes, geometry: Geometry) -> TrunkPage:
        next_trunk, count = struct.unpack_from(">II", data)
        if count > geometry.read_leaves:
            raise corrupt(f"freelist trunk page {pgno}")
        return cls(pgno, next_trunk, list(struct.unpack_from(f">{count}I", data, 8)), geometry=geometry)

    def to_bytes(self) -> bytes:
        data = struct.pack(f">II{len(self.leaves)}I", self.next_trunk, len(self.leaves), *self.leaves)
        return data.ljust(self.geometry.page_size, b"\x00")

    def copy(self) -> TrunkPage:
        return TrunkPage(self.pgno, self.next_trunk, list(self.leaves), geometry=self.geometry)


class FreePage:
    """A free leaf page: its content does not matter (written as zeros)."""

    def __init__(self, pgno: int, *, geometry: Geometry) -> None:
        self.pgno = pgno
        self.geometry = geometry

    @classmethod
    def from_bytes(cls, pgno: int, data: bytes, geometry: Geometry) -> FreePage:
        return cls(pgno, geometry=geometry)

    def to_bytes(self) -> bytes:
        return bytes(self.geometry.page_size)

    def copy(self) -> FreePage:
        return FreePage(self.pgno, geometry=self.geometry)
