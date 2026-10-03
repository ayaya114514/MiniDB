"""SQLite's write-ahead log, for databases in its file format with
``journal_mode = WAL``: the log ``<db>-wal`` and the wal-index ``<db>-shm``,
in SQLite's own formats and with its locking protocol, so that MiniDB and
sqlite3 processes can read and write the database at the same time.

* ``<db>-wal``: a 32-byte header (magic, version, page size, checkpoint
  sequence, two salts, checksum), then frames - a 24-byte header (page
  number, the database size in pages for the last frame of a commit, the
  salts, a checksum running over the whole log) and the page.  A frame
  counts only if its salts match the header's and its checksum is right.
* ``<db>-shm``: two copies of the index header (the last committed frame,
  the database size, the salts, a checksum), the checkpoint information
  (how many frames are in the database file, five read marks), then blocks
  of 32 KB: the page number of each frame and a hash table from page number
  to frame.  Its values are in the machine's byte order; SQLite maps it into
  memory, MiniDB reads and writes it with pread / pwrite (the same pages).
* locks on bytes 120-128 of ``<db>-shm``: WRITE (the one writer), CKPT (the
  one checkpointer), RECOVER, READ0-READ4 (a reader holds one shared: 0 if
  it reads only the database file, else the slot whose read mark - the last
  frame it may read - is its snapshot), and DMS (held shared by every
  connection; the first one in truncates the file).  A connection in WAL
  mode also holds SHARED on the database file for as long as it is open.

Readers and the writer do not block each other.  A checkpoint copies to the
database file the frames no reader still needs older pages for (PASSIVE;
after every commit once the log has 1000 frames); the last connection to
close copies everything and deletes both files, as SQLite does.
"""

from __future__ import annotations

import os
import struct
import sys
import time
from array import array
from typing import Any, Callable

from minidb.errors import DatabaseError
from minidb.locking import LockTimeout, _LockFile, fsync_directory
from minidb.sqlite_format import valid_page_size

WAL_MAGIC = 0x377F0682  # | 1: big-endian checksums
WAL_VERSION = 3007000
WAL_HEADER_SIZE = 32
FRAME_HEADER_SIZE = 24

SHM_HEADER_SIZE = 136  # two index headers, the checkpoint information
SHM_BLOCK = 32768
BLOCK_FRAMES = 4096  # frames per block (the first block has room for fewer)
FIRST_BLOCK_FRAMES = BLOCK_FRAMES - SHM_HEADER_SIZE // 4
HASH_SLOTS = 8192
HASH_OFFSET = BLOCK_FRAMES * 4  # where a block's hash table starts
READERS = 5
NOT_USED = 0xFFFFFFFF  # a read mark no one uses

WRITE, CKPT, RECOVER, DMS = 0, 1, 2, 8
SHM_SPANS = {byte: (120 + byte, 1) for byte in range(9)}


def READ(i: int) -> int:
    return 3 + i


LITTLE = sys.byteorder == "little"
MASK = 0xFFFFFFFF

_wal_header = struct.Struct(">IIII8sII")  # magic, version, page size, checkpoint sequence, salts, checksum
_index_header = struct.Struct("=IIIBBHII2I8s2I")
_marks = struct.Struct("=6I")  # nBackfill, aReadMark[5] (at 96)
_u32 = struct.Struct("=I")
CHECKPOINT_INFO = 96
BACKFILL_ATTEMPTED = 128


def _pread(fd: int, size: int, offset: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)  # pragma: no cover - Windows (the descriptor is this connection's)
    return os.read(fd, size)


def _pwrite(fd: int, data: bytes, offset: int) -> None:
    if hasattr(os, "pwrite"):
        os.pwrite(fd, data, offset)
        return
    os.lseek(fd, offset, os.SEEK_SET)  # pragma: no cover - Windows
    os.write(fd, data)


def checksum(data: bytes, s1: int, s2: int, big_endian: bool) -> tuple[int, int]:
    """SQLite's WAL checksum over ``data`` (a multiple of 8 bytes), continuing (s1, s2)."""
    words = struct.unpack(f"{'>' if big_endian else '<'}{len(data) // 4}I", data)
    it = iter(words)
    for a, b in zip(it, it):
        s1 = (s1 + a + s2) & MASK
        s2 = (s2 + b + s1) & MASK
    return s1, s2


