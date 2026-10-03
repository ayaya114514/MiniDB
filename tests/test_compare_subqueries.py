"""MiniDB vs sqlite3: CASE, CAST, subqueries, compound SELECTs, USING/NATURAL, derived tables."""

import itertools

import pytest

from sqlcompare import Pair

SETUP = [
    "CREATE TABLE t (i INTEGER, s TEXT)",
    "INSERT INTO t VALUES (1, '1'), (2, 'x'), (NULL, NULL), (3, '3.0'), (5, 'five')",
    "CREATE TABLE u (k INTEGER, v TEXT)",
    "INSERT INTO u VALUES (1, 'a'), (1, 'b'), (3, 'c'), (4, NULL)",
    "CREATE TABLE w (i INTEGER, z TEXT)",
    "INSERT INTO w VALUES (1, 'w1'), (5, 'w5'), (7, 'w7')",
    "CREATE TABLE emp (id INTEGER PRIMARY KEY, name TEXT, dept TEXT, salary INTEGER, boss INTEGER)",
    """INSERT INTO emp VALUES (1, 'ann', 'eng', 120, NULL), (2, 'bob', 'eng', 100, 1),
        (3, 'cat', 'ops', 90, 1), (4, 'dan', 'ops', 95, 3), (5, 'eve', NULL, 70, 3),
        (6, 'fay', 'eng', 130, 1), (7, 'gus', 'hr', NULL, 6)""",
    "CREATE INDEX emp_dept ON emp (dept)",
]

VALUES = ["NULL", "0", "1", "-7", "3.9", "-3.9", "1e30", "'12abc'", "'1e3'", "' 42 '", "'abc'", "''",
          "'3.0'", "'3.5'", "'-'", "'0x10'", "' +7.5e1x'", "'9223372036854775808'", "4.0", "1e20"]
TYPES = ["INTEGER", "INT", "BIGINT", "TEXT", "VARCHAR(10)", "CHARACTER(20)", "REAL", "DOUBLE",
         "DOUBLE PRECISION", "FLOAT", "NUMERIC", "DECIMAL(10, 5)", "BOOLEAN", "DATE", "whatever"]


@pytest.fixture
def pair():
    p = Pair()
    p.script(SETUP)
    yield p
    p.close()


def test_cast(pair):
    for value, type_name in itertools.product(VALUES, TYPES):
        expr = f"CAST({value} AS {type_name})"
        pair.run(f"SELECT {expr}, typeof({expr})")
    pair.run("SELECT CAST(i AS TEXT) = '3', CAST(i AS TEXT) = 3, CAST(s AS INTEGER) = '3' FROM t")
    pair.run("SELECT i FROM t WHERE CAST(s AS REAL) > 2")
    pair.run("SELECT CAST(salary AS TEXT) || '$', CAST(name AS INTEGER) FROM emp")


def test_case(pair):
    pair.script([
        "SELECT CASE i WHEN 1 THEN 'one' WHEN '2' THEN 'two' ELSE 'other' END FROM t",
        "SELECT CASE s WHEN 1 THEN 'n' WHEN '3.0' THEN 'three' END FROM t",
        "SELECT CASE WHEN i > 1 THEN i WHEN s IS NULL THEN -1 END FROM t",
        "SELECT CASE NULL WHEN NULL THEN 1 ELSE 0 END, CASE WHEN NULL THEN 1 ELSE 0 END",
        "SELECT CASE i WHEN '3' THEN 'y' ELSE 'n' END, CASE '3' WHEN i THEN 'y' ELSE 'n' END FROM t",
        "SELECT CASE s WHEN 3 THEN 'y' ELSE 'n' END FROM t",
        "SELECT name, CASE WHEN salary >= 120 THEN 'high' WHEN salary >= 95 THEN 'mid' ELSE 'low' END FROM emp",
        "SELECT dept, sum(CASE WHEN salary > 100 THEN 1 ELSE 0 END) FROM emp GROUP BY dept",
        "SELECT name FROM emp ORDER BY CASE dept WHEN 'ops' THEN 0 ELSE 1 END, name",
        "SELECT typeof(CASE WHEN 1 THEN 1 END), typeof(CASE WHEN 0 THEN 1 END)",
        "SELECT CASE 1 WHEN 1 THEN 'a' WHEN 1 THEN 'b' END",
        "SELECT CASE END",
        "SELECT CASE 1 ELSE 2 END",
    ])


