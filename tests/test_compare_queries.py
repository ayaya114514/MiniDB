"""MiniDB vs sqlite3: ORDER BY, LIMIT, aggregates, GROUP BY, HAVING and joins."""

import itertools
import random

import pytest

from minidb import Database
from minidb.errors import NotSupportedError, OperationalError
from sqlcompare import Pair

SETUP = [
    "CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT, dept TEXT, salary INTEGER, bonus INTEGER, boss INTEGER)",
    "CREATE TABLE dept (name TEXT UNIQUE, floor INTEGER, budget INTEGER)",
    """INSERT INTO emp VALUES
        (1, 'ann', 'eng', 120, 10, NULL),
        (2, 'bob', 'eng', 100, NULL, 1),
        (3, 'cat', 'ops', 90, 5, 1),
        (4, 'dan', 'ops', 90, 7, 3),
        (5, 'eve', NULL, 70, NULL, 3),
        (6, 'fay', 'eng', 130, 20, 1),
        (7, 'gus', 'hr', NULL, 1, 6),
        (8, 'hal', 'hr', 60, 2, 7),
        (9, 'ivy', 'sales', 85, 3, NULL),
        (10, 'jon', 'eng', '95', 4, 2)""",
    """INSERT INTO dept VALUES ('eng', 3, 1000), ('ops', 1, 300), ('hr', 2, 200),
        ('legal', 4, 150), (NULL, 5, 10)""",
]


@pytest.fixture
def pair():
    p = Pair()
    p.script(SETUP)
    yield p
    p.close()


def test_order_by(pair):
    pair.script([
        "SELECT * FROM emp ORDER BY salary, id",
        "SELECT * FROM emp ORDER BY salary DESC, id DESC",
        "SELECT name, dept FROM emp ORDER BY dept, name",
        "SELECT name, dept FROM emp ORDER BY dept DESC, name ASC",
        "SELECT name FROM emp ORDER BY bonus NULLS LAST, id",
        "SELECT name FROM emp ORDER BY bonus DESC NULLS FIRST, id",
        "SELECT name, salary + bonus AS total FROM emp ORDER BY total, name",
        "SELECT name, salary FROM emp ORDER BY 2 DESC, 1",
        "SELECT name AS dept, dept AS name FROM emp ORDER BY dept",
        "SELECT name FROM emp ORDER BY length(name) * -1, name",
        "SELECT name FROM emp ORDER BY boss IS NULL, boss, id",
        "SELECT name FROM emp ORDER BY 'constant', id",
        "SELECT id FROM emp ORDER BY id % 3, id DESC",
        "SELECT DISTINCT dept FROM emp ORDER BY dept",
        "SELECT DISTINCT salary FROM emp ORDER BY 1 DESC",
        "SELECT name FROM emp WHERE salary > 80 ORDER BY salary DESC, name",
        "SELECT name FROM emp ORDER BY 0",
        "SELECT name FROM emp ORDER BY 2",
        "SELECT name FROM emp ORDER BY -1",
        "SELECT name, salary FROM emp ORDER BY 3",
        "SELECT name FROM emp ORDER BY nosuch",
    ])


def test_limit_and_offset(pair):
    for limit, offset in itertools.product(["0", "1", "3", "10", "20", "-1", "'2'", "2.0", "1 + 1"],
                                           [None, "0", "2", "9", "-3", "'1'"]):
        suffix = f" LIMIT {limit}" + (f" OFFSET {offset}" if offset is not None else "")
        pair.run("SELECT id, name FROM emp ORDER BY id" + suffix)
    pair.script([
        "SELECT id FROM emp ORDER BY id LIMIT 2, 3",
        "SELECT id FROM emp ORDER BY salary DESC, id LIMIT 3",
        "SELECT id FROM emp ORDER BY id LIMIT 2.5",
        "SELECT id FROM emp ORDER BY id LIMIT NULL",
        "SELECT id FROM emp ORDER BY id LIMIT 'x'",
        "SELECT id FROM emp ORDER BY id LIMIT 3 OFFSET 2.5",
        "SELECT count(*) FROM emp LIMIT 1",
        "SELECT count(*) FROM emp LIMIT 0",
    ])


def test_aggregates(pair):
    columns = ["id", "name", "dept", "salary", "bonus", "boss"]
    for column in columns:
        pair.run(
            f"SELECT count({column}), count(DISTINCT {column}), sum({column}), total({column}), "
            f"avg({column}), min({column}), max({column}), sum(DISTINCT {column}), "
            f"avg(DISTINCT {column}) FROM emp"
        )
    pair.script([
        "SELECT count(*), count() FROM emp",
        "SELECT count(*), sum(salary), avg(salary), min(name), max(name) FROM emp WHERE 0",
        "SELECT count(*) FROM emp WHERE salary > 100",
        "SELECT sum(salary * 2) + 1, max(salary) - min(salary), avg(bonus) * count(bonus) FROM emp",
        "SELECT max(salary), name FROM emp",
        "SELECT min(salary), name FROM emp",
        "SELECT sum(salary) / count(salary), sum(salary) * 1.0 / count(salary) FROM emp",
        "SELECT group_concat(name) FROM emp",
        "SELECT group_concat(name, '; '), group_concat(DISTINCT dept) FROM emp",
        "SELECT sum(9223372036854775807) FROM emp",
        "SELECT sum(salary) FROM emp WHERE salary > 1000",
        "SELECT total(salary) FROM emp WHERE salary > 1000",
        "SELECT count(*) FROM emp WHERE count(*) > 1",
        "SELECT sum(count(*)) FROM emp",
        "SELECT count(name, dept) FROM emp",
        "SELECT sum(*) FROM emp",
        "SELECT min() FROM emp",
        "SELECT count(*) FROM emp HAVING count(*) > 5",
        "SELECT count(*) FROM emp HAVING count(*) > 50",
        "SELECT name FROM emp HAVING name > 'a'",
    ])


def test_float_sums_match_sqlite():
    pair = Pair()
    pair.run("CREATE TABLE f (g INTEGER, x INTEGER)")
    rng = random.Random(4)
    rows = []
    for _ in range(400):
        value = rng.choice([
            str(rng.randint(-10**6, 10**6)),
            repr(rng.uniform(-1000, 1000)),
            repr(rng.uniform(-1e-3, 1e-3)),
            f"'{rng.randint(0, 99)}'",
            f"'{rng.uniform(0, 5):.3f}'",
            "'abc'",
            "NULL",
            str(rng.choice([2**62, -(2**62), 2**53 + 1])),
            repr(rng.choice([1e16, -1e16, 0.1, 1e300])),
        ])
        rows.append(f"({rng.randint(0, 9)}, {value})")
    pair.run("INSERT INTO f VALUES " + ", ".join(rows))
    pair.run("SELECT g, sum(x), avg(x), total(x), count(x) FROM f GROUP BY g")
    pair.run("SELECT sum(x), avg(x), total(x) FROM f")
    pair.run("SELECT g, sum(x) FROM f WHERE typeof(x) = 'integer' GROUP BY g")
    pair.close()


def test_group_by(pair):
    pair.script([
        "SELECT dept, count(*) FROM emp GROUP BY dept",
        "SELECT dept, count(*), sum(salary), avg(salary), min(name), max(bonus) FROM emp GROUP BY dept",
        "SELECT dept, count(*) AS n FROM emp GROUP BY dept ORDER BY n DESC, dept",
        "SELECT dept, sum(salary) FROM emp GROUP BY 1 ORDER BY 2, 1",
        "SELECT dept AS d, count(*) FROM emp GROUP BY d ORDER BY d",
        "SELECT dept, boss IS NULL, count(*) FROM emp GROUP BY dept, boss IS NULL",
        "SELECT salary % 20, count(*) FROM emp GROUP BY salary % 20",
        "SELECT count(*) FROM emp GROUP BY dept",
        "SELECT dept, max(salary), name FROM emp GROUP BY dept ORDER BY dept",
        "SELECT dept, min(salary), name FROM emp WHERE dept != 'ops' GROUP BY dept ORDER BY dept",
        "SELECT dept FROM emp GROUP BY dept HAVING count(*) > 1 ORDER BY dept",
        "SELECT dept, count(*) FROM emp GROUP BY dept HAVING sum(salary) > 200 OR dept IS NULL",
        "SELECT dept FROM emp GROUP BY dept HAVING max(bonus) > 5",
        "SELECT dept, avg(salary) FROM emp WHERE salary > 60 GROUP BY dept HAVING avg(salary) > 80 ORDER BY avg(salary)",
        "SELECT dept, count(*) FROM emp GROUP BY dept ORDER BY count(*) DESC, dept LIMIT 2",
        "SELECT DISTINCT count(*) FROM emp GROUP BY dept",
        "SELECT dept, count(*) FROM emp WHERE 0 GROUP BY dept",
        "SELECT dept FROM emp GROUP BY count(*)",
        "SELECT dept FROM emp GROUP BY 7",
        "SELECT boss, count(*) FROM emp GROUP BY boss ORDER BY boss",
        "SELECT dept, group_concat(name) FROM emp GROUP BY dept ORDER BY dept",
    ])


def test_group_by_type_equality():
    pair = Pair()
    pair.script([
        "CREATE TABLE g (k TEXT, v INTEGER, n INTEGER)",
        "INSERT INTO g (k, n) VALUES ('1', 1), ('1.0', 2), ('a', 3), (NULL, 4), (NULL, 5)",
        "INSERT INTO g (v, n) VALUES (1, 6), (1.0, 7), ('1', 8), (2.5, 9), ('x', 10)",
        "SELECT k, count(*), sum(n) FROM g GROUP BY k",
        "SELECT v, count(*), sum(n) FROM g GROUP BY v",
        "SELECT k, v, count(*) FROM g GROUP BY k, v",
        "SELECT count(DISTINCT v), count(DISTINCT k) FROM g",
    ])
    pair.close()


def test_joins(pair):
    pair.script([
        "SELECT emp.name, dept.floor FROM emp JOIN dept ON emp.dept = dept.name",
        "SELECT e.name, d.floor FROM emp e INNER JOIN dept AS d ON e.dept = d.name WHERE d.floor > 1",
        "SELECT e.name, d.name, d.floor FROM emp e LEFT JOIN dept d ON e.dept = d.name",
        "SELECT d.name, count(e.id) FROM dept d LEFT JOIN emp e ON e.dept = d.name GROUP BY d.name",
        "SELECT d.name, e.name FROM dept d LEFT JOIN emp e ON e.dept = d.name AND e.salary > 95",
        "SELECT d.name, e.name FROM dept d LEFT JOIN emp e ON e.dept = d.name WHERE e.salary > 95",
        "SELECT d.name, e.name FROM dept d LEFT JOIN emp e ON e.dept = d.name WHERE e.id IS NULL",
        "SELECT * FROM emp, dept WHERE emp.dept = dept.name AND budget < 500",
        "SELECT emp.*, dept.budget FROM emp, dept WHERE emp.dept = dept.name",
        "SELECT count(*) FROM emp CROSS JOIN dept",
        "SELECT count(*) FROM emp, dept",
        "SELECT a.name, b.name FROM emp a JOIN emp b ON a.boss = b.id",
        "SELECT a.name, b.name FROM emp a LEFT JOIN emp b ON a.boss = b.id ORDER BY a.id",
        "SELECT b.name, count(*) FROM emp a JOIN emp b ON a.boss = b.id GROUP BY b.name",
        "SELECT a.name FROM emp a JOIN emp b ON a.boss = b.id WHERE b.boss = 1",
        "SELECT a.name, b.name, c.name FROM emp a JOIN emp b ON a.boss = b.id JOIN emp c ON b.boss = c.id",
        "SELECT e.name, d.budget FROM emp e JOIN dept d ON d.budget > e.salary * 3",
        "SELECT e.name, d.name FROM emp e JOIN dept d ON e.id = d.floor",
        "SELECT e.name, d.name FROM dept d JOIN emp e ON e.id = d.floor",
        "SELECT e.name, d.name FROM dept d JOIN emp e ON e.id > d.floor * 2 AND e.id < d.floor * 2 + 2",
        "SELECT e.name, d.name FROM dept d JOIN emp e ON e.id IN (d.floor, d.floor + 5)",
        "SELECT * FROM emp JOIN dept ON 1 WHERE emp.id = 3",
        "SELECT * FROM emp LEFT JOIN dept ON 0 WHERE emp.id < 3",
        "SELECT name FROM emp, dept",
        "SELECT emp.name, nosuch FROM emp, dept",
        "SELECT x.name FROM emp JOIN dept ON emp.dept = dept.name",
        "SELECT * FROM emp JOIN nosuch ON 1",
        "SELECT e.name, d.floor FROM emp e JOIN dept d ON e.dept = d.name ORDER BY d.floor DESC, e.name LIMIT 4",
        "SELECT DISTINCT d.floor FROM emp e JOIN dept d ON e.dept = d.name",
        "SELECT e.name FROM emp e LEFT JOIN dept d ON e.dept = d.name WHERE d.floor IS NULL OR d.floor > 2",
    ])


