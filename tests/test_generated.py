"""Generated columns (GENERATED ALWAYS AS ... VIRTUAL / STORED) compared
with SQLite, in both file formats."""

import pytest

from sqlcompare import Pair


@pytest.fixture(params=[None, "sqlite"])
def pair(request, tmp_path):
    path = str(tmp_path / "db") if request.param else None
    return Pair(path, check_messages=True, format=request.param)


def run_all(pair, script):
    for sql in script.strip().split(";\n"):
        pair.run(sql)


def test_values_and_writes(pair):
    run_all(pair, """
        CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT, c AS (b || 'x'), d INT GENERATED ALWAYS AS (a*2) STORED,
                        e AS (c || d) VIRTUAL);
        INSERT INTO t VALUES (1, 'q');
        INSERT INTO t VALUES (1, 'q', 3);
        INSERT INTO t (a, c) VALUES (2, 3);
        INSERT INTO t (b) VALUES ('z');
        SELECT * FROM t;
        SELECT typeof(d), typeof(c) FROM t;
        UPDATE t SET d = 5;
        UPDATE t SET b = 'w' WHERE a = 1;
        SELECT * FROM t;
        SELECT * FROM t WHERE e = 'zx4';
        PRAGMA table_info(t);
        PRAGMA table_xinfo(t);
        SELECT * FROM pragma_table_xinfo('t');
        CREATE TABLE y (a, b AS (a + 1));
        INSERT INTO y SELECT 1;
        INSERT INTO y DEFAULT VALUES;
        SELECT * FROM y;
        CREATE TABLE z17 (a AS (b), b);
        INSERT INTO z17 VALUES (3);
        INSERT INTO z17 (a) VALUES (3);
        SELECT * FROM z17;
        CREATE TABLE w7 (a, b AS (a) STORED, c AS (b) VIRTUAL, d AS (c) STORED);
        INSERT INTO w7 VALUES (5);
        SELECT * FROM w7;
        CREATE TABLE x8 (a, b AS (c), c AS (a + 1) STORED, d AS (b * 10));
        INSERT INTO x8 VALUES (1);
        UPDATE x8 SET a = 5;
        SELECT * FROM x8;
        CREATE TABLE z3 (a INTEGER PRIMARY KEY, b AS (a) NOT NULL);
        INSERT INTO z3 VALUES (NULL);
        SELECT * FROM z3;
        DELETE FROM w7 RETURNING *
        """)


def test_affinity_collation_and_json(pair):
    run_all(pair, """
        CREATE TABLE ta (a INT, b AS (typeof(a)), c AS (typeof(a)) STORED);
        INSERT INTO ta VALUES ('1');
        SELECT * FROM ta;
        CREATE TABLE w2 (a, b TEXT AS (a));
        INSERT INTO w2 VALUES (1);
        SELECT typeof(b) FROM w2;
        CREATE TABLE w3 (a, b INT AS (a) STORED);
        INSERT INTO w3 VALUES ('12');
        SELECT typeof(b) FROM w3;
        CREATE TABLE t10 (a INTEGER, b REAL AS (a));
        INSERT INTO t10 VALUES (3);
        SELECT b, typeof(b) FROM t10;
        CREATE TABLE t8 (a TEXT, b AS (a) COLLATE nocase);
        INSERT INTO t8 VALUES ('X');
        SELECT * FROM t8 WHERE b = 'x';
        SELECT b < 'y', b = 'x' FROM t8;
        CREATE TABLE d3 (a, b AS (unixepoch(a)), c AS (json(a)), e AS (json_array(c)), f AS (json(a)) STORED);
        INSERT INTO d3 VALUES ('[1]');
        SELECT * FROM d3;
        SELECT json_array(c), json_array(f) FROM d3
        """)


