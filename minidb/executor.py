"""Statement execution.

Stage 2: a single hard-coded table ``users(id, name, age)`` whose rows are
stored in a chain of data pages starting at page 1.
"""

import struct
from dataclasses import dataclass

from minidb.pager import PAGE_SIZE
from minidb.record import decode_record, encode_record


class ExecutionError(Exception):
    pass


@dataclass
class InsertStatement:
    id: int
    name: str
    age: int


@dataclass
class SelectStatement:
    pass


class DataPage:
    """A page holding a list of records: u32 next page, u16 count, then the records."""

    _header = struct.Struct(">IH")

    def __init__(self, pgno, next_page=0, records=None):
        self.pgno = pgno
        self.next_page = next_page
        self.records = records if records is not None else []
        self.size = self._header.size + sum(len(r) for r in self.records)

    @classmethod
    def from_bytes(cls, pgno, data):
        next_page, count = cls._header.unpack_from(data)
        pos = cls._header.size
        records = []
        for _ in range(count):
            _, end = decode_record(data, pos)
            records.append(bytes(data[pos:end]))
            pos = end
        return cls(pgno, next_page, records)

    def to_bytes(self):
        data = self._header.pack(self.next_page, len(self.records)) + b"".join(self.records)
        return data.ljust(PAGE_SIZE, b"\x00")

    def fits(self, record):
        return self.size + len(record) <= PAGE_SIZE


class Table:
    """The single fixed table ``users(id, name, age)``."""

    columns = ("id", "name", "age")
    first_page = 1

    def __init__(self, pager):
        self.pager = pager
        if pager.page_count == 1:
            pager.allocate(DataPage)

    def pages(self):
        pgno = self.first_page
        while pgno:
            page = self.pager.get(pgno, DataPage)
            yield page
            pgno = page.next_page

    def rows(self):
        for page in self.pages():
            for record in page.records:
                yield tuple(decode_record(record)[0])

    def insert(self, row):
        record = encode_record(list(row))
        if len(record) > PAGE_SIZE - DataPage._header.size:
            raise ExecutionError("row too large")
        for page in self.pages():
            last = page
        if not last.fits(record):
            new_page = self.pager.allocate(DataPage)
            self.pager.write(last)
            last.next_page = new_page.pgno
            last = new_page
        self.pager.write(last)
        last.records.append(record)
        last.size += len(record)

    def execute(self, stmt):
        if isinstance(stmt, InsertStatement):
            if any(row[0] == stmt.id for row in self.rows()):
                raise ExecutionError(f"duplicate id {stmt.id}")
            self.insert((stmt.id, stmt.name, stmt.age))
            return []
        if isinstance(stmt, SelectStatement):
            return sorted(self.rows())
        raise ExecutionError(f"unsupported statement {stmt!r}")
