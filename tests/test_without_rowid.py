"""WITHOUT ROWID tables, compared with SQLite in both file formats, and
SQLite files with such tables that sqlite3 and MiniDB write in turn."""

import sqlite3
from contextlib import closing

import pytest

from minidb.database import Database
from sqlcompare import Pair, typed


@pytest.fixture(params=[None, "sqlite"])
def pair(request, tmp_path):
    path = str(tmp_path / "db") if request.param else None
    return Pair(path, check_messages=True, format=request.param)


def run_all(pair, script):
    for sql in script.strip().split(";\n"):
        pair.run(sql)


def test_keys_indexes_and_queries(pair):
    run_all(pair, """
        CREATE TABLE w (a, b, c UNIQUE, d, PRIMARY KEY (b, a)) WITHOUT ROWID;
        CREATE INDEX wd ON w (d, a);
        CREATE INDEX wcb ON w (c COLLATE nocase, b);
        PRAGMA index_info(wd);
        PRAGMA index_xinfo(wd);
        PRAGMA index_xinfo(wcb);
        PRAGMA index_xinfo(w);
        PRAGMA table_list(w);
        INSERT INTO w VALUES (1, 'x', 'c1', 5), (2, 'x', 'c2', 5), (1, 'y', 'C3', 6), (3, 'z', NULL, NULL);
        INSERT INTO w VALUES (1, 'x', 'c9', 0);
        INSERT INTO w VALUES (NULL, 'q', 'c9', 0);
        SELECT * FROM w;
        SELECT * FROM w WHERE d = 5;
        SELECT a FROM w WHERE d = 5 ORDER BY a;
        SELECT b, a FROM w ORDER BY b, a;
        SELECT b, count(*) FROM w GROUP BY b;
        SELECT * FROM w WHERE c = 'c3' COLLATE nocase;
        SELECT * FROM w WHERE b IN ('x', 'z');
        SELECT * FROM w WHERE b = 'x' AND a > 1;
        SELECT * FROM w WHERE (b = 'y' OR d = 5);
        SELECT * FROM w INDEXED BY wd WHERE d > 0;
        SELECT rowid FROM w;
        SELECT w.rowid FROM w;
        UPDATE w SET rowid = 1;
        INSERT INTO w (rowid, a, b) VALUES (1, 1, 1);
        CREATE TRIGGER tr3 AFTER INSERT ON w BEGIN SELECT new.rowid; END;
        CREATE TABLE nokey (a) WITHOUT ROWID;
        CREATE TABLE auto (a INTEGER PRIMARY KEY AUTOINCREMENT) WITHOUT ROWID;
        CREATE TABLE dup (a, b, PRIMARY KEY (a, b, a)) WITHOUT ROWID;
        INSERT INTO dup VALUES (1, 2), (1, 3);
        INSERT INTO dup VALUES (1, 2);
        SELECT * FROM dup;
        PRAGMA index_xinfo(dup);
        CREATE TABLE dup2 (a, b, PRIMARY KEY (a, b, a COLLATE nocase)) WITHOUT ROWID;
        PRAGMA index_xinfo(dup2);
        INSERT INTO dup2 VALUES ('x', 1), ('X', 1);
        SELECT * FROM dup2
    """)


