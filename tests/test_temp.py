"""Temporary tables (CREATE TEMP TABLE, the temp schema), CREATE TABLE ...
AS SELECT, compared with SQLite in both file formats."""

import pytest

from minidb.database import Database
from sqlcompare import Pair


@pytest.fixture(params=[None, "sqlite"])
def pair(request, tmp_path):
    path = str(tmp_path / "db") if request.param else None
    return Pair(path, check_messages=True, format=request.param)


def run_all(pair, script):
    for sql in script.strip().split(";\n"):
        pair.run(sql)


# (MiniDB's own format names automatic indexes minidb_autoindex_...)
TEMP_SCHEMA = ("SELECT type, replace(name, 'minidb_', 'sqlite_'), tbl_name, sql FROM sqlite_temp_master "
               "ORDER BY type, name")


def test_temp_tables_and_names(pair):
    run_all(pair, """
        CREATE TABLE t (a);
        INSERT INTO t VALUES (1);
        CREATE TEMP TABLE t (b, c UNIQUE);
        INSERT INTO t VALUES (2, 3);
        SELECT * FROM t;
        SELECT * FROM main.t;
        SELECT * FROM temp.t;
        SELECT * FROM TEMP.t;
        SELECT * FROM foo.t;
        INSERT INTO foo.t VALUES (1);
        CREATE TABLE foo.t (a);
        CREATE TEMPORARY TABLE IF NOT EXISTS t (x);
        CREATE TEMP TABLE t (x);
        CREATE TABLE temp.u (x INTEGER PRIMARY KEY AUTOINCREMENT, y);
        INSERT INTO u (y) VALUES (5);
        SELECT * FROM temp.sqlite_sequence;
        CREATE TABLE main.v (x);
        CREATE TEMP TABLE main.w (x);
        CREATE TEMP TABLE temp.w (x);
        CREATE TEMP TABLE sqlite_x (a);
        CREATE INDEX ti ON t (b);
        CREATE INDEX main.ti2 ON u (y);
        CREATE INDEX temp.ti3 ON v (x);
        CREATE INDEX foo.i ON t (b);
        SELECT b FROM t INDEXED BY ti WHERE b > 0;
        UPDATE temp.t SET b = 1;
        DELETE FROM main.t;
        DROP TABLE t;
        SELECT * FROM t;
        DROP TABLE temp.zz;
        DROP TABLE IF EXISTS temp.zz;
        DROP TABLE foo.t;
        CREATE TEMP TABLE t (b);
        DROP TABLE main.t;
        SELECT * FROM t;
        SELECT type, name FROM sqlite_schema ORDER BY name;
        SELECT type, name FROM main.sqlite_master ORDER BY name;
        SELECT type, name FROM temp.sqlite_schema ORDER BY name;
        SELECT type, name FROM sqlite_temp_schema ORDER BY name;
        SELECT * FROM main.sqlite_temp_master;
        """ + TEMP_SCHEMA + """;
        PRAGMA table_info(u);
        PRAGMA temp.table_info(u);
        PRAGMA main.table_info(u);
        CREATE TEMP TABLE v (q);
        PRAGMA table_info(v);
        PRAGMA main.table_info(v);
        SELECT * FROM pragma_table_info('v');
        SELECT * FROM pragma_table_info('v', 'main');
        SELECT * FROM pragma_table_info('v', 'temp');
        SELECT seq, name FROM pragma_database_list;
        SELECT name FROM pragma_table_list ORDER BY 1;
        PRAGMA integrity_check;
        PRAGMA temp.integrity_check
        """)


