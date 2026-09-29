"""MiniDB vs sqlite3 on the statements supported so far."""

import itertools
import random

import pytest

from minidb import Database
from sqlcompare import Pair

LITERALS = [
    "NULL", "0", "1", "-1", "2", "7", "1.5", "-2.5", "0.0", "'1'", "'1.0'", "'abc'", "''",
    "' 12'", "'12abc'", "'ABC'", "9223372036854775807", "-9223372036854775808", "'1e2'",
    "x''", "x'61'", "x'3132'", "x'ff00'",
]

BINARY_OPERATORS = [
    "+", "-", "*", "/", "%", "||", "=", "!=", "<", "<=", ">", ">=", "IS", "IS NOT",
    "AND", "OR", "LIKE", "&", "|", "<<", ">>",
]


@pytest.fixture
def pair():
    p = Pair()
    yield p
    p.close()


@pytest.mark.parametrize("op", BINARY_OPERATORS)
def test_binary_operators_on_literals(pair, op):
    for a, b in itertools.product(LITERALS, repeat=2):
        pair.run(f"SELECT {a} {op} {b}, typeof({a} {op} {b})")


@pytest.mark.parametrize("value", LITERALS)
def test_unary_operators_and_functions(pair, value):
    for expr in ["-{v}", "+{v}", "NOT {v}", "- -{v}", "~{v}", "~~{v}", "{v} << 63", "{v} >> 70",
                 "{v} << -2", "-1 >> {v}", "1 << {v}", "abs({v})", "length({v})", "lower({v})",
                 "upper({v})", "typeof({v})", "coalesce({v}, 'x')", "ifnull({v}, 3)",
                 "nullif({v}, 1)", "min({v}, 1)", "max({v}, 'a', 2)", "{v} IS NULL",
                 "{v} IN (1, 'abc', NULL)", "{v} NOT IN (1, 2)", "{v} IN (1, 2)",
                 "{v} BETWEEN 0 AND 2", "{v} NOT BETWEEN 'a' AND 'z'"]:
        sql = "SELECT " + expr.format(v=value)
        pair.run(f"{sql}, typeof(({sql[7:]}))")


def test_precedence_matches_sqlite(pair):
    rng = random.Random(0)
    operators = ["+", "-", "*", "||", "=", "<", ">=", "!=", "AND", "OR", "IS", "%"]
    operands = ["1", "2", "3", "0", "NULL", "'a'", "'2'", "NOT 1", "-2"]
    for _ in range(3000):
        parts = [rng.choice(operands)]
        for _ in range(rng.randint(1, 5)):
            parts += [rng.choice(operators), rng.choice(operands)]
        expr = " ".join(parts)
        pair.run(f"SELECT {expr}")


def test_like_patterns(pair):
    for value in ["'abc'", "'ABC'", "'a_c'", "'a%c'", "''", "'xabcx'", "'日本語'", "123", "1.5"]:
        for pattern in ["'abc'", "'a%'", "'%c'", "'%b%'", "'a_c'", "'_'", "'%'", "''", "'A%C'",
                        "'%%'", "'日%'", "'1%'", "'1._'", "'__'"]:
            pair.run(f"SELECT {value} LIKE {pattern}, {value} NOT LIKE {pattern}")


def test_column_affinity_on_insert(pair):
    pair.run("CREATE TABLE t (i INTEGER, s TEXT)")
    for value in LITERALS + ["'  7  '", "'3.0'", "3.0", "'-0.0'", "'.5'", "'1.'", "1e18",
                             "'0x10'", "'1e400'", "9.2e18", "-9.223372036854775808e18"]:
        pair.run(f"INSERT INTO t VALUES ({value}, {value})")
    pair.run("SELECT i, typeof(i), s, typeof(s) FROM t")