def test_constraints_indexes_and_triggers(pair):
    run_all(pair, """
        CREATE TABLE u (a, b AS (a) UNIQUE NOT NULL CHECK (b > 0) COLLATE nocase);
        INSERT INTO u VALUES (NULL);
        INSERT INTO u VALUES (0);
        INSERT INTO u VALUES ('A');
        INSERT INTO u VALUES ('a');
        CREATE TABLE zz (a, b AS (a) NOT NULL ON CONFLICT IGNORE);
        INSERT INTO zz VALUES (NULL);
        SELECT count(*) FROM zz;
        CREATE TABLE z12 (a, b AS (a) STORED, CHECK (b > 0));
        INSERT INTO z12 VALUES (0);
        CREATE TABLE z16 (a, b AS (a), UNIQUE (b));
        INSERT INTO z16 VALUES (1);
        INSERT INTO z16 VALUES (1);
        CREATE TABLE t11 (a, b AS (a) STORED UNIQUE ON CONFLICT REPLACE);
        INSERT INTO t11 VALUES (1);
        INSERT INTO t11 VALUES (1);
        SELECT rowid, * FROM t11;
        CREATE TABLE x (a, b AS (a * 2), c);
        INSERT INTO x VALUES (1, 2);
        CREATE TABLE log (t);
        CREATE TRIGGER tr BEFORE INSERT ON x BEGIN INSERT INTO log VALUES (NEW.b); END;
        CREATE TRIGGER tr2 AFTER UPDATE ON x BEGIN INSERT INTO log VALUES (OLD.b || '>' || NEW.b); END;
        INSERT INTO x VALUES (5, 6);
        UPDATE x SET a = 7 WHERE a = 5;
        INSERT INTO x VALUES (8, 9) RETURNING *;
        CREATE UNIQUE INDEX xb ON x (b);
        INSERT INTO x VALUES (1, 0) ON CONFLICT (b) DO UPDATE SET c = excluded.b + 100;
        INSERT INTO x VALUES (1, 0) ON CONFLICT (b) DO UPDATE SET b = 1;
        INSERT OR REPLACE INTO x VALUES (7, 1);
        SELECT * FROM x;
        SELECT b FROM x WHERE b > 2 ORDER BY b;
        SELECT b FROM x INDEXED BY xb WHERE b = 16;
        CREATE TABLE t5 (a INTEGER PRIMARY KEY, b AS (coalesce(a, 'none')));
        CREATE TRIGGER tr5 BEFORE INSERT ON t5 BEGIN INSERT INTO log VALUES (NEW.a || ':' || NEW.b); END;
        INSERT INTO t5 VALUES (NULL);
        INSERT INTO t5 (a) VALUES (7);
        CREATE TABLE t6 (a, b AS (a + 1), c);
        CREATE TRIGGER tr6 AFTER UPDATE OF b ON t6 BEGIN INSERT INTO log VALUES ('b changed'); END;
        INSERT INTO t6 VALUES (1, 1);
        UPDATE t6 SET a = 2;
        SELECT * FROM log;
        PRAGMA integrity_check
        """)


def test_create_table_errors(pair):
    pair.run("CREATE TABLE e (a, b AS (?))", parameters=(1,))
    for sql in [
            "CREATE TABLE e (a, b AS (b))", "CREATE TABLE e (a, b AS (c), c AS (b))",
            "CREATE TABLE e (a, b AS (random()))", "CREATE TABLE e (a, b AS ((SELECT 1)))",
            "CREATE TABLE e (a, b AS (EXISTS (SELECT 1)))", "CREATE TABLE e (a, b AS (count(*)))",
            "CREATE TABLE e (a, b AS (max(a, 1)), c AS (min(a)))", "CREATE TABLE e (a, b AS (row_number() OVER ()))",
            "CREATE TABLE e (a, b AS (a) DEFAULT 3)", "CREATE TABLE e (a, b DEFAULT 1 AS (a))",
            "CREATE TABLE e (a, b AS (a) PRIMARY KEY)", "CREATE TABLE e (a, b INTEGER PRIMARY KEY AS (a))",
            "CREATE TABLE e (a, b AS (a), PRIMARY KEY (a, b))", "CREATE TABLE e (a AS (1))",
            "CREATE TABLE e (a, b AS (rowid))", "CREATE TABLE e (a, b AS (e.a))", "CREATE TABLE e (a, b AS (x.a))",
            "CREATE TABLE e (a, b AS (zz))", "CREATE TABLE e (a, b AS (a) foo)", "CREATE TABLE e (a, b AS (a) AS (a))",
            "CREATE TABLE e (a, b AS (last_insert_rowid()))", "CREATE TABLE e (a, b AS (current_timestamp))",
            "CREATE TABLE e (a, b AS (c), c AS (d), d AS (b))", "CREATE TABLE e (a, b AS (d), c AS (b), d AS (c))",
            "CREATE TABLE e (a, b AS (c + d), c AS (a), d AS (b))", "CREATE TABLE e (a, b AS (a), c AS (b), d AS (d+c))",
            "CREATE TABLE e (a, b AS (d), c AS (b), d AS (c), e AS (e))", "CREATE TABLE e (b AS (yy))",
            "CREATE TABLE e (a CHECK (zz), b AS (yy))"]:
        pair.run(sql)
    # A loop through a STORED column shows when a row is written.
    run_all(pair, """
        CREATE TABLE x5 (a, b AS (c), c AS (b) STORED);
        INSERT INTO x5 VALUES (1);
        INSERT INTO x5 SELECT 1 WHERE 0;
        CREATE TABLE x6 (a, b AS (a) STORED, c AS (c) STORED);
        UPDATE x6 SET a = 1;
        CREATE TABLE x7 (a, b AS (c) STORED, c AS (a + 1) STORED);
        INSERT INTO x7 VALUES (1);
        SELECT * FROM x7
        """)


