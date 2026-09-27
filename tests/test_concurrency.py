"""Several connections and processes on one database file: locking and cache coherence."""

import os
import subprocess
import sys
import textwrap
import time

import pytest

from minidb.database import Database
from minidb.errors import OperationalError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def path(tmp_path):
    path = str(tmp_path / "db")
    db = Database(path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'one')")
    db.close()
    return path


def locked(call):
    """Run ``call``; return how long it took to fail with 'database is locked'."""
    start = time.monotonic()
    with pytest.raises(OperationalError, match="database is locked"):
        call()
    return time.monotonic() - start


# ---- connections in one process ---------------------------------------------------


def test_committed_changes_are_visible_to_other_connections(path):
    a, b = Database(path), Database(path)
    assert b.execute("SELECT v FROM t") == [("one",)]  # b caches the pages
    a.execute("INSERT INTO t VALUES (2, 'two')")
    a.execute("UPDATE t SET v = 'uno' WHERE id = 1")
    assert b.execute("SELECT * FROM t") == [(1, "uno"), (2, "two")]
    b.execute("DELETE FROM t WHERE id = 2")
    assert a.execute("SELECT * FROM t") == [(1, "uno")]
    a.close()
    b.close()


def test_schema_changes_are_visible_to_other_connections(path):
    a, b = Database(path), Database(path)
    b.execute("SELECT * FROM t")
    a.execute("CREATE TABLE u (x INTEGER)")
    a.execute("CREATE INDEX t_v ON t (v)")
    b.execute("INSERT INTO u VALUES (5)")
    assert b.execute("EXPLAIN SELECT * FROM t WHERE v = 'one'") == [("t", "SEARCH USING INDEX t_v (v=?)")]
    assert a.execute("SELECT * FROM u") == [(5,)]
    a.execute("DROP TABLE u")
    with pytest.raises(OperationalError, match="no such table"):
        b.execute("SELECT * FROM u")
    assert a.integrity_check() == [] and b.integrity_check() == []
    a.close()
    b.close()


def test_one_writer_at_a_time(path):
    a, b = Database(path), Database(path, timeout=0.2)
    a.execute("BEGIN")
    a.execute("INSERT INTO t VALUES (2, 'two')")  # a now holds RESERVED
    assert locked(lambda: b.execute("INSERT INTO t VALUES (3, 'three')")) >= 0.15
    assert b.execute("SELECT count(*) FROM t") == [(1,)]  # reading is fine, and isolated
    a.execute("COMMIT")
    b.execute("INSERT INTO t VALUES (3, 'three')")
    assert a.execute("SELECT id FROM t") == [(1,), (2,), (3,)]
    a.close()
    b.close()


def test_commit_waits_for_readers_and_can_be_retried(path):
    reader, writer = Database(path), Database(path, timeout=0.2)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t")  # reader holds SHARED
    writer.execute("BEGIN")
    writer.execute("INSERT INTO t VALUES (2, 'two')")
    locked(lambda: writer.execute("COMMIT"))  # needs EXCLUSIVE
    assert writer.in_transaction  # nothing was lost
    assert reader.execute("SELECT count(*) FROM t") == [(1,)]
    reader.execute("COMMIT")
    writer.execute("COMMIT")
    assert reader.execute("SELECT count(*) FROM t") == [(2,)]
    # An autocommit statement whose commit times out is undone instead.
    reader.execute("BEGIN")
    reader.execute("SELECT 1 FROM t")
    locked(lambda: writer.execute("INSERT INTO t VALUES (3, 'three')"))
    reader.execute("ROLLBACK")
    assert writer.execute("SELECT count(*) FROM t") == [(2,)]
    assert writer.integrity_check() == []
    reader.close()
    writer.close()


def test_deadlock_is_avoided_by_failing_fast(path):
    a, b = Database(path, timeout=2), Database(path, timeout=0.2)
    a.execute("BEGIN")
    a.execute("SELECT * FROM t")
    b.execute("BEGIN")
    b.execute("SELECT * FROM t")
    b.execute("INSERT INTO t VALUES (2, 'b')")  # b is the writer
    # a holds SHARED and wants RESERVED: waiting could deadlock, so it fails at once.
    assert locked(lambda: a.execute("INSERT INTO t VALUES (3, 'a')")) < 0.5
    locked(lambda: b.execute("COMMIT"))  # b waits for a's SHARED
    a.execute("ROLLBACK")
    b.execute("COMMIT")
    assert a.execute("SELECT * FROM t") == [(1, "one"), (2, "b")]
    a.close()
    b.close()


def test_begin_immediate_reserves_at_once(path):
    a, b = Database(path), Database(path, timeout=0.1)
    a.execute("BEGIN IMMEDIATE")
    locked(lambda: b.execute("BEGIN IMMEDIATE"))
    assert not b.in_transaction
    b.execute("BEGIN")  # deferred: only reads so far
    assert b.execute("SELECT count(*) FROM t") == [(1,)]
    b.execute("ROLLBACK")
    a.execute("COMMIT")
    b.execute("BEGIN EXCLUSIVE")
    b.execute("COMMIT")
    a.close()
    b.close()