def test_random_queries_against_sqlite(pair):
    rng = random.Random(8)
    columns = ["id", "name", "dept", "salary", "bonus", "boss"]
    conditions = [
        "salary > {n}", "bonus IS NULL", "dept = 'eng'", "boss IN (1, 3, {n})", "id BETWEEN 2 AND {n}",
        "name LIKE '%a%'", "salary + bonus < {n}", "NOT (dept = 'ops')", "id % 2 = 0", "boss IS NOT NULL",
    ]
    for _ in range(600):
        where = " AND ".join(rng.sample(conditions, rng.randint(0, 2))).format(n=rng.randint(0, 130))
        where_sql = f" WHERE {where}" if where else ""
        kind = rng.random()
        if kind < 0.4:
            cols = rng.sample(columns, rng.randint(1, 3))
            order = rng.sample(columns, rng.randint(1, 2))
            order_sql = ", ".join(f"{c} {rng.choice(['ASC', 'DESC'])}" for c in order) + ", id"
            limit = f" LIMIT {rng.randint(0, 6)} OFFSET {rng.randint(0, 3)}" if rng.random() < 0.5 else ""
            pair.run(f"SELECT {', '.join(cols)} FROM emp{where_sql} ORDER BY {order_sql}{limit}")
        elif kind < 0.8:
            group = rng.choice(["dept", "boss", "salary > 90", "bonus IS NULL", "id % 3"])
            aggregate = rng.choice(["count(*)", "sum(salary)", "avg(bonus)", "min(name)", "max(salary)",
                                    "count(DISTINCT dept)", "total(bonus)"])
            having = f" HAVING count(*) > {rng.randint(0, 2)}" if rng.random() < 0.3 else ""
            pair.run(f"SELECT {group}, {aggregate} FROM emp{where_sql} GROUP BY {group}{having}")
        else:
            join = rng.choice(["JOIN", "LEFT JOIN"])
            on = rng.choice(["e.dept = d.name", "e.id = d.floor", "e.salary > d.budget / 10"])
            pair.run(f"SELECT e.name, d.name, d.floor FROM emp e {join} dept d ON {on}"
                     + (f" WHERE e.{where}" if where and '(' not in where and ' AND ' not in where else ""))


def test_constant_order_by_terms_follow_sqlite(pair):
    """SQLite treats small integer constants (also under unary +/- and the
    folded ``<literal> IS [NOT] NULL``) as column numbers; other constants
    do not affect the order."""
    for term in ["1", "+1", "-1", "+(2)", "- -2", "-(-2)", "1+1", "(2 IS NULL)", "(2 IS NOT NULL)",
                 "('x' IS NULL)", "(-3 IS NOT NULL) DESC", "(NULL IS NULL)", "'1'", "1.0",
                 "2147483647", "2147483648", "9223372036854775807", "(1 = 1)", "abs(2)",
                 "(salary AND 0)", "(0 AND salary)", "(salary AND 0.0)", "(salary AND -0)",
                 "(abs(salary) AND 0)", "(name LIKE 'a' AND 0)", "((salary AND 0) AND id)",
                 "(salary IN (1, 2) AND 0)", "(-(salary AND 0))", "((0 AND salary) IS NULL)",
                 "((salary AND 0) OR 0)", "(salary AND 0) + 1", "(salary BETWEEN 1 AND 2 AND 0)"]:
        pair.run(f"SELECT name, id FROM emp ORDER BY {term}, id")
        pair.run(f"SELECT dept, count(*) FROM emp GROUP BY {term}")


def test_rowid_after_maximum_is_random():
    db_pair = Pair()
    db_pair.script([
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)",
        "INSERT INTO t VALUES (9223372036854775807, 'max')",
    ])
    db = db_pair.mini
    db.execute("INSERT INTO t (v) VALUES ('a'), ('b')")
    ids = [r[0] for r in db.execute("SELECT id FROM t WHERE v != 'max'")]
    assert len(set(ids)) == 2 and all(0 < i <= 2**62 for i in ids)
    db_pair.close()


def test_sqllogictest_syntax(pair):
    """Forms that SQLite's sqllogictest corpus uses: aggregate(ALL x), empty
    IN lists, x IN table, and parenthesized joins in FROM."""
    pair.run("CREATE TABLE t (a INTEGER, b TEXT)")
    pair.run("CREATE TABLE u (a INTEGER, c REAL)")
    pair.run("CREATE INDEX ua ON u (a)")
    pair.run("INSERT INTO t VALUES (1, 'x'), (2, NULL), (NULL, 'z'), (3, 'x')")
    pair.run("INSERT INTO u VALUES (1, 1.5), (3, 2), (NULL, 0)")
    for sql in [
        "SELECT 1 IN (), 1 NOT IN (), NULL IN (), NULL NOT IN (), typeof(NULL IN ())",
        "SELECT a FROM t WHERE a IN ()", "SELECT a FROM t WHERE a NOT IN ()",
        "SELECT a FROM t WHERE rowid IN ()", "SELECT a FROM u WHERE a IN () OR a = 3",
        "SELECT count(ALL a), sum(ALL a), max(ALL b), count(DISTINCT a), total(ALL a) FROM t",
        "SELECT b, count(ALL a) FROM t GROUP BY b HAVING count(ALL b) > 0",
        "SELECT abs(ALL -3), coalesce(ALL NULL, 2)",
        "SELECT a, 1 IN u, a IN u, a NOT IN u FROM t",
        "SELECT a FROM t WHERE a IN u", "SELECT a FROM t WHERE a NOT IN u",
        "SELECT * FROM (t CROSS JOIN u)",
        "SELECT * FROM (t AS x CROSS JOIN u y) WHERE x.a = y.a",
        "SELECT * FROM (t x JOIN u y ON x.a = y.a)",
        "SELECT * FROM (t x LEFT JOIN u y ON x.a = y.a)",
        "SELECT * FROM (t x LEFT JOIN u y ON x.a = y.a), u z",
        "SELECT * FROM u z, (t x LEFT JOIN u y ON x.a = y.a)",
        "SELECT * FROM u z JOIN (t x LEFT JOIN u y ON x.a = y.a) WHERE z.a = 1",
        "SELECT * FROM ((t))", "SELECT * FROM (t)",
        "SELECT * FROM ((t x CROSS JOIN u) CROSS JOIN u z)",
        "SELECT x.a, count(*) FROM (t x CROSS JOIN u) GROUP BY x.a",
    ]:
        pair.run(sql)


def test_insert_select():
    pair = Pair(check_messages=True)
    pair.run("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT, c REAL)")
    pair.run("CREATE TABLE u (x INTEGER, y TEXT UNIQUE)")
    pair.run("INSERT INTO t VALUES (1, 'x', 1), (2, '2', 2.5), (5, NULL, '3')")
    for sql in [
        "INSERT INTO u SELECT a, b FROM t", "SELECT * FROM u",
        "INSERT INTO u SELECT a, b FROM t",  # UNIQUE: the whole statement fails
        "SELECT * FROM u",
        "INSERT INTO t SELECT a + 10, b, c FROM t",  # reads the table it inserts into
        "INSERT INTO t (b) SELECT b FROM t WHERE a > 3",  # new row ids after the largest
        "SELECT *, typeof(c) FROM t",
        "INSERT INTO t SELECT a FROM t", "INSERT INTO t (a, b) SELECT a FROM t",
        "INSERT INTO u (y, x) SELECT 'k' || a, count(*) FROM t GROUP BY a HAVING a < 3 ORDER BY 1 LIMIT 1",
        "INSERT INTO u SELECT 1, 'z' UNION ALL SELECT 2, 'w'",
        "INSERT INTO u SELECT * FROM (SELECT 3, 'v') WHERE 0",
        "INSERT INTO u SELECT x, y || '!' FROM u WHERE x IN (SELECT a FROM t)",
        "SELECT * FROM u",
        "INSERT INTO t SELECT * FROM t WHERE a = 1",
        "INSERT INTO nowhere SELECT 1", "INSERT INTO u (nope) SELECT 1",
    ]:
        pair.run(sql)
    pair.close()


def test_views():
    pair = Pair(check_messages=True)
    pair.run("CREATE TABLE t (a INTEGER, b TEXT)")
    pair.run("INSERT INTO t VALUES (1, 'x'), (3, 'y'), (2, NULL)")
    for sql in [
        "CREATE VIEW v AS SELECT a, b || '!' FROM t", "SELECT * FROM v ORDER BY a",
        "CREATE VIEW v AS SELECT 1", "CREATE VIEW t AS SELECT 1", "CREATE TABLE v (x)",
        "CREATE VIEW IF NOT EXISTS v AS SELECT 1", "CREATE TABLE IF NOT EXISTS v (x)",
        "CREATE VIEW w (x, y) AS SELECT a, b FROM t WHERE a > 1", "SELECT x, y FROM w ORDER BY x",
        "CREATE VIEW w2 (x) AS SELECT a, b FROM t", "SELECT * FROM w2",  # error only when used
        "CREATE VIEW bad AS SELECT * FROM nosuch", "SELECT * FROM bad",
        "INSERT INTO v VALUES (1, 2)", "UPDATE v SET a = 1", "DELETE FROM v",
        "DROP TABLE v", "DROP VIEW t", "DROP VIEW nosuch", "DROP VIEW IF EXISTS nosuch",
        "CREATE INDEX i ON v (a)", "CREATE INDEX v ON t (a)",
        "CREATE VIEW dup AS SELECT a, a, A AS a FROM t", "SELECT * FROM dup ORDER BY 1",
        "SELECT * FROM (SELECT a, a FROM t) ORDER BY 1",
        "CREATE VIEW top AS SELECT a AS q FROM t ORDER BY a DESC LIMIT 1", "SELECT * FROM top",
        "SELECT * FROM top JOIN w ON q = x", "SELECT v.a, x.a FROM v, v AS x WHERE v.a < x.a ORDER BY 1, 2",
        "SELECT q, (SELECT count(*) FROM v) FROM top WHERE q IN (SELECT a FROM v)",
        "SELECT count(*), sum(a), max(\"b || '!'\") FROM v", "SELECT 3 IN top, 4 IN top",
        "SELECT b, count(*) FROM w GROUP BY b ORDER BY 1",
        "UPDATE t SET a = a + 10 WHERE a IN (SELECT q FROM top)",
        "DELETE FROM t WHERE EXISTS (SELECT 1 FROM w WHERE w.x = t.a AND w.y IS NULL)",
        "SELECT * FROM t ORDER BY a", "INSERT INTO t SELECT a + 100, b FROM v", "SELECT * FROM v ORDER BY a",
        "CREATE VIEW c1 AS SELECT * FROM c2", "CREATE VIEW c2 AS SELECT * FROM c1", "SELECT * FROM c1",
        "CREATE VIEW self AS SELECT * FROM self", "SELECT * FROM self",
        "DROP VIEW v", "SELECT * FROM v", "CREATE VIEW v AS SELECT a * 2 AS a FROM t", "SELECT * FROM v ORDER BY a",
        "CREATE VIEW nested AS SELECT a FROM v WHERE a > 4", "SELECT * FROM nested ORDER BY a",
        "DROP TABLE t", "SELECT * FROM v", "SELECT * FROM nested",
    ]:
        pair.run(sql)
    pair.close()


def test_views_are_stored(tmp_path):
    path = str(tmp_path / "views.db")
    db = Database(path)
    db.execute("CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (5), (-1);"
               "CREATE VIEW v (x) AS SELECT a + 1 FROM t WHERE a > 0")
    db.close()
    db = Database(path)
    result = db.execute("SELECT * FROM v")
    assert result == [(6,)] and result.columns == ["x"]
    assert db.catalog.views["v"].sql == "CREATE VIEW v (x) AS SELECT a + 1 FROM t WHERE a > 0"
    db.execute("DROP VIEW v")
    db.close()
    db = Database(path)
    assert db.catalog.views == {} and db.integrity_check() == []
    db.close()


class StatePair(Pair):
    """Also compares total_changes, the transaction state and last_insert_rowid."""

    def run(self, sql, ordered=None, parameters=None):
        super().run(sql, ordered, parameters)
        assert self.mini.in_transaction == self.lite.in_transaction, sql
        assert self.mini.total_changes == self.lite.total_changes, sql
        assert self.mini.last_insert_rowid == self.lite.execute("SELECT last_insert_rowid()").fetchone()[0], sql


def test_conflict_resolution():
    pair = StatePair(check_messages=True)
    for sql in [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, u TEXT UNIQUE, n INTEGER NOT NULL)",
        "CREATE TABLE p (k TEXT PRIMARY KEY, a INTEGER, b INTEGER, UNIQ INTEGER UNIQUE)",
        "CREATE UNIQUE INDEX pab ON p (a, b)",
        "INSERT INTO t VALUES (1, 'a', 1), (2, 'b', 2)",
        # FAIL keeps the rows before the error, even outside a transaction
        "INSERT OR FAIL INTO t VALUES (3, 'c', 3), (4, 'a', 4), (5, 'e', 5)", "SELECT * FROM t",
        "INSERT OR ABORT INTO t VALUES (6, 'f', 6), (7, 'a', 7)", "SELECT * FROM t",
        "INSERT OR IGNORE INTO t VALUES (6, 'f', 6), (7, 'a', 7), (8, 'h', NULL), (1, 'z', 1), (9, 'i', 9)",
        "INSERT OR REPLACE INTO t VALUES (10, 'a', 10), (2, 'c', 20)",  # deletes rows 1 and 3
        "INSERT OR REPLACE INTO t VALUES (11, 'k', NULL)",  # NOT NULL without a default: an error
        "REPLACE INTO t VALUES (12, 'f', 12)", "REPLACE INTO t (u, n) SELECT u || '2', n FROM t",
        "SELECT * FROM t",
        "BEGIN", "INSERT INTO t VALUES (13, 'm', 13)", "INSERT OR ROLLBACK INTO t VALUES (14, 'm', 14)",
        "SELECT * FROM t", "COMMIT",
        "BEGIN", "INSERT INTO t VALUES (13, 'm', 13)", "INSERT OR FAIL INTO t VALUES (15, 'o', 15), (16, 'm', 16)",
        "INSERT OR ABORT INTO t VALUES (17, 'q', 17), (18, 'm', 18)", "COMMIT", "SELECT * FROM t",
        "UPDATE OR IGNORE t SET u = 'a' WHERE id = 13", "UPDATE OR IGNORE t SET n = NULL WHERE id = 13",
        "UPDATE OR REPLACE t SET u = 'a' WHERE id = 13", "UPDATE OR REPLACE t SET id = 15 WHERE id = 13",
        "UPDATE OR FAIL t SET u = 'o' WHERE id = 12", "SELECT * FROM t",
        "BEGIN", "UPDATE OR ROLLBACK t SET u = 'o' WHERE id = 12", "SELECT * FROM t",
        "INSERT INTO p VALUES ('x', 1, 1, 1), ('y', 1, 2, 2), ('z', NULL, 1, NULL), ('w', NULL, 1, NULL)",
        "INSERT OR IGNORE INTO p VALUES ('x', 9, 9, 9), ('v', 1, 1, 9), ('u', 5, 5, 1), ('t', 5, 5, 5)",
        "INSERT OR REPLACE INTO p VALUES ('s', 1, 2, 1)",  # conflicts with two different rows
        "SELECT * FROM p ORDER BY k",
        "UPDATE OR REPLACE p SET a = 5, b = 5 WHERE k = 's'", "SELECT * FROM p ORDER BY k",
    ]:
        pair.run(sql)
    pair.close()


