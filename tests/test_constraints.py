"""Constraints compared with SQLite: CHECK, ON CONFLICT clauses of
constraints, table constraints, AUTOINCREMENT, and the schema text and
ALTER TABLE edits that go with them."""

import pytest

from sqlcompare import Pair


@pytest.fixture(params=[None, "sqlite"])
def pair(request, tmp_path):
    path = str(tmp_path / "db") if request.param else None
    return Pair(path, check_messages=True, format=request.param)


def run_all(pair, script):
    for sql in script.strip().split(";\n"):
        pair.run(sql)


# (MiniDB's own format names automatic indexes minidb_autoindex_...)
SCHEMA = ("SELECT type, replace(name, 'minidb_', 'sqlite_'), tbl_name, sql FROM sqlite_schema "
          "WHERE type != 'stat' ORDER BY rowid")


def test_schema_keeps_the_sql_as_written(pair):
    run_all(pair, """
        create   table   t1 ( a   int  , "b"  text  collate  nocase ) ;
        CREATE TABLE IF NOT EXISTS [t2] (x, y, unique (x, y), check (x <> y));
        create unique index  i1  on t1 ( b  collate rtrim desc , a );
        """ + SCHEMA)


def test_check_constraints(pair):
    run_all(pair, """
        CREATE TABLE t (a, b CHECK (b > 0), CONSTRAINT pos CHECK (a  >  1 ), CHECK(a<100));
        INSERT INTO t VALUES (5, 0);
        INSERT INTO t VALUES (0, 5);
        INSERT INTO t VALUES (500, 5);
        INSERT INTO t VALUES (NULL, NULL);
        INSERT INTO t VALUES (5, 5);
        INSERT OR IGNORE INTO t VALUES (0, 1), (6, 6);
        INSERT OR REPLACE INTO t VALUES (0, 1);
        UPDATE t SET b = -b;
        UPDATE OR IGNORE t SET a = a - 5;
        SELECT * FROM t;
        CREATE TABLE c1 (a INT CHECK (typeof(a) = 'integer'), b REAL CHECK (typeof(b) = 'real'));
        INSERT INTO c1 VALUES ('5', 1);
        CREATE TABLE c4 (a INTEGER PRIMARY KEY CHECK (a > 10), b);
        INSERT INTO c4 (b) VALUES (1);
        INSERT INTO c4 VALUES (11, 1);
        CREATE TABLE c5 (a CHECK (rowid > 1));
        INSERT INTO c5 VALUES (1);
        CREATE TABLE c7 (a NOT NULL CHECK (a > 0));
        INSERT INTO c7 VALUES (NULL);
        CREATE TABLE c8 (a UNIQUE CHECK (a > 0));
        INSERT INTO c8 VALUES (1);
        INSERT INTO c8 VALUES (1);
        INSERT INTO c8 VALUES (-1);
        CREATE TABLE c9 (a, b, CHECK (a + b > 0) ON CONFLICT IGNORE, CHECK (c9.a IS NOT 7));
        INSERT INTO c9 VALUES (1, 1);
        UPDATE c9 SET a = 7;
        UPDATE c9 SET b = -5;
        SELECT * FROM c9
        """)


def test_check_constraint_errors(pair):
    pair.run("CREATE TABLE e (a CHECK (a > ?))", parameters=(1,))
    for sql in ["CREATE TABLE e (a CHECK ((SELECT 1)))",
                "CREATE TABLE e (a CHECK (count(*)))", "CREATE TABLE e (a CHECK (b > 0))",
                "CREATE TABLE e (a CHECK (nosuch(a)))", "CREATE TABLE e (a CHECK (x.a > 0))",
                "CREATE TABLE e (a CHECK (EXISTS (SELECT 1)))", "CREATE TABLE e (a CHECK (a IN (SELECT 1)))",
                "CREATE TABLE e (a, CHECK (row_number() OVER () > 0))",
                "CREATE TABLE e (a CHECK (e.a > 0), b CHECK (random() IS NOT NULL))"]:
        pair.run(sql)
    pair.run("INSERT INTO e VALUES (1, 1)")