def test_open_connection_recovers_after_another_crashes(path):
    a, b = Database(path), Database(path)
    b.execute("SELECT * FROM t")

    def crash(point, index):
        if point == "db_page" and index == 1:
            raise KeyboardInterrupt("simulated crash")

    a.execute("BEGIN")
    a.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, '{'x' * 100}')" for i in range(2, 400)))
    a.pager.crash_hook = crash
    with pytest.raises(KeyboardInterrupt):
        a.execute("COMMIT")  # the WAL is complete; the database file is half written
    assert os.path.exists(path + "-wal")
    assert b.execute("SELECT count(*) FROM t") == [(399,)]  # b replays the WAL first
    assert not os.path.exists(path + "-wal")
    assert b.integrity_check() == []
    b.close()


def test_in_memory_databases_are_independent():
    a, b = Database(), Database()
    a.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(OperationalError):
        b.execute("SELECT * FROM t")


# ---- several processes ------------------------------------------------------------

WORKER = textwrap.dedent("""
    import random, sys
    sys.path.insert(0, {root!r})
    from minidb.database import Database

    path, worker, rounds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    db = Database(path, timeout=30)
    rng = random.Random(worker)
    for i in range(rounds):
        if i % 3 == 0:
            # autocommit statements
            db.execute("UPDATE counter SET n = n + 1")
            db.execute("INSERT INTO log (worker, i) VALUES (?, ?)", (worker, i))
        else:
            db.execute("BEGIN IMMEDIATE")
            (n,) = db.execute("SELECT n FROM counter")[0]
            db.execute("UPDATE counter SET n = ?", (n + 1,))
            db.execute("INSERT INTO log (worker, i) VALUES (?, ?)", (worker, i))
            # move money between accounts: the total must never change
            a, b = rng.sample(range(5), 2)
            amount = rng.randint(1, 50)
            db.execute("UPDATE account SET balance = balance - ? WHERE id = ?", (amount, a))
            db.execute("UPDATE account SET balance = balance + ? WHERE id = ?", (amount, b))
            db.execute("COMMIT")
    db.close()
""")

READER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {root!r})
    from minidb.database import Database

    path, seconds = sys.argv[1], float(sys.argv[2])
    db = Database(path, timeout=30)
    deadline = time.monotonic() + seconds
    checks = 0
    while time.monotonic() < deadline:
        db.execute("BEGIN")
        total = db.execute("SELECT sum(balance) FROM account")[0][0]
        n = db.execute("SELECT n FROM counter")[0][0]
        logged = db.execute("SELECT count(*) FROM log")[0][0]
        db.execute("COMMIT")
        assert total == 5000, total  # transfers happen only inside transactions
        # Autocommit rounds bump the counter and insert the log row as two
        # separate commits, so each worker may be one step ahead in between.
        assert 0 <= n - logged <= int(sys.argv[3]), (n, logged)
        checks += 1
    print(checks)
""")


def test_many_processes_lose_no_updates_and_see_only_whole_commits(tmp_path):
    path = str(tmp_path / "shared.db")
    db = Database(path)
    db.execute("CREATE TABLE counter (n INTEGER)")
    db.execute("INSERT INTO counter VALUES (0)")
    db.execute("CREATE TABLE log (id INTEGER PRIMARY KEY, worker INTEGER, i INTEGER)")
    db.execute("CREATE INDEX log_worker ON log (worker)")
    db.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, balance INTEGER)")
    db.execute("INSERT INTO account VALUES (0, 1000), (1, 1000), (2, 1000), (3, 1000), (4, 1000)")
    db.close()
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(WORKER.format(root=ROOT))
    reader_script = tmp_path / "reader.py"
    reader_script.write_text(READER.format(root=ROOT))
    workers, rounds = 4, 60
    procs = [
        subprocess.Popen([sys.executable, str(worker_script), path, str(w), str(rounds)])
        for w in range(workers)
    ]
    reader = subprocess.Popen(
        [sys.executable, str(reader_script), path, "3", str(workers)],
        stdout=subprocess.PIPE, text=True,
    )
    assert [p.wait(timeout=120) for p in procs] == [0] * workers
    out, _ = reader.communicate(timeout=120)
    assert reader.returncode == 0
    assert int(out) > 10  # the reader really ran alongside the writers
    db = Database(path)
    assert db.execute("SELECT n FROM counter") == [(workers * rounds,)]
    assert db.execute("SELECT count(*), count(DISTINCT worker) FROM log") == [(workers * rounds, workers)]
    assert db.execute("SELECT sum(balance) FROM account") == [(5000,)]
    assert db.integrity_check() == []
    db.close()


def test_lock_of_a_dead_process_is_released(path, tmp_path):
    script = tmp_path / "hold.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {ROOT!r})
        from minidb.database import Database
        db = Database({path!r})
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO t VALUES (99, 'never committed')")
        os._exit(3)
    """))
    assert subprocess.run([sys.executable, str(script)]).returncode == 3
    db = Database(path, timeout=0.5)
    db.execute("INSERT INTO t VALUES (2, 'two')")
    assert db.execute("SELECT id FROM t") == [(1,), (2,)]
    db.close()