def test_comparisons_with_column_affinity(pair):
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, i INTEGER, s TEXT)")
    stored = ["NULL", "5", "'5'", "'abc'", "2.5", "'2.5'", "-3", "''"]
    for value in stored:
        pair.run(f"INSERT INTO t (i, s) VALUES ({value}, {value})")
    for op in ["=", "<", ">=", "!=", "IS"]:
        for literal in ["5", "'5'", "'5.0'", "2.5", "'abc'", "NULL", "'10'", "10"]:
            pair.run(f"SELECT id FROM t WHERE i {op} {literal}")
            pair.run(f"SELECT id FROM t WHERE s {op} {literal}")
            pair.run(f"SELECT id FROM t WHERE {literal} {op} i")
            pair.run(f"SELECT id FROM t WHERE id {op} {literal}")
        pair.run(f"SELECT id FROM t WHERE i {op} s")
        pair.run(f"SELECT id FROM t WHERE s {op} i")
    for expr in ["i IN ('5', 2.5)", "s IN (5, 2.5)", "'5' IN (i)", "5 IN (s)", "s IN (i)",
                 "i BETWEEN '1' AND '9'", "s BETWEEN 1 AND 9", "+i = '5'", "i || '' = '5'",
                 "s LIKE 5", "i LIKE '5%'"]:
        pair.run(f"SELECT id FROM t WHERE {expr}")


TYPE_NAMES = [
    "INTEGER", "INT", "TINYINT", "UNSIGNED BIG INT", "INT8", "TEXT", "VARCHAR(30)",
    "CHARACTER(20)", "NATIVE CHARACTER(70)", "CLOB", "BLOB", "", "REAL", "DOUBLE",
    "DOUBLE PRECISION", "FLOAT", "NUMERIC", "DECIMAL(10,5)", "BOOLEAN", "DATE", "DATETIME",
    "STRING", "FLOATING POINT", "POINT",  # "FLOATING POINT" has INT in it: INTEGER affinity
]


@pytest.mark.parametrize("type_name", TYPE_NAMES)
def test_affinity_of_declared_types(pair, type_name):
    pair.run(f"CREATE TABLE t (id INTEGER PRIMARY KEY, v {type_name})")
    values = LITERALS + ["'  7  '", "'3.0'", "3.0", "'-0.0'", "'.5'", "'1.'", "1e18", "'0x10'",
                         "'1e400'", "9.2e18", "'99999999999999999999'", "4.5", "'4.5'"]
    for value in values:
        pair.run(f"INSERT INTO t (v) VALUES ({value})")
    pair.run("SELECT id, v, typeof(v) FROM t ORDER BY id")
    pair.run("UPDATE t SET v = v || '' WHERE id % 3 = 0")
    pair.run("UPDATE t SET v = id * 2 WHERE id % 3 = 1")
    pair.run("SELECT id, v, typeof(v) FROM t ORDER BY id")
    for literal in ["5", "'5'", "'5.0'", "4.5", "'4.5'", "'abc'", "10", "'10'"]:
        pair.run(f"SELECT id FROM t WHERE v = {literal} ORDER BY id")
        pair.run(f"SELECT id FROM t WHERE v < {literal} ORDER BY id")
    pair.run("CREATE INDEX tv ON t (v)")
    for literal in ["5", "'5'", "'5.0'", "4.5", "'abc'", "'10'"]:
        pair.run(f"SELECT id FROM t WHERE v = {literal} ORDER BY id")
        pair.run(f"SELECT id FROM t WHERE v >= {literal} ORDER BY id")
        pair.run(f"SELECT id FROM t WHERE v IN ({literal}, 7) ORDER BY id")
    pair.run("SELECT v, count(*) FROM t GROUP BY v ORDER BY 1")