def test_upsert_and_returning():
    pair = StatePair(check_messages=True)
    for sql in [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, u TEXT UNIQUE, n INTEGER NOT NULL)",
        "CREATE TABLE c (word TEXT PRIMARY KEY, hits INTEGER)",
        "INSERT INTO t VALUES (13, 'm', 13), (20, 'a', 1)",
        "INSERT INTO t VALUES (13, 'x', 1) ON CONFLICT DO NOTHING",
        "INSERT INTO t VALUES (13, 'x', 1) ON CONFLICT (id) DO UPDATE SET n = n + excluded.n, u = excluded.u || u",
        "INSERT INTO t VALUES (21, 'a', 1) ON CONFLICT (u) DO UPDATE SET n = 99 WHERE excluded.n > 5",
        "INSERT INTO t VALUES (20, 'a', 7) ON CONFLICT (u) DO UPDATE SET n = 99 WHERE excluded.n > 5",
        "INSERT INTO t VALUES (20, 'a', 7) ON CONFLICT (n) DO NOTHING",
        "INSERT INTO t VALUES (22, 'q', 7) ON CONFLICT (u) DO UPDATE SET n = NULL",
        "INSERT INTO t VALUES (13, 'y', 1) ON CONFLICT (u) DO NOTHING",
        "INSERT INTO t VALUES (13, 'x', 1) ON CONFLICT (u) DO NOTHING ON CONFLICT DO UPDATE SET n = -1 RETURNING *",
        "INSERT INTO t VALUES (13, 'x', 1) ON CONFLICT (id) DO UPDATE SET id = 20",
        "INSERT INTO t VALUES (13, 'a', 1) ON CONFLICT (id) DO UPDATE SET n = 5",
        "INSERT OR REPLACE INTO t VALUES (13, 'a', 1) ON CONFLICT (id) DO NOTHING",
        "INSERT INTO t VALUES (50, 'zz', 1) ON CONFLICT (u) DO UPDATE SET n = excluded.n RETURNING *",
        "INSERT INTO t VALUES (51, 'zz', 5) ON CONFLICT (u) DO UPDATE SET n = t.n + excluded.n RETURNING rowid, *",
        "INSERT INTO t VALUES (51, 'zz', 5) ON CONFLICT (u) DO UPDATE SET n = n + 1 WHERE t.id = 50",
        "INSERT INTO t VALUES (52, 'zz', 5) ON CONFLICT (u) DO UPDATE SET zz = 1",
        "INSERT INTO t VALUES (52, 'zz', 5) ON CONFLICT (u) DO UPDATE SET n = excluded.nope",
        "INSERT INTO t SELECT 40, 'y', 1 WHERE 1 ON CONFLICT DO NOTHING",
        "SELECT * FROM t",
        "INSERT INTO c VALUES ('a', 1), ('b', 1), ('a', 1), ('a', 1) ON CONFLICT (word) DO UPDATE SET hits = hits + 1",
        "SELECT * FROM c",
        "INSERT INTO t VALUES (30, 'r', 1), (31, 's', 2) RETURNING id * 2 AS dbl, u, typeof(n)",
        "INSERT INTO t (u, n) VALUES ('t', '3') RETURNING id, n, typeof(n)",
        "UPDATE t SET n = n + 1 WHERE id >= 30 RETURNING *, rowid",
        "UPDATE t SET n = n + 1 WHERE id = -5 RETURNING *",
        "DELETE FROM t WHERE id >= 30 RETURNING id, n", "INSERT OR IGNORE INTO t VALUES (13, 'x', 1) RETURNING id",
        "INSERT INTO t VALUES (60, 'aa', 1) RETURNING nope", "DELETE FROM t RETURNING upper(u), (SELECT count(*) FROM c)",
        "SELECT * FROM t",
    ]:
        pair.run(sql)
    pair.close()


STATEMENT_JOURNAL_TABLES = [
    "CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "CREATE TABLE t (id INTEGER PRIMARY KEY, c UNIQUE)",
    "CREATE TABLE t (id INTEGER PRIMARY KEY, c NOT NULL)", "CREATE TABLE t (c, d UNIQUE)",
]
STATEMENT_JOURNAL_INSERTS = [
    "INSERT INTO t VALUES (7, 1), (5.25, 2)",
    "INSERT INTO t VALUES (7, 1), (5.25, 2) ON CONFLICT (id) DO NOTHING",
    "INSERT INTO t VALUES (7, 1), (5.25, 2) ON CONFLICT DO NOTHING",
    "INSERT INTO t VALUES (7, 1), (5.25, 2) ON CONFLICT (id) DO UPDATE SET c = 9",
    "INSERT INTO t VALUES (7, 1), (5.25, 2) ON CONFLICT (id) DO UPDATE SET c = 9 WHERE 0",
    "INSERT OR IGNORE INTO t VALUES (7, 1), (5.25, 2)", "INSERT OR REPLACE INTO t VALUES (7, 1), (5.25, 2)",
    "INSERT OR FAIL INTO t VALUES (7, 1), (5.25, 2)", "INSERT OR ROLLBACK INTO t VALUES (7, 1), (5.25, 2)",
    "INSERT INTO t VALUES (7, 1), (5.25, 2) RETURNING rowid", "INSERT INTO t VALUES (7, 1), ('x', 2)",
    "INSERT OR IGNORE INTO t SELECT 8, 3 UNION ALL SELECT 'y', 4", "INSERT OR IGNORE INTO t VALUES ('z', 5)",
]


@pytest.mark.parametrize("table", STATEMENT_JOURNAL_TABLES)
def test_error_inside_transaction_keeps_rows_without_statement_journal(table):
    """Inside a transaction SQLite undoes a statement that fails with a
    non-constraint error (a datatype mismatch) only if a constraint could
    have aborted it; OR IGNORE and upserts that handle every constraint
    leave the rows written before the error."""
    for sql in STATEMENT_JOURNAL_INSERTS:
        if table.startswith("CREATE TABLE t (c, d"):
            sql = sql.replace("(id)", "(d)")
        pair = StatePair(check_messages=True)
        for step in [table, "INSERT INTO t VALUES (1, 1)", "BEGIN", sql, "SELECT rowid, * FROM t",
                     "COMMIT", "SELECT rowid, * FROM t"]:
            pair.run(step)
        pair.close()


@pytest.mark.parametrize("table, sql", [
    ("CREATE TABLE t (a, c)", "UPDATE t SET c = CASE a WHEN 1 THEN 10 ELSE abs(a * 0 - 9223372036854775807 - 1) END"),
    ("CREATE TABLE t (a, c)", "UPDATE OR IGNORE t SET c = CASE a WHEN 1 THEN 10 ELSE abs(a * 0 - 9223372036854775807 - 1) END"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "INSERT OR IGNORE INTO t VALUES (7, 1), (5.25, 2) RETURNING id"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "INSERT OR IGNORE INTO t VALUES (7, length('x')), (5.25, 2)"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "INSERT OR IGNORE INTO t VALUES (7, coalesce(1, 2)), (5.25, 2)"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "INSERT OR IGNORE INTO t VALUES (7, 'a' LIKE 'b'), (5.25, 2)"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "INSERT OR IGNORE INTO t VALUES (7, (SELECT max(1, 2))), (5.25, 2)"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "INSERT OR IGNORE INTO t VALUES (7, (SELECT count(*) FROM t)), (5.25, 2)"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "INSERT OR IGNORE INTO t SELECT id + 10, c FROM t UNION ALL SELECT 2.5, 1"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "UPDATE OR IGNORE t SET id = CASE id WHEN 1 THEN 10 ELSE 2.5 END RETURNING id"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "UPDATE OR IGNORE t SET id = CASE id WHEN 1 THEN 10 ELSE 2.5 END RETURNING upper(c)"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "UPDATE OR IGNORE t SET id = CASE id WHEN 1 THEN 10 ELSE 2.5 END WHERE typeof(c) = 'integer'"),
    ("CREATE TABLE t (id INTEGER PRIMARY KEY, c)", "UPDATE t SET c = c + 1, id = CASE id WHEN 1 THEN 10 ELSE 2.5 END"),
])
def test_function_calls_make_sqlite_keep_a_statement_journal(table, sql):
    """A call of a (not inlined) function may raise an error, so SQLite keeps
    a statement journal for it: the statement is undone after any error."""
    pair = StatePair(check_messages=True)
    for step in [table, "INSERT INTO t VALUES (1, 1), (3, 3)", "BEGIN", sql, "SELECT rowid, * FROM t",
                 "COMMIT", "SELECT rowid, * FROM t"]:
        pair.run(step)
    pair.close()


def test_bitwise_precedence_distinct_and_index_hints():
    pair = Pair(check_messages=True)
    for sql in [
        "SELECT 5 & 3, 5 | 3, 1 << 2 + 1, (1 << 2) + 1, 6 & 3 = 2, ~0, - ~1, 2 | 1 < 3, 1 || 2 << 1, 3 * 2 << 1",
        "SELECT 1 << 63, 1 << 64, -8 >> 1, -1 >> 70, 1 << -1, 2 << 1.5, '5' & 1.9, NULL | 1, ~NULL, ~'7'",
        "SELECT 4 & 6 | 1, 4 | 6 & 1, 1 << 2 << 3, 256 >> 2 >> 3, ~5 & 7, NOT 1 & 0",
        "CREATE TABLE t (a, b)", "CREATE INDEX ta ON t (a)", "CREATE TABLE u (x)", "CREATE INDEX ux ON u (x)",
        "INSERT INTO t VALUES (1, 2), (3, 4), (1, 5)",
        "SELECT abs(DISTINCT a) FROM t", "SELECT count(DISTINCT a, b) FROM t", "SELECT max(DISTINCT a, b) FROM t",
        "SELECT group_concat(DISTINCT a, '-') FROM t", "SELECT coalesce(DISTINCT a, b) FROM t",
        "SELECT total(DISTINCT a), sum(DISTINCT a), avg(DISTINCT a), count(DISTINCT a) FROM t",
        "SELECT a FROM t INDEXED BY ta WHERE a > 0", "SELECT a FROM t AS x INDEXED BY ta",
        "SELECT a FROM t INDEXED BY ux", "SELECT a FROM t INDEXED BY nope", "SELECT a FROM t NOT INDEXED WHERE a = 1",
        "UPDATE t INDEXED BY ta SET b = 5 WHERE a = 1", "DELETE FROM t NOT INDEXED WHERE a = 9",
        "UPDATE t INDEXED BY ux SET b = 5", "SELECT * FROM t",
        "REINDEX", "REINDEX t", "REINDEX ta", "REINDEX nocase", "REINDEX binary", "REINDEX nope", "REINDEX main.t",
        "SELECT * FROM t WHERE a = 1",
    ]:
        pair.run(sql)
    pair.close()


def test_temporary_views():
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t (a, b)", "INSERT INTO t VALUES (1, 2), (3, 4)",
        "CREATE TEMP VIEW tv AS SELECT a FROM t", "SELECT * FROM tv",
        "CREATE VIEW tv AS SELECT 1", "SELECT * FROM tv",  # the temporary view wins
        "CREATE TEMP VIEW t AS SELECT 5", "SELECT * FROM t", "INSERT INTO t VALUES (9, 9)", "DROP TABLE t",
        "DROP VIEW t", "SELECT * FROM t", "DROP VIEW tv", "SELECT * FROM tv", "DROP VIEW tv", "SELECT * FROM tv",
        "CREATE TEMPORARY VIEW IF NOT EXISTS w AS SELECT 2", "CREATE TEMP VIEW w AS SELECT 3", "SELECT * FROM w",
    ]:
        pair.run(sql)
    pair.close()


def test_temporary_views_are_not_stored(tmp_path):
    path = str(tmp_path / "temp.db")
    db = Database(path)
    db.execute("CREATE TABLE t (a); CREATE TEMP VIEW v AS SELECT a FROM t")
    assert db.execute("SELECT * FROM v") == []
    db.close()
    db = Database(path)
    with pytest.raises(OperationalError, match="no such table: v"):
        db.execute("SELECT * FROM v")
    db.close()
    with pytest.raises(NotSupportedError):
        Database(None).execute("CREATE TEMP TABLE x (a)")


