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

Transactions, the write-ahead log and locking: see ``Pager`` and ``minidb.locking``.

Statement journal: between ``begin_statement()`` and ``end_statement()`` the
pager keeps a copy of every page as it was before the statement first touched
it (every page class implements ``copy()``), so ``rollback_statement()`` can
undo a statement that failed halfway.  Dirty pages are never written to the
file before a commit, so a page that was not cached still has its old content
on disk and needs no copy.
"""

from __future__ import annotations

import io
import os
import struct
import time
import zlib
from bisect import bisect_right
from collections.abc import Callable
from typing import Any, Protocol, Self

from minidb.errors import DatabaseError
from minidb.locking import READ_SLOTS
from minidb.locking import FileLocks, LockTimeout, fsync_directory

PAGE_SIZE = 4096
CHECKSUM_SIZE = 4
USABLE_SIZE = PAGE_SIZE - CHECKSUM_SIZE
MAGIC = b"MiniDB format 3\x00"
MAGIC_PREFIX = b"MiniDB format "
WAL_MAGIC = b"MiniDB WAL 3\x00\x00\x00\x00"

_u32 = struct.Struct(">I")
_wal_header = struct.Struct(">16sII")  # magic, page size, generation
_frame_fields = struct.Struct(">III")  # page number, page count if commit frame else 0, generation
_frame_header = struct.Struct(">IIII")  # the fields above + chained CRC32
FRAME_SIZE = _frame_header.size + PAGE_SIZE
# <db>-shm: magic, log generation, frames copied into the database file
# (backfilled), then per read slot: (generation, frames in the snapshot).
_shm_header = struct.Struct(">4sII")
_shm_mark = struct.Struct(">II")
_shm_marks = struct.Struct(">" + "II" * (READ_SLOTS + 1))
SHM_MAGIC = b"MDBS"


def _pread(fd: int, size: int, offset: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)  # pragma: no cover - Windows
    return os.read(fd, size)


class Page(Protocol):
    """What the pager needs from a page object."""

    pgno: int

    def to_bytes(self) -> bytes: ...

    def copy(self) -> Page: ...


class PageDecoder(Protocol):
    """A page class, or anything else that decodes page images."""

    def from_bytes(self, pgno: int, data: bytes) -> Page: ...


class RawPage:
    """A page whose content is an uninterpreted byte array."""

    def __init__(self, pgno: int, data: bytes | bytearray | None = None) -> None:
        self.pgno = pgno
        self.data = bytearray(data) if data is not None else bytearray(USABLE_SIZE)

    @classmethod
    def from_bytes(cls, pgno: int, data: bytes) -> Self:
        return cls(pgno, data)

    def to_bytes(self) -> bytes:
        return bytes(self.data)

    def copy(self) -> Self:
        return RawPage(self.pgno, self.data)


class FreePage:
    """A page on the free list; stores the next free page number."""

    _format = struct.Struct(">I")

    def __init__(self, pgno: int, next_free: int = 0) -> None:
        self.pgno = pgno
        self.next_free = next_free

    @classmethod
    def from_bytes(cls, pgno: int, data: bytes) -> Self:
        return cls(pgno, cls._format.unpack_from(data)[0])

    def to_bytes(self) -> bytes:
        return self._format.pack(self.next_free).ljust(USABLE_SIZE, b"\x00")

    def copy(self) -> Self:
        return FreePage(self.pgno, self.next_free)


class Header:
    """Page 0: magic string, page count, free list head and change counter."""

    _format = struct.Struct(">16sIII")

    def __init__(self, pgno: int = 0, page_count: int = 1, freelist_head: int = 0, change_counter: int = 0) -> None:
        self.pgno = pgno
        self.page_count = page_count
        self.freelist_head = freelist_head
        self.change_counter = change_counter

    @classmethod
    def from_bytes(cls, pgno: int, data: bytes) -> Self:
        _magic, page_count, freelist_head, counter = cls._format.unpack_from(data)
        return cls(pgno, page_count, freelist_head, counter)

    def to_bytes(self) -> bytes:
        data = self._format.pack(MAGIC, self.page_count, self.freelist_head, self.change_counter)
        return data.ljust(USABLE_SIZE, b"\x00")

    def copy(self) -> Self:
        return Header(self.pgno, self.page_count, self.freelist_head, self.change_counter)


def with_checksum(data: bytes) -> bytes:
    """A page image as stored on disk: ``data`` followed by its CRC32."""
    assert len(data) == USABLE_SIZE, len(data)
    return data + _u32.pack(zlib.crc32(data))


def verify_page(pgno: int, image: bytes) -> bytes:
    """Return the usable part of a page image, or raise if it is damaged."""
    data = image[:USABLE_SIZE]
    if _u32.unpack_from(image, USABLE_SIZE)[0] != zlib.crc32(data):
        raise DatabaseError(f"database disk image is malformed (bad checksum on page {pgno})")
    return data


class Pager:
    """Reads, caches and commits the pages of one database file.

    File databases use a write-ahead log in ``<path>-wal`` (SQLite's WAL
    mode).  The log is a header followed by frames; each frame is a page
    image with a header holding its page number, the page count if it ends a
    commit (else 0), the log's generation and a CRC32 chained through all
    frames of the generation.  A frame counts only if its generation and
    checksum match; frames after the last commit frame do not count.

    * ``begin_read()`` fixes a *snapshot*: the frames up to the last commit,
      protected from checkpoints by a read slot (``_take_read_slot``).  A page is read from its newest frame within the
      snapshot, otherwise from the database file.  Other connections may
      commit meanwhile without disturbing the snapshot.
    * ``begin_write()`` takes RESERVED (one writer at a time).  A transaction
      whose snapshot is no longer the newest state cannot start writing
      ("database is locked"), as in SQLite.
    * ``commit()`` appends the dirty pages as frames, the last one marked as
      commit frame, and fsyncs the log.  The database file is not touched.
    * ``spill()`` appends dirty pages early as uncommitted frames, so a large
      transaction need not keep them in memory; ``rollback()`` truncates the
      log back to the last commit.
    * ``checkpoint()`` copies frames into the database file (backfills), up
      to the oldest snapshot a reader still uses (its read mark), shrinks
      the file to the page count of that state and records how far it got
      in ``<db>-shm``.  Once everything is copied and no one reads it
      empties the log.  It never waits.
    * Frames already copied are read from the database file, so a fully
      copied log is needed by no one: a writer then starts it afresh (a new
      generation) even while others read.  A long log that readers keep
      from being copied makes the writer wait briefly for them
      (``_wait_for_readers``), so the log stays short even while there are
      always readers.

    A crash can only leave frames after the last commit frame, which readers
    ignore and the next writer truncates; a crash during a checkpoint leaves
    the log intact.  In-memory databases have no log: commits write pages
    directly.

    ``crash_hook``, if set, is called as ``crash_hook(point, detail)`` at every
    step of a commit or checkpoint so tests can simulate a crash there.
    """

    def __init__(self, path: str | None = None, timeout: float = 5.0) -> None:
        """Open (or create) the database file at ``path``; ``None`` means in
        memory.  The new pager is inside a read transaction."""
        self.path = path
        self.wal_path = None if path is None else path + "-wal"
        self.crash_hook = None
        self.checkpoint_frames = 1000  # a writer copies a log this long first
        self.restart_wait = 0.1  # seconds a writer waits for readers of a long log
        self.cache = {}
        self.dirty = set()
        self.journal = None  # pgno -> page copy (or None) while a statement runs
        self.journal_dirty = None
        self.header = None
        self.reading = False
        self.read_snapshot = None  # (generation, committed) when page 0 was last read
        self.wal = None
        self._reset_wal_index()
        if path is None:
            self.file = io.BytesIO()
            self.locks = None
        else:
            created = not os.path.exists(path)
            self.file = open(path, "w+b" if created else "r+b", buffering=0)
            if created:
                fsync_directory(path)
            self.locks = FileLocks(path, timeout)
            wal_exists = os.path.exists(self.wal_path)
            self.wal = open(self.wal_path, "r+b" if wal_exists else "w+b", buffering=0)
            if not wal_exists:
                fsync_directory(self.wal_path)
            if self.locks.try_lock_out_readers():
                # No one else reads: forget what <db>-shm says, it may be
                # left over from a crash (copying again is always safe).
                self.locks.file.write(0, _shm_header.pack(SHM_MAGIC, 0, 0))
                self.locks.unlock_readers()
        try:
            self.begin_read()
        except BaseException:
            self.close_files()
            raise

    @property
    def page_count(self) -> int:
        return self.header.page_count

    @property
    def is_new(self) -> bool:
        """True while the file has no committed header yet."""
        return 0 in self.dirty and self.header.change_counter == 0 and self.header.page_count == 1

    # ---- the write-ahead log ------------------------------------------

    def _reset_wal_index(self) -> None:
        self.wal_generation = None
        self.wait_at = 0  # see _wait_for_readers
        self.frames = {}  # pgno -> ascending frame numbers holding it
        self.committed = 0  # frames in the snapshot (the last commit frame)
        self.frame_total = 0  # plus our own uncommitted frames
        self.scan_crc = 0  # checksum chain after the last committed frame
        self.append_crc = 0

    # ---- <db>-shm: backfill and read marks ------------------------------
    #
    # Not durable, and not trusted beyond the log generation it names: after
    # a crash it may be stale, which only makes checkpoints copy again.

    def _read_marks(self) -> list[tuple[int, int]]:
        """[(generation, frames)] per read slot.  Missing bytes read as
        zeros: generation 0 is never used."""
        data = self.locks.file.read(_shm_header.size, _shm_marks.size).ljust(_shm_marks.size, b"\x00")
        values = _shm_marks.unpack(data)
        return list(zip(values[::2], values[1::2]))

    def _read_mark(self, slot: int) -> tuple[int, int]:
        data = self.locks.file.read(_shm_header.size + _shm_mark.size * slot, _shm_mark.size)
        return _shm_mark.unpack(data.ljust(_shm_mark.size, b"\x00"))

    def _backfilled(self) -> int:
        """Frames of the current log generation already in the database file."""
        data = self.locks.file.read(0, _shm_header.size)
        if len(data) < _shm_header.size or self.wal_generation is None:
            return 0
        magic, generation, backfill = _shm_header.unpack(data)
        return backfill if magic == SHM_MAGIC and generation == self.wal_generation else 0

    def _write_backfill(self, frames: int) -> None:
        # Only the header: marks belong to the readers holding their slots.
        self.locks.file.write(0, _shm_header.pack(SHM_MAGIC, self.wal_generation or 0, frames))

    def _write_mark(self, slot: int, frames: int) -> None:
        offset = _shm_header.size + _shm_mark.size * slot
        self.locks.file.write(offset, _shm_mark.pack(self.wal_generation, frames))

    def _frame_offset(self, number: int) -> int:
        return _wal_header.size + (number - 1) * FRAME_SIZE

    def _scan_wal(self, apply: bool) -> bool:
        """Read the log's committed frames past our snapshot.

        With ``apply`` the index and snapshot are updated; otherwise only
        returns whether the log holds a newer state than our snapshot."""
        self.locks.begin_wal_read()  # a writer may restart the log meanwhile
        try:
            return self._scan_wal_locked(apply)
        finally:
            self.locks.end_wal_read()

    def _scan_wal_locked(self, apply: bool) -> bool:
        fd = self.wal.fileno()
        size = os.fstat(fd).st_size
        header = None
        if size >= _wal_header.size:
            magic, page_size, generation = _wal_header.unpack(_pread(fd, _wal_header.size, 0))
            if magic == WAL_MAGIC and page_size == PAGE_SIZE:
                header = generation
        if header is None or header != self.wal_generation:
            if not apply:
                return header is not None or self.committed > 0
            self._reset_wal_index()
            if header is None:
                return False
            self.wal_generation = header
            self.scan_crc = header
        if not apply and header is None:
            return False
        start = self._frame_offset(self.committed + 1)
        data = _pread(fd, size - start, start) if size > start else b""
        crc, number, pending, changed = self.scan_crc, self.committed, [], False
        for pos in range(0, len(data) - FRAME_SIZE + 1, FRAME_SIZE):
            pgno, commit, generation, checksum = _frame_header.unpack_from(data, pos)
            image = data[pos + _frame_header.size:pos + FRAME_SIZE]
            crc = zlib.crc32(data[pos:pos + 12] + image, crc)
            if generation != self.wal_generation or checksum != crc:
                break
            number += 1
            pending.append((pgno, number))
            if commit:
                changed = True
                if not apply:
                    return True
                for page, frame in pending:
                    self.frames.setdefault(page, []).append(frame)
                pending = []
                self.committed = self.frame_total = number
                self.scan_crc = crc
        return changed

    def _wal_frame_for(self, pgno: int) -> int | None:
        frames = self.frames.get(pgno)
        if not frames:
            return None
        i = bisect_right(frames, self.frame_total)
        return frames[i - 1] if i else None

    def _append_frames(self, pages: list[tuple[int, bytes]], commit: bool) -> None:
        """Append (pgno, image) frames; the last one ends a commit if ``commit``."""
        wal = self.wal
        if self.frame_total == self.committed and self.wal_generation is not None:
            # First frames of this transaction.  Our snapshot is the newest
            # state (we hold RESERVED).  If the log is long, copy what readers
            # allow; if all of it is in the database file no reader needs it
            # (see _read_image), so start it afresh.  Otherwise drop anything
            # after its last commit frame (left by a crashed writer).
            if self.committed >= self.checkpoint_frames and not self._copy_log():
                self._wait_for_readers()
            if self._backfilled() == self.committed and self.locks.try_lock_wal():
                try:
                    self._crash_point("wal_restart")
                    self._reset_wal_index()  # read every page from the database file
                    self._start_generation()
                finally:
                    self.locks.unlock_wal()
            else:
                wal.truncate(self._frame_offset(self.committed + 1))
                self.append_crc = self.scan_crc
        if self.wal_generation is None:
            self._start_generation()
        wal.seek(self._frame_offset(self.frame_total + 1))
        crc = self.append_crc
        last = len(pages) - 1
        for i, (pgno, image) in enumerate(pages):
            if commit and i == last:
                self._crash_point("wal_commit")
            self._crash_point("wal_frame", i)
            commit_size = self.header.page_count if commit and i == last else 0
            fields = _frame_fields.pack(pgno, commit_size, self.wal_generation)
            crc = zlib.crc32(fields + image, crc)
            wal.write(fields + _u32.pack(crc) + image)
            self.frame_total += 1
            self.frames.setdefault(pgno, []).append(self.frame_total)
        self.append_crc = crc

    def _start_generation(self) -> None:
        """Start a new generation of the log (overwriting the old one)."""
        self.wal_generation = int.from_bytes(os.urandom(4), "big") or 1
        self.wal.seek(0)
        self.wal.truncate()
        self.wal.write(_wal_header.pack(WAL_MAGIC, PAGE_SIZE, self.wal_generation))
        self.scan_crc = self.append_crc = self.wal_generation

    # ---- reading ------------------------------------------------------

    def _read_image(self, pgno: int) -> bytes:
        frame = self._wal_frame_for(pgno) if self.wal is not None else None
        if frame is not None:
            # A frame already copied into the database file is read there
            # (the file then holds exactly that frame's page: no checkpoint
            # copies past our read mark).  So once the whole log is copied no
            # reader needs it, and a writer may restart it: see _append_frames.
            self.locks.begin_wal_read()
            try:
                if frame > self._backfilled():
                    self.wal.seek(self._frame_offset(frame) + _frame_header.size)
                    image = self.wal.read(PAGE_SIZE)
                else:
                    frame = None
            finally:
                self.locks.end_wal_read()
        if frame is None:
            self.file.seek(pgno * PAGE_SIZE)
            image = self.file.read(PAGE_SIZE)
        if len(image) != PAGE_SIZE:
            raise DatabaseError(f"database disk image is malformed (short read of page {pgno})")
        return image

    def _read(self, pgno: int) -> bytes:
        return verify_page(pgno, self._read_image(pgno))

    def _read_header(self) -> Header | None:
        """Read and validate page 0 (from the log or the file); None if the
        database has never been committed."""
        self.file.seek(0, io.SEEK_END)
        size = self.file.tell()
        if size % PAGE_SIZE:
            raise DatabaseError("database file size is not a multiple of the page size")
        if size == 0 and self._wal_frame_for(0) is None:
            return None
        image = self._read_image(0)
        if not image.startswith(MAGIC_PREFIX):
            raise DatabaseError("file is not a MiniDB database")
        if not image.startswith(MAGIC):
            raise DatabaseError(f"unsupported MiniDB file format: {image[:15].decode(errors='replace')}")
        header = Header.from_bytes(0, verify_page(0, image))
        if not self.frames and header.page_count > size // PAGE_SIZE:
            raise DatabaseError("database disk image is malformed (file is truncated)")
        return header

    def get(self, pgno: int, page_class: PageDecoder) -> Page:
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

    def write(self, page: Page) -> None:
        """Declare that ``page`` is about to be modified (or replaced by ``page``)."""
        pgno = page.pgno
        if self.journal is not None and pgno not in self.journal:
            old = self.cache.get(pgno)
            self.journal[pgno] = old.copy() if old is not None else None
        self.dirty.add(pgno)
        self.cache[pgno] = page

    # ---- statements ---------------------------------------------------

    def begin_statement(self) -> None:
        self.journal = {}
        self.journal_dirty = set(self.dirty)

    def end_statement(self) -> None:
        self.journal = None
        self.journal_dirty = None

    def rollback_statement(self) -> None:
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

    def allocate(self, page_class: Callable[..., Page], *args: Any) -> Any:
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

    def free(self, pgno: int) -> None:
        header = self.header
        self.write(header)
        self.write(FreePage(pgno, header.freelist_head))
        header.freelist_head = pgno

    def free_page_count(self) -> int:
        count = 0
        pgno = self.header.freelist_head
        while pgno:
            count += 1
            pgno = self.get(pgno, FreePage).next_free
        return count

    def check_checksums(self) -> list[int]:
        """Read every committed page and verify its checksum; returns the
        damaged page numbers.  (Pages allocated by the current transaction
        are not stored yet.)"""
        saved = self.frame_total
        self.frame_total = self.committed  # look at the committed state only
        try:
            damaged = []
            committed = self._read_header()
            for pgno in range(committed.page_count if committed else 0):
                try:
                    self._read(pgno)
                except DatabaseError:
                    damaged.append(pgno)
            return damaged
        finally:
            self.frame_total = saved

    def shrink_cache(self, limit: int = 10_000) -> None:
        """Drop clean pages from the cache once it holds more than ``limit`` pages.

        Only call this between statements: B+ tree code holds page objects
        while it works.
        """
        if len(self.cache) > limit:
            self.cache = {
                pgno: page for pgno, page in self.cache.items() if pgno in self.dirty or pgno == 0
            }

    # ---- transactions -------------------------------------------------

    def begin_read(self) -> bool:
        """Start reading: snapshot of the log, read slot, cache validation.

        Returns True if the cache was dropped because the database changed."""
        if self.dirty - {0}:
            raise AssertionError("begin_read() inside a transaction with changes")
        if self.wal is not None:
            self._take_read_slot()
        self.reading = True
        snapshot = (self.wal_generation, self.committed)
        if snapshot == self.read_snapshot and snapshot[0] is not None and not self.dirty:
            return False  # the same log state as last time: page 0 cannot differ
        self.read_snapshot = snapshot
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

    def _take_read_slot(self) -> None:
        """Take a snapshot of the log and protect it from checkpoints
        (SQLite's read marks).

        If the log is fully copied into the database file we read the file
        only and share slot 0: checkpoints copy nothing meanwhile, but a
        writer may restart the log.  Otherwise we hold a slot whose mark is
        at most our snapshot: checkpoints copy no frame past the mark.  Our
        snapshot may be outdated once the slot is ours - a checkpoint may
        have copied newer frames just before - so if a newer commit exists
        we start over with it.  WAL_READ keeps the log from being restarted
        meanwhile."""
        locks = self.locks
        deadline = time.monotonic() + locks.timeout
        while True:
            locks.begin_wal_read()
            try:
                self._scan_wal_locked(apply=True)
                if self.committed == self._backfilled():
                    locks.share_slot_zero()
                    if not self._newer_commit() and self.committed == self._backfilled():
                        self.frames = {}  # every page is in the database file: read it there
                        return
                elif self._take_mark_slot() and not self._newer_commit():
                    return
                busy = locks.slot is None
                locks.release_slot()
            finally:
                locks.end_wal_read()
            if busy:  # every slot is taken
                if time.monotonic() >= deadline:
                    raise LockTimeout("database is locked")
                time.sleep(0.001)

    def _newer_commit(self) -> bool:
        """Whether a commit followed our snapshot (we hold WAL_READ, so the
        log was not restarted since we scanned it)."""
        if os.fstat(self.wal.fileno()).st_size < self._frame_offset(self.committed + 2):
            return False  # not a single frame more
        return self._scan_wal_locked(apply=False)

    def _take_mark_slot(self) -> bool:
        """Share a slot marked with our snapshot, else mark a free one with
        it, else share the slot with the highest mark below it."""
        locks, generation, committed = self.locks, self.wal_generation, self.committed
        marks = self._read_marks()
        usable = [(frames, i) for i, (gen, frames) in enumerate(marks) if i and gen == generation and frames <= committed]
        best = max(usable, default=None)
        if best is not None and best[0] == committed and locks.try_slot(best[1], exclusive=False):
            if self._read_mark(best[1]) == (generation, committed):
                return True
            locks.release_slot()
        for i in range(1, READ_SLOTS + 1):
            if locks.try_slot(i, exclusive=True):
                self._write_mark(i, committed)
                locks.downgrade_slot()
                if self._read_mark(i) == (generation, committed):
                    return True
                locks.release_slot()
                break
        for frames, i in sorted(usable, reverse=True):
            if locks.try_slot(i, exclusive=False):
                gen, mark = self._read_mark(i)
                if gen == generation and mark <= committed:
                    return True
                locks.release_slot()
        return False

    def begin_write(self, wait: bool = True) -> None:
        """Become the (only) writer: take RESERVED.  Fails like a busy lock if
        another connection committed after our snapshot was taken."""
        if self.locks is None or self.locks.reserved:
            return
        self.locks.reserve(wait)
        if self.reading and self._scan_wal(apply=False):
            self.locks.release_reserved()
            raise LockTimeout("database is locked")

    def end_transaction(self) -> None:
        self.reading = False
        if self.locks is not None:
            self.locks.release_all()

    def _crash_point(self, point: str, detail: int | None = None) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point, detail)

    def commit(self) -> None:
        """Make every change durable (see the class docstring).

        May raise ``OperationalError("database is locked")`` before anything
        is written; the transaction is then still intact."""
        if not self.dirty and self.frame_total == self.committed:
            return
        if self.locks is not None:
            self.begin_write()
        self.write(self.header)
        self.header.change_counter = (self.header.change_counter + 1) & 0xFFFFFFFF
        pages = [(pgno, with_checksum(self.cache[pgno].to_bytes())) for pgno in sorted(self.dirty)]
        if self.wal is None:
            for pgno, image in pages:
                self.file.seek(pgno * PAGE_SIZE)
                self.file.write(image)
            self.file.truncate(self.header.page_count * PAGE_SIZE)
        else:
            self._append_frames(pages, commit=True)
            self._crash_point("wal_sync")
            os.fsync(self.wal.fileno())
            self.committed = self.frame_total
            self.scan_crc = self.append_crc
        self.dirty.clear()

    def spill(self) -> None:
        """Write the dirty pages to the log as uncommitted frames, so they no
        longer need to stay in memory.  Call between statements."""
        if self.wal is None or not self.dirty:
            return
        self.begin_write()
        pages = [(pgno, with_checksum(self.cache[pgno].to_bytes())) for pgno in sorted(self.dirty)]
        self._append_frames(pages, commit=False)
        self.dirty.clear()

    def rollback(self) -> None:
        """Discard every uncommitted change, including spilled frames."""
        for pgno in self.dirty:
            self.cache.pop(pgno, None)
        self.dirty.clear()
        self.end_statement()
        if self.wal is not None and self.frame_total > self.committed:
            for pgno, frames in list(self.frames.items()):
                while frames and frames[-1] > self.committed:
                    frames.pop()
                    self.cache.pop(pgno, None)
                if not frames:
                    del self.frames[pgno]
            self.frame_total = self.committed
            self.wal.truncate(self._frame_offset(self.committed + 1))
        header = self._read_header()
        if header is None:  # a new file that was never committed
            header = Header()
            self.dirty = {0}
        self.header = header
        self.cache[0] = header

    def checkpoint(self) -> bool:
        """Copy the log into the database file as far as readers allow, and
        empty it once everything is copied and no other connection reads.
        Never waits.  Returns whether the whole log is in the database file.

        Called outside a transaction or right after our own commit (our
        snapshot is then the newest, and we read nothing more)."""
        if self.wal is None or self.dirty or self.frame_total != self.committed:
            return False
        locks = self.locks
        had_reserved = locks.reserved
        if not had_reserved and not locks.try_reserve():
            return False
        try:
            locks.release_slot()
            self._scan_wal(apply=True)  # we might have been behind
            if not self._copy_log():
                return False
            if self.committed and locks.try_lock_out_readers():
                try:
                    if locks.try_lock_wal():  # (a reader may be scanning the log)
                        try:
                            self._crash_point("wal_reset")  # no one reads: empty the log
                            self.wal.truncate(0)
                            os.fsync(self.wal.fileno())
                            self._reset_wal_index()
                            self._write_backfill(0)
                        finally:
                            locks.unlock_wal()
                finally:
                    locks.unlock_readers()
            return True
        finally:
            if not had_reserved:
                locks.release_reserved()

    def _copy_log(self) -> bool:
        """Copy the log into the database file as far as readers allow (we
        hold RESERVED and our snapshot is the newest); returns whether all
        of it is there now."""
        backfilled = self._backfilled()
        if backfilled < self.committed:
            locks = self.locks
            locks.release_slot()  # (we read nothing the copy could change)
            safe = self._safe_frames(backfilled)
            if safe > backfilled and locks.try_lock_slots(range(0, 1)):
                try:
                    self._backfill(backfilled, safe)
                finally:
                    locks.unlock_slots(range(0, 1))
                backfilled = safe
        return backfilled == self.committed

    def _wait_for_readers(self) -> None:
        """A long log that readers keep from being copied - each began before
        the last commit - is copied once they finish: as we hold RESERVED
        nothing new is committed, so new readers start at the end of the log.
        Waits briefly, and after a failed wait not again before the log has
        grown by ``4 * checkpoint_frames`` (one long reader must not slow
        every write)."""
        if self.committed < self.wait_at:
            return
        deadline = time.monotonic() + self.restart_wait
        while time.monotonic() < deadline:
            time.sleep(0.0005)
            if self._copy_log():
                return
        self.wait_at = self.committed + 4 * self.checkpoint_frames

    def _safe_frames(self, backfilled: int) -> int:
        """How far the log may be copied: not past the mark of any read
        slot in use (a mark of another generation is being written now)."""
        safe = self.committed
        marks = self._read_marks()
        for i in range(1, READ_SLOTS + 1):
            generation, frames = marks[i]
            stale = generation != self.wal_generation
            if (stale or frames < safe) and self.locks.slot_in_use(i):
                safe = backfilled if stale else max(frames, backfilled)
        return safe

    def _backfill(self, backfilled: int, safe: int) -> None:
        """Copy the newest frame up to ``safe`` of every page changed after
        ``backfilled`` into the database file (we hold slot 0 exclusively).
        Pages past the end of that state are dropped: the file shrinks, and
        no reader's snapshot needs them (all are at ``safe`` or later)."""
        newest = {}
        for pgno, frames in self.frames.items():
            i = bisect_right(frames, safe)
            if i and frames[i - 1] > backfilled:
                newest[pgno] = frames[i - 1]
        images = {}
        for pgno, frame in newest.items():
            self.wal.seek(self._frame_offset(frame) + _frame_header.size)
            images[pgno] = self.wal.read(PAGE_SIZE)
        page_count = Header.from_bytes(0, verify_page(0, images[0])).page_count
        for i, (pgno, image) in enumerate(sorted(images.items())):
            if pgno >= page_count:
                continue
            self._crash_point("checkpoint_page", i)
            self.file.seek(pgno * PAGE_SIZE)
            self.file.write(image)
        self.file.truncate(page_count * PAGE_SIZE)
        self._crash_point("checkpoint_sync")
        os.fsync(self.file.fileno())
        self._write_backfill(safe)

    def close_files(self) -> None:
        """Close without committing (also used after a crash)."""
        if self.locks is not None:
            self.locks.close()
        if self.wal is not None:
            self.wal.close()
        self.file.close()

    def close(self) -> None:
        """Commit any dirty pages, checkpoint if possible and close the file."""
        if self.file.closed:
            return
        try:
            self.commit()
            self.end_transaction()
            self.checkpoint()
        finally:
            self.close_files()
