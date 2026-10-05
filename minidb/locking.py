"""Cross-process locks for one database file.

Every lock is a lock on one byte of ``<db>-shm``, past the data the pager
keeps there (SQLite's WAL locking works the same way):

* RESERVED  - held exclusively by the one connection that writes.
* WAL_READ  - shared around every read of a frame from the log; a writer
              restarts the log only while holding it exclusively.
* read slots 0 .. READ_SLOTS - SQLite's read marks, see
              ``Pager._take_read_slot``.  Every read transaction holds one, so
              locking all of them shows that no one reads.

POSIX record locks (``fcntl``) belong to a process, not to an open file: two
connections in one process would not exclude each other, and closing *any*
descriptor of the file would drop all of the process's locks on it.  So, as
in SQLite, a process opens ``<db>-shm`` once (``_LockFile``, found by device
and inode, shared by its connections and closed with the last one) and
arbitrates between its own connections itself: the process holds the lock
on a byte while any of its connections does.  On Windows the process-level
locks are LockFileEx ranges (``WindowsLocks``).  A lock that is busy is retried
until ``timeout`` seconds have passed, then ``OperationalError("database is
locked")`` is raised, like SQLite's busy timeout.
"""

from __future__ import annotations

import errno
import mmap
import os
import threading
import time

from minidb.errors import OperationalError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

READ_SLOTS = 8  # slots with a read mark (slot 0 has none)

LOCK_OFFSET = 4096  # the locked bytes; the pager's data comes before them
RESERVED_BYTE, WAL_READ_BYTE, SLOT_BYTE = 0, 1, 2
ALL_SLOTS = range(READ_SLOTS + 1)


class LockTimeout(OperationalError):
    """A lock stayed busy for the whole timeout ("database is locked")."""


Span = tuple[int, int]  # (offset, length) of a locked range