def test_conflicts_upserts_and_triggers(pair):
    run_all(pair, """
        CREATE TABLE w (a, b, c UNIQUE, d, PRIMARY KEY (b, a)) WITHOUT ROWID;
        INSERT INTO w VALUES (1, 'x', 'c1', 5), (2, 'x', 'c2', 5), (1, 'y', 'C3', 6), (3, 'z', NULL, NULL);
        INSERT INTO w VALUES (1, 'x', 'c9', 0) ON CONFLICT (b, a) DO UPDATE SET d = excluded.d + 100 RETURNING *;
        INSERT INTO w VALUES (9, 'q', 'c1', 0) ON CONFLICT (c) DO UPDATE SET d = -1;
        INSERT INTO w VALUES (9, 'q', 'c1', 0) ON CONFLICT DO NOTHING;
        INSERT OR REPLACE INTO w VALUES (2, 'x', 'C3', 7);
        SELECT * FROM w;
        INSERT OR IGNORE INTO w VALUES (2, 'x', 'new', 7), (4, 'x', 'new', 8);
        UPDATE OR REPLACE w SET a = 1 WHERE b = 'x' AND a = 4;
        SELECT * FROM w;
        UPDATE w SET b = 'y', a = 1 WHERE a = 1 AND b = 'x';
        SELECT * FROM w;
        CREATE TABLE log (t);
        CREATE TRIGGER tr AFTER UPDATE ON w BEGIN INSERT INTO log VALUES (old.b || old.a || '>' || new.b || new.a); END;
        CREATE TRIGGER tr2 BEFORE DELETE ON w BEGIN INSERT INTO log VALUES ('del ' || old.b); END;
        UPDATE w SET a = a + 10 WHERE d IS NOT NULL;
        DELETE FROM w WHERE a > 12;
        SELECT * FROM log;
        SELECT * FROM w;
        PRAGMA integrity_check
    """)


def test_foreign_keys(pair):
    run_all(pair, """
        PRAGMA foreign_keys = ON;
        CREATE TABLE w (a, b, c, PRIMARY KEY (b, a)) WITHOUT ROWID;
        INSERT INTO w VALUES (1, 'x', 1), (11, 'y', 2);
        CREATE TABLE child (x, y, FOREIGN KEY (x, y) REFERENCES w (b, a) ON DELETE CASCADE ON UPDATE CASCADE);
        CREATE TABLE child2 (x REFERENCES w);
        INSERT INTO child VALUES ('y', 11);
        INSERT INTO child VALUES ('nope', 1);
        INSERT INTO child2 VALUES (1);
        UPDATE w SET a = 99 WHERE b = 'y' AND a = 11;
        SELECT * FROM child;
        DELETE FROM w WHERE a = 99;
        SELECT * FROM child;
        CREATE TABLE wp (id INTEGER PRIMARY KEY, v) WITHOUT ROWID;
        CREATE TABLE wc (pid REFERENCES wp (id), q, PRIMARY KEY (q)) WITHOUT ROWID;
        INSERT INTO wp VALUES (1, 'a');
        INSERT INTO wc VALUES (1, 'q'), (2, 'r');
        INSERT INTO wc VALUES (1, 'q2');
        DELETE FROM wp;
        PRAGMA foreign_keys = OFF;
        INSERT INTO wc VALUES (5, 'z');
        PRAGMA foreign_key_check;
        PRAGMA foreign_key_check(wc)
    """)


def test_replace_when_the_primary_key_changes(pair):
    # A new PRIMARY KEY rewrites every index entry, so SQLite checks every
    # UNIQUE index - and compiles REPLACE's delete, foreign keys and all.
    run_all(pair, """
        PRAGMA foreign_keys = ON;
        CREATE TABLE temp.t0 (id INTEGER PRIMARY KEY, c1 REAL);
        CREATE TABLE t1 (id INTEGER PRIMARY KEY, c0 INT REFERENCES t0 (c1), c1, c2 UNIQUE ON CONFLICT REPLACE) WITHOUT ROWID;
        UPDATE t1 SET id = 5;
        UPDATE t1 SET c1 = 5;
        CREATE TABLE t2 (id PRIMARY KEY, c, u UNIQUE ON CONFLICT REPLACE) WITHOUT ROWID;
        INSERT INTO t2 VALUES (1, 'a', 10), (2, 'b', 20);
        UPDATE t2 SET id = 3, u = 10 WHERE id = 2;
        SELECT * FROM t2
    """)