def test_table_constraints_and_automatic_indexes(pair):
    run_all(pair, """
        CREATE TABLE t1 (a UNIQUE, b, c, UNIQUE (b, c), PRIMARY KEY (c), UNIQUE (a));
        CREATE TABLE t2 (a INTEGER, b, PRIMARY KEY (a DESC));
        INSERT INTO t2 VALUES (5, 1);
        INSERT INTO t2 VALUES ('x', 1);
        SELECT rowid, * FROM t2;
        CREATE TABLE t3 (a INTEGER PRIMARY KEY DESC, b);
        INSERT INTO t3 VALUES (5, 1);
        SELECT rowid, * FROM t3;
        CREATE TABLE t4 (a UNIQUE ON CONFLICT IGNORE PRIMARY KEY, b);
        CREATE TABLE t5 (a INTEGER PRIMARY KEY UNIQUE);
        CREATE TABLE t6 (a INTEGER, b, UNIQUE (a), PRIMARY KEY (a));
        INSERT INTO t6 VALUES ('x', 1);
        CREATE TABLE t7 (a, b, c, PRIMARY KEY (a, b) ON CONFLICT REPLACE, UNIQUE (b, c));
        INSERT INTO t7 VALUES (1, 'x', 1);
        INSERT INTO t7 VALUES (1, 'x', 2);
        INSERT INTO t7 VALUES (2, 'x', 2);
        SELECT * FROM t7;
        CREATE TABLE t8 (a, b CONSTRAINT k UNIQUE, CONSTRAINT p PRIMARY KEY (a) CONSTRAINT q CHECK (a > b));
        INSERT INTO t8 VALUES (1, 2);
        CREATE TABLE t9 (a, b, UNIQUE (a, a));
        CREATE TABLE t10 (a CONSTRAINT foo, b DEFAULT 1 NOT NULL DEFAULT 2);
        INSERT INTO t10 (a) VALUES (1);
        SELECT * FROM t10;
        """ + SCHEMA)


def test_create_table_errors(pair):
    for sql in ["CREATE TABLE e (a, PRIMARY KEY (x))", "CREATE TABLE e (a, UNIQUE (x))",
                "CREATE TABLE e (a PRIMARY KEY, b, PRIMARY KEY (b))",
                "CREATE TABLE e (a INTEGER PRIMARY KEY, b PRIMARY KEY)",
                "CREATE TABLE e (a REFERENCES p (x, y))", "CREATE TABLE e (a, b, FOREIGN KEY (a, b) REFERENCES p (x))",
                "CREATE TABLE e (a, FOREIGN KEY (z) REFERENCES p (x))", "CREATE TABLE e (a TEXT COLLATE foo)",
                "CREATE TABLE e (a PRIMARY KEY ON CONFLICT IGNORE UNIQUE ON CONFLICT REPLACE)",
                "CREATE TABLE e (a UNIQUE ON CONFLICT IGNORE, UNIQUE (a) ON CONFLICT REPLACE)",
                "CREATE TABLE e (a INT PRIMARY KEY AUTOINCREMENT)", "CREATE TABLE e (a, UNIQUE (a + 1))",
                "CREATE TABLE e (a, b INTEGER, PRIMARY KEY (b AUTOINCREMENT))"]:
        pair.run(sql)
    pair.run(SCHEMA)


def test_on_conflict_clauses_of_constraints(pair):
    run_all(pair, """
        CREATE TABLE t (a UNIQUE ON CONFLICT REPLACE, b NOT NULL ON CONFLICT IGNORE, c);
        INSERT INTO t VALUES (1, 1, 1);
        INSERT INTO t VALUES (1, 2, 2);
        INSERT INTO t VALUES (2, NULL, 3);
        INSERT OR ABORT INTO t VALUES (1, 3, 3);
        INSERT OR FAIL INTO t VALUES (3, NULL, 3);
        UPDATE t SET b = NULL;
        SELECT * FROM t;
        CREATE TABLE u (id INTEGER PRIMARY KEY ON CONFLICT REPLACE, v UNIQUE ON CONFLICT FAIL);
        INSERT INTO u VALUES (1, 'a'), (2, 'b');
        INSERT INTO u VALUES (1, 'c');
        INSERT INTO u VALUES (3, 'b');
        BEGIN;
        INSERT INTO u VALUES (4, 'd'), (5, 'a'), (6, 'e');
        COMMIT;
        SELECT * FROM u;
        CREATE TABLE w (id INTEGER PRIMARY KEY ON CONFLICT ROLLBACK, v);
        BEGIN;
        INSERT INTO w VALUES (1, 1);
        INSERT INTO w VALUES (1, 2);
        SELECT * FROM w;
        CREATE TABLE r (a INTEGER PRIMARY KEY ON CONFLICT REPLACE, b UNIQUE);
        INSERT INTO r VALUES (1, 1), (2, 2);
        INSERT INTO r VALUES (1, 2);
        UPDATE r SET a = 2 WHERE a = 1;
        SELECT * FROM r
        """)


