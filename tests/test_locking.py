"""The lock layer: in-process arbitration and the Windows backend.

Windows itself is not available here: ``WindowsLocks`` runs against a fake
kernel with the semantics of ``LockFileEx`` / ``UnlockFileEx``: shared and
exclusive byte-range locks per handle; an exclusive lock overlaps no other
lock, also not of the same handle; a shared lock overlaps shared locks and
exclusive ones of the same handle; an unlock must match a locked range and
drops its exclusive lock first."""

import os

import pytest

import minidb.locking as locking
from minidb.locking import LOCK_OFFSET, FileLocks, WindowsLocks, _LockFile


class FakeKernel:
    def __init__(self):
        self.locks = []  # [file, fd, offset, length, exclusive]

    def lock(self, fd, offset, length, exclusive):
        st = os.fstat(fd)
        file = (st.st_dev, st.st_ino)
        for other, handle, start, size, held_exclusively in self.locks:
            if other == file and start < offset + length and offset < start + size:
                if exclusive or (held_exclusively and handle != fd):
                    return False
        self.locks.append([file, fd, offset, length, exclusive])
        return True

    def unlock(self, fd, offset, length):
        st = os.fstat(fd)
        mine = [lock for lock in self.locks if lock[:4] == [(st.st_dev, st.st_ino), fd, offset, length]]
        assert mine, f"unlocking a range that is not locked: {offset}+{length}"
        self.locks.remove(next((lock for lock in mine if lock[4]), mine[0]))


def process(path, kernel):
    """A _LockFile as another process would have it: its own handle."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    return _LockFile(fd, ("fake", fd), WindowsLocks(kernel))


def test_windows_backend_shares_and_excludes(tmp_path):
    kernel = FakeKernel()
    path = str(tmp_path / "db-shm")
    a, b, c = (process(path, kernel) for _ in range(3))
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
    assert c.try_lock(owner, 5, False)  # downgrade (atomic)
    assert a.try_lock(owner, 5, False)
    assert not c.try_lock(owner, 5, True)  # an upgrade while another shares: no
    c.unlock(owner, 5)
    a.unlock(owner, 5)
    assert kernel.locks == []
    for lock_file in (a, b, c):
        os.close(lock_file.fd)


def test_windows_locks_lie_past_the_data(tmp_path):
    kernel = FakeKernel()
    path = str(tmp_path / "db-shm")
    a = process(path, kernel)
    assert a.try_lock(object(), 0, True)
    assert min(offset for _, _, offset, _, _ in kernel.locks) >= LOCK_OFFSET
    os.close(a.fd)


def test_windows_backend_shares_among_many_processes(tmp_path):
    kernel = FakeKernel()
    path = str(tmp_path / "db-shm")
    processes = [process(path, kernel) for _ in range(100)]
    owner = object()
    assert all(p.try_lock(owner, 1, False) for p in processes)  # no limit (msvcrt had one)
    for p in processes:
        p.unlock(owner, 1)
        os.close(p.fd)
    assert kernel.locks == []


def test_windows_backend_shares_sqlites_read_lock(tmp_path):
    """sqlite3 on Windows reads under a shared LockFileEx lock on the whole
    SHARED range: MiniDB's readers share it, its writers wait."""
    from minidb.sqlite_pager import SHARED, SPANS

    kernel = FakeKernel()
    path = str(tmp_path / "db")
    sqlite_fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    assert kernel.lock(sqlite_fd, *SPANS[SHARED], False)  # what winGetReadLock takes
    fd = os.open(path, os.O_RDWR)
    mine = _LockFile(fd, ("fake", fd), WindowsLocks(kernel), spans=SPANS)
    owner = object()
    assert mine.try_lock(owner, SHARED, False)
    assert not mine.try_lock(owner, SHARED, True)
    kernel.unlock(sqlite_fd, *SPANS[SHARED])
    assert mine.try_lock(owner, SHARED, True)
    mine.unlock(owner, SHARED)
    assert kernel.locks == []
    os.close(fd)
    os.close(sqlite_fd)


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
    """Run the pager on the Windows backend (with the fake kernel)."""
    monkeypatch.setattr(locking, "BACKEND", WindowsLocks(FakeKernel()))


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
    """SQLite's protocol upgrades SHARED to EXCLUSIVE.  LockFileEx cannot
    convert a lock, so (as SQLite's winLock) the upgrade unlocks, locks
    exclusively, and shares again if that fails."""
    kernel = FakeKernel()
    path = str(tmp_path / "db")
    a, b = process(path, kernel), process(path, kernel)
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
    assert kernel.locks == []
    for lock_file in (a, b):
        os.close(lock_file.fd)


@pytest.mark.parametrize("format", ["sqlite", None])
def test_databases_on_the_windows_backend(tmp_path, monkeypatch, format):
    """Both file formats work on WindowsLocks (the fake kernel): creating a
    file, writers and readers in one process, reopening.  (The SQLite
    protocol upgrades its shared lock: this failed on real Windows.)"""
    from minidb.database import Database

    kernel = FakeKernel()
    monkeypatch.setattr(locking, "BACKEND", WindowsLocks(kernel))
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
    assert kernel.locks == []