def test_descending_primary_keys(pair):
    run_all(pair, """
        CREATE TABLE t1 (c0 UNIQUE, c2 INTEGER NOT NULL, c3, PRIMARY KEY (c2 DESC)) WITHOUT ROWID;
        CREATE INDEX i3 ON t1 (c3);
        INSERT INTO t1 VALUES (NULL, 9, 1), (NULL, 1, 1), (NULL, 0, 1), (5, 3, 2), (NULL, 7, 1);
        SELECT * FROM t1 ORDER BY c2;
        SELECT c2 FROM t1 WHERE c0 IS NULL ORDER BY c0, c2;
        SELECT c2 FROM t1 WHERE c3 = 1 ORDER BY c2 DESC;
        PRAGMA index_xinfo(i3);
        DELETE FROM t1 WHERE c2 IN (1, 9);
        PRAGMA integrity_check
    """)
    names = [row[1] for row in pair.lite.execute("PRAGMA index_list(t1)")]
    assert "sqlite_autoindex_t1_1" in names
    if pair.mini.executor.catalog.sqlite:  # (MiniDB's own format names it minidb_autoindex_t1_1)
        pair.run("PRAGMA index_xinfo(sqlite_autoindex_t1_1)")


def test_alter_and_drop(pair):
    run_all(pair, """
        CREATE TABLE w (a, b, c UNIQUE, d, PRIMARY KEY (b, a)) WITHOUT ROWID;
        INSERT INTO w VALUES (1, 'x', 'c1', 5), (2, 'x', 'c2', 5);
        ALTER TABLE w RENAME COLUMN a TO aa;
        SELECT sql FROM sqlite_master WHERE name = 'w';
        ALTER TABLE w ADD COLUMN e DEFAULT 'new';
        SELECT * FROM w;
        ALTER TABLE w DROP COLUMN d;
        ALTER TABLE w DROP COLUMN aa;
        ALTER TABLE w DROP COLUMN e;
        SELECT * FROM w;
        ALTER TABLE w RENAME TO w9;
        SELECT type, replace(name, 'minidb_', 'sqlite_'), tbl_name FROM sqlite_master WHERE tbl_name = 'w9';
        PRAGMA integrity_check;
        VACUUM;
        SELECT * FROM w9;
        REINDEX;
        DROP TABLE w9;
        SELECT name FROM sqlite_master
    """)


def test_generated_columns_and_reals(pair):
    run_all(pair, """
        CREATE TABLE g (a INTEGER PRIMARY KEY, b, v AS (a * 2), s AS (b || '!') STORED, CHECK (b != 'bad')) WITHOUT ROWID;
        INSERT INTO g VALUES (1, 'x'), (2, 'y');
        INSERT INTO g VALUES (3, 'bad');
        SELECT * FROM g WHERE v = 4;
        CREATE INDEX gv ON g (v);
        SELECT * FROM g WHERE v > 1;
        UPDATE g SET a = 10 WHERE a = 1 RETURNING *;
        SELECT * FROM g;
        CREATE TABLE r (x REAL PRIMARY KEY, y) WITHOUT ROWID;
        INSERT INTO r VALUES (1, 'a'), (2.5, 'b'), ('3', 'c');
        SELECT x, typeof(x) FROM r;
        SELECT * FROM r WHERE x = 1
    """)


def test_an_index_built_on_a_copy_of_a_real_column(pair):
    # CREATE INDEX reads a VIRTUAL copy of a REAL column as the record holds
    # it (10.0 as 10); a SELECT that scans the index reads it from there.
    run_all(pair, """
        CREATE TABLE t1 (c1 FLOAT, g1 AS (g0 || 'x'), g0 AS (c1), c2);
        INSERT INTO t1 (c1, c2) VALUES (10, 1);
        CREATE INDEX i14 ON t1 (g0);
        INSERT INTO t1 (c1, c2) VALUES (20, 2);
        SELECT g0, g1, typeof(g0) FROM t1 INDEXED BY i14 WHERE g0 > 0;
        SELECT g1 FROM t1 WHERE g0 = 10 AND c2 = 1;
        SELECT g0, g1 FROM t1 NOT INDEXED;
        UPDATE t1 SET c2 = g1 WHERE g0 = 10;
        SELECT c2 FROM t1 ORDER BY c2;
        CREATE TABLE t3 (c1 FLOAT, g0 AS (c1) STORED, g1 AS (g0 || 'x'));
        INSERT INTO t3 (c1) VALUES (10);
        CREATE INDEX i3 ON t3 (g0);
        SELECT g0, g1 FROM t3 INDEXED BY i3 WHERE g0 > 0;
        DELETE FROM t1 WHERE g0 = 10;
        PRAGMA integrity_check
    """)


