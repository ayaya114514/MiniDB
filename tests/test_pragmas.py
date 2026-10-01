"""PRAGMA statements and pragma_xxx() table-valued functions, compared with SQLite."""

import os
import sqlite3
from contextlib import closing

import pytest

from minidb.database import Database
from sqlcompare import Pair

SCHEMA = """
    CREATE TABLE p (id INTEGER PRIMARY KEY, k TEXT UNIQUE COLLATE nocase, x);
    CREATE TABLE t (a int NOT NULL DEFAULT 5, "b c" varchar(10) DEFAULT 'x' REFERENCES p (k) ON DELETE CASCADE
        ON UPDATE SET NULL, c DEFAULT ( 2*3 ) CHECK (c > 0), d integer DEFAULT -1.5 COLLATE rtrim,
        e DEFAULT CURRENT_TIMESTAMP, f DEFAULT x'ab', g DEFAULT "ident", h Text,
        PRIMARY KEY ("b c", a DESC), UNIQUE (c COLLATE nocase, d) ON CONFLICT REPLACE,
        FOREIGN KEY (c, d) REFERENCES p (id, x) DEFERRABLE INITIALLY DEFERRED);
    CREATE INDEX ti ON t (e DESC, c COLLATE nocase);
    CREATE VIEW v AS SELECT a, "b c" + 1 AS z, k, h FROM t, p;
    INSERT INTO p VALUES (1, 'x', 1);
    INSERT INTO t (a, "b c", c) VALUES (1, 'x', 3), (2, 'zz', 4)
    """


@pytest.fixture(params=[None, "sqlite"])
def pair(request, tmp_path):
    path = str(tmp_path / "db") if request.param else None
    pair = Pair(path, check_messages=True, format=request.param)
    pair.format = request.param
    for sql in SCHEMA.strip().split(";\n"):
        pair.run(sql)
    return pair


def same(pair, sql, ordered=True):
    """Like Pair.run, also comparing the column names."""
    if not pair.run(sql, ordered=ordered):
        return  # (both failed, or no rows: possibly a statement that changes something)
    lite = pair.lite.execute(sql)
    names = [d[0] for d in lite.description] if lite.description else []
    assert pair.mini.execute(sql).columns == names, sql


@pytest.mark.parametrize("sql", [
    "PRAGMA table_info(t)", "PRAGMA table_info('T')", "PRAGMA TABLE_INFO(p)", "PRAGMA table_xinfo(t)",
    "PRAGMA table_info(v)", "PRAGMA table_info(nosuch)", "PRAGMA table_info", "PRAGMA table_info(sqlite_master)",
    "PRAGMA index_list(t)", "PRAGMA index_list(p)", "PRAGMA index_info(ti)", "PRAGMA index_xinfo(ti)",
    "PRAGMA index_info(sqlite_autoindex_t_1)", "PRAGMA index_xinfo(sqlite_autoindex_t_2)",
    "PRAGMA index_info(p)", "PRAGMA foreign_key_list(t)", "PRAGMA foreign_key_list(p)",
    "PRAGMA encoding", "PRAGMA collation_list", "PRAGMA cache_size", "PRAGMA synchronous", "PRAGMA temp_store",
    "PRAGMA auto_vacuum", "PRAGMA locking_mode", "PRAGMA nosuch", "PRAGMA nosuch = 3", "PRAGMA page_size",
    "PRAGMA foreign_keys", "PRAGMA defer_foreign_keys", "PRAGMA ignore_check_constraints",
    "PRAGMA recursive_triggers", "PRAGMA main.user_version", "PRAGMA temp.user_version", "PRAGMA xyz.user_version",
    "PRAGMA integrity_check", "PRAGMA quick_check", "PRAGMA integrity_check(1)", "PRAGMA integrity_check(t)",
    "PRAGMA table_list", "PRAGMA table_list(t)",
    "SELECT * FROM pragma_table_info('t') WHERE pk", "SELECT name, hidden FROM pragma_table_xinfo('t')",
    "SELECT * FROM pragma_user_version", "SELECT * FROM pragma_index_info('ti')",
    "SELECT arg, schema FROM pragma_table_info('t')", "SELECT * FROM pragma_table_info",
    "SELECT * FROM pragma_table_info(NULL)", "SELECT * FROM pragma_table_info('T') t1",
    "SELECT * FROM pragma_table_info('t', 'main')", "SELECT * FROM pragma_table_info('t', 'xyz')",
    "SELECT * FROM pragma_foreign_keys", "SELECT * FROM pragma_index_list('t') ORDER BY seq DESC",
    "SELECT * FROM pragma_foreign_key_list('t') WHERE seq = 1",
    "SELECT m.name, p.name FROM sqlite_master m JOIN pragma_table_info(m.name) p WHERE m.type = 'table'",
    "SELECT m.name, p.name FROM sqlite_master m, pragma_index_list(m.name) p",
    "SELECT m.name, p.name FROM sqlite_master m LEFT JOIN pragma_index_list(m.name) p",
    "SELECT m.name, (SELECT count(*) FROM pragma_table_info(m.name)) FROM sqlite_master m WHERE type != 'index'",
    "SELECT name FROM pragma_table_info('p') UNION SELECT name FROM pragma_index_list('t')",
])
def test_pragma_reports(pair, sql):
    if pair.format is None and ("index_list" in sql or "autoindex" in sql):
        pytest.skip("MiniDB's own format names automatic indexes minidb_autoindex_...")
    same(pair, sql)


