"""Secondary indexes: maintenance, constraints, planner use, sqlite3 agreement."""

import random

import pytest

from minidb.database import Database
from minidb.errors import IntegrityError, OperationalError
from sqlcompare import Pair


def plan(db, sql):
    return [p for _, p in db.execute("EXPLAIN " + sql)]


@pytest.fixture
def db():
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c INTEGER)")
    db.execute("CREATE INDEX t_a ON t (a)")
    db.execute("CREATE INDEX t_bc ON t (b, c)")
    rows = ", ".join(f"({i}, {i % 50}, 'b{i % 7}', {i % 11})" for i in range(1, 1001))
    db.execute(f"INSERT INTO t VALUES {rows}")
    return db


@pytest.mark.parametrize(
    "where, expected",
    [
        ("a = 5", "SEARCH USING INDEX t_a (a=?)"),
        ("5 = a", "SEARCH USING INDEX t_a (a=?)"),
        ("a = '5'", "SEARCH USING INDEX t_a (a=?)"),
        ("a > 5", "SEARCH USING INDEX t_a (a>?)"),
        ("a >= 5 AND a < 9", "SEARCH USING INDEX t_a (a>=? AND a<?)"),
        ("b = 'b1'", "SEARCH USING INDEX t_bc (b=?)"),
        ("b = 'b1' AND c = 3", "SEARCH USING INDEX t_bc (b=? AND c=?)"),
        ("b = 'b1' AND c > 3", "SEARCH USING INDEX t_bc (b=? AND c>?)"),
        ("c = 3 AND b = 'b1' AND a = 2", "SEARCH USING INDEX t_bc (b=? AND c=?)"),
        ("id = 3 AND a = 2", "SEARCH USING ROWID (=)"),
        ("id > 3 AND a = 2", "SEARCH USING INDEX t_a (a=?)"),
        ("id > 3 AND a > 2", "SEARCH USING ROWID (range)"),
        ("c = 3", "SCAN"),
        ("a + 0 = 5", "SCAN"),
        ("a != 5", "SCAN"),
        ("a = 5 OR a = 6", "MULTI-INDEX OR (SEARCH USING INDEX t_a (a=?); SEARCH USING INDEX t_a (a=?))"),
        ("b = 5", "SEARCH USING INDEX t_bc (b=?)"),
    ],
)
def test_index_choice(db, where, expected):
    assert plan(db, f"SELECT * FROM t WHERE {where}") == [expected]


def test_text_column_compared_with_integer_column_cannot_use_index():
    db = Database()
    db.execute("CREATE TABLE s (x TEXT)")
    db.execute("CREATE TABLE n (y INTEGER)")
    db.execute("CREATE INDEX s_x ON s (x)")
    db.execute("CREATE INDEX n_y ON n (y)")
    # x = y converts x to a number, so the index on x (ordered as text) is unusable ...
    assert plan(db, "SELECT * FROM n JOIN s ON s.x = n.y") == ["SCAN", "SCAN"]
    # ... but the index on y is fine: the text side is converted instead.
    assert plan(db, "SELECT * FROM s JOIN n ON s.x = n.y") == ["SCAN", "SEARCH USING COVERING INDEX n_y (y=?)"]


def test_join_uses_index_and_rowid_on_inner_table(db):
    db.execute("CREATE TABLE u (k INTEGER, name TEXT)")
    db.execute("CREATE INDEX u_k ON u (k)")
    assert plan(db, "SELECT * FROM u JOIN t ON t.id = u.k") == ["SCAN", "SEARCH USING ROWID (=)"]
    assert plan(db, "SELECT * FROM t JOIN u ON u.k = t.a") == ["SCAN", "SEARCH USING INDEX u_k (k=?)"]
    assert plan(db, "SELECT * FROM t LEFT JOIN u ON u.k = t.a WHERE u.name = 'x'") == [
        "SCAN", "SEARCH USING INDEX u_k (k=?)"
    ]


def test_integrity_after_random_changes():
    rng = random.Random(21)
    pair = Pair()
    pair.script([
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, u TEXT UNIQUE)",
        "CREATE INDEX t_a ON t (a)",
        "CREATE INDEX t_ba ON t (b, a)",
        "CREATE UNIQUE INDEX t_ab ON t (a, b)",
    ])
    for step in range(2500):
        a = rng.choice(["NULL", str(rng.randint(0, 30)), f"'{rng.randint(0, 30)}'", "'x'", "2.5"])
        b = rng.choice(["NULL", f"'b{rng.randint(0, 9)}'", str(rng.randint(0, 3))])
        u = rng.choice(["NULL", f"'u{rng.randint(0, 400)}'"])
        n = rng.randint(0, 30)
        choice = rng.random()
        if choice < 0.45:
            pair.run(f"INSERT INTO t (a, b, u) VALUES ({a}, {b}, {u})")
        elif choice < 0.6:
            pair.run(f"UPDATE t SET a = {a}, b = {b} WHERE a = {n} OR id % 17 = {n % 17}")
        elif choice < 0.7:
            pair.run(f"UPDATE t SET id = id + 1000, u = {u} WHERE id % 23 = {n % 23}")
        elif choice < 0.8:
            pair.run(f"DELETE FROM t WHERE a < {n} AND b > 'b5'")
        else:
            condition = rng.choice([
                f"a = {n}", f"a = '{n}'", f"a > {n}", f"a <= {n} AND a > {n - 5}", f"b = 'b{n % 10}'",
                f"b = 'b{n % 10}' AND a >= {n}", f"a = {n} AND b = 'b{n % 10}'", "a = 'x'", "a > 'a'",
                "a < 2.6", f"u = 'u{n}'", "a IS NULL", f"b = {n % 4}",
            ])
            pair.run(f"SELECT * FROM t WHERE {condition}")
        if step % 500 == 0:
            assert pair.mini.integrity_check() == []
    assert pair.mini.integrity_check() == []
    pair.run("SELECT * FROM t")
    pair.close()


