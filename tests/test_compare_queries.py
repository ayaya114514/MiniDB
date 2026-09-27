"""MiniDB vs sqlite3: ORDER BY, LIMIT, aggregates, GROUP BY, HAVING and joins."""

import itertools
import random

import pytest

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
                 "2147483647", "2147483648", "9223372036854775807", "(1 = 1)", "abs(2)"]:
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
