"""Storage layer: reads and writes fixed-size pages of the database file.

Pages are kept in a cache as *page objects*.  Every page class provides
``from_bytes(pgno, data)`` and ``to_bytes()``; higher layers (the B+ tree)
work directly with decoded objects, and the pager serializes dirty pages when
they are written back.  Callers must call ``pager.write(page)`` *before*
modifying a page so the pager can track (and later journal) the change.

Page 0 is the database header.  Freed pages form a singly linked free list.

Statement journal: between ``begin_statement()`` and ``end_statement()`` the
pager keeps a copy of every page as it was before the statement first touched
it (every page class implements ``copy()``), so ``rollback_statement()`` can
undo a statement that failed halfway.  Dirty pages are never written to the
file before a commit, so a page that was not cached still has its old content
on disk and needs no copy.
"""

import io
import os
import struct

from minidb.errors import DatabaseError

PAGE_SIZE = 4096
MAGIC = b"MiniDB format 1\x00"


class RawPage:
    """A page whose content is an uninterpreted byte array."""

    def __init__(self, pgno, data=None):
        self.pgno = pgno
        self.data = bytearray(data) if data is not None else bytearray(PAGE_SIZE)

    @classmethod
    def from_bytes(cls, pgno, data):
        return cls(pgno, data)

    def to_bytes(self):
        return bytes(self.data)

    def copy(self):
        return RawPage(self.pgno, self.data)


class FreePage:
    """A page on the free list; stores the next free page number."""

    _format = struct.Struct(">I")

    def __init__(self, pgno, next_free=0):
        self.pgno = pgno
        self.next_free = next_free

    @classmethod
    def from_bytes(cls, pgno, data):
        return cls(pgno, cls._format.unpack_from(data)[0])

    def to_bytes(self):
        return self._format.pack(self.next_free).ljust(PAGE_SIZE, b"\x00")

    def copy(self):
        return FreePage(self.pgno, self.next_free)


class Header:
    """Page 0: magic string, total page count and head of the free list."""

    _format = struct.Struct(">16sII")

    def __init__(self, pgno=0, page_count=1, freelist_head=0):
        self.pgno = pgno
        self.page_count = page_count
        self.freelist_head = freelist_head

    @classmethod
    def from_bytes(cls, pgno, data):
        magic, page_count, freelist_head = cls._format.unpack_from(data)
        if magic != MAGIC:
            raise DatabaseError("file is not a MiniDB database")
        return cls(pgno, page_count, freelist_head)

    def to_bytes(self):
        data = self._format.pack(MAGIC, self.page_count, self.freelist_head)
        return data.ljust(PAGE_SIZE, b"\x00")

    def copy(self):
        return Header(self.pgno, self.page_count, self.freelist_head)


class Pager:
    def __init__(self, path=None):
        """Open (or create) the database file at ``path``; ``None`` means in memory."""
        self.path = path
        if path is None:
            self.file = io.BytesIO()
        else:
            self.file = open(path, "r+b" if os.path.exists(path) else "w+b")
        self.file.seek(0, io.SEEK_END)
        size = self.file.tell()
        if size % PAGE_SIZE:
            raise DatabaseError("database file size is not a multiple of the page size")
        self.cache = {}
        self.dirty = set()
        self.journal = None  # pgno -> page copy (or None) while a statement runs
        self.journal_dirty = None
        if size == 0:
            self.header = Header()
            self.cache[0] = self.header
            self.dirty.add(0)
        else:
            self.header = Header.from_bytes(0, self._read(0))
            self.cache[0] = self.header
            if self.header.page_count > size // PAGE_SIZE:
                raise DatabaseError("database file is truncated")

    @property
    def page_count(self):
        return self.header.page_count

    def _read(self, pgno):
        self.file.seek(pgno * PAGE_SIZE)
        data = self.file.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise DatabaseError(f"short read of page {pgno}")
        return data

    def get(self, pgno, page_class):
        """Return page ``pgno`` decoded as ``page_class`` (cached)."""
        page = self.cache.get(pgno)
        if page is None:
            if not 0 < pgno < self.header.page_count:
                raise DatabaseError(f"page number {pgno} out of range")
            page = page_class.from_bytes(pgno, self._read(pgno))
            self.cache[pgno] = page
        return page

    def write(self, page):
        """Declare that ``page`` is about to be modified (or replaced by ``page``)."""
        pgno = page.pgno
        if self.journal is not None and pgno not in self.journal:
            old = self.cache.get(pgno)
            self.journal[pgno] = old.copy() if old is not None else None
        self.dirty.add(pgno)
        self.cache[pgno] = page

    # ---- statements ---------------------------------------------------

    def begin_statement(self):
        self.journal = {}
        self.journal_dirty = set(self.dirty)

    def end_statement(self):
        self.journal = None
        self.journal_dirty = None

    def rollback_statement(self):
        """Undo every change made since ``begin_statement()``."""
        for pgno, old in self.journal.items():
            if old is None:
                self.cache.pop(pgno, None)
            else:
                self.cache[pgno] = old
        self.dirty = self.journal_dirty
        self.header = self.cache[0]
        self.end_statement()

    def allocate(self, page_class, *args):
        """Allocate a page (reusing the free list first) as ``page_class(pgno, *args)``."""
        header = self.header
        self.write(header)
        if header.freelist_head:
            pgno = header.freelist_head
            header.freelist_head = self.get(pgno, FreePage).next_free
        else:
            pgno = header.page_count
            header.page_count += 1
        page = page_class(pgno, *args)
        self.write(page)
        return page

    def free(self, pgno):
        header = self.header
        self.write(header)
        self.write(FreePage(pgno, header.freelist_head))
        header.freelist_head = pgno

    def free_page_count(self):
        count = 0
        pgno = self.header.freelist_head
        while pgno:
            count += 1
            pgno = self.get(pgno, FreePage).next_free
        return count

    def shrink_cache(self, limit=10_000):
        """Drop clean pages from the cache once it holds more than ``limit`` pages.

        Only call this between statements: B+ tree code holds page objects
        while it works.
        """
        if len(self.cache) > limit:
            self.cache = {
                pgno: page for pgno, page in self.cache.items() if pgno in self.dirty or pgno == 0
            }

    def commit(self):
        self.flush()

    def flush(self):
        """Write every dirty page back to the file."""
        for pgno in sorted(self.dirty):
            self.file.seek(pgno * PAGE_SIZE)
            self.file.write(self.cache[pgno].to_bytes())
        self.dirty.clear()
        self.file.flush()

    def close(self):
        if self.file.closed:
            return
        self.flush()
        self.file.close()
