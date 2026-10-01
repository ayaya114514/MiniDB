"""The lock layer: in-process arbitration and the Windows backend.

Windows itself is not available here: ``WindowsLocks`` runs against a fake
``msvcrt`` with the semantics of ``msvcrt.locking`` (exclusive byte-range
locks per handle; locking bytes that are already locked fails, also for the
same handle; unlocking must match a locked range)."""

import errno
import os

import pytest

import minidb.locking as locking
from minidb.locking import LOCK_OFFSET, FileLocks, WindowsLocks, _LockFile


class FakeMsvcrt:
    LK_UNLCK, LK_NBLCK = 0, 2

    def __init__(self):
        self.locked = {}  # (device, inode, offset) -> fd

    def locking(self, fd, mode, size):
        st = os.fstat(fd)
        position = os.lseek(fd, 0, os.SEEK_CUR)
        keys = [(st.st_dev, st.st_ino, offset) for offset in range(position, position + size)]
        if mode == self.LK_NBLCK:
            if any(key in self.locked for key in keys):
                raise OSError(errno.EACCES, "locked")
            for key in keys:
                self.locked[key] = fd
        elif mode == self.LK_UNLCK:
            if any(self.locked.get(key) != fd for key in keys):
                raise OSError(errno.EACCES, "not locked")
            for key in keys:
                del self.locked[key]
        else:  # pragma: no cover
            raise ValueError(mode)


