"""ANALYZE statistics, cost-based access paths, OR / IN index plans, join order."""

import itertools
import random

import pytest

from minidb.database import Database
from minidb.errors import OperationalError
from sqlcompare import Pair


def plan(db, sql):
    return [p for _, p in db.execute("EXPLAIN " + sql)]


@pytest.fixture
def db():
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c INTEGER)")
    db.execute("CREATE INDEX t_a ON t (a)")
    db.execute("CREATE INDEX t_bc ON t (b, c)")
    rows = ", ".join(f"({i}, {i % 50}, 'b{i % 7}', {i % 11})" for i in range(1, 3001))
    db.execute(f"INSERT INTO t VALUES {rows}")
    return db


def test_analyze_stores_statistics(tmp_path):
    path = str(tmp_path / "db")
    db = Database(path)
    db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    db.execute("CREATE INDEX t_ab ON t (a, b)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i % 4}, 'x{i % 8}')" for i in range(80)))
    db.execute("ANALYZE")
    table = db.catalog.get_table("t")
    assert table.stat_rows == 80
    assert db.catalog.indexes["t_ab"].stat_average == [20.0, 10.0]
    db.close()
    db = Database(path)  # statistics persist
    assert db.catalog.get_table("t").stat_rows == 80
    assert db.catalog.indexes["t_ab"].stat_average == [20.0, 10.0]
    db.execute("ANALYZE t_ab")
    db.execute("ANALYZE t")
    with pytest.raises(OperationalError, match="no such table or index"):
        db.execute("ANALYZE nope")
    db.execute("DROP INDEX t_ab")
    db.execute("DROP TABLE t")
    kinds = [row[0] for row in db.catalog.schema.scan()]
    assert kinds == []  # statistics went away with their table and index
    assert db.integrity_check() == []
    db.close()


def test_statistics_change_the_plan():
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, flag INTEGER, k INTEGER)")
    db.execute("CREATE INDEX t_flag ON t (flag)")
    db.execute("CREATE INDEX t_k ON t (k)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, {i % 2}, {i})" for i in range(1, 2001)))
    sql = "SELECT * FROM t WHERE flag = 1 AND k > 1990"
    before = plan(db, sql)
    db.execute("ANALYZE")
    # flag = 1 matches half the table; the range on k is far more selective.
    assert plan(db, sql) == ["SEARCH USING INDEX t_k (k>?)"]
    assert before == ["SEARCH USING INDEX t_flag (flag=?)"]  # the default guess
    assert db.execute(sql + " ORDER BY id") == [(i, 1, i) for i in range(1991, 2001, 2)]
    db.execute("DELETE FROM t WHERE id > 8")
    db.execute("ANALYZE")
    assert plan(db, sql) == ["SEARCH USING INDEX t_k (k>?)"]  # as SQLite, even for 8 rows


def test_in_and_or_use_indexes(db):
    assert plan(db, "SELECT * FROM t WHERE a IN (1, 2, 3)") == [
        "MULTI-INDEX IN (SEARCH USING INDEX t_a (a=?); SEARCH USING INDEX t_a (a=?); "
        "SEARCH USING INDEX t_a (a=?))"
    ]
    assert plan(db, "SELECT * FROM t WHERE a = 1 OR b = 'b2'") == [
        "MULTI-INDEX OR (SEARCH USING INDEX t_a (a=?); SEARCH USING INDEX t_bc (b=?))"
    ]
    assert plan(db, "SELECT * FROM t WHERE (b = 'b1' AND c = 3) OR id IN (4, 5) OR a = 48") == [
        "MULTI-INDEX OR (SEARCH USING INDEX t_bc (b=? AND c=?); SEARCH USING ROWID (=); "
        "SEARCH USING INDEX t_a (a=?))"
    ]
    # As SQLite, which prices a full scan at 3N: two lookups beat it.
    assert plan(db, "SELECT * FROM t WHERE a = 1 OR a > 48") == [
        "MULTI-INDEX OR (SEARCH USING INDEX t_a (a=?); SEARCH USING INDEX t_a (a>?))"
    ]
    assert plan(db, "SELECT * FROM t WHERE a = 1 OR a = 2") == [
        "MULTI-INDEX IN (SEARCH USING INDEX t_a (a=?); SEARCH USING INDEX t_a (a=?))"
    ]
    assert plan(db, "SELECT * FROM t WHERE a = 1 OR c = 2") == ["SCAN"]  # c alone has no index