def test_scalar_subqueries(pair):
    pair.script([
        "SELECT i, (SELECT v FROM u WHERE k = t.i ORDER BY v), (SELECT count(*) FROM u WHERE k = t.i), (SELECT 5) FROM t",
        "SELECT (SELECT k, v FROM u)",
        "SELECT (SELECT v FROM u ORDER BY v DESC)",
        "SELECT (SELECT max(salary) FROM emp) - salary, name FROM emp",
        "SELECT name FROM emp WHERE salary > (SELECT avg(salary) FROM emp)",
        "SELECT name FROM emp e WHERE salary = (SELECT max(salary) FROM emp WHERE dept = e.dept)",
        "SELECT name, (SELECT name FROM emp b WHERE b.id = e.boss) FROM emp e",
        "SELECT name FROM emp e WHERE (SELECT count(*) FROM emp r WHERE r.boss = e.id) >= 2",
        "SELECT dept, (SELECT count(*) FROM emp x WHERE x.dept = emp.dept) FROM emp GROUP BY dept",
        "SELECT dept, max(salary), (SELECT name FROM emp x WHERE x.salary = max(emp.salary)) FROM emp GROUP BY dept",
        "SELECT name FROM emp ORDER BY (SELECT count(*) FROM emp r WHERE r.boss = emp.id) DESC, name",
        "SELECT (SELECT (SELECT max(i) FROM t) + 1)",
        "SELECT name FROM emp e WHERE salary > (SELECT avg(salary) FROM emp x WHERE x.dept = e.dept AND x.id != e.id)",
        "SELECT (SELECT z FROM w WHERE w.i = (SELECT max(k) FROM u WHERE u.k < t.i)) FROM t",
        "SELECT typeof((SELECT s FROM t WHERE i = 1)), (SELECT s FROM t WHERE i = 1) = 1",
        "SELECT (SELECT 1 WHERE 0)",
        "SELECT i FROM t LIMIT (SELECT 2)",
    ])


def test_in_and_exists_subqueries(pair):
    pair.script([
        "SELECT i, i IN (SELECT k FROM u), s IN (SELECT k FROM u), i NOT IN (SELECT k FROM u), i IN (SELECT NULL) FROM t",
        "SELECT '1' IN (SELECT k FROM u), 1 IN (SELECT v FROM u), 1 IN (SELECT '1'), (SELECT 1) IN (SELECT '1')",
        "SELECT i IN (SELECT k FROM u WHERE 0), NULL IN (SELECT k FROM u WHERE 0), NULL NOT IN (SELECT 1 WHERE 0) FROM t",
        "SELECT 1 IN (SELECT k, v FROM u)",
        "SELECT i, EXISTS (SELECT 1 FROM u WHERE k = t.i), NOT EXISTS (SELECT * FROM u WHERE k = i) FROM t",
        "SELECT name FROM emp WHERE id IN (SELECT boss FROM emp)",
        "SELECT name FROM emp WHERE id NOT IN (SELECT boss FROM emp)",
        "SELECT name FROM emp WHERE id NOT IN (SELECT boss FROM emp WHERE boss IS NOT NULL)",
        "SELECT name FROM emp e WHERE EXISTS (SELECT 1 FROM emp r WHERE r.boss = e.id AND r.dept = e.dept)",
        "SELECT name FROM emp e WHERE NOT EXISTS (SELECT 1 FROM emp r WHERE r.boss = e.id)",
        "SELECT name FROM emp WHERE dept IN (SELECT dept FROM emp GROUP BY dept HAVING count(*) > 1)",
        "SELECT name FROM emp WHERE salary IN (SELECT salary FROM emp WHERE dept = 'ops' UNION SELECT 120)",
        "SELECT k FROM u WHERE k IN (SELECT i FROM t WHERE s = CAST(u.k AS TEXT))",
        "SELECT i FROM t WHERE s IN (SELECT CAST(k AS TEXT) FROM u)",
        "SELECT name FROM emp WHERE id IN (SELECT id FROM emp) AND id = 3",
    ])