def test_temp_tables_and_transactions(pair):
    run_all(pair, """
        CREATE TEMP TABLE u (x INTEGER PRIMARY KEY, y UNIQUE);
        INSERT INTO u (y) VALUES (5);
        BEGIN;
        INSERT INTO u (y) VALUES (6);
        ROLLBACK;
        SELECT * FROM u;
        BEGIN;
        INSERT INTO u (y) VALUES (7);
        CREATE TEMP TABLE inside (a);
        INSERT INTO inside VALUES (1);
        COMMIT;
        SELECT * FROM u;
        SELECT * FROM inside;
        BEGIN;
        CREATE TEMP TABLE gone (a);
        INSERT INTO gone VALUES (1);
        ROLLBACK;
        SELECT * FROM gone;
        INSERT INTO u (y) VALUES (8), (7);
        SELECT * FROM u;
        BEGIN;
        INSERT INTO u (y) VALUES (9);
        INSERT INTO u (y) VALUES (9);
        INSERT OR ROLLBACK INTO u (y) VALUES (9);
        SELECT * FROM u
        """)


def test_temp_views_triggers_and_foreign_keys(pair):
    run_all(pair, """
        CREATE TABLE v (x);
        CREATE TEMP TABLE u (x INTEGER PRIMARY KEY, y);
        CREATE VIEW vv AS SELECT * FROM u;
        SELECT * FROM vv;
        CREATE TEMP VIEW tv AS SELECT * FROM u, v;
        SELECT * FROM tv;
        CREATE VIEW temp.v4 AS SELECT 1;
        CREATE TEMP VIEW main.v2 AS SELECT 1;
        CREATE TRIGGER tr AFTER INSERT ON u BEGIN INSERT INTO v VALUES (new.y * 10); END;
        CREATE TRIGGER tr2 AFTER INSERT ON v BEGIN INSERT INTO u (y) VALUES (new.x); END;
        INSERT INTO u (y) VALUES (1);
        SELECT * FROM v;
        INSERT INTO v VALUES (9);
        CREATE TEMP TRIGGER tt AFTER INSERT ON v BEGIN INSERT INTO u (y) VALUES (new.x + 1); END;
        CREATE TRIGGER temp.tt2 AFTER DELETE ON main.v BEGIN INSERT INTO u (y) VALUES (-old.x); END;
        CREATE TRIGGER tr4 AFTER INSERT ON main.u BEGIN SELECT 1; END;
        CREATE TRIGGER main.tr5 AFTER INSERT ON u BEGIN SELECT 1; END;
        CREATE TRIGGER tr6 AFTER INSERT ON temp.v BEGIN SELECT 1; END;
        CREATE TEMP TRIGGER main.tr7 AFTER INSERT ON v BEGIN SELECT 1; END;
        DROP TRIGGER tr2;
        INSERT INTO v VALUES (20);
        DELETE FROM v WHERE x = 20;
        SELECT * FROM u;
        SELECT type, name, tbl_name FROM sqlite_temp_master WHERE type = 'trigger' ORDER BY name;
        DROP TRIGGER main.tt;
        DROP TRIGGER temp.tt;
        DROP TABLE v;
        SELECT type, name, tbl_name FROM sqlite_temp_master WHERE type = 'trigger' ORDER BY name;
        ALTER TABLE u RENAME TO u2;
        SELECT name, sql FROM sqlite_temp_master ORDER BY name;
        ALTER TABLE temp.u2 ADD COLUMN z DEFAULT 3;
        ALTER TABLE main.u2 ADD COLUMN q;
        SELECT * FROM u2;
        CREATE TEMP TABLE p (id INTEGER PRIMARY KEY);
        CREATE TEMP TABLE ch (pid REFERENCES p (id));
        CREATE TABLE mch (pid REFERENCES p (id));
        PRAGMA foreign_keys = ON;
        INSERT INTO ch VALUES (1);
        INSERT INTO mch VALUES (1);
        INSERT INTO p VALUES (1);
        INSERT INTO ch VALUES (1);
        DELETE FROM p;
        PRAGMA foreign_key_check;
        PRAGMA foreign_key_check(ch)
        """)


