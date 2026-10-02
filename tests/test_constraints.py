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
    # An unnamed CHECK is named after its text, which SQLite dequotes: a
    # leading quoted part is all that remains (Northwind's [UnitPrice]>=(0)).
    pair.run("CREATE TABLE q (x CHECK ([x]>=(0)), y CHECK (\"y\" > 0 OR y < -5), z CHECK ('a''b' != z), "
             "w CHECK ( `w` > 1 ))")
    for sql in ["INSERT INTO q VALUES (-1, 1, 1, 2)", "INSERT INTO q VALUES (1, -1, 1, 2)",
                "INSERT INTO q VALUES (1, 1, 'a''b', 2)", "INSERT INTO q VALUES (1, 1, 1, 0)"]:
        pair.run(sql)


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


def test_not_null_rowid_alias(pair):
    """NULL into an INTEGER PRIMARY KEY NOT NULL (also a table-level PRIMARY
    KEY, as Chinook declares them) means a new row id, not a NOT NULL error."""
    for sql in ["CREATE TABLE a (id INTEGER PRIMARY KEY NOT NULL, n)",
                "CREATE TABLE b ([id] INTEGER NOT NULL ON CONFLICT IGNORE, n, CONSTRAINT [pk] PRIMARY KEY ([id]))",
                "INSERT INTO a (n) VALUES ('x')", "INSERT INTO a VALUES (NULL, 'y')",
                "INSERT OR REPLACE INTO a VALUES (NULL, 'z')", "UPDATE a SET id = NULL WHERE id = 1",
                "UPDATE OR IGNORE a SET id = NULL", "SELECT rowid, * FROM a", "INSERT INTO b (n) VALUES ('x')",
                "INSERT INTO b VALUES (NULL, 'x')", "UPDATE b SET id = NULL", "SELECT rowid, * FROM b"]:
        pair.run(sql)


def test_not_null_in_two_passes(pair):
    """SQLite checks NOT NULL in two passes: in column order, a REPLACE
    column with a default gets it and the others are checked; then a REPLACE
    column still NULL fails, as ABORT."""
    for sql in ["CREATE TABLE t (a NOT NULL ON CONFLICT REPLACE DEFAULT NULL, b NOT NULL ON CONFLICT ROLLBACK, "
                "c NOT NULL ON CONFLICT REPLACE DEFAULT 5, d NOT NULL)", "BEGIN",
                "INSERT INTO t VALUES (NULL, 1, NULL, 1)", "INSERT INTO t VALUES (1, 1, NULL, NULL)",
                "INSERT INTO t VALUES (NULL, NULL, NULL, 1)", "SELECT * FROM t", "BEGIN",
                "INSERT OR FAIL INTO t VALUES (NULL, NULL, 1, 1)", "INSERT INTO t VALUES (1, 2, NULL, 3)",
                "SELECT * FROM t"]:
        pair.run(sql)


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


# ---- collations ------------------------------------------------------------------

COLLATION_DATA = """
    CREATE TABLE t (a TEXT COLLATE NOCASE, b TEXT, c TEXT COLLATE RTRIM, n);
    INSERT INTO t VALUES ('abc', 'ABC', 'x ', 1), ('ABC', 'abc', 'x', 2), ('b', 'B', 'y  ', 3),
        ('Abc', '_', 'x  ', 4), ('_', 'b', NULL, 5), (NULL, 'Ab', 'y', 6), (10, 'é', 'É ', 7), ('É', 'É', 'é', 8);
    """