@pytest.mark.parametrize("table", [
    "CREATE TABLE t (id INTEGER PRIMARY KEY, b BLOB, s TEXT UNIQUE, i INTEGER, r)",
    "CREATE TABLE t (id INTEGER PRIMARY KEY, b BLOB, s TEXT UNIQUE, i INTEGER UNIQUE, r)",
    "CREATE TABLE t (id INTEGER PRIMARY KEY, b BLOB, s TEXT, i INTEGER, r)",
])
@pytest.mark.parametrize("target", ["(id)", "(s)", "", "(i)"])
def test_upsert_excluded_values(table, target):
    """excluded.x has no affinity, and its value is converted by the column's
    affinity only if SQLite found the conflict after checking an index."""
    pair = Pair(check_messages=True)
    pair.run(table)
    pair.run("INSERT INTO t VALUES (1, 0, '0', 5, 7)")
    for expr in ["t.s = excluded.b", "excluded.s = 0", "excluded.i = '5'", "typeof(excluded.s)",
                 "excluded.i < '10'", "typeof(excluded.id)", "excluded.id", "excluded.rowid",
                 "typeof(excluded.i) || typeof(excluded.r)"]:
        pair.run("UPDATE t SET r = 7 WHERE id = 1")
        pair.run(f"INSERT INTO t VALUES ('1', 0, 0, '5', 7) ON CONFLICT {target} DO UPDATE SET r = ({expr})")
        pair.run("SELECT r FROM t ORDER BY rowid")
        pair.run(f"INSERT INTO t (s, i) VALUES (0, '5') ON CONFLICT {target} DO UPDATE SET r = ({expr})")
        pair.run("SELECT r FROM t ORDER BY rowid")
    pair.close()


def test_upsert_excluded_values_in_real_columns():
    pair = Pair(check_messages=True)
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, r REAL, f FLOAT, x)")
    pair.run("INSERT INTO t VALUES (1, 1, 1, 1)")
    for value in ["0", "'5'", "'5.5'", "'abc'", "2.5", "x'35'", "NULL", "9223372036854775807"]:
        pair.run(f"INSERT INTO t VALUES (1, {value}, {value}, 9) ON CONFLICT (id) DO UPDATE SET "
                 "x = typeof(excluded.r) || ' ' || quote(excluded.r) || ' ' || typeof(excluded.f)")
        pair.run("SELECT x FROM t")
    pair.close()


def test_upsert_clauses_resolved_only_when_reachable():
    # SQLite compiles DO UPDATE only for a conflict check that can reach it:
    # the row id is checked only when the INSERT gives it.
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t1 (c0, c1)",
        "CREATE TABLE t2 (id INTEGER PRIMARY KEY, c0)",
        "CREATE TABLE t3 (id INTEGER PRIMARY KEY, c0 UNIQUE, c1 UNIQUE)",
        "INSERT INTO t1 (c0) VALUES (1) ON CONFLICT DO UPDATE SET c2 = 1",
        "INSERT INTO t2 (c0) VALUES (1) ON CONFLICT DO UPDATE SET c2 = 1",
        "INSERT INTO t2 (id, c0) VALUES (1, 1) ON CONFLICT DO UPDATE SET c2 = 1",
        "INSERT INTO t2 VALUES (1, 1) ON CONFLICT (id) DO UPDATE SET c2 = 1",
        "INSERT INTO t2 (c0) VALUES (1) ON CONFLICT (id) DO UPDATE SET c2 = 1",
        "INSERT INTO t3 (c0) VALUES (1) ON CONFLICT (id) DO UPDATE SET c2 = 1",
        "INSERT INTO t3 (c0) VALUES (1) ON CONFLICT (c0) DO UPDATE SET c2 = 1",
        "INSERT INTO t3 (c0) VALUES (1) ON CONFLICT (c0) DO UPDATE SET c1 = 1 ON CONFLICT (c1) DO UPDATE SET c1 = x",
        "INSERT INTO t3 (c0) VALUES (1) ON CONFLICT (c0) DO UPDATE SET c1 = 1 "
        "ON CONFLICT (c1) DO UPDATE SET c1 = 1 ON CONFLICT DO UPDATE SET c1 = x",
        "INSERT INTO t3 (id) VALUES (1) ON CONFLICT (c0) DO UPDATE SET c1 = 1 "
        "ON CONFLICT (c1) DO UPDATE SET c1 = 1 ON CONFLICT DO UPDATE SET c1 = x",
        "INSERT INTO t3 (id, c0) VALUES (1, 2) ON CONFLICT (id) DO UPDATE SET c1 = 3 WHERE y",
        "INSERT INTO t1 (c0) VALUES (1) ON CONFLICT DO UPDATE SET c1 = 1 RETURNING nosuch",
        "SELECT rowid, * FROM t1", "SELECT rowid, * FROM t2", "SELECT rowid, * FROM t3",
    ]:
        pair.run(sql)
    pair.close()


def test_insert_row_id_by_name():
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t1 (c0, c1 UNIQUE)",
        "CREATE TABLE t2 (id INTEGER PRIMARY KEY, c0)",
        "INSERT INTO t1 (rowid, c0) VALUES (5, 1), ('7', 2), (8.0, 3), (NULL, 4)",
        "INSERT INTO t1 (rowid, c0) VALUES ('x', 1)",
        "INSERT INTO t1 (rowid, c0) VALUES (8.5, 1)",
        "INSERT INTO t1 (oid, c0) VALUES (5, 9)",
        "INSERT OR REPLACE INTO t1 (_rowid_, c0) VALUES (5, 9)",
        "INSERT INTO t1 (rowid, c0) VALUES (5, 10) ON CONFLICT DO UPDATE SET c0 = excluded.c0 + 100 RETURNING rowid, *",
        "INSERT INTO t1 (rowid, c0) VALUES (6, 1) ON CONFLICT DO UPDATE SET c2 = 1",
        "INSERT INTO t2 (rowid, c0) VALUES (3, 3), (NULL, 4)",
        "INSERT INTO t2 (rowid, c0) VALUES (3, 5) ON CONFLICT (id) DO UPDATE SET c0 = -1",
        "INSERT INTO t1 (nosuch, c0) VALUES (1, 1)",
        "SELECT rowid, * FROM t1", "SELECT rowid, * FROM t2",
    ]:
        pair.run(sql)
    pair.close()


def test_right_and_full_joins():
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE a (x, y)", "CREATE TABLE b (x, z)", "CREATE TABLE c (x INTEGER PRIMARY KEY, w)", "CREATE TABLE d (x)",
        "INSERT INTO a VALUES (1, 'a1'), (2, 'a2'), (3, 'a3'), (NULL, 'an')",
        "INSERT INTO b VALUES (2, 'b2'), (3, 'b3'), (4, 'b4'), (NULL, 'bn'), (4, 'b4b')",
        "INSERT INTO c VALUES (3, 'c3'), (4, 'c4'), (5, 'c5'), (1, 'c1')", "INSERT INTO d VALUES (9)",
    ]:
        pair.run(sql)
    for sql in [
        "SELECT * FROM a RIGHT JOIN b ON a.x = b.x",
        "SELECT * FROM a FULL JOIN b ON a.x = b.x",
        "SELECT * FROM a FULL OUTER JOIN b ON a.x = b.x WHERE a.y IS NULL",
        "SELECT * FROM a RIGHT OUTER JOIN b ON a.x = b.x WHERE a.x = 2",
        "SELECT * FROM a RIGHT JOIN b ON a.x = b.x WHERE a.x IS NULL OR a.x > 2",
        "SELECT * FROM a JOIN b ON a.x = b.x RIGHT JOIN c ON c.x = b.x",
        "SELECT * FROM a JOIN b ON 0 RIGHT JOIN c ON 1",
        "SELECT * FROM a RIGHT JOIN b ON a.x = b.x RIGHT JOIN c ON c.x = b.x",
        "SELECT * FROM a RIGHT JOIN b ON a.x = b.x FULL JOIN c ON c.x = a.x",
        "SELECT * FROM a FULL JOIN b ON a.x = b.x LEFT JOIN c ON c.x = b.x",
        "SELECT * FROM a LEFT JOIN b ON a.x = b.x FULL JOIN c ON c.x = b.x",
        "SELECT * FROM a RIGHT JOIN b ON a.x = b.x JOIN c ON c.x = coalesce(a.x, b.x)",
        "SELECT * FROM a RIGHT JOIN b ON a.x = b.x JOIN c ON a.x = 3",
        "SELECT count(*), sum(b.x), group_concat(a.y) FROM a FULL JOIN b ON a.x = b.x",
        "SELECT * FROM a FULL JOIN b ON a.x = b.x ORDER BY a.x, b.z",
        "SELECT * FROM a FULL JOIN c ON a.x = c.x ORDER BY a.x LIMIT 3",
        "SELECT * FROM c FULL JOIN a ON a.x = c.x ORDER BY c.x LIMIT 4",
        "SELECT * FROM (SELECT x FROM a) AS s RIGHT JOIN (SELECT x FROM b) AS t ON s.x = t.x",
        "SELECT * FROM a RIGHT JOIN b",
        "SELECT * FROM a RIGHT JOIN b ON 0",
        "SELECT * FROM a FULL JOIN b ON 0 WHERE 1",
        "SELECT * FROM a, b FULL JOIN c ON c.x = a.x AND c.x = b.x",
        "SELECT (SELECT count(*) FROM a RIGHT JOIN b ON a.x = b.x AND b.x = c.x) FROM c",
        "SELECT * FROM c WHERE EXISTS (SELECT 1 FROM a FULL JOIN b ON a.x = c.x WHERE b.x = c.x)",
        "SELECT * FROM a RIGHT JOIN b ON a.x = b.x WHERE b.x = 4",
        "SELECT * FROM c RIGHT JOIN b ON c.x = b.x WHERE c.x = 4",
        "SELECT * FROM c RIGHT JOIN b ON c.x = b.x AND c.w = 'c4'",
        "SELECT * FROM a RIGHT JOIN b USING (x)",
        "SELECT x, a.x, b.x FROM a FULL JOIN b USING (x)",
        "SELECT * FROM a NATURAL FULL JOIN b",
        "SELECT * FROM a NATURAL RIGHT JOIN b",
        "SELECT * FROM a FULL JOIN b USING (x) FULL JOIN c USING (x)",
        "SELECT x FROM a FULL JOIN b USING (x) FULL JOIN c USING (x)",
        "SELECT * FROM a RIGHT JOIN b USING (x) JOIN c USING (x)",
        "SELECT * FROM a JOIN b USING (x) FULL JOIN c USING (x)",
        "SELECT x FROM a FULL JOIN b USING (x) JOIN d ON 1",
        "SELECT * FROM a FULL JOIN b USING (x) JOIN d ON 1",
        "SELECT a.* FROM a FULL JOIN b USING (x)",
        "SELECT b.* FROM a FULL JOIN b USING (x)",
        "SELECT * FROM a JOIN d ON 1 FULL JOIN b USING (x)",
        "SELECT * FROM a FULL JOIN b USING (x) WHERE x > 1",
        "SELECT typeof(x), x FROM a FULL JOIN b USING (x) WHERE x = '2'",
        "SELECT (SELECT x) FROM a FULL JOIN b USING (x)",
        "SELECT (SELECT x FROM d AS a) FROM a FULL JOIN b USING (x)",
        "SELECT * FROM a LEFT JOIN b USING (x) RIGHT JOIN c ON 1",
        "SELECT x FROM a LEFT JOIN b USING (x) RIGHT JOIN c USING (x)",
        "SELECT x, count(*) FROM a FULL JOIN b USING (x) GROUP BY x ORDER BY x",
        "SELECT * FROM a FULL JOIN b USING (x) ORDER BY x DESC",
        "SELECT * FROM a FULL JOIN b USING (x) FULL JOIN c ON c.x = x",
        "SELECT * FROM a JOIN b USING (x) JOIN c USING (x) RIGHT JOIN d ON 1",
        "SELECT * FROM a JOIN b ON 1 JOIN c USING (x)",
        "SELECT * FROM a RIGHT JOIN b USING (x) RIGHT JOIN c USING (x)",
        "SELECT * FROM a FULL JOIN b USING (x) RIGHT JOIN c USING (x)",
        "SELECT * FROM a RIGHT JOIN b USING (x) FULL JOIN c USING (x)",
        "SELECT * FROM a FULL JOIN b USING (x) WHERE a.x IS NULL",
        "SELECT * FROM a left JOIN b ON 1 RIGHT JOIN c ON 0 WHERE 0",
        # An ON condition with a subquery before a RIGHT JOIN filters only its own join.
        "SELECT * FROM a JOIN b ON (NOT EXISTS (SELECT 1 FROM a AS s)) RIGHT JOIN c ON c.x = b.x",
        "SELECT * FROM a JOIN b ON (SELECT a.x) = b.x RIGHT JOIN c ON c.x = b.x",
        "SELECT * FROM a LEFT JOIN b ON (SELECT a.x + 1) = b.x FULL JOIN c ON c.x = b.x",
        # An outer join's ON (any ON, with a RIGHT or FULL JOIN) may not use a table to its right.
        "SELECT * FROM a LEFT JOIN b ON b.z = d.x, d", "SELECT * FROM a JOIN b ON b.z = d.x, d",
        "SELECT * FROM a JOIN b ON b.z = d.x RIGHT JOIN c ON 1", "SELECT * FROM a FULL JOIN b ON b.z = d.x JOIN d",
        "SELECT * FROM a LEFT JOIN b ON b.x = (SELECT d.x) JOIN d",
        "SELECT * FROM a LEFT JOIN b ON EXISTS (SELECT 1 FROM a AS e WHERE e.x = d.x), d",
        "SELECT * FROM a LEFT JOIN b ON b.x IN (SELECT x FROM d AS f) JOIN d",
        # The coalesce() of USING columns has its first argument's affinity.
        "SELECT * FROM b RIGHT JOIN a USING (x) FULL JOIN c USING (x) WHERE x = '3'",
        "SELECT * FROM a RIGHT JOIN b USING (x) FULL JOIN (SELECT CAST(x AS TEXT) AS x FROM c) AS t USING (x)",
    ]:
        pair.run(sql)
    pair.close()