def test_temp_tables_without_rowid(pair):
    run_all(pair, """
        CREATE TEMP TABLE t (k TEXT PRIMARY KEY, v) WITHOUT ROWID;
        INSERT INTO t VALUES ('b', 2), ('a', 1);
        SELECT * FROM t;
        BEGIN;
        UPDATE t SET k = 'c' WHERE k = 'a';
        ROLLBACK;
        SELECT * FROM temp.t;
        CREATE TABLE copy AS SELECT * FROM t;
        SELECT * FROM copy
    """)


# ---- SQLite files ----------------------------------------------------------------------


SETUP = """
    CREATE TABLE kv (k TEXT PRIMARY KEY, v INT, note) WITHOUT ROWID;
    CREATE INDEX kv_v ON kv (v);
    CREATE TABLE pair (a INT, b TEXT, c UNIQUE, PRIMARY KEY (a DESC, b)) WITHOUT ROWID;
"""
QUERIES = ["SELECT * FROM kv ORDER BY k", "SELECT k FROM kv WHERE v BETWEEN 10 AND 40 ORDER BY v, k",
           "SELECT * FROM pair ORDER BY a, b", "SELECT a, b FROM pair WHERE c > 'c5' ORDER BY c",
           "SELECT count(*), sum(v) FROM kv"]


