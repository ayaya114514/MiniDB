"""Cross-process locks for one database file.

Three locks, modelled on SQLite's locking (see ``minidb.pager`` for how the
WAL uses them):

* SHARED    - held while a transaction reads.  ``flock(LOCK_SH)`` on the
              database file.
* RESERVED  - held by the (single) connection that has started writing.
              ``flock(LOCK_EX)`` on ``<db>-lock``.
* EXCLUSIVE - held while a checkpoint copies pages from the WAL into the
              database file; no reader may hold SHARED meanwhile.
              ``flock(LOCK_EX)`` on the database file, only ever tried
              without waiting.

``flock`` locks belong to an open file, so two connections in the same
process exclude each other too (POSIX ``fcntl`` locks would not).  Every
connection opens its own file objects.  A lock that is busy is retried until
``timeout`` seconds have passed, then ``OperationalError("database is
locked")`` is raised, like SQLite's busy timeout.
"""

from __future__ import annotations

import os
import time
from typing import BinaryIO

from minidb.errors import OperationalError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no locking
    fcntl = None

UNLOCKED, SHARED, EXCLUSIVE = 0, 1, 2


class LockTimeout(OperationalError):
    """A lock stayed busy for the whole timeout ("database is locked")."""


def _try_flock(fd: int, operation: int) -> bool:
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _flock(fd: int, operation: int, timeout: float) -> None:
    """flock with a busy timeout (0 = try once)."""
    deadline = time.monotonic() + timeout
    delay = 0.0005
    while not _try_flock(fd, operation):
        if time.monotonic() >= deadline:
            raise LockTimeout("database is locked")
        time.sleep(delay)
        delay = min(delay * 2, 0.02)


class FileLocks:
    def __init__(self, db_file: BinaryIO, path: str, timeout: float) -> None:
        self.db_fd = db_file.fileno()
        self.lock_file = open(path + "-lock", "a+b", buffering=0) if fcntl else None
        self.timeout = timeout
        self.db_level = UNLOCKED
        self.reserved = False

    # ---- SHARED / EXCLUSIVE on the database file ---------------------------

    def shared(self) -> None:
        if fcntl is None or self.db_level != UNLOCKED:
            return
        _flock(self.db_fd, fcntl.LOCK_SH, self.timeout)
        self.db_level = SHARED

    def try_exclusive(self) -> bool:
        """Take EXCLUSIVE only if no other connection reads right now (used by
        checkpoints, which never wait).  Returns whether it worked."""
        if fcntl is None or self.db_level == EXCLUSIVE:
            return True
        if _try_flock(self.db_fd, fcntl.LOCK_EX):
            self.db_level = EXCLUSIVE
            return True
        return False

    def downgrade(self) -> None:
        """EXCLUSIVE -> SHARED (after a commit, keeping the reader's lock)."""
        if fcntl is None or self.db_level != EXCLUSIVE:
            return
        fcntl.flock(self.db_fd, fcntl.LOCK_SH)  # we hold EX, so this cannot block
        self.db_level = SHARED

    def release_db(self) -> None:
        if fcntl is None or self.db_level == UNLOCKED:
            return
        fcntl.flock(self.db_fd, fcntl.LOCK_UN)
        self.db_level = UNLOCKED

    # ---- RESERVED on <db>-lock ---------------------------------------------

    def reserve(self, wait: bool = True) -> None:
        """Become the writer.  ``wait=False`` fails at once if another
        connection is writing (used when we already hold SHARED: waiting
        could deadlock with that writer waiting for our SHARED)."""
        if fcntl is None or self.reserved:
            return
        _flock(self.lock_file.fileno(), fcntl.LOCK_EX, self.timeout if wait else 0)
        self.reserved = True

    def try_reserve(self) -> bool:
        if fcntl is None or self.reserved:
            return True
        if _try_flock(self.lock_file.fileno(), fcntl.LOCK_EX):
            self.reserved = True
        return self.reserved

    def release_reserved(self) -> None:
        if fcntl is None or not self.reserved:
            return
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
        self.reserved = False

    def release_all(self) -> None:
        self.release_db()
        self.release_reserved()

    def close(self) -> None:
        self.release_all()
        if self.lock_file is not None:
            self.lock_file.close()


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