def test_comparison_affinity_applies_to_both_operands():
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t (i INTEGER, r REAL, x TEXT, b BLOB, n)",
        "INSERT INTO t VALUES (5, 5.0, '5', x'35', 5), (NULL, NULL, 'a', '5', '5')",
        "CREATE TABLE p (c0 REAL)", "CREATE TABLE q (c0 TEXT)", "INSERT INTO q VALUES ('0')",
        "SELECT v, v = 5, v = '5', v < 10 FROM (SELECT i AS v FROM t UNION ALL SELECT '5' UNION ALL SELECT ' 5')",
        "SELECT v, v = 5, v = '5' FROM (SELECT x AS v FROM t UNION ALL SELECT 5 UNION ALL SELECT 5.0)",
        "SELECT v IN (5, '5'), v BETWEEN 4 AND '6', CASE v WHEN 5 THEN 'five' END "
        "FROM (SELECT i AS v FROM t UNION ALL SELECT '5')",
        "SELECT a.x = b.i, a.b = b.x, a.n = b.x, a.x = b.r FROM t AS a, t AS b",
        "SELECT v = 5 FROM (SELECT r AS v FROM t UNION ALL SELECT '5.0' UNION ALL SELECT 'abc')",
        "SELECT v IS 5, v IS NOT '5' FROM (SELECT i AS v FROM t UNION ALL SELECT '5')",
        # coalesce(p.c0, q.c0) has REAL affinity, which the text '0' then gets.
        "SELECT * FROM p RIGHT JOIN q USING (c0) JOIN q AS r USING (c0)",
        # ... also when an index on the other side is used to find it.
        "CREATE TABLE r (c0 REAL UNIQUE)", "INSERT INTO r VALUES (0), (1)",
        "SELECT * FROM p RIGHT JOIN q USING (c0) JOIN r USING (c0)",
    ]:
        pair.run(sql)
    pair.close()


def test_result_column_aliases_in_other_clauses():
    # SQLite resolves a name that is no column of the FROM clause as a result
    # column alias in WHERE, ON, GROUP BY, HAVING and ORDER BY (subqueries too).
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t (a, b)", "INSERT INTO t VALUES (1, 2), (3, 4), (5, 6), (3, 9)",
        "CREATE TABLE u (a, k)", "INSERT INTO u VALUES (1, 10), (3, 30)",
        "SELECT a + 1 AS k FROM t WHERE k > 2",
        "SELECT a + 1 AS k, count(*) FROM t GROUP BY k",
        "SELECT a + 1 AS k, count(*) AS n FROM t GROUP BY k HAVING n > 0 AND k > 2",
        "SELECT a AS b FROM t WHERE b > 2",
        "SELECT a + 1 AS k FROM t WHERE (SELECT k) > 2",
        "SELECT count(*) AS n FROM t WHERE n > 0",
        "SELECT a AS k, k + 1 AS m FROM t",
        "SELECT a AS k FROM t ORDER BY k + 1 DESC",
        "SELECT a * 2 AS k FROM t ORDER BY -k",
        "SELECT a AS k FROM t AS s JOIN u ON k = u.a",
        "SELECT s.a AS z FROM t AS s JOIN u ON z = u.a",
        "SELECT s.a AS z FROM t AS s LEFT JOIN u ON z = u.a AND u.k > 10",
        "SELECT a AS k FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = k)",
        "SELECT a AS k FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = k * 10)",
        "SELECT b AS q FROM t WHERE a IN (SELECT a FROM u WHERE k > q)",
        "SELECT a + b AS k FROM t WHERE k IN (SELECT k FROM u)",
        "SELECT count(*) AS n FROM t GROUP BY n",
        "SELECT count(*) AS n FROM t GROUP BY a HAVING n > 1",
        "SELECT a AS x, count(*) AS n FROM t GROUP BY x ORDER BY n * -1, x",
        "SELECT a AS k FROM t WHERE (SELECT u.a AS k FROM u WHERE k = 3) = k",
        "SELECT a AS k FROM t WHERE k = (SELECT k FROM u WHERE u.a = 1)",
        "SELECT a AS k FROM t WHERE (SELECT k + 1 AS j FROM u AS w WHERE j > 20 LIMIT 1) > k",
        "SELECT DISTINCT a % 2 AS m FROM t WHERE m = 1",
        "SELECT a AS K FROM t WHERE k > 1",
        "SELECT a AS k FROM t WHERE t.k > 1",
        "SELECT a AS k, b AS k FROM t WHERE k = 4",
        "SELECT a AS rowid FROM t WHERE rowid = 3",
        "SELECT a AS k FROM t UNION SELECT a AS j FROM u WHERE j = 3",
        "SELECT a AS k FROM t WHERE k BETWEEN 2 AND 4 AND k LIKE '3' AND k IN (3, 5) AND CASE k WHEN 3 THEN 1 END",
        "SELECT a AS k FROM t WHERE coalesce(k, 0) > 1 ORDER BY 1",
        "SELECT sum(a) AS s FROM t HAVING s > 1",
        "SELECT a AS k FROM t WHERE (SELECT max(k) FROM u) > 1",
        "SELECT a AS k FROM t GROUP BY k + 0",
        "SELECT max(a) AS m FROM t ORDER BY m",
        "SELECT count(*) AS n FROM t WHERE (SELECT n) > 0",
        "SELECT count(*) AS n FROM t AS x LEFT JOIN t AS y ON n > 0",
    ]:
        pair.run(sql)
    pair.close()


def test_aggregates_of_an_enclosing_query():
    # An aggregate whose arguments use only an enclosing query's columns
    # belongs to that query (which then is an aggregate query).
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t (a, b)", "INSERT INTO t VALUES (1, 2), (3, 4), (5, 6), (3, 9)",
        "CREATE TABLE u (a, k)", "INSERT INTO u VALUES (1, 10), (3, 30), (3, 31)", "CREATE TABLE e (a, b)",
        "SELECT (SELECT count(t.a) FROM u) FROM t",
        "SELECT (SELECT count(t.a)) FROM t",
        "SELECT a, (SELECT sum(t.b) FROM u WHERE u.a = t.a) FROM t GROUP BY a",
        "SELECT a FROM t WHERE (SELECT count(t.a)) > 0",
        "SELECT a FROM t GROUP BY a HAVING (SELECT count(t.b)) > 1",
        "SELECT (SELECT count(t.a) + count(*) FROM u) FROM t",
        "SELECT (SELECT count(u.k + t.a) FROM u) FROM t",
        "SELECT a FROM t ORDER BY (SELECT count(t.a))",
        "SELECT (SELECT max(t.a) FROM u WHERE u.k > 10) FROM t",
        "SELECT (SELECT max(t.a) FROM u WHERE u.k > 100) FROM t",
        "SELECT (SELECT 1 FROM u WHERE count(t.a) > 0) FROM t",
        "SELECT (SELECT count(t.a) FROM u GROUP BY u.a) FROM t",
        "SELECT (SELECT (SELECT count(t.a))) FROM t",
        "SELECT (SELECT count(t.a) FROM u) FROM t WHERE 0",
        "SELECT (SELECT count(t.a) FROM u) FROM e",
        "SELECT b, (SELECT group_concat(t.a || u.a) FROM u) FROM t",
        "SELECT (SELECT count(*) FROM u HAVING count(t.a) > 1) FROM t",
        "SELECT (SELECT u.a FROM u ORDER BY count(t.a)) FROM t",
        "SELECT count(*), (SELECT sum(t.a) FROM u) FROM t",
        "SELECT a IN (SELECT count(t.b) FROM u) FROM t",
        "SELECT EXISTS (SELECT max(t.a)) FROM t",
        "SELECT (SELECT count(x.a) FROM t AS x WHERE x.a = t.a) FROM t",
        "SELECT (SELECT sum(t.a + x.a) FROM t AS x) FROM t",
        "SELECT (SELECT count(t.a) FROM u AS t) FROM t",
        "SELECT (SELECT total(t.a) FROM u) + 1 FROM t GROUP BY b",
        "SELECT * FROM (SELECT (SELECT count(t.a)) AS n FROM t)",
        "SELECT (SELECT count(a)) FROM t",
        "SELECT (SELECT count(k) FROM u) FROM t",
        "SELECT (SELECT count(b) FROM u) FROM t",
        "SELECT a FROM t ORDER BY count(*)",
        "SELECT a FROM t HAVING count(*) > 0",
        "SELECT a FROM t GROUP BY count(*)",
        "SELECT a FROM t GROUP BY (SELECT count(t.b))",
        "SELECT a FROM t WHERE count(*) > 1",
        "SELECT count(*) FROM t WHERE count(*) > 1",
        "SELECT count(*) FROM t AS x JOIN t AS y ON count(*) > 1",
        "SELECT sum(a) FROM t WHERE (SELECT count(t.b))",
        "SELECT a FROM t GROUP BY a HAVING a > 0 ORDER BY count(*)",
        "SELECT a, count(*) AS n FROM t GROUP BY a HAVING (SELECT n) > 1",
        "SELECT a AS k FROM t WHERE (SELECT count(k)) > 0",
        "SELECT (SELECT count(*) FROM u WHERE u.k > count(t.a) * 7) FROM t",
        "SELECT a, (SELECT min(t.b) + max(u.k) FROM u) FROM t GROUP BY a",
        "SELECT (SELECT count(DISTINCT t.a)) FROM t",
        "SELECT max((SELECT count(t.a))) FROM t",
        "SELECT sum((SELECT count(t.a) FROM u)) FROM t", "SELECT max((SELECT count(t.a) + t.b)) FROM t",
        "SELECT count(*) AS n, max((SELECT n)) FROM t", "SELECT a, count(*) AS n FROM t GROUP BY a ORDER BY (SELECT n)",
        "SELECT sum(a) AS n FROM t WHERE (SELECT n) > 1",
        "SELECT (SELECT count(t.a) FROM u) FROM t GROUP BY a ORDER BY 1",
        "SELECT a, (SELECT count(t.b)) AS c FROM t GROUP BY a ORDER BY c DESC, a",
        "SELECT (SELECT count(t.a) FROM u) FROM t UNION ALL SELECT (SELECT sum(u.k)) FROM u",
        "WITH w AS (SELECT (SELECT count(t.a)) AS n FROM t) SELECT * FROM w",
        "SELECT DISTINCT (SELECT count(t.a)) FROM t",
        "SELECT (SELECT count(t.a) FROM u LIMIT 1) FROM t LIMIT 5",
        "SELECT (SELECT u.a FROM u WHERE u.a = (SELECT max(t.a)) ) FROM t",
    ]:
        pair.run(sql)
    pair.close()


def test_upsert_sees_defaults_that_replace_put_in_not_null_columns():
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, c0 INTEGER NOT NULL DEFAULT '10', c1 REAL NOT NULL DEFAULT 7, "
        "c2 TEXT NOT NULL DEFAULT 5)",
        "INSERT INTO t VALUES (1, 1, 1, 1)",
        "INSERT OR REPLACE INTO t VALUES (1, NULL, NULL, NULL) ON CONFLICT (id) DO UPDATE "
        "SET c0 = typeof(excluded.c0) || excluded.c0, c1 = typeof(excluded.c1), c2 = typeof(excluded.c2)",
        "CREATE UNIQUE INDEX u ON t (c2)",
        "INSERT OR REPLACE INTO t VALUES (2, NULL, NULL, 'real') ON CONFLICT (c2) DO UPDATE "
        "SET c0 = typeof(excluded.c0) || excluded.c0, c1 = typeof(excluded.c1) || excluded.c1",
        "SELECT * FROM t",
    ]:
        pair.run(sql)
    pair.close()


def test_upsert_sees_defaults_converted_by_an_earlier_row():
    """SQLite computes a DEFAULT once per statement and converts it in place
    when a row is checked against an index or stored: later rows'
    "excluded" values show the converted default."""
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, c1 VARCHAR(5) DEFAULT -1.5, c2 INT DEFAULT '7', c3)",
        "CREATE TABLE u (id INTEGER PRIMARY KEY, c1 VARCHAR(5) DEFAULT -1.5, c3 UNIQUE)",
        "INSERT INTO t (id) VALUES (1)", "INSERT INTO u (id, c3) VALUES (1, 1)",
        "INSERT INTO t (id) VALUES (5), (1) ON CONFLICT (id) DO UPDATE "
        "SET c3 = excluded.c1 || typeof(excluded.c1) || typeof(excluded.c2) RETURNING *",
        "INSERT INTO t (id) VALUES (1), (6) ON CONFLICT (id) DO UPDATE SET c3 = typeof(excluded.c1) RETURNING *",
        "INSERT INTO t (id) SELECT 7 UNION ALL SELECT 1 ON CONFLICT DO UPDATE SET c3 = typeof(excluded.c2) RETURNING *",
        "INSERT INTO u (id, c3) VALUES (1, 9), (2, 1) ON CONFLICT DO UPDATE SET c1 = typeof(excluded.c1) RETURNING *",
        "INSERT OR IGNORE INTO u (id, c3) VALUES (3, 1), (1, 8) ON CONFLICT (id) DO UPDATE "
        "SET c1 = typeof(excluded.c1) RETURNING *",
        "INSERT INTO u (id, c1, c3) VALUES (4, 2.5, 4), (1, 2.5, 5) ON CONFLICT (id) DO UPDATE "
        "SET c3 = typeof(excluded.c1) RETURNING *",
    ]:
        pair.run(sql)
    pair.close()