def test_comparisons_between_affinities(pair):
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, i INTEGER, r REAL, n NUMERIC, s TEXT, b BLOB, x)")
    for value in ["5", "'5'", "'5.0'", "2.5", "'2.5'", "'abc'", "NULL", "''", "'007'"]:
        pair.run(f"INSERT INTO t (i, r, n, s, b, x) VALUES ({value}, {value}, {value}, {value}, {value}, {value})")
    columns = ["i", "r", "n", "s", "b", "x", "CAST(s AS REAL)", "CAST(b AS TEXT)", "+r"]
    for left, right in itertools.product(columns, repeat=2):
        pair.run(f"SELECT id FROM t WHERE {left} = {right} ORDER BY id")
    for column in columns:
        for literal in ["5", "'5'", "'5.0'", "'abc'", "2.5"]:
            pair.run(f"SELECT id FROM t WHERE {column} = {literal} ORDER BY id")
            pair.run(f"SELECT id FROM t WHERE {column} IN ({literal}) ORDER BY id")
        pair.run(f"SELECT id FROM t WHERE {column} IN (SELECT s FROM t) ORDER BY id")
        pair.run(f"SELECT id FROM t WHERE s IN (SELECT {column} FROM t) ORDER BY id")


def test_only_integer_primary_key_is_the_row_id(pair):
    for type_name in ["INTEGER", "integer", "INT", "BIGINT", "INTEGER(8)"]:
        pair.run(f"DROP TABLE IF EXISTS t")
        pair.run(f"CREATE TABLE t (k {type_name} PRIMARY KEY, v TEXT)")
        pair.run("INSERT INTO t (k, v) VALUES (5, 'a'), ('7', 'b'), (NULL, 'c')")
        pair.run("SELECT rowid, k, typeof(k), v FROM t ORDER BY v")
        pair.run("INSERT INTO t (k, v) VALUES ('x', 'd')")


def test_declared_types_survive_reopening(tmp_path):
    pair = Pair(str(tmp_path / "types.db"))
    pair.run("CREATE TABLE t (a FLOAT, b VARCHAR(10), c, d DECIMAL(10, -2), e DOUBLE PRECISION NOT NULL)")
    pair.run("INSERT INTO t VALUES ('1', 2, '3', '4.0', 5)")
    pair.mini.close()
    pair.mini = Database(str(tmp_path / "types.db"))  # sqlite3's side stays open in memory
    pair.run("INSERT INTO t VALUES ('1', 2, '3', '4.0', 5)")
    pair.run("SELECT a, typeof(a), b, typeof(b), c, typeof(c), d, typeof(d), e, typeof(e) FROM t")
    pair.run("INSERT INTO t (a) VALUES (1)")  # NOT NULL survived too


def test_crud_workflow(pair):
    pair.script([
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER, email TEXT UNIQUE)",
        "INSERT INTO users VALUES (1, 'alice', 30, 'a@x')",
        "INSERT INTO users (name, age) VALUES ('bob', 25), ('carol', 41)",
        "INSERT INTO users (age, name, id) VALUES (19, 'dave', 10)",
        "INSERT INTO users (name) VALUES ('erin')",
        "SELECT * FROM users",
        "SELECT id, name FROM users WHERE age > 20 AND age < 40",
        "SELECT name, age * 2 + 1, name || '!' FROM users WHERE id >= 2",
        "SELECT * FROM users WHERE email IS NULL",
        "SELECT * FROM users WHERE age IS NOT NULL AND (name = 'bob' OR name = 'dave')",
        "UPDATE users SET age = age + 1 WHERE age IS NOT NULL",
        "UPDATE users SET email = name || '@example.com' WHERE id > 1",
        "SELECT * FROM users",
        "UPDATE users SET id = id + 100 WHERE id = 10",
        "SELECT rowid, * FROM users",
        "DELETE FROM users WHERE age < 30",
        "SELECT * FROM users",
        "DELETE FROM users",
        "SELECT * FROM users",
        "INSERT INTO users (name) VALUES ('frank')",
        "SELECT * FROM users",
    ])


