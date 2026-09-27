"""Prepared plans: reused across executions, never stale."""

import pytest

import minidb.executor as executor_module
from minidb.database import Database
from minidb.errors import OperationalError


@pytest.fixture
def compiles(monkeypatch):
    counts = {"select": 0}
    original = executor_module.CompiledSelect.__init__

    def counting(self, *args, **kwargs):
        counts["select"] += 1
        original(self, *args, **kwargs)

    monkeypatch.setattr(executor_module.CompiledSelect, "__init__", counting)
    return counts


def test_plans_are_reused(compiles):
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    compiles["select"] = 0
    for i in (1, 2, 3, 1):
        assert db.execute("SELECT v FROM t WHERE id = ?", (i,)) == [("abc"[i - 1],)]
    assert compiles["select"] == 1


def test_schema_change_invalidates_plans(compiles):
    db = Database()
    db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'x')")
    sql = "SELECT * FROM t WHERE a = ?"
    assert db.execute(sql, (1,)) == [(1, "x")]
    db.execute("CREATE INDEX t_a ON t (a)")
    assert db.execute(sql, (1,)) == [(1, "x")]
    assert compiles["select"] == 2  # recompiled: the new plan can use the index
    db.execute("DROP TABLE t")
    db.execute("CREATE TABLE t (b TEXT, a INTEGER, c INTEGER)")
    db.execute("INSERT INTO t VALUES ('y', 1, 9)")
    assert db.execute(sql, (1,)) == [("y", 1, 9)]
    db.execute("DROP TABLE t")
    with pytest.raises(OperationalError, match="no such table"):
        db.execute(sql, (1,))


def test_rollback_of_ddl_invalidates_plans():
    db = Database()
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("BEGIN")
    db.execute("DROP TABLE t")
    db.execute("CREATE TABLE t (z TEXT)")
    db.execute("INSERT INTO t VALUES ('new')")
    assert db.execute("SELECT * FROM t") == [("new",)]
    db.execute("ROLLBACK")
    assert db.execute("SELECT * FROM t") == [(1,)]


def test_schema_change_by_another_connection(tmp_path):
    path = str(tmp_path / "db")
    a, b = Database(path), Database(path)
    a.execute("CREATE TABLE t (x INTEGER)")
    a.execute("INSERT INTO t VALUES (1)")
    assert b.execute("SELECT * FROM t") == [(1,)]
    a.execute("DROP TABLE t")
    a.execute("CREATE TABLE t (y TEXT, x INTEGER)")
    a.execute("INSERT INTO t VALUES ('two', 2)")
    assert b.execute("SELECT * FROM t") == [("two", 2)]
    a.close()
    b.close()


def test_uncorrelated_subquery_is_recomputed_per_execution():
    db = Database()
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    sql = "SELECT (SELECT max(a) FROM t), (SELECT count(*) FROM t WHERE a > ?)"
    assert db.execute(sql, (0,)) == [(1, 1)]
    db.execute("INSERT INTO t VALUES (5)")
    assert db.execute(sql, (0,)) == [(5, 2)]
    assert db.execute(sql, (3,)) == [(5, 1)]


def test_prepared_dml_with_parameters():
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")
    for i in range(50):
        db.execute("INSERT INTO t (n) VALUES (?)", (i,))
    for i in range(0, 50, 5):
        db.execute("UPDATE t SET n = n + ? WHERE id = ?", (100, i + 1))
    for i in range(1, 50, 5):
        db.execute("DELETE FROM t WHERE n = ?", (i,))
    assert db.execute("SELECT count(*), sum(n) FROM t") == [(40, sum(range(50)) + 1000 - sum(range(1, 50, 5)))]
    assert db.integrity_check() == []
