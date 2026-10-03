"""SQLite files in WAL mode: MiniDB and sqlite3 read and write each other's
-wal / -shm, at the same time (in separate processes), checkpoint, switch
journal modes and recover each other's crashes.

While MiniDB has a WAL database open, sqlite3 runs in another process
(``lite_child``): POSIX locks belong to a process, so an sqlite3 connection
in this one could not see MiniDB's - closing, it would think itself the
last user and delete the log MiniDB is using."""

import ast
import os
import sqlite3
import subprocess
import sys
import textwrap
from contextlib import closing

import pytest

from minidb.database import Database
from minidb.errors import OperationalError
from test_transactions import SimulatedCrash, crash_at

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lite(path, **options):
    connection = sqlite3.connect(path, isolation_level=None, **options)
    connection.text_factory = lambda data: data.decode("utf-8", "surrogateescape")
    return connection


CHILD = textwrap.dedent("""
    import sqlite3, sys
    connection = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=10)
    connection.text_factory = lambda data: data.decode("utf-8", "surrogateescape")
    print(repr([connection.execute(sql).fetchall() for sql in sys.argv[2:]]))
""")


def lite_child(path, *statements):
    """Run ``statements`` with sqlite3 in another process: their results."""
    done = subprocess.run([sys.executable, "-c", CHILD, path, *statements], capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr
    return ast.literal_eval(done.stdout)


def read(name):
    with open(name, "rb") as f:
        return f.read()


def files(path):
    return [suffix for suffix in ("-wal", "-shm", "-journal") if os.path.exists(path + suffix)]


def wal_database(path, rows=300, page_size=4096):
    """A database sqlite3 left in WAL mode with its changes still in the log."""
    with closing(lite(path)) as connection:
        connection.execute(f"PRAGMA page_size = {page_size}")
        assert connection.execute("PRAGMA journal_mode = WAL").fetchall() == [("wal",)]
        connection.executescript("""
            CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b BLOB);
            CREATE INDEX ta ON t (a);
        """)
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.executemany("INSERT INTO t (a, b) VALUES (?, ?)",
                               [(f"v{i:04}" + "x" * (i % 90), bytes(i % 700)) for i in range(rows)])
        # Keep the log: copy the files while the connection is still open.
        image = {suffix: read(path + suffix) for suffix in ("", "-wal", "-shm")}
    for suffix, data in image.items():
        with open(path + suffix, "wb") as f:
            f.write(data)
    return image


QUERIES = ["SELECT * FROM t ORDER BY id", "SELECT a FROM t WHERE a > 'v02' ORDER BY a",
           "SELECT count(*), sum(length(b)) FROM t"]


def same(path, db):
    for sql, expected in zip(QUERIES, lite_child(path, *QUERIES)):
        assert db.execute(sql) == expected, sql


# ---- reading and writing each other's log ------------------------------------------


def test_a_log_sqlite3_left(tmp_path):
    path = str(tmp_path / "db")
    wal_database(path)
    assert os.path.getsize(path + "-wal") > 100_000
    with Database(path) as db:
        assert db.execute("PRAGMA journal_mode") == [("wal",)]
        assert db.execute("SELECT count(*) FROM t") == [(300,)]
        same(path, db)
        db.execute("UPDATE t SET a = a || '!' WHERE id % 3 = 0")
        db.execute("DELETE FROM t WHERE id % 7 = 0")
        db.execute("CREATE TABLE u (x)")
        same(path, db)
        assert db.integrity_check() == []
        assert files(path) == ["-wal", "-shm"]
    assert files(path) == []  # the last connection copied the log and deleted it
    with closing(lite(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA journal_mode").fetchall() == [("wal",)]
        assert connection.execute("SELECT count(*) FROM t").fetchall() == [(300 - 42,)]


def test_turns_on_wal_mode_for_sqlite3(tmp_path):
    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b BLOB)")
        db.execute("CREATE INDEX ta ON t (a)")
        assert db.execute("PRAGMA journal_mode = WAL") == [("wal",)]
        assert files(path) == ["-wal", "-shm"]
        for i in range(200):
            db.execute("INSERT INTO t (a, b) VALUES (?, ?)", (f"m{i}", bytes(i * 3)))
        assert lite_child(path, "PRAGMA journal_mode", "SELECT count(*), sum(length(b)) FROM t",
                          "PRAGMA integrity_check", "INSERT INTO t (a) VALUES ('lite')") == [
            [("wal",)], [(200, 59700)], [("ok",)], []]  # (reads the log MiniDB wrote)
        assert db.execute("SELECT a FROM t WHERE id > 199") == [("m199",), ("lite",)]
        same(path, db)
    assert read(path)[18:20] == b"\x02\x02"


def test_a_transaction_bigger_than_a_hash_table(tmp_path):
    # More than 4062 frames: the wal-index needs a second block.
    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        db.execute("PRAGMA page_size = 512")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA wal_autocheckpoint = 0")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b BLOB)")
        db.execute("CREATE INDEX ta ON t (a)")
        db.execute("BEGIN")
        for i in range(3000):
            db.execute("INSERT INTO t (a, b) VALUES (?, ?)", (f"{i:05}" * 8, bytes(300)))
        db.execute("COMMIT")
        db.execute("UPDATE t SET b = x'01' WHERE id % 10 = 0")
        assert db.pager.committed > 4100
        assert lite_child(path, "SELECT count(*), sum(length(b)) FROM t", "PRAGMA integrity_check",
                          "UPDATE t SET b = x'02' WHERE id = 2999") == [[(3000, 2700 * 300 + 300)], [("ok",)], []]
        assert db.execute("SELECT b FROM t WHERE id = 2999") == [(b"\x02",)]
    # The log is gone; recovery of a log that big:
    with closing(lite(path)) as connection:
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.execute("UPDATE t SET a = 'z' || a")
        image = read(path + "-wal")
        expected = connection.execute("SELECT count(*), sum(length(a)) FROM t").fetchall()
    with open(path + "-wal", "wb") as f:  # (sqlite3 had copied and deleted it)
        f.write(image)
    with Database(path) as db:
        assert db.execute("SELECT count(*), sum(length(a)) FROM t") == expected
        assert db.integrity_check() == []


# ---- checkpoints and journal modes --------------------------------------------------


def test_checkpoints(tmp_path):
    path = str(tmp_path / "db")
    wal_database(path, rows=200)
    db = Database(path)
    frames, copied = db.pager.wal.describe()["frames"], db.pager.wal.describe()["backfill"]
    assert frames > 200 and copied == 0
    reader = Database(path)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM t") == [(200,)]
    db.execute("INSERT INTO t (a) VALUES ('after')")
    # The reader's snapshot holds back the frame after it.
    busy, log, checkpointed = db.execute("PRAGMA wal_checkpoint")[0]
    assert (busy, log) == (1, frames + 2) and checkpointed == frames
    assert reader.execute("SELECT count(*) FROM t") == [(200,)]
    with pytest.raises(OperationalError, match="database table is locked"):
        reader.execute("PRAGMA wal_checkpoint")
    reader.execute("COMMIT")
    assert db.execute("PRAGMA wal_checkpoint(PASSIVE)") == [(0, frames + 2, frames + 2)]
    assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)") == [(0, 0, 0)]
    assert os.path.getsize(path + "-wal") == 0
    db.execute("INSERT INTO t (a) VALUES ('restarted')")
    assert reader.execute("SELECT a FROM t WHERE id > 200") == [("after",), ("restarted",)]
    assert lite_child(path, "SELECT count(*) FROM t", "PRAGMA integrity_check") == [[(202,)], [("ok",)]]
    assert db.execute("PRAGMA wal_autocheckpoint") == [(1000,)]
    assert db.execute("PRAGMA wal_autocheckpoint = 5") == [(5,)]
    for i in range(10):
        db.execute("INSERT INTO t (a) VALUES ('auto')")
    # (Checkpoints at 5 frames; the next commit then starts the log over.)
    assert db.pager.wal.describe()["frames"] <= 6
    reader.close()
    db.close()
    assert files(path) == []