def test_the_clock_in_generated_columns(pair):
    run_all(pair, """
        CREATE TABLE d1 (a, b AS (date()));
        INSERT INTO d1 VALUES (1);
        CREATE TABLE d2 (a, b AS (strftime('%s', a)));
        INSERT INTO d2 VALUES ('now');
        INSERT INTO d2 VALUES ('2000-01-01');
        UPDATE d2 SET a = 'now';
        SELECT * FROM d2;
        CREATE TABLE d3 (a, b AS (datetime(a, 'utc')));
        INSERT INTO d3 VALUES ('2020-01-01');
        CREATE TABLE d4 (a, b AS (datetime(a, 'localtime')) STORED);
        INSERT INTO d4 VALUES ('2020-01-01');
        CREATE TABLE d5 (a, b AS (timediff(a, 'now')));
        INSERT INTO d5 VALUES ('2020-01-01');
        CREATE TABLE d6 (a, b AS (julianday(a, 'subsec')));
        INSERT INTO d6 VALUES ('2020-01-01');
        SELECT * FROM d6;
        CREATE TABLE c (a CHECK (date(a) IS NOT NULL OR 1));
        INSERT INTO c VALUES ('now');
        SELECT * FROM c
        """)


def test_alter_table(pair):
    run_all(pair, """
        CREATE TABLE u (a);
        ALTER TABLE u ADD COLUMN s AS (a || 1) STORED;
        INSERT INTO u VALUES ('A');
        ALTER TABLE u ADD COLUMN s2 AS (a || 2) STORED;
        ALTER TABLE u ADD COLUMN c AS (a || 1);
        ALTER TABLE u ADD COLUMN d AS (a || 1) NOT NULL;
        SELECT * FROM u;
        CREATE TABLE tt (a, b);
        INSERT INTO tt VALUES (NULL, 1);
        ALTER TABLE tt ADD COLUMN c AS (a) NOT NULL;
        ALTER TABLE tt ADD COLUMN c AS (b) CHECK (c > 5);
        ALTER TABLE tt ADD COLUMN c AS (b + 1) CHECK (c > 1);
        ALTER TABLE tt ADD COLUMN d AS (zz);
        ALTER TABLE tt ADD COLUMN d AS (random());
        SELECT * FROM tt;
        CREATE TABLE uu (a, b AS (a) STORED, c, d AS (c));
        INSERT INTO uu VALUES (1, 2);
        ALTER TABLE uu DROP COLUMN b;
        SELECT * FROM uu;
        ALTER TABLE uu DROP COLUMN d;
        SELECT * FROM uu;
        ALTER TABLE uu DROP COLUMN c;
        CREATE TABLE z4 (a, b AS (a) STORED);
        INSERT INTO z4 VALUES ('x');
        ALTER TABLE z4 RENAME COLUMN a TO aa;
        SELECT * FROM z4;
        ALTER TABLE z4 DROP COLUMN aa;
        CREATE TABLE ww (a, b AS (a) STORED, c AS ("a" || ww2), ww2);
        INSERT INTO ww VALUES (1, 2);
        ALTER TABLE ww RENAME COLUMN a TO x;
        ALTER TABLE ww RENAME TO w2;
        SELECT * FROM w2;
        SELECT sql FROM sqlite_schema WHERE type = 'table' ORDER BY name;
        PRAGMA integrity_check
        """)