@pytest.mark.parametrize("sql", [
    "SELECT a = b, b = a, a = b COLLATE binary, b COLLATE rtrim = a, a COLLATE nocase = c FROM t",
    "SELECT (a || '') = 'ABC', (a COLLATE binary || '') = 'abc', a + 0 = 'ABC', -a, +a = 'ABC' FROM t",
    "SELECT cast(a AS text) = 'ABC', (a) = 'ABC', c = 'x', c = 'y', c < 'x', c >= 'y ' FROM t",
    "SELECT 'A' COLLATE nocase || 'b' = 'aB', 'x' = 'X' COLLATE nocase COLLATE binary, 'a' < 'B' COLLATE nocase",
    "SELECT 'a' IN ('A' COLLATE nocase), 'a' COLLATE nocase IN ('A', 'z'), 'a' IN ('A' COLLATE nocase, 'z')",
    "SELECT a IN ('ABC', 'x'), b IN ('abc', 'x'), c IN ('x', 'z'), a NOT IN ('B', 'Z') FROM t",
    "SELECT b IN (SELECT a FROM t), a IN (SELECT b FROM t), 'abc' IN (SELECT a FROM t) FROM t",
    "SELECT 'a' IN (SELECT 'A' COLLATE nocase), 'a' IN (SELECT 'b' UNION SELECT 'A' COLLATE nocase)",
    "SELECT 'a' IN (SELECT 'A' COLLATE nocase UNION SELECT 'b')",
    "SELECT a BETWEEN 'AAA' AND 'ABD', b BETWEEN 'a' AND 'b' COLLATE nocase FROM t",
    "SELECT CASE a WHEN 'ABC' THEN 1 ELSE 0 END, CASE 'ABC' WHEN a THEN 1 END, CASE b WHEN 'abc' COLLATE nocase THEN 1 END FROM t",
    "SELECT a LIKE 'ABC', a GLOB 'A*' FROM t",
    "SELECT max(a, 'Z'), min(b, 'aaa'), nullif(a, 'ABC'), min('B' COLLATE nocase, 'a'), max(b, a) FROM t",
    "SELECT (SELECT a FROM t LIMIT 1) = 'ABC'",
    "SELECT * FROM t t1 JOIN t t2 ON t1.a = t2.b",
    "SELECT t1.n, t2.n FROM t t1 JOIN t t2 ON t2.b = t1.a",
    "SELECT x FROM (SELECT a AS x FROM t) WHERE x = 'ABC'",
    "SELECT x FROM (SELECT b AS x FROM t UNION SELECT a FROM t) WHERE x = 'abc'",
    "SELECT x FROM (SELECT a AS x FROM t UNION ALL SELECT b FROM t) WHERE x = 'abc'",
    "SELECT x FROM (SELECT 'a' COLLATE nocase AS x UNION SELECT 'b') WHERE x = 'A'",
    "SELECT x FROM (SELECT 'a' AS x UNION SELECT 'b' COLLATE nocase) WHERE x = 'A'",
    "WITH w(x) AS (SELECT a FROM t) SELECT x FROM w WHERE x = 'abc'",
    "SELECT a FROM t UNION SELECT b FROM t", "SELECT b FROM t UNION SELECT a FROM t",
    "SELECT b FROM t INTERSECT SELECT a FROM t", "SELECT a FROM t EXCEPT SELECT 'ABC'",
    "SELECT a FROM t INTERSECT SELECT 'ABC'", "SELECT b FROM t EXCEPT SELECT 'B' COLLATE nocase",
    "SELECT c FROM t UNION SELECT 'x'", "SELECT a FROM t UNION SELECT b FROM t ORDER BY 1 DESC",
    "SELECT a, n FROM t UNION SELECT b, n FROM t ORDER BY 1 COLLATE binary, 2",
    "SELECT a FROM t ORDER BY a, n", "SELECT b FROM t ORDER BY b COLLATE nocase, n", "SELECT c FROM t ORDER BY c, n DESC",
    "SELECT a FROM t ORDER BY a DESC, n", "SELECT a COLLATE binary FROM t ORDER BY 1",
    "SELECT a FROM t ORDER BY a COLLATE binary", "SELECT a AS z FROM t ORDER BY z COLLATE binary",
    "SELECT a AS z FROM t ORDER BY 1 COLLATE rtrim, n",
    "SELECT DISTINCT a FROM t", "SELECT DISTINCT c FROM t", "SELECT DISTINCT a, b COLLATE nocase FROM t",
    "SELECT a, count(*) FROM t GROUP BY a", "SELECT a, b, count(*) FROM t GROUP BY a",
    "SELECT a, b, max(n) FROM t GROUP BY a", "SELECT a FROM t GROUP BY a COLLATE binary",
    "SELECT a, b FROM t GROUP BY 1", "SELECT b, count(*) FROM t GROUP BY 1 COLLATE nocase",
    "SELECT x, count(*) FROM (SELECT a AS x FROM t) GROUP BY x", "SELECT c, count(*) FROM t GROUP BY c",
    "SELECT max(a), min(b), max(b COLLATE nocase), min(c), count(DISTINCT a), count(DISTINCT c) FROM t",
    "SELECT group_concat(DISTINCT a), sum(DISTINCT a), group_concat(DISTINCT b COLLATE nocase) FROM t",
    "SELECT min(a) FROM (SELECT 'b' COLLATE nocase AS a UNION ALL SELECT 'B')",
    "SELECT group_concat(a) OVER (PARTITION BY a ORDER BY n), max(b) OVER (ORDER BY a ROWS 1 PRECEDING) FROM t",
    "SELECT a, row_number() OVER (ORDER BY a, n), rank() OVER (ORDER BY a), max(a) OVER () FROM t",
    "SELECT a, min(a) OVER (ORDER BY n ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t",
    "SELECT a COLLATE foo = 'x' FROM t", "SELECT 'x' COLLATE foo", "SELECT * FROM (SELECT 'x' COLLATE foo)",
    "SELECT 'x' COLLATE foo UNION SELECT 'y'", "SELECT 'x' COLLATE foo UNION ALL SELECT 'y'",
    "SELECT DISTINCT 'x' COLLATE foo", "SELECT 1 IN (SELECT 'x' COLLATE foo)",
])
def test_collation_semantics(pair, sql):
    run_all(pair, COLLATION_DATA)
    pair.run(sql)


