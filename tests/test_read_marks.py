"""Read marks: checkpoints copy the log as far as readers allow, and a writer
restarts a fully copied log even while readers keep reading."""

import os
import threading

import pytest

from minidb.database import Database
from minidb.locking import READ_SLOTS
from minidb.pager import FRAME_SIZE
from test_transactions import SimulatedCrash, crash_at


@pytest.fixture
def path(tmp_path):
    path = str(tmp_path / "db")
    with Database(path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'one')")
    return path


def begin(db):
    db.execute("BEGIN")
    return db.execute("SELECT count(*) FROM t")[0][0]


def count(db):
    return db.execute("SELECT count(*) FROM t")[0][0]


def fresh_count(path):
    with Database(path) as db:
        return count(db)


def wal_frames(path):
    return os.path.getsize(path + "-wal") // FRAME_SIZE


def test_checkpoint_copies_up_to_the_oldest_reader(path):
    writer, reader = Database(path), Database(path)
    writer.execute("INSERT INTO t VALUES (2, 'two')")
    assert begin(reader) == 2
    mark = reader.pager.committed
    writer.execute("INSERT INTO t VALUES (3, 'three')")
    assert not writer.pager.checkpoint()  # only part of the log could be copied
    assert writer.pager._backfilled() == mark < writer.pager.committed
    assert count(reader) == 2  # the reader's snapshot is intact
    assert fresh_count(path) == 3
    reader.execute("COMMIT")
    assert writer.pager.checkpoint()
    assert wal_frames(path) == 0  # no one reads: the log is emptied
    assert count(reader) == 3
    reader.close()
    writer.close()


def test_writer_restarts_a_copied_log_while_others_read(path):
    writer, old, new = Database(path), Database(path), Database(path)
    writer.execute("INSERT INTO t VALUES (2, 'two')")
    assert begin(old) == 2  # holds a read mark
    assert writer.pager.checkpoint()  # all copied, but not emptied: old reads
    assert wal_frames(path) > 0
    assert begin(new) == 2  # reads the database file only (slot 0)
    assert new.pager.locks.slot == 0
    generation = writer.pager.wal_generation
    writer.execute("INSERT INTO t VALUES (3, 'three')")
    assert writer.pager.wal_generation != generation  # restarted
    assert count(old) == 2 and count(new) == 2
    assert old.execute("SELECT max(v) FROM t") == [("two",)]
    assert not writer.pager.checkpoint()  # readers of the old log hold it back
    assert fresh_count(path) == 3
    old.execute("COMMIT")
    new.execute("COMMIT")
    assert writer.pager.checkpoint()
    assert count(old) == 3 and count(new) == 3
    for db in (writer, old, new):
        assert db.integrity_check() == []
        db.close()


def test_log_stays_short_while_readers_always_overlap(path):
    writer = Database(path)
    writer.pager.checkpoint_frames = 20
    readers = [Database(path), Database(path)]
    expected = [begin(readers[0])]
    longest = 0
    for i in range(300):
        writer.execute("INSERT INTO t VALUES (?, ?)", (i + 2, "x" * 500))
        # the next reader starts before the current one ends: there is always a reader
        current, following = readers[i % 2], readers[(i + 1) % 2]
        assert begin(following) == i + 2
        assert count(current) == expected[-1]  # still its old snapshot
        current.execute("COMMIT")
        expected.append(i + 2)
        longest = max(longest, wal_frames(path))
    assert longest < 60  # without restarts: over 300 commits' worth of frames
    readers[300 % 2].execute("COMMIT")
    for db in readers + [writer]:
        assert count(db) == 301
        assert db.integrity_check() == []
        db.close()


def test_readers_share_slots(path):
    writer = Database(path)
    readers = []
    for i in range(3 * READ_SLOTS):  # more snapshots than slots
        writer.execute("INSERT INTO t VALUES (?, 'x')", (i + 2,))
        reader = Database(path)
        assert begin(reader) == i + 2
        readers.append(reader)
    writer.execute("UPDATE t SET v = 'changed'")
    writer.pager.checkpoint()
    writer.execute("DELETE FROM t WHERE id > 5")
    writer.pager.checkpoint()
    for i, reader in enumerate(readers):
        assert count(reader) == i + 2
        assert reader.execute("SELECT count(*) FROM t WHERE v = 'changed'") == [(0,)]
        reader.execute("COMMIT")
        reader.close()
    assert writer.pager.checkpoint()
    assert count(writer) == 5
    writer.close()