def test_constraint_errors(pair):
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT NOT NULL, b INTEGER UNIQUE, c TEXT PRIMARY KEY)")
    pair.script([
        "INSERT INTO t VALUES (1, 'x', 1, 'k1')",
        "INSERT INTO t VALUES (1, 'y', 2, 'k2')",
        "INSERT INTO t VALUES (2, NULL, 2, 'k2')",
        "INSERT INTO t VALUES (3, 'z', 1, 'k3')",
        "INSERT INTO t VALUES (4, 'z', 4, 'k1')",
        "INSERT INTO t VALUES (5, 'z', NULL, NULL)",
        "INSERT INTO t VALUES (6, 'z', NULL, NULL)",
        "INSERT INTO t VALUES ('abc', 'z', 7, 'k7')",
        "INSERT INTO t VALUES (2.5, 'z', 8, 'k8')",
        "INSERT INTO t VALUES ('9', 'z', 9, 'k9')",
        "INSERT INTO t VALUES (10, 'a', 10, 'k10'), (11, 'b', 10, 'k11')",
        "SELECT * FROM t",
        "UPDATE t SET b = 1 WHERE id = 9",
        "UPDATE t SET a = NULL WHERE id = 9",
        "UPDATE t SET id = 1 WHERE id = 9",
        "UPDATE t SET id = 'x' WHERE id = 9",
        "UPDATE t SET id = NULL WHERE id = 9",
        "UPDATE t SET id = '20' WHERE id = 9",
        "SELECT * FROM t",
        "UPDATE t SET b = b + 1",
        "SELECT * FROM t",
    ])


def test_statement_level_atomicity(pair):
    pair.script([
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER UNIQUE)",
        "INSERT INTO t VALUES (1, 1), (2, 2), (3, 3)",
        "INSERT INTO t VALUES (4, 4), (5, 5), (6, 1)",
        "SELECT * FROM t",
        "UPDATE t SET v = v + 1",
        "SELECT * FROM t",
        "UPDATE t SET v = 10 - v",
        "SELECT * FROM t",
        "DELETE FROM t WHERE id = 2",
        "INSERT INTO t VALUES (7, 7), (7, 8)",
        "SELECT * FROM t",
    ])


def test_semantic_errors(pair):
    pair.run("CREATE TABLE q (a INTEGER, b TEXT)")
    for sql in [
        "INSERT INTO q VALUES (1)",
        "INSERT INTO q VALUES (1, 2, 3)",
        "INSERT INTO q (zz) VALUES (1)",
        "INSERT INTO q (a) VALUES (1, 2)",
        "SELECT zz FROM q",
        "SELECT * FROM nope",
        "CREATE TABLE q (x INTEGER)",
        "CREATE TABLE r (a INTEGER, a TEXT)",
        "CREATE TABLE r2 (a INTEGER PRIMARY KEY, b INTEGER PRIMARY KEY)",
        "UPDATE q SET zz = 1",
        "DROP TABLE nope",
        "SELECT q.a FROM q AS x",
        "SELECT x.a FROM q AS x",
        "SELECT x.* FROM q",
        "SELECT *",
        "SELECT nosuchfunc(1)",
        "SELECT abs(1, 2)",
        "SELECT abs(-9223372036854775808)",
        "INSERT INTO q VALUES (a, 1)",
        "DELETE FROM nope",
        "UPDATE nope SET a = 1",
    ]:
        pair.run(sql)


def test_messages_match_sqlite():
    pair = Pair(check_messages=True)
    pair.run("CREATE TABLE q (id INTEGER PRIMARY KEY, a INTEGER NOT NULL, b TEXT UNIQUE)")
    pair.run("INSERT INTO q VALUES (1, 1, 'x')")
    for sql in [
        "INSERT INTO q VALUES (1)",
        "INSERT INTO q (zz) VALUES (1)",
        "SELECT zz FROM q",
        "SELECT * FROM nope",
        "CREATE TABLE q (x INTEGER)",
        "CREATE TABLE r (a INTEGER, a TEXT)",
        "DROP TABLE nope",
        "SELECT q.a FROM q AS x",
        "INSERT INTO q VALUES (1, 2, 'y')",
        "INSERT INTO q VALUES (2, NULL, 'y')",
        "INSERT INTO q VALUES (2, 2, 'x')",
        "INSERT INTO q VALUES ('a', 2, 'z')",
        "SELECT abs(-9223372036854775808)",
    ]:
        pair.run(sql)
    pair.close()


