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
    assert b.execute("EXPLAIN SELECT * FROM t WHERE v = 'one'") == [("t", "SEARCH USING COVERING INDEX t_v (v=?)")]
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


def test_readers_keep_their_snapshot_while_writers_commit(path):
    reader, writer = Database(path), Database(path, timeout=0.2)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM t") == [(1,)]
    for i in range(2, 50):
        writer.execute("INSERT INTO t VALUES (?, 'w')", (i,))  # commits do not wait for readers
    writer.execute("UPDATE t SET v = 'changed' WHERE id = 1")
    assert reader.execute("SELECT count(*) FROM t") == [(1,)]  # same snapshot
    assert reader.execute("SELECT v FROM t WHERE id = 1") == [("one",)]
    reader.execute("COMMIT")
    assert reader.execute("SELECT count(*), max(v) FROM t") == [(49, "w")]
    assert reader.integrity_check() == [] and writer.integrity_check() == []
    reader.close()
    writer.close()


def test_checkpoint_waits_for_no_one(path):
    reader, writer = Database(path), Database(path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t")
    writer.execute("INSERT INTO t VALUES (2, 'two')")
    assert not writer.pager.checkpoint()  # a reader is active: skipped, not waited for
    assert os.path.getsize(path + "-wal") > 0
    reader.execute("COMMIT")
    assert writer.pager.checkpoint()
    assert os.path.getsize(path + "-wal") == 0
    assert reader.execute("SELECT count(*) FROM t") == [(2,)]
    reader.close()
    writer.close()


def test_stale_snapshot_cannot_start_writing(path):
    a, b = Database(path, timeout=2), Database(path, timeout=0.2)
    a.execute("BEGIN")
    a.execute("SELECT * FROM t")
    b.execute("BEGIN")
    b.execute("SELECT * FROM t")
    b.execute("INSERT INTO t VALUES (2, 'b')")  # b is the writer
    # a holds a snapshot and wants RESERVED: it fails at once instead of waiting.
    assert locked(lambda: a.execute("INSERT INTO t VALUES (3, 'a')")) < 0.5
    b.execute("COMMIT")  # does not wait for a
    # RESERVED is free now, but a's snapshot is older than b's commit.
    locked(lambda: a.execute("INSERT INTO t VALUES (3, 'a')"))
    assert a.execute("SELECT count(*) FROM t") == [(1,)]
    a.execute("ROLLBACK")
    a.execute("INSERT INTO t VALUES (3, 'a')")
    assert b.execute("SELECT * FROM t") == [(1, "one"), (2, "b"), (3, "a")]
    a.close()
    b.close()


def test_large_transaction_spills_to_the_log(path):
    db = Database(path)
    db.execute("BEGIN")
    for start in range(0, 40_000, 1000):
        db.execute("INSERT INTO t (v) VALUES " + ", ".join(f"('{'x' * 200}{i}')" for i in range(start, start + 1000)))
        assert len(db.pager.dirty) <= 1200  # spilled regularly
    assert len(db.pager.cache) < 2 * 1200
    assert db.execute("SELECT count(*), max(length(v)) FROM t") == [(40_001, 205)]
    other = Database(path)
    assert other.execute("SELECT count(*) FROM t") == [(1,)]  # uncommitted: invisible
    db.execute("ROLLBACK")
    assert db.execute("SELECT count(*) FROM t") == [(1,)]
    db.execute("BEGIN")
    db.execute("INSERT INTO t (v) VALUES " + ", ".join(f"('{i}')" for i in range(5000)))
    db.pager.spill()
    db.execute("COMMIT")
    assert other.execute("SELECT count(*) FROM t") == [(5001,)]
    assert db.integrity_check() == []
    other.close()
    db.close()


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


def test_open_connection_survives_another_crashing(path):
    a, b = Database(path), Database(path)
    b.execute("SELECT * FROM t")

    def crash(point, index):
        if point == "wal_sync":
            raise KeyboardInterrupt("simulated crash")

    a.execute("BEGIN")
    a.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, '{'x' * 100}')" for i in range(2, 400)))
    a.pager.crash_hook = crash
    with pytest.raises(KeyboardInterrupt):
        a.execute("COMMIT")  # the commit frame was written, not yet synced
    assert b.execute("SELECT count(*) FROM t") == [(399,)]
    assert b.integrity_check() == []
    b.execute("DELETE FROM t WHERE id > 100")
    b.close()
    a.pager.close_files()  # what the crashed process's exit would do
    with Database(path) as db:
        assert db.execute("SELECT count(*) FROM t") == [(100,)]


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