def test_collated_indexes(pair):
    run_all(pair, COLLATION_DATA + """;
        CREATE INDEX ta ON t (a);
        CREATE INDEX tb ON t (b COLLATE nocase);
        CREATE INDEX tc ON t (c, n);
        CREATE TABLE u (k TEXT UNIQUE COLLATE nocase, v, UNIQUE (v COLLATE rtrim));
        INSERT INTO u VALUES ('x', 'a');
        INSERT INTO u VALUES ('X', 'b');
        INSERT INTO u VALUES ('y', 'a  ');
        INSERT OR REPLACE INTO u VALUES ('X', 'c');
        UPDATE u SET k = 'Y' WHERE k = 'X';
        SELECT * FROM u;
        CREATE UNIQUE INDEX ub ON t (b);
        CREATE UNIQUE INDEX ua ON t (a);
        DELETE FROM t WHERE a = 'ABC';
        SELECT * FROM t ORDER BY n
        """)
    for sql in [
        "SELECT n FROM t WHERE a = 'ABC'", "SELECT n FROM t WHERE a = 'ABC' COLLATE binary",
        "SELECT n FROM t WHERE b = 'ab'", "SELECT n FROM t WHERE b COLLATE nocase = 'ab'",
        "SELECT n FROM t WHERE b > 'A' COLLATE nocase", "SELECT n FROM t WHERE a > 'B' AND a < 'z'",
        "SELECT n FROM t WHERE c = 'y'", "SELECT n FROM t WHERE c = 'y' AND n > 2",
        "SELECT n FROM t WHERE a IN ('B', 'abc')", "SELECT a, n FROM t ORDER BY a LIMIT 3",
        "SELECT a, n FROM t ORDER BY a COLLATE binary LIMIT 3", "SELECT b, n FROM t ORDER BY b COLLATE nocase LIMIT 3",
        "SELECT b FROM t ORDER BY b", "SELECT a FROM t WHERE a > 'a'", "SELECT k FROM u WHERE k = 'x'",
    ]:
        pair.run(sql)
        theirs = [r[-1] for r in pair.lite.execute("EXPLAIN QUERY PLAN " + sql)
                  if not r[-1].startswith("USE TEMP B-TREE")]  # (MiniDB shows the tables only)
        mine = [r[-1] for r in pair.mini.execute("EXPLAIN QUERY PLAN " + sql)]
        mine = [p.split("(", 1)[1].split(";")[0] if p.startswith("MULTI-INDEX IN") else p
                for p in mine]  # (SQLite shows IN as one search)
        assert [" ".join(p.split(" ")[:1] + p.split(" ")[2:]) if p.startswith(("SCAN", "SEARCH")) else p
                for p in theirs] == [p.replace("minidb_autoindex", "sqlite_autoindex") for p in mine], sql


def test_sqlite_checks_collated_indexes_minidb_wrote(tmp_path):
    """The records of a NOCASE / RTRIM index hold the values themselves, in
    the order SQLite's collations give."""
    import sqlite3
    from contextlib import closing

    from minidb.database import Database

    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        db.execute("CREATE TABLE t (a TEXT COLLATE nocase UNIQUE, b COLLATE rtrim, c)")
        db.execute("CREATE INDEX tb ON t (b, c COLLATE nocase DESC)")
        words = ["Apple", "apricot", "BANANA", "banana split", "_x", "Zeta", "zebra", "é", "É", "a  ", "A b"]
        for i, word in enumerate(words):
            db.execute("INSERT INTO t VALUES (?, ?, ?)", (word, word + " " * (i % 3), word.swapcase()))
        db.execute("DELETE FROM t WHERE a = 'BANANA'")
        db.execute("UPDATE t SET b = upper(b) WHERE a > 'y'")
        rows = db.execute("SELECT a, b, c FROM t ORDER BY a")
        assert db.execute("SELECT a FROM t WHERE a = 'APPLE'") == [("Apple",)]
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("SELECT a, b, c FROM t ORDER BY a").fetchall() == rows
        assert connection.execute("SELECT a FROM t INDEXED BY sqlite_autoindex_t_1 WHERE a = 'zeta'").fetchall() == [
            ("Zeta",)]