def lite(path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.text_factory = lambda data: data.decode("utf-8", "surrogateescape")
    return connection


def same_results(path, db):
    with closing(lite(path)) as other:
        for sql in QUERIES:
            assert [typed(r) for r in db.execute(sql)] == [typed(r) for r in other.execute(sql)], sql


def test_written_by_sqlite(tmp_path):
    path = str(tmp_path / "db")
    with closing(lite(path)) as connection:
        connection.executescript(SETUP)
        connection.executemany("INSERT INTO kv VALUES (?, ?, ?)",
                               [(f"key{i:04}", i % 50, "n" * (i % 300)) for i in range(800)])
        connection.executemany("INSERT INTO pair VALUES (?, ?, ?)", [(i % 9, f"b{i}", f"c{i}") for i in range(200)])
    with Database(path) as db:
        same_results(path, db)
        db.execute("INSERT INTO kv VALUES ('new', 7, 'x')")
        db.execute("UPDATE kv SET v = v + 1 WHERE v % 3 = 0")
        db.execute("UPDATE kv SET k = k || '!' WHERE v = 20")
        db.execute("DELETE FROM kv WHERE v % 5 = 0")
        db.execute("UPDATE pair SET a = a + 100 WHERE b LIKE 'b1%'")
        db.execute("DELETE FROM pair WHERE a = 3")
        same_results(path, db)
    with closing(lite(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_written_by_minidb(tmp_path):
    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        for sql in SETUP.strip().split(";"):
            db.execute(sql)
        for i in range(800):
            db.execute("INSERT INTO kv VALUES (?, ?, ?)", (f"key{i:04}", i % 50, "n" * (i % 300)))
        for i in range(200):
            db.execute("INSERT INTO pair VALUES (?, ?, ?)", (i % 9, f"b{i}", f"c{i}"))
        db.execute("DELETE FROM kv WHERE v % 7 = 0")
    with closing(lite(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        connection.execute("INSERT INTO kv VALUES ('lite', 1, 'y')")
        connection.execute("DELETE FROM pair WHERE a = 4")
    with Database(path) as db:
        same_results(path, db)
        assert db.execute("PRAGMA integrity_check") == [("ok",)]


def test_self_referencing_foreign_keys(pair):
    # When the SET assigns the PRIMARY KEY (whatever the value) or changes a
    # child key that refers to the table itself, SQLite deletes the old row
    # before looking up the new one's parent; a row matches itself only if
    # the values are equal as they are (TEXT '5' is not 5).
    run_all(pair, """
        PRAGMA foreign_keys = ON;
        CREATE TABLE t2 (id INTEGER PRIMARY KEY, c0 TEXT REFERENCES t2 (id), c1) WITHOUT ROWID;
        INSERT INTO t2 (id) VALUES (5);
        UPDATE t2 SET c0 = id WHERE id = 5;
        CREATE TABLE t7 (id INT PRIMARY KEY, c0 INT REFERENCES t7 (id)) WITHOUT ROWID;
        INSERT INTO t7 VALUES (1, 1);
        UPDATE t7 SET c0 = 1 WHERE id = 1;
        INSERT INTO t7 VALUES (2, NULL);
        UPDATE t7 SET c0 = 2 WHERE id = 2;
        CREATE TABLE d (id INTEGER PRIMARY KEY, c0 REFERENCES d (id) DEFERRABLE INITIALLY DEFERRED, c2) WITHOUT ROWID;
        INSERT INTO d VALUES (1, 1, 5);
        BEGIN;
        INSERT INTO d VALUES (-4, 2, 7);
        INSERT INTO d VALUES (1, 1, 0) ON CONFLICT (id) DO UPDATE SET id = 1, c2 = 9 WHERE d.c2;
        COMMIT;
        ROLLBACK;
        SELECT * FROM d
    """)


def test_replace_gives_a_primary_key_column_its_default(pair):
    # (The row's key must be made after REPLACE fixed the NOT NULL column.)
    run_all(pair, """
        CREATE TABLE t2 (c0 TEXT, c1 INT NOT NULL DEFAULT (2 * 3), PRIMARY KEY (c1 DESC)) WITHOUT ROWID;
        INSERT INTO t2 (c1) VALUES (5), (3);
        UPDATE OR REPLACE t2 SET c1 = NULL WHERE c1 = 3;
        CREATE INDEX i11 ON t2 (c1);
        SELECT * FROM t2 WHERE c1 = 6;
        SELECT * FROM t2 INDEXED BY i11 WHERE c1 < 7;
        INSERT OR REPLACE INTO t2 VALUES ('x', NULL);
        UPDATE OR REPLACE t2 SET c1 = NULL WHERE c1 = 5;
        SELECT * FROM t2;
        PRAGMA integrity_check
    """)


@pytest.mark.parametrize("format", [None, "sqlite"])
def test_json_primary_key_read_back_from_an_index(format):
    # The key holds the values as the record does: a PRIMARY KEY value
    # inserted as JSON (with the subtype) reads back from a covering index
    # scan as plain text.
    pair = Pair(format=format)
    pair.run("CREATE TABLE t (c0 BLOB PRIMARY KEY, c1 UNIQUE, c2) WITHOUT ROWID")
    pair.run("""INSERT INTO t VALUES (json('[1,[2,{"a":3}],"s",true]'), 7.75, 1), (0, '1', 2), (2.5, '0', 3)""")
    pair.run("INSERT INTO t VALUES (json('[9]'), 1.5, 4) ON CONFLICT (c0) DO UPDATE SET c2 = 5")
    pair.run("SELECT a.c0, j.key FROM t AS a, json_each(a.c0) AS j")
    pair.run("SELECT json_quote(c0), json_array(c0) FROM t")
    pair.run("SELECT c1, json_array(c0) FROM t WHERE c1 > '0'")
    pair.close()