def test_subqueries_in_modifications(pair):
    pair.script([
        "CREATE TABLE copy (id INTEGER PRIMARY KEY, name TEXT, top INTEGER)",
        "INSERT INTO copy VALUES ((SELECT max(id) FROM emp) + 1, (SELECT name FROM emp WHERE id = 1), NULL)",
        "UPDATE emp SET salary = (SELECT max(salary) FROM emp) WHERE salary IS NULL",
        "UPDATE emp SET salary = salary + (SELECT count(*) FROM emp r WHERE r.boss = emp.id)",
        "DELETE FROM emp WHERE id IN (SELECT boss FROM emp WHERE dept = 'hr')",
        "DELETE FROM emp WHERE NOT EXISTS (SELECT 1 FROM emp b WHERE b.id = emp.boss) AND boss IS NOT NULL",
        "SELECT * FROM emp",
        "SELECT * FROM copy",
        "UPDATE copy SET top = (SELECT name FROM emp ORDER BY salary DESC LIMIT 1)",
        "SELECT * FROM copy",
    ])
    assert pair.mini.integrity_check() == []


def test_compound_selects(pair):
    pair.script([
        "SELECT i FROM t UNION SELECT k FROM u",
        "SELECT i FROM t UNION ALL SELECT k FROM u",
        "SELECT k FROM u INTERSECT SELECT i FROM t",
        "SELECT i FROM t EXCEPT SELECT k FROM u",
        "SELECT i AS x, s FROM t UNION SELECT k, v FROM u ORDER BY x DESC, 2",
        "SELECT i FROM t UNION SELECT k FROM u ORDER BY i+1",
        "SELECT i FROM t UNION SELECT k FROM u ORDER BY k",
        "SELECT i FROM t UNION SELECT k FROM u ORDER BY 1 LIMIT 2 OFFSET 1",
        "SELECT i, s FROM t UNION SELECT k FROM u",
        "SELECT i FROM t UNION ALL SELECT k FROM u INTERSECT SELECT 3",
        "SELECT 1 UNION ALL SELECT 2 UNION SELECT 1 EXCEPT SELECT 2",
        "SELECT dept FROM emp UNION SELECT 'sales' ORDER BY 1",
        "SELECT dept, count(*) FROM emp GROUP BY dept UNION ALL SELECT 'total', count(*) FROM emp ORDER BY 2, 1",
        "SELECT name FROM emp WHERE salary > 100 EXCEPT SELECT name FROM emp WHERE dept = 'eng'",
        "SELECT i FROM t UNION SELECT k FROM u ORDER BY 0",
        "SELECT i FROM t UNION SELECT k FROM u ORDER BY 2",
        "SELECT DISTINCT i FROM t UNION ALL SELECT DISTINCT k FROM u",
        "SELECT i, i + 1 FROM t UNION SELECT k, k + 1 FROM u ORDER BY i + 1 DESC",
        "SELECT 'a' UNION SELECT 'a' UNION SELECT 'b'",
    ])


