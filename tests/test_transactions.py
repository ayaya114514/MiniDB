"""Transactions (BEGIN/COMMIT/ROLLBACK), the write-ahead log and crash recovery."""

import os
import random
import subprocess
import sys
import textwrap

import pytest

from minidb.database import Database
from minidb.errors import DatabaseError, IntegrityError, OperationalError
from minidb.pager import PAGE_SIZE, Pager
from sqlcompare import Pair


class SimulatedCrash(BaseException):
    """Raised by a crash hook; stands for the process dying at that point."""


def crash_at(point, detail=None):
    def hook(where, index):
        if where == point and (detail is None or index == detail):
            raise SimulatedCrash(f"{point} {index}")
    return hook


def snapshot(db):
    return {
        table.name: db.execute(f'SELECT rowid, * FROM "{table.name}" ORDER BY rowid')
        for table in db.catalog.tables.values()
    }


# ---- transaction semantics ------------------------------------------------------


def test_transaction_statements_match_sqlite():
    pair = Pair(check_messages=True)
    pair.script([
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE)",
        "COMMIT",
        "ROLLBACK",
        "BEGIN",
        "BEGIN",
        "INSERT INTO t VALUES (1, 'a')",
        "SELECT * FROM t",
        "ROLLBACK",
        "SELECT * FROM t",
        "BEGIN TRANSACTION",
        "INSERT INTO t VALUES (1, 'a'), (2, 'b')",
        "INSERT INTO t VALUES (3, 'a')",
        "INSERT INTO t VALUES (3, 'c')",
        "COMMIT TRANSACTION",
        "SELECT * FROM t",
        "BEGIN",
        "UPDATE t SET v = v || '!'",
        "DELETE FROM t WHERE id = 1",
        "CREATE TABLE u (x INTEGER)",
        "INSERT INTO u VALUES (1)",
        "CREATE INDEX t_v ON t (v)",
        "ROLLBACK",
        "SELECT * FROM t",
        "SELECT * FROM u",
        "SELECT * FROM t WHERE v = 'b'",
        "BEGIN DEFERRED",
        "DROP TABLE t",
        "END",
        "SELECT * FROM t",
    ])
    pair.close()


def test_random_transactions_match_sqlite(tmp_path):
    rng = random.Random(77)
    pair = Pair(path=str(tmp_path / "db"))
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT UNIQUE)")
    pair.run("CREATE INDEX t_a ON t (a)")
    in_transaction = False
    for _ in range(3000):
        choice = rng.random()
        if choice < 0.06:
            pair.run("BEGIN")
            in_transaction = True
        elif choice < 0.10:
            pair.run(rng.choice(["COMMIT", "ROLLBACK"]))
            in_transaction = False
        elif choice < 0.6:
            pair.run(f"INSERT INTO t (a, b) VALUES ({rng.randint(0, 50)}, 'b{rng.randint(0, 3000)}')")
        elif choice < 0.75:
            pair.run(f"UPDATE t SET a = a + 1, b = b || 'x' WHERE a = {rng.randint(0, 50)}")
        elif choice < 0.85:
            pair.run(f"DELETE FROM t WHERE a > {rng.randint(0, 50)} AND id % 3 = 0")
        else:
            pair.run(f"SELECT * FROM t WHERE a = {rng.randint(0, 50)}")
    if in_transaction:
        pair.run("COMMIT")
    pair.run("SELECT * FROM t")
    assert pair.mini.integrity_check() == []
    pair.mini.close()
    reopened = Database(str(tmp_path / "db"))
    assert reopened.execute("SELECT * FROM t") == pair.lite.execute("SELECT * FROM t").fetchall()
    reopened.close()


def test_failed_statement_inside_transaction_keeps_earlier_work():
    db = Database()
    db.execute("CREATE TABLE t (a INTEGER UNIQUE)")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(IntegrityError):
        db.execute("INSERT INTO t VALUES (2), (1)")
    db.execute("INSERT INTO t VALUES (3)")
    db.execute("COMMIT")
    assert db.execute("SELECT a FROM t") == [(1,), (3,)]