def test_log_is_checkpointed_automatically(path):
    db = Database(path)
    for i in range(2, 1500):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "v" * 50))
        assert db.pager.committed < 1100  # never far beyond the threshold
    assert db.execute("SELECT count(*) FROM t") == [(1499,)]
    db.close()


# ---- deferred transactions, as SQLite starts them ------------------------------------


def deferred_scenarios(connect):
    """What happens to deferred transactions next to another writer: the
    first statement starts the transaction (a write waits for the lock
    first, so its snapshot is the newest), and BEGIN alone holds nothing."""
    import sqlite3

    errors = (OperationalError, sqlite3.OperationalError)
    outcome = []
    a, b = connect(), connect()
    a("BEGIN")
    b("INSERT INTO t (v) VALUES ('b1')")  # (BEGIN alone holds no lock, also with a rollback journal)
    a("UPDATE t SET v = v || '!' WHERE id = 1")  # first statement: a write, so the newest snapshot
    outcome.append(a("SELECT count(*), max(v) FROM t"))
    a("COMMIT")
    a("BEGIN")
    outcome.append(a("SELECT count(*) FROM t"))  # the snapshot starts here
    try:
        b("INSERT INTO t (v) VALUES ('b2')")
        outcome.append("b committed")
        a("UPDATE t SET v = 'a' WHERE id = 1")
        outcome.append("a wrote")
    except errors as exc:
        outcome.append(str(exc))
    a("ROLLBACK")
    outcome.append(b("SELECT count(*) FROM t"))
    return outcome


@pytest.mark.parametrize("kind", ["minidb", "sqlite-journal", pytest.param("sqlite-wal", marks=pytest.mark.skipif(
    sys.platform == "win32", reason="SQLite's WAL mode has not been run on Windows"))])
def test_deferred_transactions_start_at_their_first_statement(tmp_path, kind):
    import sqlite3

    def make(path):
        with Database(path, format=None if kind == "minidb" else "sqlite") as db:
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            db.execute("INSERT INTO t VALUES (1, 'one')")
            if kind == "sqlite-wal":
                db.execute("PRAGMA journal_mode = WAL")

    mini_path, lite_path = str(tmp_path / "mini"), str(tmp_path / "lite")
    make(mini_path)
    make(lite_path)
    opened = []

    def mini():
        db = Database(mini_path, timeout=0.3)
        opened.append(db)
        return lambda sql: db.execute(sql)

    def lite():
        connection = sqlite3.connect(lite_path, isolation_level=None, timeout=0.3)
        opened.append(connection)
        return lambda sql: connection.execute(sql).fetchall()

    try:
        got = deferred_scenarios(mini)
        expected = ([[(2, "one!")], [(2,)], "b committed", "database is locked", [(3,)]] if kind != "sqlite-journal"
                    else [[(2, "one!")], [(2,)], "database is locked", [(2,)]])
        assert got == expected
        if kind != "minidb":
            assert deferred_scenarios(lite) == expected
    finally:
        for connection in opened:
            connection.close()


@pytest.mark.parametrize("kind", ["minidb", "sqlite-journal", pytest.param("sqlite-wal", marks=pytest.mark.skipif(
    sys.platform == "win32", reason="SQLite's WAL mode has not been run on Windows"))])
def test_a_transaction_that_never_read_ends_without_touching_the_file(tmp_path, kind):
    # BEGIN; ROLLBACK (or COMMIT) with nothing in between has no snapshot:
    # reloading the schema there read the database file without the pages
    # still in the WAL - and the next table reused a page (fuzz seed 2269).
    import sqlite3

    path = str(tmp_path / "db")
    db = Database(path, format=None if kind == "minidb" else "sqlite")
    try:
        if kind == "sqlite-wal":
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA wal_autocheckpoint = 0")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t (v) VALUES " + ", ".join(f"('{'v' * 50}{i}')" for i in range(300)))
        for end in ("ROLLBACK", "COMMIT"):
            db.execute("BEGIN")
            db.execute(end)
        db.execute("CREATE TABLE u (w TEXT)")
        db.execute("INSERT INTO u VALUES " + ", ".join(f"('{'w' * 50}{i}')" for i in range(300)))
        assert db.execute("SELECT count(*) FROM t") == [(300,)]
        assert db.integrity_check() == []
    finally:
        db.close()
    if kind != "minidb":
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("SELECT count(*) FROM u").fetchall() == [(300,)]
        finally:
            connection.close()