def test_index_queries_match_sqlite():
    pair = Pair()
    pair.script([
        "CREATE TABLE m (id INTEGER PRIMARY KEY, i INTEGER, s TEXT)",
        "INSERT INTO m (i, s) VALUES (1, '1'), ('2', 2), (2.5, '2.5'), ('abc', 'abc'), (NULL, NULL),"
        " (-3, '-3'), ('10', '10'), (3.0, 3.0), ('', ''), ('x10', 'x10'), (10, 'B'), (7, 'b')",
        "CREATE INDEX m_i ON m (i)",
        "CREATE INDEX m_s ON m (s)",
    ])
    literals = ["2", "'2'", "2.5", "'2.5'", "'abc'", "NULL", "10", "'10'", "3", "'3.0'", "''", "'a'", "-5", "'B'"]
    for column in ["i", "s"]:
        for op in ["=", "<", "<=", ">", ">="]:
            for literal in literals:
                pair.run(f"SELECT id FROM m WHERE {column} {op} {literal}")
                pair.run(f"SELECT id FROM m WHERE {literal} {op} {column}")
        pair.run(f"SELECT id FROM m WHERE {column} > 2 AND {column} < 'b'")
        pair.run(f"SELECT id FROM m WHERE {column} BETWEEN 1 AND 3")
    pair.run("SELECT a.id, b.id FROM m a JOIN m b ON a.i = b.s")
    pair.run("SELECT a.id, b.id FROM m a JOIN m b ON a.s = b.i")
    pair.run("SELECT a.id, b.id FROM m a LEFT JOIN m b ON b.i > a.i AND b.i < a.i + 3")
    pair.close()


def test_unique_constraints_and_messages():
    pair = Pair(check_messages=True)
    pair.script([
        "CREATE TABLE t (a INTEGER, b TEXT UNIQUE, c TEXT PRIMARY KEY)",
        "INSERT INTO t VALUES (1, 'x', 'k'), (1, 'y', 'j')",
        "CREATE INDEX i ON t (a)",
        "CREATE INDEX i ON t (b)",
        "CREATE INDEX t ON t (a)",
        "CREATE TABLE i (x INTEGER)",
        "CREATE INDEX j ON nope (a)",
        "CREATE INDEX j ON t (zz)",
        "CREATE UNIQUE INDEX u ON t (a)",
        "CREATE INDEX IF NOT EXISTS i ON t (a)",
        "DROP INDEX nope",
        "DROP INDEX IF EXISTS nope",
        "CREATE UNIQUE INDEX u2 ON t (a, b)",
        "INSERT INTO t VALUES (1, 'x', 'z')",
        "INSERT INTO t VALUES (1, 'z', 'k')",
        "INSERT INTO t VALUES (2, 'z', 'q')",
        "INSERT INTO t VALUES (2, 'z2', NULL), (3, 'z3', NULL)",
        "INSERT INTO t VALUES (3, NULL, 'n1'), (3, NULL, 'n2')",
        "UPDATE t SET b = 'x' WHERE c = 'j'",
        "UPDATE t SET b = 'x' WHERE c = 'k'",
        "UPDATE t SET a = 1, b = 'y' WHERE c = 'q'",
        "UPDATE t SET c = 'k' WHERE c = 'q'",
        "SELECT * FROM t ORDER BY c",
        "DROP INDEX i",
        "SELECT * FROM t WHERE a = 1 ORDER BY c",
    ])
    pair.close()


def test_auto_indexes_cannot_be_dropped_or_named():
    db = Database()
    db.execute("CREATE TABLE t (a TEXT UNIQUE)")
    auto = db.catalog.get_table("t").indexes[0].name
    assert auto == "minidb_autoindex_t_1"
    with pytest.raises(OperationalError, match="cannot be dropped"):
        db.execute(f"DROP INDEX {auto}")
    with pytest.raises(OperationalError, match="reserved"):
        db.execute("CREATE INDEX minidb_x ON t (a)")
    with pytest.raises(OperationalError, match="reserved"):
        db.execute("CREATE TABLE minidb_y (a INTEGER)")


