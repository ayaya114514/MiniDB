"""VACUUM: rebuild the database compactly (and VACUUM INTO a new file)."""

import os

import pytest

from minidb.database import Database
from minidb.errors import OperationalError
from minidb.pager import PAGE_SIZE
from sqlcompare import Pair
from test_transactions import SimulatedCrash, crash_at


def contents(db):
    return {
        name: sorted(db.execute(f"SELECT rowid, * FROM {name}"))
        for name in ("t", "u")
    }


def fill(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b)")
    db.execute("CREATE INDEX t_a ON t (a)")
    db.execute("CREATE TABLE u (x UNIQUE, y)")
    db.execute("CREATE VIEW v AS SELECT a, count(*) AS n FROM t GROUP BY a")
    db.execute("BEGIN")
    for i in range(3000):
        # every 100th row has a value that needs overflow pages
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, f"text{i % 37}", "x" * (6000 if i % 100 == 0 else 20)))
        db.execute("INSERT INTO u VALUES (?, ?)", (i, i * 2))
    db.execute("COMMIT")
    db.execute("ANALYZE")
    db.execute("DELETE FROM t WHERE id % 4 != 0")
    db.execute("DELETE FROM u WHERE x > 200")


def test_vacuum_keeps_everything_and_shrinks_the_file(tmp_path):
    path = str(tmp_path / "db")
    db = Database(path)
    fill(db)
    before = contents(db)
    views = db.execute("SELECT * FROM v ORDER BY a")
    db.pager.checkpoint()
    size = os.path.getsize(path)
    assert db.pager.free_page_count() > 0
    db.execute("VACUUM")
    assert os.path.getsize(path) < size * 0.7
    assert os.path.getsize(path) == db.pager.page_count * PAGE_SIZE
    assert db.pager.free_page_count() == 0
    assert contents(db) == before
    assert db.execute("SELECT * FROM v ORDER BY a") == views
    assert db.integrity_check() == []
    # indexes and statistics survive, and so does writing afterwards
    assert db.execute("EXPLAIN SELECT * FROM t WHERE a = 'text3'") == [("t", "SEARCH USING INDEX t_a (a=?)")]
    with pytest.raises(Exception, match="UNIQUE"):
        db.execute("INSERT INTO u VALUES (5, 0)")
    db.execute("INSERT INTO t (a) VALUES ('new')")
    db.close()
    db = Database(path)
    assert db.execute("SELECT count(*) FROM t WHERE a = 'new'") == [(1,)]
    assert db.integrity_check() == []
    db.close()


def test_vacuum_in_memory_and_errors():
    db = Database()
    fill(db)
    before = contents(db)
    db.execute("VACUUM main")
    db.execute("VACUUM temp")
    assert contents(db) == before
    assert db.integrity_check() == []
    db.execute("BEGIN")
    with pytest.raises(OperationalError, match="cannot VACUUM from within a transaction"):
        db.execute("VACUUM")
    db.execute("ROLLBACK")
    with pytest.raises(OperationalError, match="unknown database other"):
        db.execute("VACUUM other")


def test_vacuum_into_writes_a_compact_copy(tmp_path):
    db = Database(str(tmp_path / "db"))
    fill(db)
    target = str(tmp_path / "copy")
    db.execute("VACUUM INTO ?", [target])
    with pytest.raises(OperationalError, match="output file already exists"):
        db.execute(f"VACUUM INTO '{target}'")
    copy = Database(target)
    assert contents(copy) == contents(db)
    assert copy.integrity_check() == []
    assert os.path.getsize(target) == copy.pager.page_count * PAGE_SIZE
    copy.close()
    db.close()


def test_vacuum_while_another_connection_reads(tmp_path):
    path = str(tmp_path / "db")
    db = Database(path)
    fill(db)
    before = contents(db)
    reader = Database(path)
    reader.execute("BEGIN")
    assert contents(reader) == before
    db.execute("VACUUM")  # commits; the file cannot shrink while the reader reads
    assert contents(reader) == before  # the reader keeps its snapshot
    reader.execute("COMMIT")
    assert contents(reader) == before  # and then sees the rebuilt database
    assert reader.integrity_check() == []
    reader.close()
    db.pager.checkpoint()  # (the reader's close may already have done it)
    assert os.path.getsize(path) == db.pager.page_count * PAGE_SIZE
    db.close()


@pytest.mark.parametrize("point, detail", [
    ("wal_frame", 3), ("wal_commit", None), ("wal_sync", None),
    ("checkpoint_page", 2), ("checkpoint_sync", None), ("wal_reset", None),
])
def test_crash_during_vacuum(tmp_path, point, detail):
    path = str(tmp_path / "db")
    db = Database(path)
    fill(db)
    before = contents(db)
    db.pager.crash_hook = crash_at(point, detail)
    with pytest.raises(SimulatedCrash):
        db.execute("VACUUM")
    db.pager.close_files()  # the process dies
    db = Database(path)
    assert contents(db) == before
    assert db.integrity_check() == []
    db.close()


def test_vacuum_matches_sqlite():
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t (a, b UNIQUE)", "INSERT INTO t VALUES (1, 2), (3, 4), (5, 6)",
        "DELETE FROM t WHERE a = 3", "VACUUM", "SELECT rowid, * FROM t", "VACUUM main",
        "BEGIN", "VACUUM", "ROLLBACK", "VACUUM nosuch", "SELECT rowid, * FROM t",
    ]:
        pair.run(sql)
    pair.close()