def test_large_rollback_restores_everything(tmp_path):
    path = str(tmp_path / "db")
    db = Database(path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("CREATE INDEX t_v ON t (v)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, 'v{i}')" for i in range(2000)))
    before = snapshot(db)
    pages, free = db.pager.page_count, db.pager.free_page_count()
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, '{'w' * 300}{i}')" for i in range(2000, 4000)))
    db.execute("DELETE FROM t WHERE id % 2 = 0")
    db.execute("CREATE TABLE extra (x TEXT)")
    db.execute(f"INSERT INTO extra VALUES ('{'z' * 20000}')")
    db.execute("DROP INDEX t_v")
    assert db.pager.page_count > pages
    db.execute("ROLLBACK")
    assert snapshot(db) == before
    assert (db.pager.page_count, db.pager.free_page_count()) == (pages, free)
    assert db.integrity_check() == []
    db.close()
    db = Database(path)
    assert snapshot(db) == before
    db.close()


def test_close_rolls_back_open_transaction(tmp_path):
    path = str(tmp_path / "db")
    db = Database(path)
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (1)")
    db.close()
    db = Database(path)
    assert db.execute("SELECT * FROM t") == []
    db.close()


def test_uncommitted_changes_never_reach_the_file(tmp_path):
    path = str(tmp_path / "db")
    db = Database(path)
    db.execute("CREATE TABLE t (a TEXT)")
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        content = f.read()
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"('{i}')" for i in range(3000)))
    with open(path, "rb") as f:
        assert f.read() == content
    assert os.path.getsize(path) == size
    assert not os.path.exists(path + "-wal")
    db.execute("COMMIT")
    assert os.path.getsize(path) > size
    assert not os.path.exists(path + "-wal")
    db.close()


# ---- crash recovery -----------------------------------------------------------------

CRASH_POINTS = [
    # (point, detail, transaction survives?)
    ("wal_frame", 0, False),
    ("wal_frame", 5, False),
    ("wal_commit", None, False),
    ("wal_sync", None, True),  # the commit record was written (a process crash keeps it)
    ("db_page", 0, True),
    ("db_page", 3, True),
    ("db_page", 20, True),
    ("db_sync", None, True),
    ("wal_delete", None, True),
]


def build_database(path):
    db = Database(path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT UNIQUE)")
    db.execute("CREATE INDEX t_a ON t (a)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, {i % 13}, 'b{i}')" for i in range(1500)))
    return db


def big_transaction(db):
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES " + ", ".join(
        f"({i}, {i % 7}, '{'long' * 40}{i}')" for i in range(1500, 2500)))
    db.execute("UPDATE t SET a = a + 100 WHERE id % 5 = 0")
    db.execute("DELETE FROM t WHERE id % 7 = 3")
    db.execute("CREATE TABLE side (x INTEGER)")
    db.execute("INSERT INTO side VALUES (42)")


@pytest.mark.parametrize("point, detail, survives", CRASH_POINTS)
def test_crash_during_commit(tmp_path, point, detail, survives):
    path = str(tmp_path / "db")
    db = build_database(path)
    before = snapshot(db)
    big_transaction(db)
    after = snapshot(db)
    assert len(db.pager.dirty) > 21  # enough pages for every crash point above
    db.pager.crash_hook = crash_at(point, detail)
    with pytest.raises(SimulatedCrash):
        db.execute("COMMIT")
    with pytest.raises(DatabaseError):
        db.execute("SELECT 1")  # the crashed connection cannot be used any more

    db = Database(path)
    assert snapshot(db) == (after if survives else before)
    assert db.integrity_check() == []
    assert not os.path.exists(path + "-wal")
    db.execute("INSERT INTO t (a, b) VALUES (1, 'after recovery')")
    db.close()


@pytest.mark.parametrize("point, detail, survives", CRASH_POINTS)
def test_crash_during_autocommit_statement(tmp_path, point, detail, survives):
    path = str(tmp_path / "db")
    db = build_database(path)
    before = snapshot(db)
    db.pager.crash_hook = crash_at(point, detail)
    statement = "UPDATE t SET b = b || '-changed' WHERE id < 1200"
    with pytest.raises(SimulatedCrash):
        db.execute(statement)
    db = Database(path)
    expected = before
    if survives:
        expected = {"t": [
            (r[0], r[1], r[2], r[3] + "-changed" if r[0] < 1200 else r[3]) for r in before["t"]
        ]}
    assert snapshot(db) == expected
    assert db.integrity_check() == []
    db.close()