def test_pragma_values(pair):
    for sql in ["PRAGMA user_version", "PRAGMA user_version = 7", "PRAGMA user_version",
                "PRAGMA user_version = '12abc'", "PRAGMA user_version", "PRAGMA user_version = -3.9",
                "PRAGMA user_version", "PRAGMA user_version(9)", "PRAGMA user_version",
                "PRAGMA user_version = 99999999999", "PRAGMA user_version", "PRAGMA application_id = 0x7f",
                "PRAGMA application_id", "PRAGMA application_id = 2147483648", "SELECT * FROM pragma_application_id",
                "PRAGMA foreign_keys = 1", "PRAGMA foreign_keys", "PRAGMA foreign_keys = 'off'", "PRAGMA foreign_keys",
                "PRAGMA foreign_keys = yes", "PRAGMA foreign_keys", "PRAGMA foreign_keys = abc", "PRAGMA foreign_keys",
                "PRAGMA foreign_keys = 2", "PRAGMA foreign_keys", "BEGIN", "PRAGMA foreign_keys = 0",
                "PRAGMA foreign_keys", "COMMIT", "PRAGMA foreign_keys = 0", "PRAGMA cache_size = 100", "PRAGMA cache_size",
                "PRAGMA ignore_check_constraints = on", "INSERT INTO t (a, \"b c\", c) VALUES (5, 'y', -1)",
                "PRAGMA integrity_check", "PRAGMA quick_check", "PRAGMA ignore_check_constraints = off",
                "PRAGMA integrity_check", "UPDATE t SET a = NULL WHERE a = 5"]:
        same(pair, sql)


def test_pragma_on_sqlite_files(tmp_path):
    """Values stored in the header, read and written by both."""
    path = str(tmp_path / "db")
    with closing(sqlite3.connect(path)) as lite:
        lite.executescript("CREATE TABLE t (a); PRAGMA user_version = 41; PRAGMA application_id = 1234; "
                           "CREATE TABLE u (b); DROP TABLE u;")
        version = lite.execute("PRAGMA schema_version").fetchone()[0]
    with Database(path) as db:
        assert db.execute("PRAGMA user_version") == [(41,)]
        assert db.execute("PRAGMA application_id") == [(1234,)]
        assert db.execute("PRAGMA schema_version") == [(version,)]
        assert db.execute("PRAGMA journal_mode") == [("delete",)]
        assert db.execute("PRAGMA page_count") == [(3,)] and db.execute("PRAGMA freelist_count") == [(1,)]
        assert db.execute("PRAGMA database_list") == [(0, "main", os.path.abspath(path))]
        db.execute("PRAGMA user_version = 42")
        db.execute("CREATE TABLE v (c)")
        assert db.execute("PRAGMA schema_version") == [(version + 1,)]
    with closing(sqlite3.connect(path)) as lite:
        assert lite.execute("PRAGMA user_version").fetchone() == (42,)
        assert lite.execute("PRAGMA schema_version").fetchone() == (version + 1,)
        assert lite.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_pragma_on_minidb_files(tmp_path):
    path = str(tmp_path / "db")
    with Database(path) as db:
        assert db.execute("PRAGMA user_version") == [(0,)]
        db.execute("PRAGMA user_version = 5")
        db.execute("BEGIN")
        db.execute("PRAGMA application_id = 6")
        db.execute("ROLLBACK")
        db.execute("CREATE TABLE t (a)")
        assert db.execute("PRAGMA schema_version") == [(1,)]
        assert db.execute("PRAGMA journal_mode") == [("wal",)]
    with Database(path) as db:
        assert db.execute("PRAGMA user_version") == [(5,)]
        assert db.execute("PRAGMA application_id") == [(0,)]
        assert db.execute("PRAGMA schema_version") == [(1,)]


def test_data_version_counts_other_connections(tmp_path):
    path = str(tmp_path / "db")
    with Database(path) as first, Database(path) as second:
        before = first.execute("PRAGMA data_version")[0][0]
        first.execute("CREATE TABLE t (a)")
        assert first.execute("PRAGMA data_version") == [(before,)]
        second.execute("INSERT INTO t VALUES (1)")
        assert first.execute("PRAGMA data_version")[0][0] > before