def test_using_natural_and_derived_tables(pair):
    pair.script([
        "SELECT * FROM t JOIN w USING (i)",
        "SELECT * FROM t LEFT JOIN w USING (i)",
        "SELECT i, t.i, w.i FROM t LEFT JOIN w USING (i)",
        "SELECT * FROM t NATURAL JOIN w",
        "SELECT * FROM t NATURAL LEFT JOIN w",
        "SELECT * FROM t JOIN w USING (nope)",
        "SELECT * FROM t JOIN u USING (i)",
        "SELECT w.*, t.* FROM t JOIN w USING (i)",
        "SELECT count(*) FROM t NATURAL JOIN u",
        "SELECT x.a, x.b FROM (SELECT i AS a, s AS b FROM t WHERE i > 1) AS x",
        "SELECT * FROM (SELECT k, count(*) c FROM u GROUP BY k) WHERE c > 1",
        "SELECT typeof(a) FROM (SELECT '1' AS a)",
        "SELECT * FROM (SELECT 1 UNION SELECT 2) ORDER BY 1",
        "SELECT d.dept, d.n, e.name FROM (SELECT dept, count(*) AS n FROM emp GROUP BY dept) d JOIN emp e ON e.dept = d.dept",
        "SELECT a FROM (SELECT i AS a FROM t) WHERE a = '3'",
        "SELECT a FROM (SELECT s AS a FROM t) WHERE a = 3",
        "SELECT a FROM (SELECT i + 0 AS a FROM t) WHERE a = '3'",
        "SELECT * FROM (SELECT i FROM t) AS a JOIN (SELECT k FROM u) AS b ON a.i = b.k",
        "SELECT * FROM (SELECT i FROM t) JOIN w USING (i)",
        "SELECT (SELECT count(*) FROM (SELECT k FROM u WHERE u.k <= t.i)) FROM t",
        "SELECT max(n) FROM (SELECT dept, count(*) AS n FROM emp GROUP BY dept)",
    ])


def test_subquery_errors(pair):
    pair.script([
        "SELECT (SELECT nope FROM u)",
        "SELECT * FROM (SELECT nope FROM u)",
        "SELECT i FROM t WHERE i IN (SELECT i, s FROM t)",
        "SELECT * FROM (SELECT 1) AS x JOIN (SELECT 2) AS x ON 1",
        "SELECT EXISTS (SELECT * FROM nope)",
        "SELECT i FROM t UNION SELECT k FROM nope",
    ])


def test_error_messages_match_sqlite():
    pair = Pair(check_messages=True)
    pair.script(SETUP)
    pair.script([
        "SELECT (SELECT k, v FROM u)",
        "SELECT 1 IN (SELECT k, v FROM u)",
        "SELECT i, s FROM t UNION SELECT k FROM u",
        "SELECT i FROM t INTERSECT SELECT k, v FROM u",
        "SELECT i FROM t UNION SELECT k FROM u ORDER BY i+1",
        "SELECT i FROM t UNION SELECT k FROM u ORDER BY 3",
        "SELECT * FROM t JOIN w USING (nope)",
        "SELECT * FROM t JOIN u USING (i)",
        "SELECT (SELECT nope FROM u)",
    ])
    pair.close()


def test_evaluation_timing_matches_sqlite():
    pair = Pair()
    pair.script([
        "CREATE TABLE t (a INTEGER)",
        "INSERT INTO t VALUES (1), (2), (3)",
        "INSERT INTO t VALUES ((SELECT count(*) FROM t)), ((SELECT count(*) FROM t))",
        "UPDATE t SET a = (SELECT count(*) FROM t AS s WHERE s.a = t.a)",
        "UPDATE t SET a = (SELECT max(a) FROM t) + 1",
        "DELETE FROM t WHERE a > (SELECT min(a) FROM t)",
        "SELECT * FROM t",
        "SELECT 1 WHERE 0 AND (SELECT x FROM nosuch)",
        "SELECT 1 WHERE (SELECT x FROM nosuch) AND 0",
        "SELECT 1 WHERE 0 AND nosuch_column",
        "SELECT 1 WHERE 0 AND abs(nosuch_column)",
        "SELECT 0 AND (SELECT x FROM nosuch), 1 FROM t",
        "SELECT a FROM t WHERE a > 0 AND (0 AND (SELECT x FROM nosuch))",
    ])
    pair.close()