def test_torn_wal_is_discarded(tmp_path):
    """Power loss before the WAL was synced may cut it anywhere: recovery must
    then ignore it and keep the old state."""
    path = str(tmp_path / "db")
    db = build_database(path)
    before = snapshot(db)
    big_transaction(db)
    db.pager.crash_hook = crash_at("db_page", 0)  # WAL written in full, database untouched
    with pytest.raises(SimulatedCrash):
        db.execute("COMMIT")
    wal = path + "-wal"
    with open(wal, "rb") as f:
        complete = f.read()
    for cut in [0, 10, 25, PAGE_SIZE, len(complete) // 2, len(complete) - 1]:
        with open(wal, "wb") as f:
            f.write(complete[:cut])
        db = Database(path)
        assert snapshot(db) == before
        db.close()
        assert not os.path.exists(wal)
    corrupted = bytearray(complete)
    corrupted[len(complete) // 3] ^= 0xFF
    with open(wal, "wb") as f:
        f.write(corrupted)
    db = Database(path)
    assert snapshot(db) == before
    db.close()
    # The intact WAL, on the other hand, is replayed.
    with open(wal, "wb") as f:
        f.write(complete)
    db = Database(path)
    assert snapshot(db) != before
    assert db.integrity_check() == []
    db.close()


def test_recovery_is_idempotent(tmp_path):
    """A crash during recovery leaves the WAL in place, so replaying again works."""
    path = str(tmp_path / "db")
    db = build_database(path)
    big_transaction(db)
    after = snapshot(db)
    db.pager.crash_hook = crash_at("db_page", 10)
    with pytest.raises(SimulatedCrash):
        db.execute("COMMIT")
    with open(path + "-wal", "rb") as f:
        wal = f.read()
    for _ in range(3):
        db = Database(path)  # replays and deletes the WAL
        assert snapshot(db) == after
        db.close()
        with open(path + "-wal", "wb") as f:
            f.write(wal)  # as if the previous recovery had crashed before deleting it
    os.remove(path + "-wal")


def test_crash_on_first_commit_of_new_database(tmp_path):
    path = str(tmp_path / "db")
    pager = Pager(path)
    pager.crash_hook = crash_at("db_page", 0)
    with pytest.raises(SimulatedCrash):
        pager.commit()
    pager.file.close()
    assert os.path.getsize(path) == 0
    db = Database(path)  # recovery writes the header page from the WAL
    db.execute("CREATE TABLE t (a INTEGER)")
    db.close()


CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {root!r})
    from minidb.database import Database

    path, point, detail = sys.argv[1], sys.argv[2], sys.argv[3]
    detail = None if detail == "-" else int(detail)
    db = Database(path)
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({{i}}, 'new{{i}}')" for i in range(1000, 1600)))
    db.execute("DELETE FROM t WHERE id % 4 = 0")

    def hook(where, index):
        if where == point and (detail is None or index == detail):
            os._exit(17)  # die immediately: no cleanup, no flushing

    db.pager.crash_hook = hook
    db.execute("COMMIT")
    os._exit(0)
""")


@pytest.mark.parametrize("point, detail, survives", [
    ("wal_frame", 2, False),
    ("wal_commit", None, False),
    ("db_page", 0, True),
    ("db_page", 7, True),
    ("wal_delete", None, True),
    ("never", None, True),
])
def test_real_process_crash(tmp_path, point, detail, survives):
    path = str(tmp_path / "db")
    db = Database(path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("CREATE INDEX t_v ON t (v)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, 'old{i}')" for i in range(1000)))
    before = snapshot(db)
    db.close()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(root=root))
    result = subprocess.run(
        [sys.executable, str(script), path, point, "-" if detail is None else str(detail)]
    )
    assert result.returncode == (0 if point == "never" else 17)
    db = Database(path)
    rows = snapshot(db)["t"]
    if survives:
        assert len(rows) == 1600 - 400
        assert all(r[0] % 4 != 0 for r in rows)
    else:
        assert snapshot(db) == before
    assert db.integrity_check() == []
    db.close()