class PosixLocks:
    """Process-level locks on ranges of a file: POSIX record locks (fcntl)."""

    @staticmethod
    def span(byte: int) -> Span:
        """Where MiniDB's lock number ``byte`` lives in ``<db>-shm``."""
        return LOCK_OFFSET + byte, 1

    def lock(self, fd: int, span: Span, exclusive: bool) -> bool:
        return self._lockf(fd, span, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    def upgrade(self, fd: int, span: Span) -> bool | None:
        """Shared to exclusive: True, or False still sharing (None: lost)."""
        return self._lockf(fd, span, fcntl.LOCK_EX)  # atomic for record locks

    def downgrade(self, fd: int, span: Span) -> bool:
        return self._lockf(fd, span, fcntl.LOCK_SH)  # atomic for record locks

    def unlock(self, fd: int, span: Span) -> None:
        self._lockf(fd, span, fcntl.LOCK_UN)

    @staticmethod
    def _lockf(fd: int, span: Span, operation: int) -> bool:
        """Change the process's lock on a range without waiting."""
        try:
            if operation != fcntl.LOCK_UN:
                operation |= fcntl.LOCK_NB
            fcntl.lockf(fd, operation, span[1], span[0], os.SEEK_SET)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise


class _Kernel32:
    """LockFileEx / UnlockFileEx through ctypes (Windows only)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._lock = kernel32.LockFileEx
        self._lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.POINTER(Overlapped)]
        self._lock.restype = wintypes.BOOL
        self._unlock = kernel32.UnlockFileEx
        self._unlock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(Overlapped)]
        self._unlock.restype = wintypes.BOOL
        self._overlapped = Overlapped
        self._ctypes = ctypes

    def lock(self, fd: int, offset: int, length: int, exclusive: bool) -> bool:
        flags = 1 | (2 if exclusive else 0)  # LOCKFILE_FAIL_IMMEDIATELY | LOCKFILE_EXCLUSIVE_LOCK
        where = self._overlapped(0, 0, offset & 0xFFFFFFFF, offset >> 32, None)
        if self._lock(msvcrt.get_osfhandle(fd), flags, 0, length, 0, self._ctypes.byref(where)):
            return True
        error = self._ctypes.get_last_error()
        if error in (33, 997):  # ERROR_LOCK_VIOLATION, ERROR_IO_PENDING
            return False
        raise OSError(error, f"LockFileEx failed (error {error})")

    def unlock(self, fd: int, offset: int, length: int) -> None:
        where = self._overlapped(0, 0, offset & 0xFFFFFFFF, offset >> 32, None)
        self._unlock(msvcrt.get_osfhandle(fd), 0, length, 0, self._ctypes.byref(where))


class WindowsLocks:
    """Process-level locks on ranges of a file with LockFileEx, as SQLite's
    win32 VFS takes them: shared and exclusive, per handle, and mandatory
    (they also block other handles' reads and writes of the locked bytes),
    so they lie past the data.  (``msvcrt.locking`` has exclusive locks only:
    a MiniDB reader then could not share SQLite's read lock.)

    A lock cannot be converted.  An upgrade, as in SQLite's winLock, unlocks
    and locks the range exclusively, sharing it again if that fails (SQLite's
    protocol upgrades only while holding PENDING, which keeps new sharers out
    meanwhile).  A downgrade is atomic: a handle may share a range it holds
    exclusively, and the first unlock then drops the exclusive lock."""

    def __init__(self, kernel: object = None) -> None:
        self.kernel = kernel if kernel is not None else _Kernel32()

    @staticmethod
    def span(byte: int) -> Span:
        return LOCK_OFFSET + byte, 1

    def lock(self, fd: int, span: Span, exclusive: bool) -> bool:
        return self.kernel.lock(fd, span[0], span[1], exclusive)

    def upgrade(self, fd: int, span: Span) -> bool | None:
        """Shared to exclusive: True, or False still sharing (None: lost)."""
        self.kernel.unlock(fd, *span)
        if self.kernel.lock(fd, span[0], span[1], True):
            return True
        return False if self.kernel.lock(fd, span[0], span[1], False) else None

    def downgrade(self, fd: int, span: Span) -> bool:
        if not self.kernel.lock(fd, span[0], span[1], False):  # pragma: no cover - our own range
            return False
        self.kernel.unlock(fd, *span)  # (drops the exclusive lock)
        return True

    def unlock(self, fd: int, span: Span) -> None:
        self.kernel.unlock(fd, *span)


if fcntl is not None:
    BACKEND = PosixLocks()
elif msvcrt is not None:  # pragma: no cover
    BACKEND = WindowsLocks()
else:  # pragma: no cover - no locking at all
    BACKEND = None


class _LockFile:
    """``<db>-shm`` opened once per process, and which of the process's
    connections hold which byte."""

    _open = {}  # (device, inode) -> _LockFile
    _mutex = threading.Lock()  # guards everything below, in every instance

    def __init__(self, fd: int, key: tuple[int, int], backend: object = None, spans: dict | None = None) -> None:
        self.fd = fd
        self.key = key
        self.backend = backend if backend is not None else BACKEND
        self.spans = spans  # lock number -> range; by default MiniDB's bytes in -shm
        self.users = 0
        self.exclusive = {}  # byte -> owner
        self.shared = {}  # byte -> set of owners

    def span(self, byte: int) -> Span:
        return self.spans[byte] if self.spans is not None else self.backend.span(byte)

    @classmethod
    def acquire(cls, path: str, spans: dict | None = None) -> _LockFile:
        with cls._mutex:
            try:
                st = os.stat(path)
                lock_file = cls._open.get((st.st_dev, st.st_ino))
            except FileNotFoundError:
                lock_file = None
            if lock_file is None:
                # Never opened (and so never closed) twice: see the module docstring.
                fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
                st = os.fstat(fd)
                lock_file = cls(fd, (st.st_dev, st.st_ino), spans=spans)
                cls._open[lock_file.key] = lock_file
            lock_file.users += 1
            return lock_file

    def release(self) -> None:
        with self._mutex:
            self.users -= 1
            if self.users == 0:
                del self._open[self.key]
                os.close(self.fd)

    # On Windows the locks are mandatory: a locked byte cannot be read or
    # written through the handle, not even by the process holding the lock.
    # SQLite's -shm has its lock bytes amid its data (``spans``: the DMS byte
    # is nBackfillAttempted), so, as SQLite's win32 VFS, read and write it
    # through a mapping, which the locks do not apply to.

    def read(self, offset: int, size: int) -> bytes:
        if hasattr(os, "pread"):
            return os.pread(self.fd, size, offset)
        if self.spans is not None:  # pragma: no cover - Windows
            length = os.fstat(self.fd).st_size
            if offset >= length:
                return b""
            with mmap.mmap(self.fd, length, access=mmap.ACCESS_READ) as view:
                return view[offset:offset + size]
        with self._mutex:  # pragma: no cover - Windows
            os.lseek(self.fd, offset, os.SEEK_SET)
            return os.read(self.fd, size)

    def write(self, offset: int, data: bytes) -> None:
        if hasattr(os, "pwrite"):
            os.pwrite(self.fd, data, offset)
            return
        if self.spans is not None:  # pragma: no cover - Windows
            # (A mapping longer than the file extends it, as winShmMap does.)
            length = max(os.fstat(self.fd).st_size, offset + len(data))
            with mmap.mmap(self.fd, length, access=mmap.ACCESS_WRITE) as view:
                view[offset:offset + len(data)] = data
            return
        with self._mutex:  # pragma: no cover - Windows
            os.lseek(self.fd, offset, os.SEEK_SET)
            os.write(self.fd, data)

    def size(self) -> int:
        return os.fstat(self.fd).st_size

    def truncate(self, size: int) -> None:
        os.ftruncate(self.fd, size)

    def sync(self) -> None:
        os.fsync(self.fd)

    def try_lock(self, owner: object, byte: int, exclusive: bool) -> bool:
        """Take (or convert to) a shared or exclusive lock on ``byte``
        without waiting; returns whether ``owner`` now holds it so.  A
        failed downgrade leaves ``owner`` without the lock."""
        backend = self.backend
        if backend is None:
            return True
        with self._mutex:
            holder = self.exclusive.get(byte)
            sharers = self.shared.setdefault(byte, set())
            if exclusive:
                if holder is owner:
                    return True
                if holder is not None or sharers - {owner}:
                    return False
                if owner in sharers:
                    upgraded = backend.upgrade(self.fd, self.span(byte))
                    if upgraded is None:
                        sharers.discard(owner)  # (lost while converting)
                    if not upgraded:
                        return False
                elif not backend.lock(self.fd, self.span(byte), True):
                    return False
                sharers.discard(owner)
                self.exclusive[byte] = owner
                return True
            if owner in sharers:
                return True
            if holder is owner:
                del self.exclusive[byte]
                if not backend.downgrade(self.fd, self.span(byte)):
                    return False
            elif holder is not None or (not sharers and not backend.lock(self.fd, self.span(byte), False)):
                return False
            sharers.add(owner)
            return True

    def lock(self, owner: object, byte: int, exclusive: bool, timeout: float) -> None:
        """``try_lock`` with a busy timeout (0 = try once)."""
        deadline = time.monotonic() + timeout
        delay = 0.0005
        while not self.try_lock(owner, byte, exclusive):
            if time.monotonic() >= deadline:
                raise LockTimeout("database is locked")
            time.sleep(delay)
            delay = min(delay * 2, 0.02)

    def unlock(self, owner: object, byte: int) -> None:
        if self.backend is None:
            return
        with self._mutex:
            if self.exclusive.get(byte) is owner:
                del self.exclusive[byte]
                self.backend.unlock(self.fd, self.span(byte))
                return
            sharers = self.shared.get(byte, set())
            if owner in sharers:
                sharers.discard(owner)
                if not sharers:
                    self.backend.unlock(self.fd, self.span(byte))

    def held_by_others(self, owner: object, byte: int) -> bool:
        """Whether a connection other than ``owner`` holds ``byte`` (which
        ``owner`` must not hold itself)."""
        if self.backend is None:
            return False
        with self._mutex:
            holder = self.exclusive.get(byte)
            if (holder is not None and holder is not owner) or self.shared.get(byte, set()) - {owner}:
                return True
            if not self.backend.lock(self.fd, self.span(byte), True):
                return True  # another process
            self.backend.unlock(self.fd, self.span(byte))
            return False


class FileLocks:
    """The locks of one connection."""

    def __init__(self, path: str, timeout: float) -> None:
        self.file = _LockFile.acquire(path + "-shm")
        self.timeout = timeout
        self.reserved = False
        self.slot = None  # the read slot we hold, if any
        self.closed = False

    # ---- RESERVED -----------------------------------------------------------

    def reserve(self, wait: bool = True) -> None:
        """Become the writer.  ``wait=False`` fails at once if another
        connection is writing (used when we already hold SHARED: waiting
        could deadlock with that writer waiting for our SHARED)."""
        if self.reserved:
            return
        self.file.lock(self, RESERVED_BYTE, True, self.timeout if wait else 0)
        self.reserved = True

    def try_reserve(self) -> bool:
        if not self.reserved and self.file.try_lock(self, RESERVED_BYTE, True):
            self.reserved = True
        return self.reserved

    def release_reserved(self) -> None:
        if self.reserved:
            self.file.unlock(self, RESERVED_BYTE)
            self.reserved = False

    def release_all(self) -> None:
        self.release_slot()
        self.release_reserved()

    # ---- WAL_READ -----------------------------------------------------------

    def begin_wal_read(self) -> None:
        self.file.lock(self, WAL_READ_BYTE, False, self.timeout)

    def end_wal_read(self) -> None:
        self.file.unlock(self, WAL_READ_BYTE)

    def try_lock_wal(self) -> bool:
        return self.file.try_lock(self, WAL_READ_BYTE, True)

    def unlock_wal(self) -> None:
        self.file.unlock(self, WAL_READ_BYTE)

    # ---- read slots ---------------------------------------------------------

    def try_slot(self, i: int, exclusive: bool) -> bool:
        """Take slot ``i`` (never waiting); we must hold no slot yet."""
        if self.file.try_lock(self, SLOT_BYTE + i, exclusive):
            self.slot = i
            return True
        return False

    def downgrade_slot(self) -> bool:
        """Exclusive -> shared on our slot.  Returns False if the slot was
        lost meanwhile (only possible on Windows): we then hold none."""
        if self.file.try_lock(self, SLOT_BYTE + self.slot, False):
            return True
        self.slot = None
        return False

    def share_slot_zero(self) -> None:
        """Share slot 0 (waits while a checkpoint copies pages)."""
        self.file.lock(self, SLOT_BYTE, False, self.timeout)
        self.slot = 0

    def release_slot(self) -> None:
        if self.slot is not None:
            self.file.unlock(self, SLOT_BYTE + self.slot)
            self.slot = None

    def slot_in_use(self, i: int) -> bool:
        """Whether another connection holds slot ``i`` (never waits)."""
        return i != self.slot and self.file.held_by_others(self, SLOT_BYTE + i)

    def try_lock_slots(self, slots: range) -> bool:
        """Take ``slots`` exclusively (all or none, never waiting): no
        other connection may then take them until ``unlock_slots``."""
        taken = []
        for i in slots:
            if i == self.slot:
                continue
            if not self.file.try_lock(self, SLOT_BYTE + i, True):
                self.unlock_slots(taken)
                return False
            taken.append(i)
        return True

    def unlock_slots(self, slots: list[int] | range) -> None:
        for i in slots:
            if i != self.slot:
                self.file.unlock(self, SLOT_BYTE + i)

    def try_lock_out_readers(self) -> bool:
        """Lock every slot (we must hold none): no one reads until
        ``unlock_readers``."""
        return self.try_lock_slots(ALL_SLOTS)

    def unlock_readers(self) -> None:
        self.unlock_slots(ALL_SLOTS)

    def close(self) -> None:
        if self.closed:
            return
        self.release_all()
        self.file.release()
        self.closed = True


def fsync_directory(path: str) -> None:
    """Make a file creation or deletion in ``path``'s directory durable."""
    directory = os.path.dirname(os.path.abspath(path))
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)