def test_in_and_or_results_match_sqlite():
    pair = Pair()
    pair.script([
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT, c INTEGER)",
        "CREATE INDEX t_a ON t (a)",
        "CREATE INDEX t_bc ON t (b, c)",
        "INSERT INTO t VALUES " + ", ".join(
            f"({i}, {['NULL', i % 9, repr(str(i % 5)), i % 4 + 0.5][i % 4]}, "
            f"{['NULL', repr('b' + str(i % 5)), str(i % 3)][i % 3]}, {i % 6})"
            for i in range(1, 400)
        ),
    ])
    rng = random.Random(3)
    terms = ["a = {n}", "a = '{n}'", "a IN ({n}, {m}, '{n}', NULL)", "b = 'b{n}'", "b = {n}",
             "b = 'b{n}' AND c = {m}", "id < {n}", "id IN ({n}, {m})", "a > {n}", "a <= {m}",
             "c = {n}", "a IN (SELECT a FROM t WHERE id = {m})"]
    for _ in range(300):
        parts = [rng.choice(terms).format(n=rng.randint(0, 9), m=rng.randint(0, 9))
                 for _ in range(rng.randint(1, 3))]
        pair.run(f"SELECT * FROM t WHERE {' OR '.join(parts)}")
        pair.run(f"SELECT id FROM t WHERE ({' OR '.join(parts)}) AND id % 2 = 0")
    pair.close()


def test_join_order_follows_the_cost():
    pair = Pair()
    pair.script([
        "CREATE TABLE big (id INTEGER PRIMARY KEY, k INTEGER, v TEXT)",
        "CREATE INDEX big_k ON big (k)",
        "CREATE TABLE small (k INTEGER, tag TEXT)",
        "CREATE TABLE mid (id INTEGER PRIMARY KEY, big_id INTEGER)",
        "INSERT INTO big VALUES " + ", ".join(f"({i}, {i % 500}, 'v{i}')" for i in range(1, 5001)),
        "INSERT INTO small VALUES (7, 'a'), (8, 'b'), (9, 'c')",
        "INSERT INTO mid VALUES " + ", ".join(f"({i}, {i * 7})" for i in range(1, 301)),
    ])
    db = pair.mini
    db.execute("ANALYZE")
    written_badly = "SELECT count(*) FROM big JOIN small ON big.k = small.k"
    assert [t for t, _ in db.execute("EXPLAIN " + written_badly)] == ["small", "big"]
    three = ("SELECT count(*) FROM big, mid, small "
             "WHERE big.id = mid.big_id AND big.k = small.k")
    plans = db.execute("EXPLAIN " + three)
    assert plans[0][0] != "big"  # the big table never drives the loop
    assert all(p != "SCAN" for t, p in plans if t == "big")  # it is only probed
    pair.run(written_badly)
    pair.run(three)
    pair.close()


def test_reordered_joins_match_sqlite():
    pair = Pair()
    pair.script([
        "CREATE TABLE a (id INTEGER PRIMARY KEY, x INTEGER, y TEXT)",
        "CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER, z INTEGER)",
        "CREATE TABLE c (k INTEGER, w TEXT)",
        "CREATE INDEX b_a ON b (a_id)",
        "CREATE INDEX c_k ON c (k)",
        "INSERT INTO a VALUES " + ", ".join(f"({i}, {i % 13}, 'y{i % 5}')" for i in range(1, 200)),
        "INSERT INTO b VALUES " + ", ".join(f"({i}, {i % 250}, {i % 9})" for i in range(1, 600)),
        "INSERT INTO c VALUES " + ", ".join(f"({i % 20}, 'w{i}')" for i in range(1, 40)),
    ])
    rng = random.Random(8)
    conditions = ["a.id = b.a_id", "b.z = c.k", "a.x = c.k", "a.y = 'y1'", "b.z > 5",
                  "c.w LIKE 'w1%'", "a.id < 50", "b.id IN (3, 30, 300)", "c.k IN (1, 2, 3)"]
    for analyzed in (False, True):
        if analyzed:
            pair.mini.execute("ANALYZE")
        for _ in range(60):
            tables = rng.sample(["a", "b", "c"], rng.randint(2, 3))
            names = {"a", "b", "c"}
            usable = [c for c in conditions if {p.split(".")[0] for p in c.split() if "." in p} <= set(tables)]
            joins = [c for c in usable if c.count(".") == 2]
            if len(joins) < len(tables) - 1:
                continue  # skip cross products: they only make the test slow
            where = " AND ".join(joins[:len(tables) - 1] + rng.sample(usable, min(len(usable), 2)))
            pair.run(f"SELECT count(*), sum({tables[0]}.id) FROM {', '.join(tables)} WHERE {where}")
            assert names
    pair.close()
