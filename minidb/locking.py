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
on a byte while any of its connections does.  A lock that is busy is retried
until ``timeout`` seconds have passed, then ``OperationalError("database is
locked")`` is raised, like SQLite's busy timeout.
"""

from __future__ import annotations

import errno
import os
import threading
import time

from minidb.errors import OperationalError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no locking
    fcntl = None

READ_SLOTS = 8  # slots with a read mark (slot 0 has none)

LOCK_OFFSET = 4096  # the locked bytes; the pager's data comes before them
RESERVED_BYTE, WAL_READ_BYTE, SLOT_BYTE = 0, 1, 2
ALL_SLOTS = range(READ_SLOTS + 1)


class LockTimeout(OperationalError):
    """A lock stayed busy for the whole timeout ("database is locked")."""


def _lockf(fd: int, byte: int, operation: int) -> bool:
    """Change the process's lock on ``byte`` without waiting."""
    try:
        if operation != fcntl.LOCK_UN:
            operation |= fcntl.LOCK_NB
        fcntl.lockf(fd, operation, 1, LOCK_OFFSET + byte, os.SEEK_SET)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


class _LockFile:
    """``<db>-shm`` opened once per process, and which of the process's
    connections hold which byte."""

    _open = {}  # (device, inode) -> _LockFile
    _mutex = threading.Lock()  # guards everything below, in every instance

    def __init__(self, fd: int, key: tuple[int, int]) -> None:
        self.fd = fd
        self.key = key
        self.users = 0
        self.exclusive = {}  # byte -> owner
        self.shared = {}  # byte -> set of owners

    @classmethod
    def acquire(cls, path: str) -> _LockFile:
        with cls._mutex:
            try:
                st = os.stat(path)
                lock_file = cls._open.get((st.st_dev, st.st_ino))
            except FileNotFoundError:
                lock_file = None
            if lock_file is None:
                # Never opened (and so never closed) twice: see the module docstring.
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
                st = os.fstat(fd)
                lock_file = cls(fd, (st.st_dev, st.st_ino))
                cls._open[lock_file.key] = lock_file
            lock_file.users += 1
            return lock_file

    def release(self) -> None:
        with self._mutex:
            self.users -= 1
            if self.users == 0:
                del self._open[self.key]
                os.close(self.fd)

    def read(self, offset: int, size: int) -> bytes:
        if hasattr(os, "pread"):
            return os.pread(self.fd, size, offset)
        with self._mutex:  # pragma: no cover - Windows
            os.lseek(self.fd, offset, os.SEEK_SET)
            return os.read(self.fd, size)

    def write(self, offset: int, data: bytes) -> None:
        if hasattr(os, "pwrite"):
            os.pwrite(self.fd, data, offset)
            return
        with self._mutex:  # pragma: no cover - Windows
            os.lseek(self.fd, offset, os.SEEK_SET)
            os.write(self.fd, data)

    def try_lock(self, owner: object, byte: int, exclusive: bool) -> bool:
        if fcntl is None:
            return True
        with self._mutex:
            holder = self.exclusive.get(byte)
            sharers = self.shared.setdefault(byte, set())
            if exclusive:
                if holder is owner:
                    return True
                if holder is not None or sharers - {owner}:
                    return False
                if not _lockf(self.fd, byte, fcntl.LOCK_EX):
                    return False
                sharers.discard(owner)
                self.exclusive[byte] = owner
                return True
            if owner in sharers:
                return True
            if holder is owner:  # downgrade (atomic for record locks)
                _lockf(self.fd, byte, fcntl.LOCK_SH)
                del self.exclusive[byte]
            elif holder is not None or (not sharers and not _lockf(self.fd, byte, fcntl.LOCK_SH)):
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
        if fcntl is None:
            return
        with self._mutex:
            if self.exclusive.get(byte) is owner:
                del self.exclusive[byte]
                _lockf(self.fd, byte, fcntl.LOCK_UN)
                return
            sharers = self.shared.get(byte, set())
            if owner in sharers:
                sharers.discard(owner)
                if not sharers:
                    _lockf(self.fd, byte, fcntl.LOCK_UN)

    def held_by_others(self, owner: object, byte: int) -> bool:
        """Whether a connection other than ``owner`` holds ``byte`` (which
        ``owner`` must not hold itself)."""
        if fcntl is None:
            return False
        with self._mutex:
            holder = self.exclusive.get(byte)
            if (holder is not None and holder is not owner) or self.shared.get(byte, set()) - {owner}:
                return True
            if not _lockf(self.fd, byte, fcntl.LOCK_EX):
                return True  # another process
            _lockf(self.fd, byte, fcntl.LOCK_UN)
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

    def downgrade_slot(self) -> None:
        """Exclusive -> shared on our slot."""
        self.file.try_lock(self, SLOT_BYTE + self.slot, False)

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