@pytest.mark.parametrize("point, detail", [("wal_restart", None), ("wal_frame", 0), ("wal_sync", None)])
def test_crash_while_restarting_the_log(path, point, detail):
    writer, reader = Database(path), Database(path)
    writer.execute("INSERT INTO t VALUES (2, 'two')")
    begin(reader)
    assert writer.pager.checkpoint() and wal_frames(path) > 0
    reader.execute("COMMIT")
    writer.pager.crash_hook = crash_at(point, detail)
    with pytest.raises(SimulatedCrash):
        writer.execute("INSERT INTO t VALUES (3, 'three')")
    writer.pager.close_files()
    assert count(reader) in (2, 3) if point == "wal_sync" else count(reader) == 2
    reader.close()
    with Database(path) as db:
        assert db.execute("SELECT id FROM t")[:2] == [(1,), (2,)]
        assert db.integrity_check() == []


def test_threads_reading_and_writing(path):
    """Connections in one process arbitrate their locks among themselves."""
    with Database(path) as db:
        db.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, balance INTEGER)")
        db.execute("INSERT INTO account VALUES (0, 1000), (1, 1000), (2, 1000)")
    stop = threading.Event()
    errors, reads, generations = [], [0], set()

    def read():
        db = Database(path, timeout=30)
        try:
            while not stop.is_set():
                db.execute("BEGIN")
                total = db.execute("SELECT sum(balance) FROM account")[0][0]
                rows = count(db)
                assert db.execute("SELECT sum(balance) FROM account")[0][0] == total == 3000
                assert count(db) == rows
                db.execute("COMMIT")
                reads[0] += 1
        except BaseException as exc:  # reported below
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=read) for _ in range(3)]
    for thread in threads:
        thread.start()
    db = Database(path, timeout=30)
    db.pager.checkpoint_frames = 30
    for i in range(400):
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE account SET balance = balance - 7 WHERE id = ?", (i % 3,))
        db.execute("UPDATE account SET balance = balance + 7 WHERE id = ?", ((i + 1) % 3,))
        db.execute("INSERT INTO t (v) VALUES (?)", ("y" * 200,))
        db.execute("COMMIT")
        generations.add(db.pager.wal_generation)
    stop.set()
    for thread in threads:
        thread.join()
    assert errors == []
    assert reads[0] > 20
    assert db.execute("SELECT sum(balance) FROM account") == [(3000,)]
    assert count(db) == 401
    assert db.integrity_check() == []
    db.close()
    # The log was restarted while readers kept reading.  (How short it stays
    # depends on thread scheduling: with the GIL on a slow machine a reader
    # may outlast the writer's wait; test_processes_... checks the bound.)
    assert len(generations - {None}) > 1


READER = """
import sys, time
sys.path.insert(0, {root!r})
from minidb.database import Database

db = Database({path!r}, timeout=30)
deadline = time.monotonic() + {seconds}
checks = 0
while time.monotonic() < deadline:
    db.execute("BEGIN")
    assert db.execute("SELECT sum(balance) FROM account")[0][0] == 3000
    rows = db.execute("SELECT count(*) FROM t")[0][0]
    time.sleep(0.002)
    assert db.execute("SELECT count(*), sum(balance) FROM t, account WHERE account.id = 0")[0][0] == rows
    db.execute("COMMIT")
    checks += 1
db.close()
print(checks)
"""


def test_processes_reading_while_the_log_restarts(path):
    import subprocess
    import sys

    with Database(path) as db:
        db.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, balance INTEGER)")
        db.execute("INSERT INTO account VALUES (0, 1000), (1, 1000), (2, 1000)")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = READER.format(root=root, path=path, seconds=3)
    readers = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    db = Database(path, timeout=30)
    db.pager.checkpoint_frames = 30
    longest = commits = 0
    while any(reader.poll() is None for reader in readers):
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE account SET balance = balance - 5 WHERE id = ?", (commits % 3,))
        db.execute("UPDATE account SET balance = balance + 5 WHERE id = ?", ((commits + 1) % 3,))
        db.execute("INSERT INTO t (v) VALUES (?)", ("z" * 300,))
        db.execute("COMMIT")
        commits += 1
        longest = max(longest, wal_frames(path))
    outputs = [reader.communicate()[0] for reader in readers]
    assert [reader.returncode for reader in readers] == [0] * 4
    assert all(int(out) > 10 for out in outputs)
    assert commits > 100
    assert longest < commits * 2  # restarts keep it far below ~4 frames per commit
    assert db.integrity_check() == []
    db.close()
