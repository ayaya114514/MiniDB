"""Storage layer: reads and writes fixed-size pages of the database file.

Pages are kept in a cache as *page objects*.  Every page class provides
``from_bytes(pgno, data)`` and ``to_bytes()`` (exactly ``USABLE_SIZE``
bytes); higher layers (the B+ tree) work directly with decoded objects, and
the pager serializes dirty pages when they are written back.  Callers must
call ``pager.write(page)`` *before* modifying a page so the pager can track
(and journal) the change.

On disk every page ends with a CRC32 of its first ``USABLE_SIZE`` bytes; a
page whose checksum does not match raises ``DatabaseError``.

Page 0 is the database header: magic string, page count, head of the free
list and a change counter that every commit increments, so other connections
notice that their cached pages are stale.  Freed pages form a singly linked
free list.

Transactions and locking: see ``Pager`` and ``minidb.locking``.

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
import time
import zlib

from minidb.errors import DatabaseError
from minidb.locking import FileLocks, LockTimeout, fsync_directory

PAGE_SIZE = 4096
CHECKSUM_SIZE = 4
USABLE_SIZE = PAGE_SIZE - CHECKSUM_SIZE
MAGIC = b"MiniDB format 2\x00"
MAGIC_PREFIX = b"MiniDB format "
WAL_MAGIC = b"MiniDB WAL 2\x00\x00\x00\x00"
COMMIT_TAG = b"CMIT"

_u32 = struct.Struct(">I")
_wal_commit = struct.Struct(">I4sI")  # frame count, COMMIT_TAG, CRC32 of the frames


class RawPage:
    """A page whose content is an uninterpreted byte array."""

    def __init__(self, pgno, data=None):
        self.pgno = pgno
        self.data = bytearray(data) if data is not None else bytearray(USABLE_SIZE)

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
        return self._format.pack(self.next_free).ljust(USABLE_SIZE, b"\x00")

    def copy(self):
        return FreePage(self.pgno, self.next_free)


class Header:
    """Page 0: magic string, page count, free list head and change counter."""

    _format = struct.Struct(">16sIII")

    def __init__(self, pgno=0, page_count=1, freelist_head=0, change_counter=0):
        self.pgno = pgno
        self.page_count = page_count
        self.freelist_head = freelist_head
        self.change_counter = change_counter

    @classmethod
    def from_bytes(cls, pgno, data):
        _magic, page_count, freelist_head, counter = cls._format.unpack_from(data)
        return cls(pgno, page_count, freelist_head, counter)

    def to_bytes(self):
        data = self._format.pack(MAGIC, self.page_count, self.freelist_head, self.change_counter)
        return data.ljust(USABLE_SIZE, b"\x00")

    def copy(self):
        return Header(self.pgno, self.page_count, self.freelist_head, self.change_counter)


def with_checksum(data):
    """A page image as stored on disk: ``data`` followed by its CRC32."""
    assert len(data) == USABLE_SIZE, len(data)
    return data + _u32.pack(zlib.crc32(data))


def verify_page(pgno, image):
    """Return the usable part of a page image, or raise if it is damaged."""
    data = image[:USABLE_SIZE]
    if _u32.unpack_from(image, USABLE_SIZE)[0] != zlib.crc32(data):
        raise DatabaseError(f"database disk image is malformed (bad checksum on page {pgno})")
    return data


class Pager:
    """Reads, caches and commits the pages of one database file.

    Transactions (driven by ``Database``):

    * ``begin_read()`` takes the SHARED lock, recovers a WAL left by a crashed
      writer, and drops the page cache if another connection has committed
      since we last looked (the header's change counter moved).
    * ``begin_write()`` takes the RESERVED lock: at most one writer.
    * ``commit()`` takes the EXCLUSIVE lock first (a busy timeout here leaves
      the transaction intact), then:

      1. writes every dirty page to ``<path>-wal`` followed by a commit
         record holding the frame count and a CRC32 of the frames, fsync;
      2. writes the pages to the database file, fsync;
      3. deletes the WAL file (and fsyncs the directory).

    * ``end_transaction()`` releases the locks.

    Dirty pages never reach the database file before step 2, so recovery
    replays a complete WAL and discards an incomplete one: either the whole
    transaction is applied or none of it.

    ``crash_hook``, if set, is called as ``crash_hook(point, detail)`` at every
    step of a commit so tests can simulate a crash there.
    """

    def __init__(self, path=None, timeout=5.0):
        """Open (or create) the database file at ``path``; ``None`` means in
        memory.  The new pager is inside a read transaction."""
        self.path = path
        self.wal_path = None if path is None else path + "-wal"
        self.crash_hook = None
        self.cache = {}
        self.dirty = set()
        self.journal = None  # pgno -> page copy (or None) while a statement runs
        self.journal_dirty = None
        self.header = None
        if path is None:
            self.file = io.BytesIO()
            self.locks = None
        else:
            created = not os.path.exists(path)
            self.file = open(path, "w+b" if created else "r+b", buffering=0)
            if created:
                fsync_directory(path)
            self.locks = FileLocks(self.file, path, timeout)
        try:
            self.begin_read()
        except BaseException:
            self.close_files()
            raise

    @property
    def page_count(self):
        return self.header.page_count

    @property
    def is_new(self):
        """True while the file has no committed header yet."""
        return 0 in self.dirty and self.header.change_counter == 0 and self.header.page_count == 1

    # ---- reading ------------------------------------------------------

    def _read_image(self, pgno):
        self.file.seek(pgno * PAGE_SIZE)
        image = self.file.read(PAGE_SIZE)
        if len(image) != PAGE_SIZE:
            raise DatabaseError(f"database disk image is malformed (short read of page {pgno})")
        return image

    def _read(self, pgno):
        return verify_page(pgno, self._read_image(pgno))

    def _read_header(self):
        """Read and validate page 0 from the file; None if the file is empty."""
        self.file.seek(0, io.SEEK_END)
        size = self.file.tell()
        if size == 0:
            return None
        if size % PAGE_SIZE:
            raise DatabaseError("database file size is not a multiple of the page size")
        image = self._read_image(0)
        if not image.startswith(MAGIC_PREFIX):
            raise DatabaseError("file is not a MiniDB database")
        if not image.startswith(MAGIC):
            raise DatabaseError(f"unsupported MiniDB file format: {image[:15].decode(errors='replace')}")
        header = Header.from_bytes(0, verify_page(0, image))
        if header.page_count > size // PAGE_SIZE:
            raise DatabaseError("database disk image is malformed (file is truncated)")
        return header

    def get(self, pgno, page_class):
        """Return page ``pgno`` decoded as ``page_class`` (cached)."""
        page = self.cache.get(pgno)
        if page is None:
            if not 0 < pgno < self.header.page_count:
                raise DatabaseError(f"database disk image is malformed (page {pgno} out of range)")
            data = self._read(pgno)
            try:
                page = page_class.from_bytes(pgno, data)
            except DatabaseError:
                raise
            except Exception as exc:
                raise DatabaseError(
                    f"database disk image is malformed (page {pgno}: {exc})"
                ) from None
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

    # ---- allocation ---------------------------------------------------

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

    def check_checksums(self):
        """Read every committed page of the file and verify its checksum;
        returns the damaged page numbers.  (Pages allocated by the current
        transaction are not in the file yet.)"""
        damaged = []
        committed = self._read_header()
        for pgno in range(committed.page_count if committed else 0):
            try:
                self._read(pgno)
            except DatabaseError:
                damaged.append(pgno)
        return damaged

    def shrink_cache(self, limit=10_000):
        """Drop clean pages from the cache once it holds more than ``limit`` pages.

        Only call this between statements: B+ tree code holds page objects
        while it works.
        """
        if len(self.cache) > limit:
            self.cache = {
                pgno: page for pgno, page in self.cache.items() if pgno in self.dirty or pgno == 0
            }

    # ---- transactions -------------------------------------------------

    def begin_read(self):
        """Start reading: SHARED lock, crash recovery, cache validation.

        Returns True if the cache was dropped because the file changed."""
        if self.dirty - {0}:
            raise AssertionError("begin_read() inside a transaction with changes")
        if self.locks is not None:
            self._shared_without_hot_wal()
        header = self._read_header()
        if header is None:
            self.cache = {}
            self.header = Header()
            self.cache[0] = self.header
            self.dirty = {0}  # the header of a new file must be written
            return True
        if (
            self.header is not None
            and not self.dirty
            and header.change_counter == self.header.change_counter
            and header.page_count == self.header.page_count
        ):
            return False
        self.cache = {0: header}
        self.header = header
        self.dirty = set()
        return True

    def _shared_without_hot_wal(self):
        """Take SHARED; if a crashed writer left a WAL, recover it first."""
        locks = self.locks
        deadline = time.monotonic() + locks.timeout
        while True:
            locks.shared()
            if not os.path.exists(self.wal_path):
                return
            # We hold SHARED, so no commit is running: the WAL is left over
            # from a crash.  Whoever gets RESERVED recovers it.
            if locks.try_reserve():
                try:
                    locks.exclusive()
                    self._recover()
                    locks.downgrade()
                finally:
                    locks.release_reserved()
                return
            locks.release_db()  # someone else is recovering; retry
            if time.monotonic() >= deadline:
                raise LockTimeout("database is locked")
            time.sleep(0.001)

    def begin_write(self, wait=True):
        """Become the (only) writer: take RESERVED."""
        if self.locks is not None:
            self.locks.reserve(wait)

    def end_transaction(self):
        if self.locks is not None:
            self.locks.release_all()

    def _crash_point(self, point, detail=None):
        if self.crash_hook is not None:
            self.crash_hook(point, detail)

    def commit(self):
        """Make every dirty page durable (see the class docstring).

        May raise ``OperationalError("database is locked")`` before anything
        is written; the transaction is then still intact."""
        if not self.dirty:
            return
        if self.locks is not None:
            self.locks.reserve()
            self.locks.exclusive()
        self.write(self.header)
        self.header.change_counter = (self.header.change_counter + 1) & 0xFFFFFFFF
        pages = [(pgno, with_checksum(self.cache[pgno].to_bytes())) for pgno in sorted(self.dirty)]
        if self.wal_path is not None:
            self._write_wal(pages)
        for i, (pgno, image) in enumerate(pages):
            self._crash_point("db_page", i)
            self.file.seek(pgno * PAGE_SIZE)
            self.file.write(image)
        if self.wal_path is not None:
            self._crash_point("db_sync")
            os.fsync(self.file.fileno())
            self._crash_point("wal_delete")
            os.remove(self.wal_path)
            fsync_directory(self.wal_path)
        self.dirty.clear()
        if self.locks is not None:
            self.locks.downgrade()

    def rollback(self):
        """Discard every uncommitted change."""
        for pgno in self.dirty:
            self.cache.pop(pgno, None)
        self.dirty.clear()
        self.end_statement()
        header = self._read_header()
        if header is None:  # a new file that was never committed
            header = Header()
            self.dirty = {0}
        self.header = header
        self.cache[0] = header

    def _write_wal(self, pages):
        with open(self.wal_path, "wb", buffering=0) as wal:
            wal.write(WAL_MAGIC + _u32.pack(PAGE_SIZE))
            checksum = 0
            for i, (pgno, image) in enumerate(pages):
                self._crash_point("wal_frame", i)
                frame = _u32.pack(pgno) + image
                wal.write(frame)
                checksum = zlib.crc32(frame, checksum)
            self._crash_point("wal_commit")
            wal.write(_wal_commit.pack(len(pages), COMMIT_TAG, checksum))
            self._crash_point("wal_sync")
            os.fsync(wal.fileno())
        fsync_directory(self.wal_path)

    def _recover(self):
        """Replay a complete WAL into the database file; discard an incomplete one."""
        frames = read_wal(self.wal_path)
        for pgno, image in frames:
            self.file.seek(pgno * PAGE_SIZE)
            self.file.write(image)
        if frames:
            os.fsync(self.file.fileno())
        os.remove(self.wal_path)
        fsync_directory(self.wal_path)

    def close_files(self):
        """Close without committing (also used after a crash)."""
        if self.locks is not None:
            self.locks.close()
        self.file.close()

    def close(self):
        """Commit any dirty pages and close the file."""
        if self.file.closed:
            return
        try:
            self.commit()
        finally:
            self.close_files()


def read_wal(path):
    """Return the committed frames [(pgno, page image)] of a WAL file, or []
    if the file is incomplete or corrupt."""
    with open(path, "rb") as wal:
        data = wal.read()
    header_size = len(WAL_MAGIC) + 4
    frame_size = 4 + PAGE_SIZE
    body = len(data) - header_size - _wal_commit.size
    if body < 0 or body % frame_size or not data.startswith(WAL_MAGIC):
        return []
    if _u32.unpack_from(data, len(WAL_MAGIC))[0] != PAGE_SIZE:
        return []
    count, tag, checksum = _wal_commit.unpack_from(data, len(data) - _wal_commit.size)
    frames_bytes = data[header_size:header_size + body]
    if tag != COMMIT_TAG or count != body // frame_size or zlib.crc32(frames_bytes) != checksum:
        return []
    frames = []
    for i in range(count):
        frame = frames_bytes[i * frame_size:(i + 1) * frame_size]
        frames.append((_u32.unpack_from(frame)[0], frame[4:]))
    return frames