def process(path, fake):
    """A _LockFile as another process would have it: its own handle."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    return _LockFile(fd, ("fake", fd), WindowsLocks(fake))


def test_windows_backend_shares_and_excludes(tmp_path):
    fake = FakeMsvcrt()
    path = str(tmp_path / "db-shm")
    a, b, c = (process(path, fake) for _ in range(3))
    owner = object()
    assert a.try_lock(owner, 5, False) and b.try_lock(owner, 5, False)  # shared by two processes
    assert not c.try_lock(owner, 5, True)
    assert c.held_by_others(owner, 5)
    a.unlock(owner, 5)
    assert not c.try_lock(owner, 5, True)  # b still shares it
    b.unlock(owner, 5)
    assert not c.held_by_others(owner, 5)
    assert c.try_lock(owner, 5, True)
    assert not a.try_lock(owner, 5, False) and not a.try_lock(owner, 5, True)
    assert c.try_lock(owner, 5, False)  # downgrade
    assert a.try_lock(owner, 5, False)
    assert not c.try_lock(owner, 5, True)  # an upgrade while another shares: no
    c.unlock(owner, 5)
    a.unlock(owner, 5)
    assert fake.locked == {}
    for lock_file in (a, b, c):
        os.close(lock_file.fd)


def test_windows_locks_lie_past_the_data(tmp_path):
    fake = FakeMsvcrt()
    path = str(tmp_path / "db-shm")
    a = process(path, fake)
    assert a.try_lock(object(), 0, True)
    assert min(offset for _, _, offset in fake.locked) >= LOCK_OFFSET
    os.close(a.fd)


def test_windows_backend_shares_among_width_processes(tmp_path):
    fake = FakeMsvcrt()
    path = str(tmp_path / "db-shm")
    processes = [process(path, fake) for _ in range(WindowsLocks.WIDTH + 1)]
    owner = object()
    assert all(p.try_lock(owner, 1, False) for p in processes[:-1])
    assert not processes[-1].try_lock(owner, 1, False)  # a documented limit
    for p in processes:
        os.close(p.fd)


def test_connections_in_one_process_arbitrate(tmp_path):
    path = str(tmp_path / "db")
    a, b = FileLocks(path, 0), FileLocks(path, 0)
    assert a.file is b.file  # one descriptor per process
    a.reserve()
    with pytest.raises(locking.LockTimeout):
        b.reserve(wait=False)
    a.release_reserved()
    b.reserve(wait=False)
    assert a.try_slot(3, exclusive=False) and b.try_slot(3, exclusive=False)
    c = FileLocks(path, 0)
    assert c.slot_in_use(3) and not c.try_lock_out_readers()
    c.close()
    a.close()
    b.close()
    b.close()  # twice is fine
    assert (os.stat(path + "-shm").st_dev, os.stat(path + "-shm").st_ino) not in _LockFile._open


@pytest.fixture
def windows(monkeypatch):
    """Run the pager on the Windows backend (with the fake msvcrt)."""
    monkeypatch.setattr(locking, "BACKEND", WindowsLocks(FakeMsvcrt()))


def database_with_one_row(path):
    """What the ``path`` fixtures of test_read_marks and test_concurrency make."""
    from minidb.database import Database

    with Database(path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'one')")
    return path


@pytest.mark.parametrize("name", [
    "test_checkpoint_copies_up_to_the_oldest_reader",
    "test_writer_restarts_a_copied_log_while_others_read",
    "test_log_stays_short_while_readers_always_overlap",
    "test_readers_share_slots",
])
def test_read_marks_on_the_windows_backend(tmp_path, windows, name):
    import test_read_marks

    getattr(test_read_marks, name)(database_with_one_row(str(tmp_path / "db")))


@pytest.mark.parametrize("name", [
    "test_committed_changes_are_visible_to_other_connections",
    "test_one_writer_at_a_time",
    "test_readers_keep_their_snapshot_while_writers_commit",
    "test_checkpoint_waits_for_no_one",
    "test_stale_snapshot_cannot_start_writing",
    "test_large_transaction_spills_to_the_log",
    "test_begin_immediate_reserves_at_once",
])
def test_concurrency_on_the_windows_backend(tmp_path, windows, name):
    import test_concurrency

    getattr(test_concurrency, name)(database_with_one_row(str(tmp_path / "db")))


def test_windows_upgrade_of_our_own_shared_lock(tmp_path):
    """SQLite's protocol upgrades SHARED to EXCLUSIVE.  msvcrt cannot convert
    a lock, so (as SQLite's winLock) the upgrade drops our shared byte, locks
    the whole range, and takes a shared byte again if that fails."""
    fake = FakeMsvcrt()
    path = str(tmp_path / "db")
    a, b = process(path, fake), process(path, fake)
    owner, other = object(), object()
    assert a.try_lock(owner, 5, False)
    assert a.try_lock(owner, 5, True)  # alone: the upgrade works
    assert not b.try_lock(other, 5, False)
    assert a.try_lock(owner, 5, False)  # downgrade
    assert b.try_lock(other, 5, False)
    assert not a.try_lock(owner, 5, True)  # b shares it: no upgrade ...
    assert a.held_by_others(other, 5) and b.held_by_others(other, 5)  # ... and a still shares
    b.unlock(other, 5)
    assert a.try_lock(owner, 5, True)
    a.unlock(owner, 5)
    assert fake.locked == {}
    for lock_file in (a, b):
        os.close(lock_file.fd)


@pytest.mark.parametrize("format", ["sqlite", None])
def test_databases_on_the_windows_backend(tmp_path, monkeypatch, format):
    """Both file formats work on WindowsLocks (the fake msvcrt): creating a
    file, writers and readers in one process, reopening.  (The SQLite
    protocol upgrades its shared lock: this failed on real Windows.)"""
    from minidb.database import Database

    fake = FakeMsvcrt()
    monkeypatch.setattr(locking, "BACKEND", WindowsLocks(fake))
    path = str(tmp_path / "db")
    with Database(path, format=format) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t (v) VALUES ('a'), ('b')")
        with Database(path) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT count(*) FROM t") == [(2,)]
            if format is None:  # (WAL: a writer need not wait for readers)
                db.execute("INSERT INTO t (v) VALUES ('c')")
                assert reader.execute("SELECT count(*) FROM t") == [(2,)]  # its snapshot
            reader.execute("COMMIT")
        db.execute("INSERT INTO t (v) VALUES ('d')")
    with Database(path) as db:
        assert db.execute("SELECT v FROM t ORDER BY id")[-1] == ("d",)
        assert db.integrity_check() == []
    assert fake.locked == {}