def test_postfix_null_tests():
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t (a NOT NULL DEFAULT 5 NOT NULL, b)", "INSERT INTO t (b) VALUES (NULL), (1)",
        "SELECT 1 NOT NULL, NULL NOT NULL, 1 ISNULL, NULL NOTNULL, 1 = 1 NOT NULL, NOT 1 NOT NULL, "
        "1 + 1 NOTNULL, 2 NOT NULL = 1, 5 NOT NULL IS 1",
        "SELECT 1 NOT NULL NOT NULL, NULL ISNULL ISNULL, 1 < 2 ISNULL, 1 NOT NULL BETWEEN 0 AND 1, 1 NOT NULL AND 0",
        "SELECT a, b FROM t WHERE b NOT NULL", "SELECT b ISNULL, b NOTNULL FROM t",
    ]:
        pair.run(sql)
    pair.close()


def test_order_by_first_table_after_join_reordering():
    # The planner puts the smaller t2 first: rows then come in t2's order,
    # not in the order of t0's row ids, so ORDER BY a.id must sort.
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t0 (id INTEGER PRIMARY KEY, c0)", "CREATE TABLE t2 (id INTEGER PRIMARY KEY, c0 TEXT)",
        "INSERT INTO t0 VALUES (1, 10), (2, NULL), (3, 0), (4, 5), (5, NULL)",
        "INSERT INTO t2 VALUES (1, 'a'), (2, 'b')",
        "INSERT INTO t0 SELECT id + 5, c0 FROM t0", "INSERT INTO t0 SELECT id + 10, c0 FROM t0", "ANALYZE",
        "SELECT a.c0 FROM t0 AS a, t2 AS b ORDER BY a.id ASC, a.c0 ASC, 1 LIMIT 3 OFFSET 3",
    ]:
        pair.run(sql)
    pair.close()


def test_hash_joins_match_sqlite():
    """Equality joins on columns without an index hash the inner table's rows
    (HashLookup): affinity conversions, NULLs, mixed types in derived tables,
    outer joins, and a rebuild for every run of a correlated subquery."""
    pair = Pair(check_messages=True)
    for sql in [
        'CREATE TABLE ti (a INTEGER, b)',
        'CREATE TABLE tt (a TEXT, b)',
        'CREATE TABLE tn (a, b)',
        'CREATE TABLE tr (a REAL, b)',
        "INSERT INTO ti VALUES (1, 'i1'), (2, 'i2'), (NULL, 'in'), ('x', 'ix'), (2, 'i2b'), (1.5, 'i15')",
        "INSERT INTO tt VALUES ('1', 't1'), ('2', 't2'), (NULL, 'tn'), ('x', 'tx'), ('1.0', 't10'), (' 2', 'tsp')",
        "INSERT INTO tn VALUES (1, 'n1'), ('1', 'n1s'), (2.0, 'n2'), (NULL, 'nn'), (x'31', 'nb'), ('x', 'nx')",
        "INSERT INTO tr VALUES (1, 'r1'), (2.5, 'r25'), ('abc', 'rt'), (NULL, 'rn')",
        'SELECT x.b, y.b FROM ti AS x JOIN tt AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM tt AS x JOIN ti AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM ti AS x JOIN tn AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM tn AS x JOIN tn AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM tt AS x JOIN tn AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM tn AS x JOIN tt AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM tr AS x JOIN ti AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM ti AS x JOIN tr AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM tt AS x JOIN tr AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM ti AS x LEFT JOIN tt AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM ti AS x RIGHT JOIN tt AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM ti AS x FULL JOIN tn AS y ON y.a = x.a',
        "SELECT x.b, y.v FROM ti AS x JOIN (SELECT a AS k, b AS v FROM tn UNION ALL SELECT '2', 'u2') AS y ON y.k = x.a",
        'SELECT x.b, y.v FROM tt AS x JOIN (SELECT a + 0 AS k, b AS v FROM tn) AS y ON y.k = x.a',
        'SELECT x.b, y.v FROM tn AS x JOIN (SELECT a AS k, b AS v FROM tt) AS y ON y.k = x.a',
        'SELECT x.b, (SELECT group_concat(y.b) FROM tn AS y JOIN tt AS z ON z.a = y.a WHERE y.b >= x.b) FROM ti AS x',
        'SELECT x.b, y.b, z.b FROM ti AS x JOIN tt AS y ON y.a = x.a JOIN tn AS z ON z.a = y.a',
        'SELECT x.b, y.b FROM ti AS x JOIN tt AS y ON y.a = x.a + 0',
        "SELECT x.b, y.b FROM ti AS x JOIN tt AS y ON y.a = x.a AND y.b > 't'",
        'SELECT count(*) FROM ti AS x JOIN tt AS y ON y.a = x.a WHERE x.a IS NOT NULL',
        'WITH c AS (SELECT a, b FROM tn) SELECT x.b, c.b FROM ti AS x JOIN c ON c.a = x.a',
        'UPDATE ti SET b = (SELECT group_concat(y.b) FROM tn AS y JOIN ti AS z ON z.a = y.a WHERE z.b = ti.b) WHERE a IS NOT NULL',
        'SELECT * FROM ti',
        'SELECT x.b, y.b FROM tt AS x LEFT JOIN ti AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM tt AS x RIGHT JOIN ti AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM tt AS x FULL JOIN tr AS y ON y.a = x.a',
        'SELECT x.b, y.b FROM tn AS x LEFT JOIN tn AS y ON y.a = x.a AND y.b != x.b',
    ]:
        pair.run(sql)
    pair.close()


def test_common_table_expressions_and_values():
    pair = Pair(check_messages=True)
    pair.run("CREATE TABLE t (a)")
    pair.run("INSERT INTO t VALUES (1), (2)")
    for sql in [
 "WITH x AS (SELECT 1 AS v) SELECT * FROM x",
 "WITH x(p, q) AS (SELECT 1, 2) SELECT * FROM x",
 "WITH x(p) AS (SELECT 1, 2) SELECT * FROM x",
 "WITH a AS (SELECT * FROM b), b AS (SELECT 5) SELECT * FROM a",
 "WITH a AS (SELECT 5), b AS (SELECT * FROM a) SELECT * FROM b",
 "WITH t AS (SELECT 99) SELECT * FROM t",
 "WITH c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 5) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION SELECT n % 3 + 1 FROM c) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c LIMIT 4) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c LIMIT 3 OFFSET 2) SELECT * FROM c",
 "WITH RECURSIVE c(n, d) AS (SELECT 1, 0 UNION ALL SELECT n*2, d+1 FROM c WHERE d < 3 UNION ALL SELECT n*2+1, d+1 FROM c WHERE d < 3 ORDER BY 2 DESC) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 3 ORDER BY 1 DESC) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT count(*) FROM c) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c, c AS d WHERE n < 3) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT (SELECT n+1 FROM c) WHERE 0) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT n FROM c) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t LEFT JOIN c ON 1 WHERE n < 3) SELECT * FROM c",
 "WITH x AS (SELECT 1), x AS (SELECT 2) SELECT * FROM x",
 "SELECT (WITH y AS (SELECT a * 10 AS b) SELECT b FROM y) FROM t",
 "WITH x AS (SELECT a FROM t) INSERT INTO t SELECT a + 10 FROM x",
 "SELECT * FROM t",
 "WITH x AS (SELECT 3) UPDATE t SET a = a + (SELECT * FROM x) WHERE a < 5",
 "WITH x AS (SELECT 11) DELETE FROM t WHERE a IN x",
 "SELECT * FROM t",
 "WITH x AS MATERIALIZED (SELECT 1 AS v), y AS NOT MATERIALIZED (SELECT 2) SELECT * FROM x, y",
 "WITH x AS (SELECT 1 AS v UNION ALL SELECT 2) SELECT * FROM x WHERE v > 1",
 "WITH x AS (SELECT a, a FROM t) SELECT * FROM x",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 3), d(m) AS (SELECT n*10 FROM c) SELECT * FROM d",
 "WITH RECURSIVE c(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM c WHERE x<3) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 3 LIMIT -1) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT DISTINCT n+1 FROM c WHERE n < 3) SELECT * FROM c",
 "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 3 GROUP BY n) SELECT * FROM c",
 "VALUES (1, 'a'), (2, 'b')", "VALUES (1), (3) UNION SELECT 2 ORDER BY 1 DESC LIMIT 2", "SELECT 1 UNION VALUES (1), (1)", "VALUES (1), (1) UNION SELECT 2",
 "SELECT * FROM (VALUES (1, 2), (3, 4)) AS v WHERE column1 > 1", "SELECT 2 IN (VALUES (1), (2))", "VALUES (1, 2), (3)",
 "SELECT a, (WITH q AS (SELECT a * 2 AS d) SELECT d FROM q) FROM t",
 "WITH RECURSIVE fib(i, a, b) AS (SELECT 1, 0, 1 UNION ALL SELECT i + 1, b, a + b FROM fib WHERE i < 20) SELECT group_concat(a) FROM fib",
 "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt LIMIT 10) SELECT sum(x), count(*) FROM cnt",
 "CREATE VIEW v AS SELECT * FROM t", "WITH t AS (SELECT 'cte') SELECT * FROM v",
 "WITH RECURSIVE tree(id, depth) AS (SELECT 1, 0 UNION ALL SELECT id * 2 + k.column1, depth + 1 FROM tree, (VALUES (0), (1)) AS k WHERE depth < 3) SELECT count(*), max(id) FROM tree",
]:
        pair.run(sql)
    pair.close()


def test_values_syntax_errors():
    pair = Pair()
    for sql in ["VALUES (1), (2) ORDER BY 1", "SELECT 3 UNION VALUES (1) LIMIT 1", "WITH x AS (SELECT 1)",
                "WITH x AS SELECT 1 SELECT 2"]:
        pair.run(sql)
    pair.close()


def test_defaults_and_alter_table():
    pair = Pair(check_messages=True)
    for sql in [
 "CREATE TABLE t(a INTEGER PRIMARY KEY, b TEXT DEFAULT 'x', c DEFAULT (1 + 2), d REAL DEFAULT -5, e DEFAULT CURRENT_DATE, f NOT NULL DEFAULT 7, g DEFAULT NULL, h INT DEFAULT +3, i DEFAULT TRUE, j DEFAULT 0x10, k DEFAULT b, l TEXT DEFAULT 5, m DEFAULT (abs(-4)), n DEFAULT x'41', o DEFAULT -0x10, p DEFAULT 'a''b')",
 "INSERT INTO t (a) VALUES (1)", "SELECT a, b, c, d, typeof(d), length(e), f, g, h, i, j, k, l, typeof(l), m, n, o, p FROM t",
 "INSERT INTO t (a, f) VALUES (2, NULL)", "INSERT OR REPLACE INTO t (a, f) VALUES (3, NULL)", "SELECT a, f FROM t",
 "INSERT INTO t DEFAULT VALUES", "SELECT a, b FROM t WHERE a > 3", "UPDATE OR REPLACE t SET f = NULL WHERE a = 1", "SELECT a, f FROM t",
 "CREATE TABLE d2(x DEFAULT (random()))", "CREATE TABLE d3(x DEFAULT -'a')", "CREATE TABLE bad4(a DEFAULT (b))", "INSERT INTO d3 DEFAULT VALUES", "SELECT x, typeof(x) FROM d3",
 "SELECT true, false, true + 1, typeof(false)", "CREATE TABLE tf(true, x)", "INSERT INTO tf VALUES (5, 1)", "SELECT true, false FROM tf",
 "CREATE TABLE u(x INTEGER UNIQUE, y)", "INSERT INTO u VALUES (1, 'one'), (2, 'two')", "CREATE INDEX uy ON u(y)",
 "CREATE VIEW vu AS SELECT u.x, y FROM u WHERE x > 0", "CREATE VIEW vu2 AS SELECT q.y FROM u AS q",
 "ALTER TABLE u ADD COLUMN z INTEGER DEFAULT 42", "SELECT * FROM u", "ALTER TABLE u ADD w", "ALTER TABLE u ADD COLUMN v NOT NULL",
 "ALTER TABLE u ADD COLUMN v NOT NULL DEFAULT 'q'", "ALTER TABLE u ADD COLUMN k UNIQUE", "ALTER TABLE u ADD COLUMN k PRIMARY KEY",
 "ALTER TABLE u ADD COLUMN k DEFAULT CURRENT_TIME", "ALTER TABLE u ADD COLUMN y", "ALTER TABLE u ADD COLUMN k DEFAULT (1+1)", "ALTER TABLE u ADD COLUMN r REAL DEFAULT '7'",
 "SELECT *, typeof(r) FROM u", "INSERT INTO u (x) VALUES (3)", "SELECT * FROM u", "UPDATE u SET z = z + 1", "SELECT * FROM u WHERE z > 42",
 "ALTER TABLE u RENAME COLUMN y TO yy", "SELECT * FROM vu", "SELECT * FROM vu2", "SELECT yy FROM u WHERE yy = 'one'",
 "ALTER TABLE u RENAME TO uu", "SELECT * FROM vu", "SELECT * FROM vu2", "SELECT * FROM uu", "SELECT * FROM u",
 "ALTER TABLE uu RENAME COLUMN nope TO x2", "ALTER TABLE uu RENAME COLUMN x TO yy", "ALTER TABLE nope RENAME TO z", "ALTER TABLE uu RENAME TO t", "ALTER TABLE uu RENAME TO vu",
 "ALTER TABLE uu DROP COLUMN x", "ALTER TABLE uu DROP COLUMN yy", "DROP VIEW vu", "DROP VIEW vu2", "ALTER TABLE uu DROP COLUMN yy", "DROP INDEX uy", "ALTER TABLE uu DROP COLUMN yy",
 "SELECT * FROM uu", "ALTER TABLE uu DROP COLUMN nope", "CREATE TABLE one(a)", "ALTER TABLE one DROP COLUMN a",
 "CREATE TABLE pk(a TEXT PRIMARY KEY, b, c)", "INSERT INTO pk VALUES ('k', 1, 2)", "ALTER TABLE pk DROP COLUMN a", "ALTER TABLE pk DROP COLUMN b", "SELECT * FROM pk",
 "CREATE TABLE ip(id INTEGER PRIMARY KEY, b, c)", "INSERT INTO ip VALUES (5, 'b', 'c')", "ALTER TABLE ip DROP COLUMN b", "SELECT *, rowid FROM ip", "ALTER TABLE ip DROP COLUMN id",
 "ALTER TABLE ip RENAME COLUMN id TO ident", "SELECT ident, rowid FROM ip", "INSERT INTO ip (c) VALUES ('d')", "SELECT * FROM ip",
 "CREATE VIEW vv AS SELECT v.a FROM (SELECT 1 AS a) AS v", "ALTER TABLE ip RENAME TO v", "SELECT * FROM vv",
]:
        pair.run(sql)
    pair.close()