def test_int_real_values(pair):
    """A REAL generated column holds a whole value as SQLite's IntReal: a
    REAL, which a record (a table, the ORDER BY sorter, UNION) keeps as an
    integer."""
    run_all(pair, """
        CREATE TABLE t (a, g REAL AS (a), s REAL AS (a) STORED);
        INSERT INTO t VALUES (3), (3.5), ('4'), (-0.0), (1e20), (140737488355328), ('x'), (NULL);
        SELECT g, typeof(g), g || '', CAST(g AS TEXT), quote(g), s || '' FROM t;
        SELECT g, typeof(g) FROM t ORDER BY a;
        SELECT * FROM (SELECT g FROM t ORDER BY a LIMIT 5);
        SELECT DISTINCT g FROM t;
        SELECT g FROM t GROUP BY g;
        SELECT g FROM t UNION ALL SELECT 5;
        SELECT g FROM t UNION SELECT 5;
        SELECT (SELECT g FROM t ORDER BY a LIMIT 1);
        CREATE TABLE u (x, y TEXT, z INT, w REAL, v NUMERIC);
        INSERT INTO u SELECT g, g, g, g, g FROM t;
        SELECT x, typeof(x), y, z, w, typeof(w), v, typeof(v) FROM u;
        INSERT INTO u (y) SELECT g FROM t ORDER BY a;
        SELECT y FROM u WHERE x IS NULL;
        INSERT INTO t VALUES (7) RETURNING g, s, g || '', s || ''
        """)


def test_new_in_before_update_triggers(pair):
    """NEW's generated columns in a BEFORE UPDATE trigger see NULL for the
    columns the UPDATE does not set and no trigger names as new.x."""
    run_all(pair, """
        CREATE TABLE t (a, b, g AS (a || '-' || b), c);
        INSERT INTO t VALUES (1, 2, 3);
        CREATE TABLE log (x);
        CREATE TRIGGER tr BEFORE UPDATE ON t BEGIN INSERT INTO log VALUES (new.g); END;
        UPDATE t SET c = 9;
        UPDATE t SET a = 5;
        CREATE TRIGGER tr2 BEFORE UPDATE ON t BEGIN INSERT INTO log VALUES (new.b || '/' || new.g); END;
        UPDATE t SET c = 8;
        CREATE TRIGGER tr3 AFTER UPDATE ON t BEGIN INSERT INTO log VALUES ('after ' || new.g); END;
        UPDATE t SET c = 7;
        SELECT * FROM log;
        SELECT * FROM t
        """)


def test_json_subtype_of_new_values(pair):
    """A new row's JSON values keep their subtype in RETURNING, triggers and
    generated columns (SQLite's registers) - not in the record, and not
    when they come from a SELECT or several VALUES rows (a co-routine)."""
    run_all(pair, """
        CREATE TABLE p (c0, c1 TEXT);
        INSERT INTO p VALUES (json('{"a":1}'), json('[1]')) RETURNING json_quote(c0), json_quote(c1);
        INSERT INTO p VALUES (json('[9]'), 2), (json('[8]'), 3) RETURNING json_quote(c0);
        INSERT INTO p SELECT json('[9]'), 2 FROM (SELECT 1) RETURNING json_quote(c0);
        CREATE TABLE log (x);
        CREATE TRIGGER tr AFTER INSERT ON p BEGIN INSERT INTO log VALUES (json_quote(new.c0)); END;
        CREATE TRIGGER tr2 BEFORE INSERT ON p BEGIN INSERT INTO log VALUES (json_quote(new.c0)); END;
        INSERT INTO p VALUES (json('{"b":1}'), 1);
        SELECT * FROM log;
        UPDATE p SET c0 = json('[5]') RETURNING json_quote(c0);
        SELECT json_quote(c0), json_quote(c1) FROM p;
        CREATE INDEX pc ON p (c0);
        SELECT json_quote(c0) FROM p INDEXED BY pc WHERE c0 > '';
        CREATE TABLE t1 (c0 FLOAT, c1 TEXT, g0 INTEGER AS (json_quote(c0)) STORED, g1 AS (json_quote(c1)));
        INSERT INTO t1 (c0, c1) VALUES (json('{"a":1}'), json('[1]')) RETURNING json_quote(c0), g0;
        SELECT *, json_quote(c0) FROM t1;
        CREATE TABLE q (k UNIQUE, v);
        INSERT INTO q VALUES (1, json('[1]'));
        INSERT INTO q VALUES (1, json('[2]')) ON CONFLICT (k) DO UPDATE SET v = json_quote(excluded.v) RETURNING v
        """)