def test_switching_journal_modes(tmp_path):
    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        db.execute("CREATE TABLE t (a)")
        assert db.execute("PRAGMA journal_mode") == [("delete",)]
        db.execute("BEGIN")
        with pytest.raises(OperationalError, match="cannot change into wal mode from within a transaction"):
            db.execute("PRAGMA journal_mode = WAL")
        db.execute("COMMIT")
        assert db.execute("PRAGMA journal_mode = wal") == [("wal",)]
        assert db.execute("PRAGMA journal_mode = WAL") == [("wal",)]
        db.execute("INSERT INTO t VALUES (1)")
        other = Database(path)
        assert other.execute("SELECT * FROM t") == [(1,)]
        with pytest.raises(OperationalError, match="database is locked"):
            db.execute("PRAGMA journal_mode = DELETE")  # (the other connection has it open)
        other.close()
        db.execute("BEGIN")
        with pytest.raises(OperationalError, match="cannot change out of wal mode from within a transaction"):
            db.execute("PRAGMA journal_mode = DELETE")
        db.execute("ROLLBACK")
        assert db.execute("PRAGMA journal_mode = DELETE") == [("delete",)]
        assert files(path) == []
        db.execute("INSERT INTO t VALUES (2)")
        assert db.execute("PRAGMA journal_mode = memory") == [("delete",)]  # (not supported: unchanged)
    with closing(lite(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchall() == [("delete",)]
        assert connection.execute("SELECT * FROM t").fetchall() == [(1,), (2,)]
        assert connection.execute("PRAGMA journal_mode = WAL").fetchall() == [("wal",)]
        connection.execute("INSERT INTO t VALUES (3)")
    with Database(path) as db:
        assert db.execute("SELECT * FROM t") == [(1,), (2,), (3,)]
        db.execute("INSERT INTO t VALUES (4)")
        assert lite_child(path, "SELECT count(*) FROM t") == [[(4,)]]


def test_in_memory_copies(tmp_path):
    path = str(tmp_path / "db")
    wal_database(path, rows=50)
    with Database(path) as db:
        image = db.serialize()
        copy = Database()
        copy.deserialize(image)
        assert copy.execute("SELECT count(*) FROM t") == [(50,)]
        assert copy.execute("PRAGMA journal_mode") == [("memory",)]
        copy.execute("INSERT INTO t (a) VALUES ('copy')")
    # (As sqlite3_serialize: the header still says WAL, which sqlite3 cannot
    # open in memory - neither its own copy nor this one.)
    assert image[18:20] == b"\x02\x02"
    with closing(lite(path)) as connection:
        own = connection.serialize()
    for data in (own, image):
        other = sqlite3.connect(":memory:")
        other.deserialize(data)
        with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
            other.execute("SELECT count(*) FROM t")
        other.close()


# ---- crashes ------------------------------------------------------------------------


@pytest.mark.parametrize("point, detail, committed", [
    ("wal_header", None, False), ("wal_frames", None, False), ("wal_sync", None, True), ("wal_index", None, True),
    ("checkpoint_page", 2, True), ("checkpoint_sync", None, True),
])
@pytest.mark.parametrize("recovered_by", ["sqlite", "minidb"])
def test_crash_in_wal_mode(tmp_path, point, detail, committed, recovered_by):
    path = str(tmp_path / "db")
    wal_database(path)
    with closing(lite(path)) as connection:
        before = connection.execute("SELECT * FROM t ORDER BY id").fetchall()
        connection.execute("UPDATE t SET a = a || 'changed' WHERE id % 2 = 0")
        after = connection.execute("SELECT * FROM t ORDER BY id").fetchall()
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("UPDATE t SET a = substr(a, 1, length(a) - 7) WHERE id % 2 = 0")
    db = Database(path)
    db.execute("PRAGMA wal_autocheckpoint = 1")
    db.pager.crash_hook = crash_at(point, detail)
    with pytest.raises(SimulatedCrash):
        db.execute("UPDATE t SET a = a || 'changed' WHERE id % 2 = 0")
    db.pager.close_files()
    # Once the frames are written (here: the unsynced ones survive too) the
    # next connection to open the database finds them by recovery - no other
    # connection kept the index.  A crash while checkpointing loses nothing.
    expected = after if committed else before
    if recovered_by == "sqlite":
        with closing(lite(path)) as connection:
            assert connection.execute("SELECT * FROM t ORDER BY id").fetchall() == expected
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    with Database(path) as db:
        assert db.execute("SELECT * FROM t ORDER BY id") == expected
        assert db.integrity_check() == []
    with closing(lite(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_crash_while_another_connection_keeps_the_index(tmp_path):
    # The index survives: frames past its last commit do not count, and the
    # next writer removes their entries from the hash table.
    path = str(tmp_path / "db")
    wal_database(path)
    keeper = Database(path)
    before = keeper.execute("SELECT * FROM t ORDER BY id")
    db = Database(path)
    db.pager.crash_hook = crash_at("wal_index")
    with pytest.raises(SimulatedCrash):
        db.execute("UPDATE t SET a = 'lost' WHERE id < 100")
    db.pager.close_files()
    assert keeper.execute("SELECT * FROM t ORDER BY id") == before
    keeper.execute("UPDATE t SET a = 'kept' WHERE id = 1")
    assert keeper.execute("SELECT count(*) FROM t WHERE a = 'lost'") == [(0,)]
    assert lite_child(path, "SELECT count(*) FROM t WHERE a IN ('lost', 'kept')", "PRAGMA integrity_check") == [
        [(1,)], [("ok",)]]
    keeper.close()


NEWER_SETUP = """
    PRAGMA page_size = 1024;
    PRAGMA auto_vacuum = FULL;
    CREATE TABLE w (k TEXT, n INT, body, g AS (length(body) + n) STORED, v AS (k || n),
                    PRIMARY KEY (k, n DESC)) WITHOUT ROWID;
    CREATE INDEX wv ON w (v);
    CREATE TABLE r (id INTEGER PRIMARY KEY, x REAL, xg AS (x) VIRTUAL);
    CREATE INDEX rx ON r (xg);
"""


@pytest.mark.parametrize("wal, point", [
    (False, "db_page"), (False, "journal_sync"), (True, "wal_frames"), (True, "wal_index"), (True, "checkpoint_page"),
])
def test_crash_with_the_newer_table_kinds(tmp_path, wal, point):
    path = str(tmp_path / "db")
    with closing(lite(path)) as connection:
        connection.executescript(NEWER_SETUP)
        if wal:
            connection.execute("PRAGMA journal_mode = WAL")
        connection.executemany("INSERT INTO w (k, n, body) VALUES (?, ?, ?)",
                               [(f"k{i % 37}", i, "b" * (i % 400)) for i in range(600)])
        connection.executemany("INSERT INTO r (x) VALUES (?)", [(i / 2,) for i in range(300)])
        before = [connection.execute(f"SELECT * FROM {t}").fetchall() for t in ("w", "r")]
    db = Database(path)
    db.execute("CREATE TEMP TABLE scratch (a)")  # (the temp database commits with it)
    db.execute("PRAGMA wal_autocheckpoint = 1")
    db.pager.crash_hook = crash_at(point, 1 if point in ("db_page", "checkpoint_page") else None)
    with pytest.raises(SimulatedCrash):
        db.execute("BEGIN")
        db.execute("INSERT INTO scratch VALUES (1)")
        db.execute("DELETE FROM w WHERE n % 3 = 0")
        db.execute("UPDATE r SET x = x + 1 WHERE id % 2 = 0")
        db.execute("COMMIT")
    db.pager.close_files()
    with closing(lite(path)) as connection:
        after = [connection.execute(f"SELECT * FROM {t}").fetchall() for t in ("w", "r")]
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    if point in ("wal_index", "checkpoint_page"):  # (the frames were written: recovery keeps them)
        assert len(after[0]) == 400 and after[1] != before[1]
    else:
        assert after == before
    with Database(path) as db:
        assert [db.execute(f"SELECT * FROM {t}") for t in ("w", "r")] == after
        assert db.integrity_check() == []


def test_a_torn_frame_ends_the_log(tmp_path):
    path = str(tmp_path / "db")
    wal_database(path, rows=40)
    with open(path + "-wal", "ab") as f:
        f.write(os.urandom(4096 + 24))  # (a frame with a wrong checksum)
    with Database(path) as db:
        assert db.execute("SELECT count(*) FROM t") == [(40,)]
        db.execute("INSERT INTO t (a) VALUES ('after')")
    with closing(lite(path)) as connection:
        assert connection.execute("SELECT count(*) FROM t").fetchall() == [(41,)]
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


KILLED = textwrap.dedent("""
    import os, sqlite3, sys
    sys.path.insert(0, {root!r})
    from minidb.database import Database
    kind, path = sys.argv[1], sys.argv[2]
    if kind == "lite":
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        run = connection.execute
    else:
        db = Database(path)
        db.execute("PRAGMA wal_autocheckpoint = 0")
        run = db.execute
    run("INSERT INTO t (a) VALUES ('committed')")
    run("BEGIN")
    run("INSERT INTO t (a) VALUES ('uncommitted')")
    os._exit(0)  # (no rollback, no checkpoint: the log and the index stay)
""")


@pytest.mark.parametrize("killed, recovered_by", [("lite", "minidb"), ("mini", "sqlite"), ("mini", "minidb")])
def test_a_killed_process(tmp_path, killed, recovered_by):
    path = str(tmp_path / "db")
    wal_database(path, rows=20)
    script = tmp_path / "killed.py"
    script.write_text(KILLED.format(root=ROOT))
    subprocess.run([sys.executable, str(script), killed, path], check=True, timeout=60)
    assert files(path) == ["-wal", "-shm"]
    if recovered_by == "sqlite":
        with closing(lite(path)) as connection:
            assert connection.execute("SELECT a FROM t WHERE id > 20").fetchall() == [("committed",)]
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    else:
        with Database(path) as db:
            assert db.execute("SELECT a FROM t WHERE id > 20") == [("committed",)]
            assert db.integrity_check() == []


# ---- processes at the same time ------------------------------------------------------


WORKER = textwrap.dedent("""
    import random, sqlite3, sys, time
    sys.path.insert(0, {root!r})
    from minidb.database import Database
    kind, path, rounds, name = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    random.seed(name)
    if kind == "lite":
        connection = sqlite3.connect(path, isolation_level=None, timeout=60)
        run = lambda sql, p=(): connection.execute(sql, p).fetchall()
    else:
        db = Database(path, timeout=60)
        run = lambda sql, p=(): db.execute(sql, p)
    done = 0
    while done < rounds:
        try:
            run("BEGIN IMMEDIATE")
            (v,), = run("SELECT v FROM counter")
            run("UPDATE counter SET v = ?", (v + 1,))
            run("INSERT INTO log (who, n, data) VALUES (?, ?, ?)", (name, v, bytes(random.randint(0, 3000))))
            run("INSERT INTO kv (k, v) VALUES (?, 1) ON CONFLICT (k) DO UPDATE SET v = v + 1", (name,))
            run("COMMIT")
            done += 1
        except Exception as exc:
            if "locked" not in str(exc):
                raise
            try:
                run("ROLLBACK")
            except Exception:
                pass
        if random.random() < 0.25:  # a snapshot stays the same while others commit
            run("BEGIN")
            (count,), = run("SELECT count(*) FROM log")
            time.sleep(random.random() * 0.003)
            (v,), = run("SELECT v FROM counter")
            run("COMMIT")
            assert count == v, (count, v)
    if kind == "mini":
        db.close()
""")


def test_processes_of_both_at_once(tmp_path):
    path = str(tmp_path / "db")
    with closing(lite(path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript("""
            CREATE TABLE counter (v); INSERT INTO counter VALUES (0);
            CREATE TABLE log (id INTEGER PRIMARY KEY, who, n UNIQUE, data);
            CREATE TABLE kv (k TEXT PRIMARY KEY, v INT, twice AS (v * 2), UNIQUE (twice, k)) WITHOUT ROWID;
        """)
    script = tmp_path / "worker.py"
    script.write_text(WORKER.format(root=ROOT))
    kinds, rounds = ["lite", "mini", "mini", "lite", "mini"], 150
    procs = [subprocess.Popen([sys.executable, str(script), kind, path, str(rounds), f"{kind}{i}"])
             for i, kind in enumerate(kinds)]
    assert [p.wait(timeout=300) for p in procs] == [0] * len(kinds)
    with closing(lite(path)) as connection:
        assert connection.execute("SELECT v FROM counter").fetchall() == [(rounds * len(kinds),)]
        assert connection.execute("SELECT count(*), count(DISTINCT n) FROM log").fetchall() == [
            (rounds * len(kinds),) * 2]
        assert connection.execute("SELECT k, v, twice FROM kv ORDER BY k").fetchall() == [
            (f"{kind}{i}", rounds, 2 * rounds) for i, kind in sorted(enumerate(kinds), key=lambda p: f"{p[1]}{p[0]}")]
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    with Database(path) as db:
        assert db.integrity_check() == []
    assert files(path) == []