def test_constant_conditions_are_tested_before_the_loop():
    """SQLite tests WHERE terms that use none of the query's tables (and no
    subquery) once, before its loop: a false one skips the loop and so the
    errors that the rows would have raised; a failing one fails even when
    the table is empty."""
    pair = Pair(check_messages=True)
    pair.script([
        "CREATE TABLE big (x INTEGER)",
        "INSERT INTO big VALUES (9223372036854775807), (1)",
        "CREATE TABLE t (a INTEGER)",
        "INSERT INTO t VALUES (1), (2), (3)",
        "CREATE TABLE empty (a INTEGER)",
        "SELECT y FROM (SELECT (SELECT sum(x) FROM big) AS y FROM t) AS d WHERE 0",
        "SELECT y FROM (SELECT (SELECT sum(x) FROM big) AS y FROM t) AS d WHERE y IS NULL AND 0",
        "SELECT y FROM (SELECT (SELECT sum(x) FROM big) AS y FROM t) AS d WHERE 1",
        "SELECT count(*), sum(a) FROM t WHERE 0",
        "SELECT count(*) FROM t WHERE NULL",
        "SELECT a FROM t WHERE 1 AND a > 1",
        "SELECT * FROM empty WHERE abs(-9223372036854775808)",
        "SELECT * FROM empty WHERE a > 0 AND abs(-9223372036854775808)",
        "SELECT t.a, e.a FROM t LEFT JOIN empty AS e ON e.a = t.a WHERE 0",
        "SELECT t.a, e.a FROM t LEFT JOIN empty AS e ON 0",
        "SELECT a, (SELECT count(*) FROM t AS s WHERE o.a > 1) FROM t AS o",
        "DELETE FROM empty WHERE abs(-9223372036854775808)",
        "UPDATE t SET a = a + 1 WHERE 0 AND a",
        "DELETE FROM t WHERE 1 AND a = 3",
        "SELECT * FROM t",
    ])
    pair.run("SELECT a FROM t WHERE ?", parameters=[0])
    pair.run("SELECT a FROM t WHERE ? AND a < 3", parameters=[1])
    pair.close()


def test_affinity_of_compound_subqueries(pair):
    """A compound SELECT in FROM (a view, a CTE) gets the affinity
    sqlite3SubqueryColType gives it - none when its SELECTs disagree -
    while as a scalar subquery or IN's right side it has its last SELECT's."""
    pair.run("CREATE TABLE c (a INTEGER, b TEXT, r REAL, d)")
    pair.run("INSERT INTO c VALUES (1, '1', 1.0, '1')")
    for arms in ["a, b", "b, a", "d, a", "b, d", "a, r", "'x', a", "+a, b", "CAST(b AS INT), r",
                 "NULL, b, a", "NULL, a, r", "b, CASE WHEN 1 THEN 'x' ELSE 2 END", "a, CASE WHEN 1 THEN 3 ELSE 2 END",
                 "a, a || 'x'", "a, abs(a)", "b, x'31'"]:
        parts = [f"SELECT {arm} AS k FROM c" for arm in arms.split(", ")]
        for operator in ("UNION ALL", "UNION", "EXCEPT"):
            body = f" {operator} ".join(parts)
            for literal in ("1", "'1'"):
                pair.run(f"SELECT * FROM ({body}) WHERE k = {literal}")
        pair.run(f"WITH w AS ({' UNION ALL '.join(parts)}) SELECT * FROM w WHERE k = '1'")
    pair.run("CREATE VIEW cv AS SELECT a FROM c UNION ALL SELECT b FROM c")
    pair.run("SELECT * FROM cv WHERE a = '1'")
    pair.run("SELECT * FROM c WHERE a IN (SELECT a FROM c UNION SELECT b FROM c)")
    pair.run("SELECT (SELECT b FROM c UNION SELECT a FROM c ORDER BY 1 LIMIT 1) = 1")