def test_drop_and_recreate(pair):
    pair.script([
        "CREATE TABLE t (a INTEGER)",
        "INSERT INTO t VALUES (1), (2)",
        "DROP TABLE t",
        "SELECT * FROM t",
        "DROP TABLE IF EXISTS t",
        "CREATE TABLE t (b TEXT)",
        "CREATE TABLE IF NOT EXISTS t (c INTEGER)",
        "INSERT INTO t VALUES ('x')",
        "SELECT * FROM t",
    ])


def test_tables_without_rowid_alias(pair):
    pair.script([
        "CREATE TABLE t (name TEXT, n INTEGER)",
        "INSERT INTO t VALUES ('a', 1), ('b', 2), ('c', 3)",
        "SELECT rowid, oid, _rowid_, * FROM t",
        "DELETE FROM t WHERE rowid = 2",
        "INSERT INTO t VALUES ('d', 4)",
        "SELECT rowid, * FROM t",
        "UPDATE t SET rowid = 10 WHERE name = 'a'",
        "SELECT rowid, * FROM t WHERE rowid > 3",
        "UPDATE t SET rowid = 10 WHERE name = 'c'",
        "INSERT INTO t VALUES ('e', 5)",
        "SELECT rowid, * FROM t",
    ])


def test_rowid_access_paths_give_same_results(pair):
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    pair.run("INSERT INTO t VALUES " + ", ".join(f"({i * 3}, 'v{i}')" for i in range(200)))
    conditions = []
    for literal in ["30", "31", "'30'", "30.0", "30.5", "'abc'", "NULL", "-5", "1000", "'3e1'"]:
        for op in ["=", "<", "<=", ">", ">="]:
            conditions.append(f"id {op} {literal}")
            conditions.append(f"{literal} {op} id")
    conditions += [
        "id IN (3, 6, '9', 10, NULL)", "id IN ('abc')", "id NOT IN (3, 6)",
        "id > 10 AND id < 40", "id >= 10 AND id <= 40 AND v != 'v5'", "id > 100 AND id < 50",
        "id > 10 OR id < 5", "rowid = 33", "id BETWEEN 30 AND 60", "id = 30 AND id = 33",
        "id > 590", "id < 3", "id > -1 AND id < 2.5", "v = 'v10' AND id = 30",
    ]
    for condition in conditions:
        pair.run(f"SELECT * FROM t WHERE {condition}")
        pair.run(f"SELECT * FROM t WHERE {condition}".replace("id", "rowid"))


def test_select_without_from_and_distinct(pair):
    pair.script([
        "SELECT 1, 'a', NULL, 2.5",
        "SELECT 1 WHERE 0",
        "SELECT 1 WHERE 1",
        "SELECT 1 WHERE NULL",
        "CREATE TABLE t (a INTEGER, b TEXT)",
        "INSERT INTO t VALUES (1, 'x'), (1, 'x'), (2, 'x'), (NULL, NULL), (NULL, NULL), (1, '1')",
        "SELECT DISTINCT a FROM t",
        "SELECT DISTINCT a, b FROM t",
        "SELECT DISTINCT b FROM t WHERE a IS NOT NULL",
    ])


def test_random_crud_against_sqlite():
    rng = random.Random(11)
    pair = Pair()
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c INTEGER NOT NULL)")
    for _ in range(1500):
        choice = rng.random()
        a = rng.choice(["NULL", str(rng.randint(-50, 50)), f"'{rng.randint(0, 9)}'"])
        b = rng.choice(["NULL", f"'s{rng.randint(0, 20)}'", str(rng.randint(0, 5))])
        c = "NULL" if rng.random() < 0.1 else str(rng.randint(0, 9))
        key = rng.choice(["NULL", str(rng.randint(1, 300))])
        lo = rng.randint(-60, 60)
        condition = rng.choice([
            f"a > {lo}", f"id = {rng.randint(1, 300)}", f"id BETWEEN {lo} AND {lo + 40}",
            f"b = 's{rng.randint(0, 20)}'", "a IS NULL", f"c < {rng.randint(0, 9)} OR a = {lo}",
            f"id > {rng.randint(0, 300)} AND a < {lo}", f"a IN ({lo}, {lo + 1}, '{lo + 2}')",
        ])
        if choice < 0.5:
            pair.run(f"INSERT INTO t VALUES ({key}, {a}, {b}, {c})")
        elif choice < 0.65:
            pair.run(f"UPDATE t SET a = {a}, b = {b} WHERE {condition}")
        elif choice < 0.75:
            pair.run(f"DELETE FROM t WHERE {condition}")
        else:
            pair.run(f"SELECT * FROM t WHERE {condition}")
    pair.run("SELECT * FROM t")
    pair.close()