def test_alter_table_survives_reopening(tmp_path):
    path = str(tmp_path / "alter.db")
    db = Database(path)
    db.execute("CREATE TABLE u (x INTEGER UNIQUE, y TEXT DEFAULT 'd', z INTEGER PRIMARY KEY);"
               "INSERT INTO u (x, y) VALUES (1, 'a'), (2, NULL); CREATE INDEX uy ON u (y);"
               "CREATE VIEW v AS SELECT u.y, x FROM u WHERE y IS NOT NULL; ANALYZE;"
               "ALTER TABLE u ADD COLUMN w REAL DEFAULT -1.5; ALTER TABLE u RENAME COLUMN y TO yy;"
               "ALTER TABLE u RENAME TO t2; INSERT INTO t2 (x) VALUES (3)")
    db.close()
    db = Database(path)
    assert db.execute("SELECT * FROM t2 ORDER BY x") == [(1, "a", 1, -1.5), (2, None, 2, -1.5), (3, "d", 3, -1.5)]
    assert db.execute("SELECT * FROM v") == [("a", 1), ("d", 3)]
    assert db.catalog.views["v"].sql == 'CREATE VIEW v AS SELECT "t2".yy, x FROM "t2" WHERE yy IS NOT NULL'
    assert sorted(i.name for i in db.catalog.tables["t2"].indexes) == ["minidb_autoindex_t2_1", "uy"]
    db.execute("ALTER TABLE t2 DROP COLUMN w")
    db.close()
    db = Database(path)
    assert db.execute("SELECT * FROM t2 ORDER BY x") == [(1, "a", 1), (2, None, 2), (3, "d", 3)]
    assert db.integrity_check() == []
    db.close()


def test_join_of_many_tables():
    # More tables than Python allows nested blocks in one generated loop.
    pair = Pair(check_messages=True)
    names = [f"t{i}" for i in range(22)]
    for i, name in enumerate(names):
        pair.run(f"CREATE TABLE {name} (a{i}, b{i})")
        pair.run(f"INSERT INTO {name} VALUES ({i}, {i + 1}), ({i + 1}, {i + 2})")
    where = " AND ".join(f"b{i} = a{i + 1}" for i in range(21))
    pair.run(f"SELECT a0, b21 FROM {', '.join(names)} WHERE {where}")
    pair.run(f"SELECT count(*) FROM {', '.join(names)} WHERE a0 = 0 AND {where}")
    pair.close()


def test_between_uses_indexes_with_the_same_results():
    pair = Pair()
    for sql in [
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c)",
        "CREATE INDEX ta ON t (a)", "CREATE INDEX tb ON t (b, a)", "CREATE INDEX tc ON t (c)",
        "INSERT INTO t (a, b, c) VALUES (1, 'x', 1), (5, 'x', '5'), (NULL, 'y', NULL), (10, '10', 2.5), "
        "(7, 'y', x'00'), (3, '3', 'abc'), (-2, 'x', 7)",
    ]:
        pair.run(sql)
    for where in [
        "a BETWEEN 1 AND 7", "a BETWEEN '1' AND '7'", "a BETWEEN 7 AND 1", "a BETWEEN NULL AND 5",
        "id BETWEEN 2 AND 5", "id BETWEEN '2' AND 5.5", "b = 'x' AND a BETWEEN 0 AND 5",
        "b BETWEEN 1 AND 5", "b BETWEEN '1' AND '5'", "c BETWEEN 1 AND 6", "c BETWEEN '1' AND 'b'",
        "4 BETWEEN a AND 20", "a NOT BETWEEN 1 AND 5", "a BETWEEN 1 AND 7 OR b = 'y'",
    ]:
        pair.run(f"SELECT id FROM t WHERE {where} ORDER BY id")
    pair.close()
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER)")
    db.execute("CREATE INDEX ta ON t (a)")
    assert db.execute("EXPLAIN SELECT count(*) FROM t WHERE a BETWEEN 3 AND 5") == [
        ("t", "SEARCH USING COVERING INDEX ta (a>=? AND a<=?)")]


def test_parser_folds_and_with_zero():
    """SQLite's parser turns X AND 0 into 0 before names are resolved, unless
    a side calls a function: the other side may name missing columns."""
    pair = Pair(check_messages=True)
    pair.run("CREATE TABLE t (c0, c1)")
    pair.run("INSERT INTO t VALUES (35, 1), (2, 2)")
    for sql in [
        "SELECT * FROM t WHERE (0 AND nope AND c0 < 1) OR c0 = 35",
        "SELECT c0 AS k FROM t WHERE (0 AND (x'' != NOT (nope))) OR k = 35",
        "SELECT * FROM t WHERE (1 AND 0) AND nope", "SELECT * FROM t WHERE (0) AND nope",
        "SELECT * FROM t AS a JOIN t AS b ON 0 AND nope", "SELECT 0 AND nope, 0x0 AND 1 FROM t",
        "SELECT * FROM t WHERE 0 AND (SELECT abs(nope))",
        "SELECT * FROM t WHERE 0 AND abs(nope)", "SELECT * FROM t WHERE 0 AND nope LIKE 1",
        "SELECT * FROM t WHERE -0 AND nope", "SELECT * FROM t WHERE 0.0 AND nope",
        "SELECT * FROM t WHERE FALSE AND nope",
    ]:
        pair.run(sql)
    pair.close()


def test_true_and_false_everywhere():
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t (c0, c1)", "CREATE INDEX t0 ON t (c0)", "INSERT INTO t VALUES (1, 2), (0, 1), (2, 0)",
        "SELECT * FROM t WHERE FALSE", "SELECT * FROM t WHERE TRUE ORDER BY c0",
        "SELECT * FROM t WHERE c0 = true", "SELECT * FROM t WHERE true = c0 OR c0 IN (false, 5) ORDER BY true, c0",
        "SELECT * FROM t AS a JOIN t AS b ON true WHERE b.c0 = true OR false ORDER BY a.c0, b.c0",
        "SELECT count(*) FROM t GROUP BY true HAVING true", "UPDATE t SET c1 = true WHERE c0 = false",
        "DELETE FROM t WHERE c1 = false", "SELECT * FROM t ORDER BY c0",
        "CREATE TABLE u (\"true\", b)", "INSERT INTO u VALUES (7, 1)", "SELECT * FROM u WHERE true = 7",
        "SELECT b IS TRUE, b IS NOT TRUE FROM u",  # a column named true: a plain IS
        "SELECT x, x IS TRUE, x IS FALSE, x IS NOT TRUE, x IS NOT FALSE, x IS 1, x IS NOT 0 FROM "
        "(SELECT NULL AS x UNION ALL SELECT 0 UNION ALL SELECT 48 UNION ALL SELECT -0.5 UNION ALL SELECT 'abc' "
        "UNION ALL SELECT '12x' UNION ALL SELECT x'01' UNION ALL SELECT 0.0) ORDER BY 1",
        "SELECT * FROM t WHERE c0 IS TRUE AND c1 IS NOT FALSE ORDER BY c0",
    ]:
        pair.run(sql)
    pair.run("SELECT ? IS NOT FALSE, ? IS TRUE, ? IS FALSE", parameters=[None, "7", 0.0])
    pair.close()


@pytest.mark.parametrize("sql", [
    "VALUES (1, 2), (2, (5 IS NOT TRUE)), (3, 5 IS TRUE), (4, NULL IS NOT FALSE), (5, 'x' IS FALSE)",
    "WITH x AS (SELECT 1) SELECT * FROM (VALUES (1, 2), (2, (5 IS NOT TRUE)))",
    "VALUES (CAST(1 AS INT), 2), (2, (5 IS NOT TRUE))",
    "VALUES (+CAST(1 AS INT), 2), (2, (5 IS NOT TRUE))",
    "VALUES ((SELECT 1), 2), (2, (5 IS NOT TRUE))",
    "VALUES (1, 2), (abs(2) + (2 LIKE 2), (5 IS NOT TRUE))",
    "VALUES (1, 2), (random() * 0, (5 IS NOT TRUE)), (3, 5 IS NOT TRUE)",
    "VALUES (1, 2), (CAST(2 AS INT), 3), (3, (5 IS NOT TRUE))",
    "VALUES (1, 2), (CASE WHEN 1 THEN 2 END, NOT (5 IS TRUE) + (3 IN (1, 2)) * 10)",
    "VALUES (5 IS NOT TRUE, 2), (5 IS NOT TRUE, 3)",
    "SELECT 1, 2 UNION ALL VALUES (2, (5 IS NOT TRUE))",
    "WITH v AS (VALUES (1, 2), (2, (5 IS NOT TRUE))) SELECT * FROM v",
    "VALUES (1, 2), (2, (5 IS NOT TRUE)), ((WITH x AS (SELECT 3) SELECT * FROM x), 4)",
    "VALUES (1, 2), (date('2020-01-01') IS NULL, 5 IS NOT TRUE), (CURRENT_DATE IS NULL, 5 IS TRUE)",
])
def test_values_rows_coded_without_resolving_names(sql):
    """SQLite codes later VALUES rows directly when it can (sqlite3MultiValues):
    their ``x IS TRUE`` is then ``x IS 1``, not a truth test."""
    pair = Pair()
    pair.run(sql)
    pair.run("CREATE TABLE t (a, b)")
    if sql.startswith("VALUES"):
        pair.run("INSERT INTO t " + sql)
        pair.run("SELECT * FROM t")
    pair.close()


def test_full_scan_through_a_covering_index():
    """Like SQLite, a full scan reads a narrower index holding every column the
    query uses, so rows come in that index's order (seen by bare columns of
    aggregates, group_concat and LIMIT without ORDER BY)."""
    pair = Pair()
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b VARCHAR(30), c FLOAT, d)")
    pair.run("CREATE TABLE u (x INT UNIQUE, y VARCHAR(5))")
    pair.run("CREATE INDEX t_c ON t (c)")
    pair.run("CREATE INDEX t_ba ON t (b, a)")
    pair.run("CREATE INDEX t_d ON t (d)")
    pair.run("INSERT INTO t VALUES (1, 'q', 'z', 3.5, 2), (2, 'b', 'a', -1, 9), (3, NULL, 'm', 0, NULL), "
             "(4, 'a', NULL, NULL, -4), (5, 'z', 'a', 7, 1)")
    pair.run("INSERT INTO u VALUES (3, 'a'), (NULL, 'b'), (-1, 'c')")
    for sql in [
        "SELECT c FROM t", "SELECT id, c FROM t", "SELECT a, b FROM t", "SELECT group_concat(a) FROM t",
        "SELECT sum(c), c FROM t", "SELECT d FROM t LIMIT 2", "SELECT rowid, d FROM t WHERE d > 0",
        "SELECT x FROM u", "SELECT x, y FROM u", "SELECT * FROM t",
        "SELECT (SELECT sum(t.c) FROM u WHERE t.c < u.x) FROM t",
        "SELECT t.c, u.x FROM t CROSS JOIN u", "SELECT group_concat(c) FROM t WHERE c > 0 OR c < -0.5",
        "SELECT group_concat(id) FROM t WHERE c = 7 OR c = -1 OR id = 1", "SELECT group_concat(id) FROM t WHERE d IN (9, 1, 2)",
    ]:
        pair.run(sql, ordered=True)
        theirs = next(r[-1] for r in pair.lite.execute("EXPLAIN QUERY PLAN " + sql))
        mine = next(r[-1] for r in pair.mini.execute("EXPLAIN QUERY PLAN " + sql))
        if theirs.startswith("MULTI-INDEX OR"):
            assert mine.startswith("MULTI-INDEX OR"), sql
            continue
        words = theirs.split(" ")
        expected = " ".join(words[:1] + words[2:]).replace("sqlite_autoindex", "minidb_autoindex")
        if mine.startswith("MULTI-INDEX IN"):  # SQLite shows IN as one search
            mine = mine.split("(", 1)[1].split(";")[0]
        assert mine.replace("USING INDEX", "USING COVERING INDEX") == expected.replace(
            "USING INDEX", "USING COVERING INDEX"), sql  # (SQLite names the table)
    pair.close()