def test_create_table_as_select(pair):
    run_all(pair, """
        CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT, c REAL, d, e NUMERIC, f BLOB, g VARCHAR(10), h INT UNIQUE);
        INSERT INTO t VALUES (1, 'x', 1.5, NULL, 2, x'00', 'v', 7);
        INSERT INTO t VALUES (2, 5, 3, 'q', '3.0', 1, 9, '8');
        CREATE TABLE s AS SELECT * FROM t;
        SELECT *, typeof(c), typeof(e) FROM s;
        CREATE TEMP TABLE w2 AS SELECT 1, 'a' AS x, 2.5, b || 'q', CAST(a AS TEXT) y, NULL, x'01' z, a AS a2,
                                       a AS A2 FROM t;
        SELECT * FROM w2;
        PRAGMA table_info(w2);
        CREATE TABLE s2 AS SELECT a, count(*) AS n FROM t GROUP BY a;
        CREATE TABLE IF NOT EXISTS s AS SELECT * FROM nosuch;
        CREATE TABLE s AS SELECT 1;
        CREATE TABLE s3 AS SELECT * FROM nosuch;
        CREATE TABLE s4 AS VALUES (1, 2);
        CREATE TABLE s5 AS SELECT b AS "a b", c AS [x"y] FROM t;
        CREATE TABLE s6 AS WITH q AS (SELECT 1 AS k) SELECT * FROM q;
        CREATE TABLE s8 AS SELECT a FROM t UNION SELECT h FROM t;
        CREATE TABLE s8b AS SELECT a FROM t UNION SELECT b FROM t;
        SELECT * FROM s8;
        CREATE TABLE s9 AS SELECT a, a FROM t;
        CREATE TABLE s10 AS SELECT a AS rowid, b AS oid FROM t;
        SELECT rowid, * FROM s10;
        CREATE TABLE s11 AS SELECT a COLLATE nocase AS k FROM t;
        CREATE TABLE s12 AS SELECT max(c) m, sum(a) s, avg(a) av, group_concat(b) gc, typeof(a) ty, abs(c) ab FROM t;
        CREATE TABLE s13 AS SELECT 1 AS "order", 2 AS rowid, 3 AS "temp", 4 AS "_x", 5 AS "1a", 6 AS "é", 7 AS "",
                                   8 AS true, 9 AS "a:3", 10 AS a, 11 AS "A";
        CREATE TABLE "select" AS SELECT 1;
        CREATE TABLE "x y" AS SELECT 1 AS a, 1 AS a, 1 AS a, 1 AS a;
        CREATE TABLE s14 AS SELECT * FROM t WHERE 0;
        SELECT count(*) FROM s14;
        SELECT changes(), last_insert_rowid();
        CREATE TABLE s15 AS SELECT json('[1]') j, (SELECT b FROM t) sub, -a neg, +b pos FROM t;
        SELECT * FROM s15;
        CREATE TABLE s16 AS SELECT CAST(a AS REAL) r, CAST(b AS NUMERIC) n, CAST(c AS INTEGER) i, CAST(d AS BLOB) bl,
                                   CAST(e AS VARCHAR) v FROM t;
        CREATE TABLE s17 AS SELECT x.a, y.b FROM t x JOIN t y USING (a);
        BEGIN;
        CREATE TEMP TABLE r AS SELECT * FROM t;
        ROLLBACK;
        SELECT * FROM r;
        CREATE TABLE sqlite_q AS SELECT 1;
        SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name;
        SELECT name, sql FROM sqlite_temp_master ORDER BY name
        """)
    pair.run("CREATE TEMP TABLE p AS SELECT ? AS x, ? AS y", parameters=(1, "two"))
    pair.run("SELECT * FROM p")


def test_temp_tables_are_not_in_the_file(tmp_path):
    path = str(tmp_path / "db")
    for format in (None, "sqlite"):
        with Database(path + str(format), format=format) as db:
            db.execute("CREATE TABLE keep (a)")
            db.execute("CREATE TEMP TABLE scratch AS SELECT 1 AS a")
            db.execute("INSERT INTO keep SELECT a FROM scratch")
            assert db.execute("SELECT count(*) FROM sqlite_temp_master") == [(1,)]
        with Database(path + str(format)) as db:
            assert db.execute("SELECT * FROM keep") == [(1,)]
            assert db.execute("SELECT count(*) FROM sqlite_temp_master") == [(0,)]
            assert db.execute("SELECT name FROM sqlite_master") == [("keep",)]