def test_blobs_and_text_with_nul(pair):
    """BLOBs read as text; SQLite's C string functions (LIKE, length) stop at
    a NUL character, and its numeric conversion makes text with a NUL a REAL."""
    text = "CAST(x'610062' AS TEXT)"
    for expr in [
        "x'00' + 0", "x'0035' + 0", "x'35002e35' + 0", "x'352e3500' + 0", "x'2d3500' + 0",
        "x'61620035' + 0", "x'3500' * 2", "-x'3500'", "x'3500' % 2", "x'3500' / 2", "x'3500' | 0",
        "x'3500' AND 1", "sum(x'3500')", "'5' || x'00' + 0", "CAST(x'3500' AS INTEGER)",
        "x'3500' + x'3600'", "typeof(x'3500' + 0)", f"{text} LIKE 'a'", f"{text} LIKE 'a%'",
        f"'a' LIKE {text}", f"length({text})", "length(x'610062')", f"upper({text})", f"{text} = 'a'",
        f"length({text} || 'z')", f"total({text})", "avg(x'3500')", "CAST(x'ff' AS TEXT)",
        "CAST(CAST(x'ff80' AS TEXT) AS BLOB)", "length(CAST(x'ff80c3' AS TEXT))",
        "x'0102' < x'010203'", "x'' < 'a'", "'zzz' < x''", "max(x'01', 'a', 2)",
    ]:
        pair.run(f"SELECT {expr}")
    pair.run("CREATE TABLE t (b BLOB, s TEXT, n NUMERIC)")
    pair.run("INSERT INTO t VALUES (x'3132', x'3132', x'3132'), ('12', '12', '12'), (12, 12, 12)")
    pair.run("SELECT typeof(b), typeof(s), typeof(n), b = s, s = 12, n = 12, b = '12' FROM t")
    pair.run("SELECT count(DISTINCT b), count(DISTINCT s), max(b), min(s) FROM t")


def test_text_compares_by_utf8_bytes(pair):
    """Text made from BLOBs may hold bytes that are not UTF-8; SQLite compares
    text byte by byte, which for valid UTF-8 is code point order."""
    texts = ["CAST(x'ff' AS TEXT)", "char(1114111)", "'é'", "CAST(x'c3' AS TEXT)", "CAST(x'80' AS TEXT)",
             "char(57344)", "char(55295)", "'z'", "''", "'日本'", "CAST(x'e282' AS TEXT)"]
    pair.run("CREATE TABLE t (a TEXT)")
    pair.run("CREATE INDEX ta ON t (a)")
    for text in texts:
        pair.run(f"INSERT INTO t VALUES ({text})")
    for a, b in itertools.product(texts, repeat=2):
        pair.run(f"SELECT {a} < {b}, {a} = {b}, hex(max({a}, {b})), hex(min({a}, {b}))")
    for sql in ["SELECT hex(a) FROM t ORDER BY a", "SELECT hex(max(a)), hex(min(a)) FROM t",
                "SELECT hex(a) FROM t WHERE a > 'z' ORDER BY a",
                "SELECT hex(a) FROM t INDEXED BY ta WHERE a >= char(57344) ORDER BY a DESC",
                "SELECT DISTINCT hex(a) FROM t ORDER BY 1"]:
        pair.run(sql)