def test_failed_unique_index_creation_rolls_back():
    db = Database()
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (1), (2), (1)")
    pages = db.pager.page_count
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed: t.a"):
        db.execute("CREATE UNIQUE INDEX u ON t (a)")
    assert db.catalog.indexes == {}
    assert db.pager.page_count == pages
    db.execute("CREATE INDEX u ON t (a)")
    assert db.execute("SELECT count(*) FROM t WHERE a = 1") == [(2,)]


def test_indexes_persist_and_drop_table_removes_them(tmp_path):
    path = str(tmp_path / "db")
    with Database(path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT UNIQUE)")
        db.execute("CREATE INDEX t_a ON t (a)")
        rows = ", ".join(f"({i}, {i % 10}, 'b{i}')" for i in range(500))
        db.execute(f"INSERT INTO t VALUES {rows}")
    with Database(path) as db:
        table = db.catalog.get_table("t")
        assert [i.name for i in table.indexes] == ["t_a", "minidb_autoindex_t_1"]
        assert plan(db, "SELECT * FROM t WHERE a = 3") == ["SEARCH USING INDEX t_a (a=?)"]
        assert db.execute("SELECT count(*) FROM t WHERE a = 3") == [(50,)]
        with pytest.raises(IntegrityError):
            db.execute("INSERT INTO t VALUES (1000, 1, 'b7')")
        assert db.integrity_check() == []
        pages_in_use = db.pager.page_count - db.pager.free_page_count()
        db.execute("DROP TABLE t")
        assert db.catalog.indexes == {}
        assert db.pager.page_count - db.pager.free_page_count() == 2
        assert pages_in_use > 10


def test_long_index_keys_are_supported():
    pair = Pair()
    long = lambda i, n: f"'{chr(97 + i % 26) * n}{i}'"  # noqa: E731
    pair.script([
        "CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT UNIQUE, u TEXT)",
        "CREATE INDEX t_us ON t (u, s)",
        "INSERT INTO t VALUES " + ", ".join(
            f"({i}, {long(i, 3000 + i * 37)}, {long(i % 5, 5000)})" for i in range(60)
        ),
        f"INSERT INTO t VALUES (100, {long(7, 3000 + 7 * 37)}, 'dup')",  # UNIQUE violation
        f"SELECT id FROM t WHERE s = {long(7, 3000 + 7 * 37)}",
        f"SELECT id FROM t WHERE u = {long(3, 5000)} ORDER BY s",
        f"SELECT id, length(s) FROM t WHERE s > {long(20, 3000 + 20 * 37)} ORDER BY s LIMIT 5",
        "UPDATE t SET s = s || 'x' WHERE id % 3 = 0",
        "DELETE FROM t WHERE id % 4 = 0",
        "SELECT id, length(s), length(u) FROM t ORDER BY u, s",
    ])
    assert pair.mini.integrity_check() == []
    pair.close()


def test_delete_all_clears_indexes(db):
    db.execute("DELETE FROM t")
    assert db.integrity_check() == []
    assert db.execute("SELECT * FROM t WHERE a = 1") == []
    db.execute("INSERT INTO t VALUES (1, 1, 'b', 1)")
    assert db.execute("SELECT id FROM t WHERE b = 'b' AND c = 1") == [(1,)]


def test_order_by_follows_index_or_rowid_order():
    import itertools
    pair = Pair()
    pair.script([
        "CREATE TABLE o (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c INTEGER)",
        "INSERT INTO o VALUES " + ", ".join(
            f"({i}, {['NULL', i % 5, repr(i % 3 + 0.5), repr(str(i % 4))][i % 4]}, "
            f"{['NULL', repr('b' + str(i % 6)), str(i % 2)][i % 3]}, {i % 9})"
            for i in range(1, 120)
        ),
        "CREATE INDEX o_ab ON o (a, b)",
        "CREATE INDEX o_c ON o (c)",
    ])
    db = pair.mini
    presorted = 0
    orders = ["id", "a", "a, b", "a, b, id", "c", "c, id", "b", "a DESC", "a NULLS LAST", "rowid"]
    wheres = ["", " WHERE a = 2", " WHERE a > 1", " WHERE c = 3", " WHERE c BETWEEN 2 AND 5", " WHERE id > 50"]
    for order, where, limit in itertools.product(orders, wheres, ["", " LIMIT 5", " LIMIT 3 OFFSET 4"]):
        sql = f"SELECT id, a, b, c FROM o{where} ORDER BY {order}, id{limit}"
        pair.run(sql)
        presorted += db.executor.prepare(db.parse(sql)[0]).compiled.presorted
    pair.run("SELECT o.id, p.id FROM o JOIN o AS p ON p.c = o.c ORDER BY o.id, p.id LIMIT 20")
    pair.run("SELECT DISTINCT a FROM o ORDER BY a LIMIT 3")
    assert presorted > 50  # most of these really skip the sort
    pair.close()