def frame_offset(frame: int, page_size: int) -> int:
    return WAL_HEADER_SIZE + (frame - 1) * (FRAME_HEADER_SIZE + page_size)


def frame_block(frame: int) -> int:
    """The wal-index block that indexes ``frame`` (walFramePage)."""
    return (frame + BLOCK_FRAMES - FIRST_BLOCK_FRAMES - 1) // BLOCK_FRAMES


def block_zero(block: int) -> int:
    """The frame before the first one ``block`` indexes."""
    return 0 if block == 0 else FIRST_BLOCK_FRAMES + (block - 1) * BLOCK_FRAMES


def page_numbers_offset(block: int) -> int:
    return block * SHM_BLOCK + (SHM_HEADER_SIZE if block == 0 else 0)


class IndexHeader:
    """The wal-index header (WalIndexHdr)."""

    __slots__ = ("change", "big_endian", "page_size", "max_frame", "pages", "frame_checksum", "salt")

    def __init__(self) -> None:
        self.change = 0
        self.big_endian = not LITTLE
        self.page_size = 0
        self.max_frame = 0
        self.pages = 0
        self.frame_checksum = (0, 0)
        self.salt = b"\x00" * 8

    @classmethod
    def parse(cls, data: bytes) -> IndexHeader | None:
        """The header in ``data`` (48 bytes), or None unless it is initialized and intact."""
        (version, _, change, is_init, big_endian, size_code, max_frame, pages, c1, c2, salt,
         k1, k2) = _index_header.unpack(data)
        if not is_init or checksum(data[:40], 0, 0, not LITTLE) != (k1, k2):
            return None
        if version != WAL_VERSION:
            raise DatabaseError("unable to open database file")
        header = cls()
        header.change, header.big_endian, header.max_frame, header.pages = change, bool(big_endian), max_frame, pages
        header.page_size = (size_code & 0xFF00) | ((size_code & 1) << 16)
        header.frame_checksum, header.salt = (c1, c2), salt
        return header

    def to_bytes(self) -> bytes:
        size_code = (self.page_size & 0xFF00) | (self.page_size >> 16)
        data = _index_header.pack(WAL_VERSION, 0, self.change, 1, int(self.big_endian), size_code,
                                  self.max_frame, self.pages, *self.frame_checksum, self.salt, 0, 0)
        return data[:40] + struct.pack("=2I", *checksum(data[:40], 0, 0, not LITTLE))