def test_row_id_and_flattened_subqueries_have_no_collation(pair):
    """A row id (an INTEGER PRIMARY KEY too) has no collation, so the next
    argument of a multi-argument max() / min() picks it; a bare column of a
    subquery SQLite flattens is that column, any other expression of it has
    BINARY (SQLite's substExpr)."""
    for sql in [
        'CREATE TABLE t0 (id INTEGER PRIMARY KEY, c1, c2 COLLATE NOCASE)',
        "INSERT INTO t0 VALUES (1, 'x', 'AbC'), (2, 'y', 'ab%'), (3, 'abc', 'B')",
        "SELECT max(id, CASE WHEN 0x7f THEN 'ab%' WHEN 5 THEN c1 END, c2) FROM t0",
        "SELECT max(id, 'ab%', c2), max(rowid, 'ab%', c2), max(c1, 'ab%', c2) FROM t0",
        "SELECT min(id, 'ab%', c2), min(t0.rowid, c2, 'ab%') FROM t0",
        "SELECT max(a.id, 'ab%', b.c2) FROM t0 a JOIN t0 b USING (id)",
        "SELECT max(id, 'ab%', c2) FROM t0 a NATURAL JOIN t0 b",
        "SELECT max(x, 'ab%', y) FROM (SELECT id x, c2 y FROM t0)",
        "SELECT max(x, 'ab%', y) FROM (SELECT rowid x, c2 y FROM t0)",
        "SELECT max(x, 'ab%', y) FROM (SELECT +id x, c2 y FROM t0)",
        "SELECT max(x, 'ab%', y) FROM (SELECT * FROM (SELECT id x, c2 y FROM t0))",
        'CREATE VIEW v AS SELECT id, c2 FROM t0',
        "SELECT max(id, 'ab%', c2) FROM v",
        "SELECT x, y FROM (SELECT c1 || '' x, c2 y FROM t0) WHERE x = y",
        "SELECT x, y FROM (SELECT 'abc' x, c2 y FROM t0) WHERE x = y",
        "SELECT x FROM (SELECT 'abc' x, c2 y FROM t0) WHERE x IN (SELECT c2 FROM t0)",
        'SELECT x, y FROM (SELECT c1 x, c2 y FROM t0) WHERE x = y',
        'SELECT a.x FROM (SELECT upper(c1) x FROM t0) a JOIN t0 b ON a.x = b.c2',
        'SELECT b.c2 FROM t0 a LEFT JOIN (SELECT upper(c1) x, c2 FROM t0) b ON b.x = a.c2',
        'WITH c AS (SELECT upper(c1) x FROM t0) SELECT x FROM c, t0 WHERE x = c2',
        'SELECT x, count(*) FROM (SELECT lower(c1) x FROM t0) a JOIN t0 ON x = c2 GROUP BY 1',
        "SELECT x FROM (SELECT id x FROM t0) WHERE x = '1'",
    ]:
        pair.run(sql)


def test_excluded_has_no_collation(pair):
    """An upsert's excluded.x has the column's affinity but no collation:
    the other operand's collation (or BINARY) decides a comparison."""
    for sql in [
        'CREATE TABLE t (id INTEGER PRIMARY KEY, a COLLATE NOCASE, b, n INT)',
        'CREATE TABLE u (k COLLATE RTRIM)',
        "INSERT INTO t VALUES (1, 'x', 'y', 5)",
        "INSERT INTO t VALUES (1, 'ABC', 'q', '7') ON CONFLICT (id) DO UPDATE SET b = (excluded.a = 'abc') || (t.a = 'X') || (excluded.a > 'Zz') || ('Zz' < excluded.a) || ('abc' = excluded.a) || (excluded.n = '7') || (excluded.a COLLATE nocase = 'abc')",
        'SELECT * FROM t',
        "INSERT INTO t VALUES (1, 'ABC', 'q', '7') ON CONFLICT (id) DO UPDATE SET b = ('Zz' BETWEEN excluded.a AND 'zzz') || (excluded.a IN ('abc')) || max(excluded.a, 'abd') || (CASE excluded.a WHEN 'abc' THEN 'y' ELSE 'n' END)",
        'SELECT * FROM t',
        "INSERT INTO t VALUES (1, 'ABC', 'q', '7') ON CONFLICT (id) DO UPDATE SET b = 'w' WHERE excluded.a = 'abc'",
        'SELECT * FROM t',
        "INSERT INTO t VALUES (1, 'X', 'q', 1) ON CONFLICT (id) DO UPDATE SET b = (excluded.a = t.a) || (t.a = excluded.a) || (SELECT count(*) FROM u WHERE k = excluded.a)",
        'SELECT * FROM t',
        "INSERT INTO u VALUES ('X  ')",
        "INSERT INTO t VALUES (1, 'X', 'q', 1) ON CONFLICT (id) DO UPDATE SET b = (SELECT count(*) FROM u WHERE excluded.a = k)",
        'SELECT * FROM t',
    ]:
        pair.run(sql)