def test_autoincrement(pair):
    run_all(pair, """
        CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v);
        INSERT INTO t (v) VALUES ('a'), ('b');
        DELETE FROM t WHERE id = 2;
        INSERT INTO t (v) VALUES ('c');
        INSERT INTO t VALUES (10, 'd');
        DELETE FROM t;
        INSERT INTO t (v) VALUES ('e');
        INSERT OR IGNORE INTO t VALUES (11, 'f');
        INSERT INTO t VALUES (-5, 'g');
        SELECT * FROM t;
        SELECT * FROM sqlite_sequence;
        CREATE TABLE u (a, id INTEGER, PRIMARY KEY (id AUTOINCREMENT));
        INSERT INTO u (a) VALUES (1);
        UPDATE sqlite_sequence SET seq = 100 WHERE name = 'u';
        INSERT INTO u (a) VALUES (2);
        SELECT * FROM u;
        ALTER TABLE u RENAME TO uu;
        INSERT INTO uu (a) VALUES (3);
        SELECT * FROM sqlite_sequence;
        DROP TABLE t;
        SELECT * FROM sqlite_sequence;
        DROP TABLE sqlite_sequence;
        CREATE TABLE sqlite_sequence (a);
        INSERT INTO uu VALUES (9223372036854775807, 'max');
        INSERT INTO uu (a) VALUES ('full');
        """ + SCHEMA)


def test_alter_table_edits_the_sql(pair):
    run_all(pair, """
        CREATE TABLE p (id INTEGER PRIMARY KEY, "k" UNIQUE);
        CREATE TABLE t (a INT CHECK (a > 0) , "b" TEXT REFERENCES p (k), c, UNIQUE (a, "b"), FOREIGN KEY (c) REFERENCES p (id)) ;
        CREATE INDEX i ON t (b, a COLLATE nocase DESC);
        CREATE VIEW v AS SELECT a, b FROM t;
        ALTER TABLE t RENAME COLUMN a TO x;
        ALTER TABLE t RENAME COLUMN b TO "y z";
        ALTER TABLE t RENAME COLUMN c TO cc;
        ALTER TABLE p RENAME COLUMN k TO kk;
        ALTER TABLE p RENAME TO pp;
        ALTER TABLE t ADD COLUMN d DEFAULT 5 CHECK (d > 1) ;  ;
        ALTER TABLE t DROP COLUMN d;
        ALTER TABLE t ADD e;
        ALTER TABLE t DROP COLUMN cc;
        ALTER TABLE t DROP COLUMN x;
        INSERT INTO t (x, "y z", e) VALUES (1, 'a', 2);
        INSERT INTO t (x) VALUES (0);
        ALTER TABLE t ADD COLUMN f CHECK (f IS NULL OR f > 2);
        ALTER TABLE t ADD COLUMN g DEFAULT 1 CHECK (g > 2);
        ALTER TABLE t ADD COLUMN g DEFAULT 3 CHECK (g > 2) COLLATE nocase;
        ALTER TABLE t ADD COLUMN h COLLATE nosuch;
        CREATE TABLE d1 (a, b, UNIQUE (a));
        ALTER TABLE d1 DROP COLUMN a;
        CREATE TABLE d2 (a, b, PRIMARY KEY (a));
        ALTER TABLE d2 DROP COLUMN a;
        CREATE TABLE d3 (a UNIQUE, b);
        ALTER TABLE d3 DROP COLUMN a;
        CREATE TABLE d4 (a, b, c CHECK (c > 0) REFERENCES d1 (b));
        ALTER TABLE d4 DROP COLUMN c;
        CREATE TABLE d5 (a);
        ALTER TABLE d5 ADD COLUMN b;
        ALTER TABLE d5 DROP COLUMN a;
        ALTER TABLE d5 RENAME COLUMN b TO [x y];
        ALTER TABLE d5 RENAME COLUMN [x y] TO `z`;
        ALTER TABLE d5 RENAME COLUMN z TO w;
        CREATE TABLE self (id INTEGER PRIMARY KEY, parent REFERENCES self (id));
        ALTER TABLE self RENAME COLUMN id TO ident;
        ALTER TABLE self RENAME TO family;
        SELECT * FROM t;
        SELECT * FROM v;
        """ + SCHEMA)