class SqliteWal:
    """One connection's use of the log of the database file ``path``."""

    def __init__(self, path: str, page_size: int, timeout: float) -> None:
        self.path = path + "-wal"
        self.shm_path = path + "-shm"
        self.timeout = timeout
        self.page_size = page_size  # for a new log
        self.crash_hook: Callable[[str, int | None], None] | None = None
        self.header: IndexHeader | None = None  # of the snapshot
        self.header_bytes = b""
        self.read_lock = -1
        self.writer = False
        self.checkpoints = 0  # the checkpoint sequence number of the log header (nCkpt)
        self.frames: dict[int, int] = {}  # page number -> its last frame up to frames_end
        self.frames_end = 0
        self.frames_salt = b""
        self.fd = -1
        self.shm = _LockFile.acquire(self.shm_path, SHM_SPANS)
        try:
            self._attach()
            created = not os.path.exists(self.path)
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            if created:
                fsync_directory(self.path)
        except BaseException:
            self.close()
            raise

    # ---- the wal-index -----------------------------------------------------------

    def _attach(self) -> None:
        """The DMS lock: the first connection truncates the wal-index (made
        anew by recovery); every connection then holds DMS shared."""
        shm = self.shm
        deadline = time.monotonic() + self.timeout
        while True:
            if shm.try_lock(self, DMS, True):
                shm.truncate(0)
                shm.try_lock(self, DMS, False)
                return
            if shm.try_lock(self, DMS, False):
                return
            if time.monotonic() >= deadline:
                raise LockTimeout("database is locked")
            time.sleep(0.001)

    def _read_shm(self, offset: int, size: int) -> bytes:
        return self.shm.read(offset, size).ljust(size, b"\x00")

    def _current_header(self) -> tuple[IndexHeader | None, bytes]:
        data = self._read_shm(0, 96)
        if data[:48] != data[48:]:
            return None, b""  # a writer is changing it (or it is damaged)
        return IndexHeader.parse(data[:48]), data[:48]

    def _write_header(self, header: IndexHeader) -> None:
        data = header.to_bytes()
        self.shm.write(48, data)  # (the second copy first, as walIndexWriteHdr)
        self.shm.write(0, data)
        self.header, self.header_bytes = header, data

    def _marks(self) -> tuple[int, list[int]]:
        backfill, *marks = _marks.unpack(self._read_shm(CHECKPOINT_INFO, _marks.size))
        return backfill, marks

    def _set_mark(self, i: int, value: int) -> None:
        self.shm.write(CHECKPOINT_INFO + 4 + 4 * i, _u32.pack(value))

    def _set_backfill(self, frames: int, attempted: int | None = None) -> None:
        self.shm.write(CHECKPOINT_INFO, _u32.pack(frames))
        if attempted is not None:
            self.shm.write(BACKFILL_ATTEMPTED, _u32.pack(attempted))

    def _page_numbers(self, first: int, last: int) -> list[tuple[int, int]]:
        """(frame, page number) of frames ``first`` .. ``last`` from the wal-index."""
        found = []
        frame = first
        while frame <= last:
            block = frame_block(frame)
            zero = block_zero(block)
            end = min(last, zero + (FIRST_BLOCK_FRAMES if block == 0 else BLOCK_FRAMES))
            data = self._read_shm(page_numbers_offset(block) + (frame - zero - 1) * 4, (end - frame + 1) * 4)
            found.extend(zip(range(frame, end + 1), array("I", data)))
            frame = end + 1
        return found

    def _append_index(self, first: int, pgnos: list[int]) -> None:
        """Index frames ``first``, ``first`` + 1, ... (walIndexAppend; the
        caller writes). Entries past the last commit that a crashed writer
        left in the block are removed first (walCleanupHash)."""
        frame = first
        while frame < first + len(pgnos):
            block = frame_block(frame)
            zero = block_zero(block)
            capacity = FIRST_BLOCK_FRAMES if block == 0 else BLOCK_FRAMES
            start = page_numbers_offset(block)
            numbers = array("I", self._read_shm(start, capacity * 4))
            slots = array("H", self._read_shm(block * SHM_BLOCK + HASH_OFFSET, HASH_SLOTS * 2))
            index = frame - zero
            if index == 1:
                numbers = array("I", bytes(capacity * 4))
                slots = array("H", bytes(HASH_SLOTS * 2))
            elif numbers[index - 1]:
                limit = index - 1  # (frames up to the last commit stay)
                for key in range(HASH_SLOTS):
                    if slots[key] > limit:
                        slots[key] = 0
                for i in range(limit, capacity):
                    numbers[i] = 0
            while frame < first + len(pgnos) and frame - zero <= capacity:
                index = frame - zero
                pgno = pgnos[frame - first]
                key = (pgno * 383) & (HASH_SLOTS - 1)
                while slots[key]:
                    key = (key + 1) & (HASH_SLOTS - 1)
                numbers[index - 1] = pgno
                slots[key] = index
                frame += 1
            self.shm.write(start, numbers.tobytes())
            self.shm.write(block * SHM_BLOCK + HASH_OFFSET, slots.tobytes())

    # ---- recovery -----------------------------------------------------------------

    def _recover(self) -> None:
        """Rebuild the wal-index from the log (walIndexRecover); the caller
        holds WRITE.  The frames up to the last valid commit count."""
        shm = self.shm
        self._lock(CKPT, True)
        try:
            self._lock(RECOVER, True)
            try:
                header = IndexHeader()
                header.page_size = self.page_size
                last_commit = (0, 0)
                pgnos = []
                size = os.fstat(self.fd).st_size
                head = _pread(self.fd, WAL_HEADER_SIZE, 0) if size > WAL_HEADER_SIZE else b""
                if len(head) == WAL_HEADER_SIZE:
                    magic, version, page_size, sequence, salt, c1, c2 = _wal_header.unpack(head)
                    big = bool(magic & 1)
                    if (magic & ~1) == WAL_MAGIC and valid_page_size(page_size) \
                            and checksum(head[:24], 0, 0, big) == (c1, c2):
                        if version != WAL_VERSION:
                            raise DatabaseError("unable to open database file")
                        header.big_endian, header.salt, header.page_size = big, salt, page_size
                        self.checkpoints = sequence
                        sums = (c1, c2)
                        frame_size = FRAME_HEADER_SIZE + page_size
                        offset = WAL_HEADER_SIZE
                        while offset + frame_size <= size:
                            chunk = _pread(self.fd, min(256 * frame_size, size - offset) // frame_size * frame_size,
                                             offset)
                            for at in range(0, len(chunk), frame_size):
                                pgno, commit, frame_salt, f1, f2 = struct.unpack_from(">II8sII", chunk, at)
                                if frame_salt != salt or pgno == 0:
                                    break
                                sums = checksum(chunk[at:at + 8], *sums, big)
                                sums = checksum(chunk[at + FRAME_HEADER_SIZE:at + frame_size], *sums, big)
                                if sums != (f1, f2):
                                    break
                                pgnos.append(pgno)
                                if commit:
                                    header.max_frame, header.pages, last_commit = len(pgnos), commit, sums
                            else:
                                offset += len(chunk)
                                continue
                            break
                header.frame_checksum = last_commit
                del pgnos[header.max_frame:]
                if pgnos:
                    self._append_index(1, pgnos)
                self._write_header(header)
                self._set_backfill(0, header.max_frame)
                self._set_mark(0, 0)
                for i in range(1, READERS):
                    if shm.try_lock(self, READ(i), True):
                        self._set_mark(i, header.max_frame if i == 1 and header.max_frame else NOT_USED)
                        shm.unlock(self, READ(i))
            finally:
                shm.unlock(self, RECOVER)
        finally:
            shm.unlock(self, CKPT)

    def _lock(self, byte: int, exclusive: bool) -> None:
        self.shm.lock(self, byte, exclusive, self.timeout)

    # ---- reading ------------------------------------------------------------------

    def begin_read(self) -> bool:
        """Start a read transaction (walTryBeginRead, retried until the
        timeout); returns whether the snapshot differs from the last one."""
        deadline = time.monotonic() + self.timeout
        delay = 0.0002
        while True:
            changed = self._try_begin_read(False)
            if changed is not None:
                return changed
            if time.monotonic() >= deadline:
                raise LockTimeout("database is locked")
            time.sleep(delay)
            delay = min(delay * 2, 0.01)

    def _try_begin_read(self, use_wal: bool) -> bool | None:
        shm = self.shm
        header, raw = self._current_header()
        if header is None:
            if not self.writer and not shm.try_lock(self, WRITE, True):
                return None  # (another connection is recovering or writing it)
            try:
                header, raw = self._current_header()
                if header is None:
                    self._recover()
                    header, raw = self._current_header()
                    if header is None:
                        raise DatabaseError("database disk image is malformed")
            finally:
                if not self.writer:
                    shm.unlock(self, WRITE)
        changed = raw != self.header_bytes
        backfill, marks = self._marks()
        if not use_wal and backfill == header.max_frame:
            # Everything is in the database file: read only that.
            if shm.try_lock(self, READ(0), False):
                if self._current_header()[1] != raw:
                    shm.unlock(self, READ(0))
                    return None
                self.read_lock = 0
                self.header, self.header_bytes = header, raw
                return changed
        best, best_i = 0, 0
        for i in range(1, READERS):
            if best <= marks[i] <= header.max_frame:
                best, best_i = marks[i], i
        if best < header.max_frame or best_i == 0:
            for i in range(1, READERS):
                if shm.try_lock(self, READ(i), True):
                    self._set_mark(i, header.max_frame)
                    best, best_i = header.max_frame, i
                    shm.unlock(self, READ(i))
                    break
        if best_i == 0 or not shm.try_lock(self, READ(best_i), False):
            return None
        if self._marks()[1][best_i] != best or self._current_header()[1] != raw:
            shm.unlock(self, READ(best_i))
            return None
        self.read_lock = best_i
        self.header, self.header_bytes = header, raw
        self._index_frames(header)
        return changed

    def _index_frames(self, header: IndexHeader) -> None:
        """Bring the map page -> last frame up to the snapshot."""
        if header.salt != self.frames_salt or header.max_frame < self.frames_end:
            self.frames, self.frames_end, self.frames_salt = {}, 0, header.salt
        if header.max_frame > self.frames_end:
            frames = self.frames
            for frame, pgno in self._page_numbers(self.frames_end + 1, header.max_frame):
                frames[pgno] = frame
            self.frames_end = header.max_frame

    def read_page(self, pgno: int) -> bytes | None:
        """Page ``pgno`` as the snapshot has it in the log, or None (then
        the database file has it)."""
        if self.read_lock <= 0:
            return None
        frame = self.frames.get(pgno)
        if frame is None:
            return None
        size = self.page_size
        return _pread(self.fd, size, frame_offset(frame, size) + FRAME_HEADER_SIZE)

    def logged_pages(self) -> dict[int, int]:
        return self.frames if self.read_lock > 0 else {}

    @property
    def database_pages(self) -> int:
        """The database size the snapshot says (0: as the file says)."""
        return self.header.pages if self.header is not None else 0

    def end_read(self) -> None:
        if self.read_lock >= 0:
            self.shm.unlock(self, READ(self.read_lock))
            self.read_lock = -1

    # ---- writing ------------------------------------------------------------------

    def begin_write(self, wait: bool = True) -> None:
        """WRITE; inside a read transaction only if no one committed since
        its snapshot (else "database is locked", SQLITE_BUSY_SNAPSHOT)."""
        if self.writer:
            return
        self.shm.lock(self, WRITE, True, self.timeout if wait else 0)
        self.writer = True
        if self.read_lock >= 0 and self._current_header()[1] != self.header_bytes:
            self.end_write()
            raise LockTimeout("database is locked")

    def end_write(self) -> None:
        if self.writer:
            self.shm.unlock(self, WRITE)
            self.writer = False

    def _crash_point(self, point: str, detail: int | None = None) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point, detail)

    def _restart(self) -> None:
        """Start the log over if every frame is in the database file and no
        reader uses it (walRestartLog), then read with a read mark."""
        shm = self.shm
        backfill, _ = self._marks()
        if backfill > 0:
            taken = []
            for i in range(1, READERS):
                if not shm.try_lock(self, READ(i), True):
                    break
                taken.append(i)
            else:
                header = self.header
                self.checkpoints += 1
                header.max_frame = 0
                header.salt = ((int.from_bytes(header.salt[:4], "big") + 1) & MASK).to_bytes(4, "big") \
                    + os.urandom(4)
                self._write_header(header)
                self._set_backfill(0, 0)
                self._set_mark(1, 0)
                for i in range(2, READERS):
                    self._set_mark(i, NOT_USED)
            for i in taken:
                shm.unlock(self, READ(i))
        self.end_read()
        deadline = time.monotonic() + self.timeout
        while self._try_begin_read(True) is None:
            if time.monotonic() >= deadline:
                raise LockTimeout("database is locked")
            time.sleep(0.0005)

    def write_frames(self, pages: list[tuple[int, bytes]], database_pages: int) -> None:
        """Commit ``pages`` (page number, image): append them to the log, the
        last one marked as a commit, sync, index them, publish the header."""
        assert self.writer and self.read_lock >= 0
        if self.read_lock == 0:
            self._restart()
        header = self.header
        size = self.page_size
        if header.max_frame == 0:
            salt = os.urandom(8) if self.checkpoints == 0 else header.salt
            big = not LITTLE
            head = _wal_header.pack(WAL_MAGIC | int(big), WAL_VERSION, size, self.checkpoints, salt, 0, 0)[:24]
            sums = checksum(head, 0, 0, big)
            self._crash_point("wal_header")
            _pwrite(self.fd, head + struct.pack(">II", *sums), 0)
            os.fsync(self.fd)
            header.salt, header.big_endian, header.frame_checksum, header.page_size = salt, big, sums, size
        s1, s2 = header.frame_checksum
        big = header.big_endian
        first = header.max_frame + 1
        parts = []
        for i, (pgno, data) in enumerate(pages):
            start = struct.pack(">II", pgno, database_pages if i == len(pages) - 1 else 0)
            s1, s2 = checksum(start, s1, s2, big)
            s1, s2 = checksum(data, s1, s2, big)
            parts.append(start + header.salt + struct.pack(">II", s1, s2) + data)
        self._crash_point("wal_frames")
        _pwrite(self.fd, b"".join(parts), frame_offset(first, size))
        self._crash_point("wal_sync")
        os.fsync(self.fd)
        self._crash_point("wal_index")
        pgnos = [pgno for pgno, _ in pages]
        self._append_index(first, pgnos)
        header.max_frame = first + len(pages) - 1
        header.pages = database_pages
        header.frame_checksum = (s1, s2)
        header.change = (header.change + 1) & MASK
        self._write_header(header)
        self._index_frames(header)

    # ---- checkpoints ----------------------------------------------------------------

    def checkpoint(self, write_page: Callable[[int, bytes], None], truncate: Callable[[int], None],
                   sync: Callable[[], None]) -> tuple[int, int] | None:
        """Copy to the database file the frames no reader needs the older
        pages for (walCheckpoint, PASSIVE).  Returns (frames in the log,
        frames copied), or None if another connection is checkpointing."""
        shm = self.shm
        self.end_read()
        if not shm.try_lock(self, CKPT, True):
            return None
        try:
            header, _ = self._current_header()
            if header is None:
                return None
            backfill, marks = self._marks()
            safe = header.max_frame
            for i in range(1, READERS):
                if safe > marks[i]:
                    if shm.try_lock(self, READ(i), True):
                        self._set_mark(i, safe if i == 1 else NOT_USED)
                        shm.unlock(self, READ(i))
                    else:
                        safe = marks[i]
            if backfill < safe and shm.try_lock(self, READ(0), True):
                try:
                    self._set_backfill(backfill, safe)
                    os.fsync(self.fd)
                    latest = {}
                    for frame, pgno in self._page_numbers(1, header.max_frame):
                        latest[pgno] = frame
                    size = header.page_size
                    copied = 0
                    for pgno in sorted(latest):
                        frame = latest[pgno]
                        if backfill < frame <= safe and pgno <= header.pages:
                            self._crash_point("checkpoint_page", copied)
                            copied += 1
                            write_page(pgno, _pread(self.fd, size, frame_offset(frame, size) + FRAME_HEADER_SIZE))
                    if safe == (self._current_header()[0] or header).max_frame:
                        truncate(header.pages * size)
                    self._crash_point("checkpoint_sync")
                    sync()
                    self._set_backfill(safe)
                    backfill = safe
                finally:
                    shm.unlock(self, READ(0))
            return header.max_frame, backfill
        finally:
            shm.unlock(self, CKPT)

    def restart_log(self, truncate: bool) -> bool:
        """After a complete checkpoint: start the log over now (RESTART), and
        empty the file (TRUNCATE), if no one writes or reads from the log."""
        shm = self.shm
        if not shm.try_lock(self, WRITE, True):
            return False
        taken = []
        try:
            for i in range(1, READERS):
                if not shm.try_lock(self, READ(i), True):
                    return False
                taken.append(i)
            header, _ = self._current_header()
            backfill, _ = self._marks()
            if header is None or backfill != header.max_frame:
                return False
            if header.max_frame:
                self.checkpoints += 1
                header.max_frame = 0
                header.salt = ((int.from_bytes(header.salt[:4], "big") + 1) & MASK).to_bytes(4, "big") \
                    + os.urandom(4)
                self._write_header(header)
                self._set_backfill(0, 0)
                self._set_mark(1, 0)
                for i in range(2, READERS):
                    self._set_mark(i, NOT_USED)
            if truncate:
                os.ftruncate(self.fd, 0)
                os.fsync(self.fd)
            return True
        finally:
            for i in taken:
                shm.unlock(self, READ(i))
            shm.unlock(self, WRITE)

    # ---- the end ------------------------------------------------------------------

    def close(self, delete: bool = False) -> None:
        """Let go of the log; ``delete``: remove both files (the caller holds
        EXCLUSIVE on the database and checkpointed everything)."""
        if self.shm is not None:
            self.end_read()
            self.end_write()
            self.shm.unlock(self, DMS)
            self.shm.release()
            self.shm = None
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if delete:  # (closed first: Windows deletes no open file)
            for name in (self.shm_path, self.path):
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass

    def describe(self) -> dict[str, Any]:
        """(For tests and .wal in the REPL) the state of the log."""
        header, _ = self._current_header()
        backfill, marks = self._marks()
        return {"frames": header.max_frame if header else None, "backfill": backfill, "marks": marks,
                "read_lock": self.read_lock}