def test_index_hints_steer_the_planner():
    """As SQLite: NOT INDEXED reads the table itself (row id lookups only),
    INDEXED BY uses that index alone, scanning all of it when nothing
    narrows the search; row orders show which was used."""
    pair = Pair()
    pair.run("CREATE TABLE t1 (x INTEGER, y TEXT, z)")
    pair.run("CREATE INDEX t1_x ON t1 (x)")
    pair.run("CREATE INDEX t1_yx ON t1 (y, x)")
    pair.run("INSERT INTO t1 VALUES (5, 'b', 1), (1, 'a', 2), (3, NULL, 3), (NULL, 'c', 4), (1, 'z', 5), (2.5, 'q', 6)")
    for sql in [
        "SELECT group_concat(x) FROM t1 NOT INDEXED", "SELECT group_concat(DISTINCT x) FROM t1 NOT INDEXED",
        "SELECT group_concat(x) FROM t1 INDEXED BY t1_yx", "SELECT group_concat(z) FROM t1 INDEXED BY t1_x",
        "SELECT group_concat(z) FROM t1 INDEXED BY t1_x WHERE x > 1",
        "SELECT group_concat(z) FROM t1 NOT INDEXED WHERE x = 1 OR x = 3",
        "SELECT group_concat(z) FROM t1 INDEXED BY t1_yx WHERE x = 1 OR x = 3",
        "SELECT a.z, b.z FROM t1 AS a, t1 AS b NOT INDEXED WHERE a.x = b.x",
        "SELECT group_concat(rowid) FROM t1 NOT INDEXED WHERE rowid IN (3, 1)",
    ]:
        pair.run(sql, ordered=True)
    pair.run("UPDATE t1 NOT INDEXED SET z = z + 1 WHERE x = 1")
    pair.run("DELETE FROM t1 INDEXED BY t1_x WHERE x = 3")
    pair.run("SELECT * FROM t1 ORDER BY rowid")
    pair.close()


def test_which_row_compounds_and_groups_keep():
    """Equal rows of a compound: UNION keeps the right side's first, INTERSECT
    and EXCEPT the left side's first (SQLite merges the sorted sides).  Bare
    columns come from the rows the last min() / max() does not skip (a NULL
    is skipped once there is a value; FILTER first sets "not the first row");
    without min() / max() from the group's first row."""
    pair = Pair()
    for sql in [
        "SELECT x FROM (SELECT 1 x UNION ALL SELECT 1.0) UNION SELECT 5",
        "SELECT x FROM (SELECT 1.0 x UNION ALL SELECT 1) UNION SELECT 5", "SELECT 1 UNION SELECT 1.0",
        "SELECT 1.0 UNION SELECT 1", "SELECT 1 UNION SELECT 1.0 UNION SELECT 1",
        "SELECT x FROM (SELECT 1 x UNION ALL SELECT 1.0) INTERSECT SELECT 1", "SELECT 1.0 INTERSECT SELECT 1",
        "SELECT x FROM (SELECT 1.0 x UNION ALL SELECT 1) EXCEPT SELECT 5", "SELECT 1 UNION ALL SELECT 1.0 UNION SELECT 5",
    ]:
        pair.run(sql)
    pair.run("CREATE TABLE t (a COLLATE nocase, b, n)")
    pair.run("INSERT INTO t VALUES ('abc', 1, NULL), ('ABC', 2, NULL), ('Abc', 3, 5), ('b', 4, NULL), ('B', 5, 1), "
             "('x', 1, 1), ('X', 2, 1), ('y', 7, 2), ('Y', 3, NULL), ('Y', 9, 8)")
    aggregates = ["max(n)", "min(n)", "max(b)", "count(*)", "max(n) FILTER (WHERE b > 2)",
                  "min(b) FILTER (WHERE n IS NULL)", "max(DISTINCT n)", "min(n) FILTER (WHERE b < 3)"]
    for first in aggregates:
        for second in [None] + aggregates:
            calls = first if second is None else f"{first}, {second}"
            pair.run(f"SELECT a, b, {calls} FROM t GROUP BY a")
            pair.run(f"SELECT a, b, {calls} FROM t")
    pair.close()


def test_right_join_using_collation():
    """In a FROM clause with a RIGHT JOIN, USING compares coalesce() of the
    left tables' columns: it has the first one's collation (and affinity)."""
    pair = Pair()
    for sql in [
        "CREATE TABLE t0 (c0 INT)", "CREATE TABLE t1 (c0 REAL COLLATE rtrim, c3)",
        "INSERT INTO t0 VALUES (1), ('b')", "INSERT INTO t1 VALUES ('b ', -4), (0.0, 1), (1.0, 2)",
        "SELECT a.c3, c.c0 FROM t1 a LEFT JOIN t1 b USING (c0) RIGHT JOIN t0 c USING (c0)",
        "SELECT a.c0, c.c3 FROM t0 a LEFT JOIN t1 b USING (c0) RIGHT JOIN t1 c USING (c0)",
        "SELECT a.c0, c.c3 FROM t0 a LEFT JOIN t0 b USING (c0) RIGHT JOIN t1 c USING (c0)",
        "SELECT * FROM t1 a FULL JOIN t1 b USING (c0) FULL JOIN t0 c USING (c0)",
    ]:
        pair.run(sql)
    pair.close()


def test_min_max_on_an_equal_column():
    """A lone min(x) / max(x) with "x = <other tables' expression>" in WHERE
    reads only the first matching row in SQLite (it takes x as ordered),
    even where a numeric comparison lets '1' and '1.0' both match."""
    pair = Pair()
    for sql in [
        "CREATE TABLE t1 (c1 FLOAT, c2 INTEGER, c3 TEXT, n TEXT COLLATE nocase, UNIQUE (c3, c2))",
        "INSERT INTO t1 VALUES (1.0, NULL, '1.0', 'b'), (3.0, 0, '1.0', 'B'), (1.0, NULL, '1', 'A'), (3.0, 1, '1', 'a')",
        "CREATE TABLE a (c1 FLOAT)", "INSERT INTO a VALUES (1.0)",
        "SELECT (SELECT min(c3) FROM t1 AS s WHERE (c1 > 1) AND s.c3 = a.c1) FROM a",
        "SELECT (SELECT max(c3) || c2 FROM t1 AS s WHERE a.c1 = s.c3 AND c1 > 0) FROM a",
        "SELECT (SELECT min(c3) FROM t1 AS s WHERE s.c3 = a.c1 AND c2 IS NOT NULL) FROM a",
        "SELECT (SELECT min(c3), count(*) FROM t1 AS s WHERE (c1 > 1) AND s.c3 = a.c1) FROM a",
        "SELECT max(n), c2 FROM t1 WHERE n = 'a'", "SELECT max(n COLLATE binary), c2 FROM t1 WHERE n = 'a'",
        "SELECT min(c3) FROM t1 WHERE c3 = c1", "SELECT min(c3) FROM t1 WHERE c3 = 1 GROUP BY c2",
    ]:
        pair.run(sql)
    pair.close()


def test_truth_tests_and_likely():
    """"x IS [NOT] TRUE / FALSE" tests truth even with a COLLATE on TRUE
    (SQLite skips it) but not inside likely(), which SQLite has not yet
    resolved when it looks; likely() / unlikely() / likelihood() are the
    value itself, without its affinity or collation."""
    pair = Pair(check_messages=True)
    for sql in [
        'CREATE TABLE t (a INTEGER, b TEXT COLLATE nocase, "true")',
        "INSERT INTO t VALUES (1, 'X', 0), ('10', 'y', 5), (NULL, NULL, 1)",
        "SELECT '10' IS NOT (TRUE COLLATE RTRIM), 0 IS (FALSE COLLATE nocase), NULL IS NOT likely(TRUE), "
        "2 IS unlikely(true), 3 IS likelihood(false, 0.5)",
        "SELECT a IS (TRUE COLLATE nocase), a IS NOT likely(FALSE) FROM t",
        'SELECT a IS ("true" COLLATE nocase) FROM t', "SELECT a, a IS NOT (TRUE) FROM t WHERE a IS (TRUE COLLATE rtrim)",
        "SELECT likely(a) = '1', unlikely(b) = 'x', likelihood(a, 0.5) = '1', typeof(likely(a)), likely(b) IN ('x'), "
        "a = likely('1') FROM t",
        "SELECT likelihood(1, 1)", "SELECT likelihood(1, -0.5)", "SELECT likelihood(1, 1.5)",
        "SELECT likelihood(1, 1e0)", "SELECT likely(1, 2)", "SELECT likelihood(1)", "SELECT likelihood(5, (0.5))",
        "SELECT a FROM t WHERE likely(a > 0) AND unlikely(b IS NOT NULL)",
    ]:
        pair.run(sql)
    pair.close()


def test_group_by_through_an_ordering_index():
    """SQLite scans an index that orders the GROUP BY columns (all of them in
    any order, or the first terms in order) rather than sort: a group's
    first row - its bare columns, the value shown for a collation's equal
    texts - comes in index order, and so do the groups."""
    pair = Pair()
    for sql in [
        "CREATE TABLE t (a TEXT COLLATE rtrim, b INT, c, d TEXT)",
        "INSERT INTO t VALUES ('b', 10, 1, 'x'), ('x', 1, 2, 'y'), ('b ', -5, 3, 'z'), ('B', 0, 4, 'w')",
        "CREATE INDEX i1 ON t (a, b)", "CREATE INDEX i2 ON t (b, a COLLATE nocase)", "CREATE INDEX i3 ON t (d, a)",
        "SELECT a, count(*) FROM t GROUP BY a", "SELECT a, b, count(*) FROM t GROUP BY b, a",
        "SELECT a, count(*) FROM t GROUP BY a COLLATE nocase", "SELECT a, b FROM t GROUP BY a COLLATE binary",
        "SELECT a, c FROM t GROUP BY a", "SELECT a, max(b) FROM t GROUP BY a", "SELECT d, a, count(*) FROM t GROUP BY a, d",
        "SELECT b, a FROM t GROUP BY b, a COLLATE nocase", "SELECT a, c, count(*) FROM t GROUP BY a, c",
        "SELECT a, c, count(*) FROM t GROUP BY c, a",
        # A cross join keeps the table whose index orders the groups outermost, even after ANALYZE.
        "CREATE TABLE u (x)", "INSERT INTO u VALUES (1), (2), (3)", "ANALYZE",
        "SELECT t.a, count(*) FROM t, u GROUP BY t.a", "SELECT t.a, count(*), max(u.x) FROM t, u GROUP BY t.a",
    ]:
        pair.run(sql, ordered=True)
    pair.close()


def test_outer_aggregates_and_window_functions():
    """An aggregate of the outer query inside a subquery is a misuse when that
    query has window functions (SQLite moves it into a subquery first)."""
    pair = Pair(check_messages=True)
    for sql in [
        "CREATE TABLE t0 (a)", "CREATE TABLE t2 (c3)", "INSERT INTO t2 VALUES (1), (2)", "INSERT INTO t0 VALUES (5)",
        "SELECT (SELECT avg(c3) FROM t0 AS s), dense_rank() OVER () FROM t2",
        "SELECT (SELECT avg(c3) FROM t0 AS s), row_number() OVER (ORDER BY c3) FROM t2",
        "SELECT (SELECT avg(c3) FROM t0 AS s) FROM t2", "SELECT avg(c3), row_number() OVER () FROM t2",
        "SELECT (SELECT avg(c3) FROM t0 AS s) FROM t2 ORDER BY row_number() OVER ()",
        "SELECT avg(c3), (SELECT max(c3) FROM t0), row_number() OVER () FROM t2",
        "SELECT avg(c3), (SELECT max(t0.a) FROM t0), row_number() OVER () FROM t2",
        "SELECT (SELECT max(c3) FROM t0) w FROM t2 WINDOW x AS ()",
    ]:
        pair.run(sql)
    pair.close()


def test_limit_zero_runs_nothing():
    pair = Pair(check_messages=True)
    pair.run("CREATE TABLE t (a)")
    pair.run("INSERT INTO t VALUES (9223372036854775807), (1)")
    for sql in ["SELECT sum(a) FROM t LIMIT 0", "SELECT sum(a) OVER () FROM t LIMIT 0 OFFSET 1",
                "SELECT 1 LIMIT 0 OFFSET 'x'", "SELECT a FROM t UNION SELECT abs(-9223372036854775808) LIMIT 0",
                "SELECT 1 LIMIT 2 OFFSET 'x'", "SELECT a FROM t LIMIT -1 OFFSET 1"]:
        pair.run(sql)
    pair.close()


def test_folded_and_hides_aliases():
    """SQLite's parser folds '<literal> IS NULL' to 0 and 'X AND 0' to 0, so
    an alias (even of a window function) in the folded part is never resolved."""
    pair = Pair(check_messages=True)
    pair.run("CREATE TABLE t (a, b)")
    pair.run("INSERT INTO t VALUES (1, 2)")
    for sql in ["SELECT sum(a) OVER () AS k FROM t WHERE ('' IS NULL AND a) AND k > 0",
                "SELECT sum(a) OVER () AS k FROM t WHERE k > 0", "SELECT a AS k FROM t WHERE (5 IS NULL) AND k",
                "SELECT count(*) AS k FROM t GROUP BY b HAVING (- 'x' IS NULL AND b) AND k"]:
        pair.run(sql)
    pair.close()
