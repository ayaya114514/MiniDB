"""Window functions and FILTER, compared with SQLite (results in order:
without ORDER BY, rows come in the order the window sorts them)."""

import random

import pytest

from sqlcompare import Pair

from minidb.errors import OperationalError
from minidb.parser import Frame, WindowDef, parse
from minidb.tokenizer import SQLSyntaxError

SETUP = ["CREATE TABLE t (a, b)", "INSERT INTO t VALUES (1, 'x'), (2, ''), (3, 'y'), (3, NULL), (5, 2.5)"]

QUERIES = [
    'SELECT group_concat(b) OVER (ORDER BY a ROWS CURRENT ROW) FROM t',
    'SELECT group_concat(b) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t',
    "SELECT group_concat('') OVER ()",
    "SELECT string_agg(b, ';') OVER (ORDER BY a) FROM t",
    'SELECT string_agg(b) FROM t',
    'SELECT a FROM t WHERE row_number() OVER () > 1',
    'SELECT row_number() FROM t',
    'SELECT abs(a) OVER () FROM t',
    'SELECT nosuch(a) OVER () FROM t',
    'SELECT abs(a) FILTER (WHERE 1) FROM t',
    'SELECT row_number() FILTER (WHERE 1) OVER () FROM t',
    'SELECT count(DISTINCT a) OVER () FROM t',
    'SELECT sum(row_number() OVER ()) OVER () FROM t',
    'SELECT sum(a) OVER w FROM t',
    'SELECT sum(a) OVER w FROM t WINDOW w AS (), w AS (ORDER BY a)',
    'SELECT sum(a) OVER (w ORDER BY a) FROM t WINDOW w AS (PARTITION BY b)',
    'SELECT sum(a) OVER (w PARTITION BY a) FROM t WINDOW w AS (ORDER BY b)',
    'SELECT sum(a) OVER (w ORDER BY a) FROM t WINDOW w AS (ORDER BY b)',
    'SELECT sum(a) OVER (w) FROM t WINDOW w AS (ROWS 1 PRECEDING)',
    'SELECT sum(a) OVER w FROM t WINDOW w AS (ROWS 1 PRECEDING)',
    'SELECT sum(a) OVER (ROWS -1 PRECEDING) FROM t',
    'SELECT sum(a) OVER (ROWS 1.5 PRECEDING) FROM t',
    "SELECT sum(a) OVER (ROWS '2' PRECEDING) FROM t",
    'SELECT sum(a) OVER (ROWS a PRECEDING) FROM t',
    'SELECT sum(a) OVER (RANGE 1 PRECEDING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a RANGE 1.5 PRECEDING) FROM t',
    "SELECT sum(a) OVER (ORDER BY a RANGE '1' PRECEDING) FROM t",
    'SELECT sum(a) OVER (ORDER BY a, b RANGE 1 PRECEDING) FROM t',
    'SELECT sum(a) OVER (ROWS BETWEEN CURRENT ROW AND 1 PRECEDING) FROM t',
    'SELECT lag(a, 1.5) OVER () FROM t',
    'SELECT lag(a, -1) OVER (ORDER BY a) FROM t',
    'SELECT lag(a, -2) OVER (ORDER BY a) FROM t',
    "SELECT lag(a, '1') OVER (ORDER BY a) FROM t",
    "SELECT lag(a, 2.0, 'd') OVER (ORDER BY a) FROM t",
    "SELECT lead(a, NULL, 'd') OVER (ORDER BY a) FROM t",
    'SELECT lag(a, a) OVER (ORDER BY a) FROM t',
    'SELECT ntile(0) OVER () FROM t',
    "SELECT ntile('2') OVER (ORDER BY a) FROM t",
    'SELECT ntile(2.5) OVER (ORDER BY a) FROM t',
    'SELECT ntile(a) OVER (ORDER BY a) FROM t',
    'SELECT nth_value(a, 0) OVER () FROM t',
    'SELECT nth_value(a, 2) OVER (ORDER BY a) FROM t',
    'SELECT nth_value(a, 2.0) OVER (ORDER BY a) FROM t',
    'SELECT sum(a) OVER () FROM t WHERE 0',
    'SELECT sum(a) OVER (ROWS -1 PRECEDING) FROM t WHERE 0',
    'SELECT a FROM t GROUP BY row_number() OVER ()',
    'SELECT a FROM t ORDER BY row_number() OVER (ORDER BY a DESC)',
    'SELECT a, count(*) FROM t GROUP BY a HAVING row_number() OVER () > 1',
    'SELECT sum(count(*)) OVER () FROM t',
    'SELECT rank() OVER (ORDER BY 1) FROM t',
    'SELECT row_number() OVER (PARTITION BY 1) FROM t',
    'SELECT a, row_number() OVER (ORDER BY a NULLS LAST) FROM t',
    'SELECT count(*) FILTER (WHERE a > 1), sum(a) FILTER (WHERE b IS NOT NULL) FROM t',
    'SELECT count(*) FILTER (WHERE a > 1) OVER (ORDER BY a) FROM t',
    'SELECT sum(a) OVER (ORDER BY a GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE GROUP) FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE TIES) FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE NO OTHERS) FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE CURRENT ROW) FROM t',
    'SELECT a, sum(a) OVER (ORDER BY b RANGE BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t',
    'SELECT sum(a) OVER win FROM t WINDOW win AS (ORDER BY a) ORDER BY 1',
    'SELECT sum(a) FILTER (WHERE a > 1) OVER () FROM t',
    'SELECT sum(a) OVER (),  sum(a) OVER () FROM t',
    'SELECT first_value(a) OVER (ORDER BY a ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING) FROM t',
    'SELECT window FROM (SELECT 1 AS window)',
    'SELECT over FROM (SELECT 1 AS over)',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 2 PRECEDING) FROM t',
    'SELECT count(*) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 2 PRECEDING) FROM t',
    'SELECT a, b, row_number() OVER (ORDER BY a), rank() OVER (ORDER BY a), dense_rank() OVER (ORDER BY a), percent_rank() OVER (ORDER BY a), cume_dist() OVER (ORDER BY a) FROM t ORDER BY a, b',
    'SELECT a, ntile(2) OVER (ORDER BY a), ntile(3) OVER (ORDER BY a), ntile(10) OVER (ORDER BY a) FROM t ORDER BY a, b',
    'SELECT a, first_value(b) OVER (ORDER BY a), last_value(b) OVER (ORDER BY a), nth_value(b, 3) OVER (ORDER BY a) FROM t ORDER BY a, b',
    'SELECT a, sum(a) OVER (ORDER BY a RANGE BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t ORDER BY a, b',
    'SELECT a, sum(a) OVER (ORDER BY a DESC RANGE BETWEEN 1 PRECEDING AND 2 FOLLOWING) FROM t ORDER BY a, b',
    'SELECT a, sum(a) OVER (ORDER BY a RANGE BETWEEN 2 FOLLOWING AND 3 FOLLOWING) FROM t ORDER BY a, b',
    'SELECT a, sum(a) OVER (ORDER BY a RANGE BETWEEN 3 PRECEDING AND 1 PRECEDING) FROM t ORDER BY a, b',
    'SELECT a, sum(a) OVER (ORDER BY a GROUPS BETWEEN 1 FOLLOWING AND 2 FOLLOWING) FROM t ORDER BY a, b',
    'SELECT a, sum(a) OVER (ORDER BY a GROUPS 1 PRECEDING) FROM t ORDER BY a, b',
    'SELECT a, max(b) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING), min(b) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t ORDER BY a, b',
    'SELECT a, avg(a) OVER (ORDER BY a ROWS 2 PRECEDING), total(a) OVER (ORDER BY a ROWS 2 PRECEDING) FROM t ORDER BY a, b',
    'SELECT b, sum(a) OVER (PARTITION BY b IS NULL ORDER BY a) FROM t ORDER BY a, b',
    'SELECT a, lead(a) OVER (), lag(a) OVER (), lead(a, 2, -1) OVER (ORDER BY a DESC) FROM t ORDER BY a, b',
    'SELECT a, count(*) OVER (ORDER BY a RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM t ORDER BY a, b',
    "SELECT a, group_concat(b, '-') OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t ORDER BY a, b",
    'SELECT a, sum(a) OVER (ORDER BY a NULLS LAST RANGE 1 PRECEDING) FROM t ORDER BY a, b',
    'SELECT a, sum(a) OVER (ORDER BY b NULLS LAST RANGE BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t ORDER BY a, b',
    'SELECT a, sum(a) OVER (ORDER BY b DESC NULLS FIRST RANGE BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t ORDER BY a, b',
    'SELECT a, nth_value(a, 2) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE CURRENT ROW) FROM t ORDER BY a, b',
    'SELECT a, first_value(a) OVER (ORDER BY a GROUPS BETWEEN CURRENT ROW AND 1 FOLLOWING EXCLUDE GROUP) FROM t ORDER BY a, b',
    'SELECT a, group_concat(a) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE TIES) FROM t ORDER BY a, b',
]

COMBINATIONS = [
    'SELECT a, count(*), sum(count(*)) OVER (ORDER BY a), rank() OVER (ORDER BY count(*) DESC) FROM t GROUP BY a',
    'SELECT a, sum(a) OVER (ORDER BY a) AS s FROM t ORDER BY s DESC LIMIT 2',
    'SELECT DISTINCT sum(a) OVER (PARTITION BY a) FROM t',
    'SELECT a, row_number() OVER (ORDER BY a) + rank() OVER (ORDER BY b), row_number() OVER (ORDER BY a) * 10 FROM t',
    'SELECT x.a, y.a, row_number() OVER (PARTITION BY x.a ORDER BY y.a) FROM t AS x JOIN t AS y ON x.a < y.a',
    'SELECT a, (SELECT count(*) FROM t AS u WHERE u.a < t.a), sum(a) OVER () FROM t',
    'SELECT (SELECT sum(a) OVER () FROM t LIMIT 1)',
    'SELECT a FROM t WHERE a > (SELECT avg(a) OVER () FROM t LIMIT 1)',
    'WITH w AS (SELECT a, row_number() OVER (ORDER BY a DESC) AS r FROM t) SELECT * FROM w WHERE r <= 2',
    'SELECT * FROM (SELECT a, lag(a) OVER (ORDER BY a) AS prev FROM t) WHERE prev IS NOT NULL',
    'SELECT a, sum(a) OVER (ORDER BY a ROWS UNBOUNDED PRECEDING) FROM t UNION ALL SELECT 0, 0',
    'SELECT a, max(a) OVER () FROM t GROUP BY a HAVING count(*) > 1',
    'SELECT count(*) OVER () FROM t GROUP BY a',
    'SELECT sum(a) OVER (), a FROM t LIMIT 2 OFFSET 1',
    'SELECT a, row_number() OVER w, sum(a) OVER w FROM t WINDOW w AS (ORDER BY a ROWS 1 PRECEDING) ORDER BY 2 DESC',
    'SELECT a, first_value(a) OVER (PARTITION BY b IS NULL), last_value(b) OVER (PARTITION BY a) FROM t',
    'SELECT typeof(sum(a) OVER ()), typeof(avg(a) OVER ()), typeof(count(*) OVER ()), typeof(percent_rank() OVER ()) FROM t',
    'SELECT a, ntile(2) OVER (PARTITION BY a) FROM t',
    'SELECT sum(a) OVER (ORDER BY a) FROM t WHERE a > 100',
    'SELECT a, sum(a) OVER (ORDER BY a RANGE BETWEEN 0.5 PRECEDING AND 0.5 FOLLOWING) FROM t',
    'SELECT count(*) OVER (), count(*) FROM t',
    'SELECT a, row_number() OVER () FROM t ORDER BY a DESC',
    'SELECT row_number() OVER (ORDER BY a) AS r FROM t WHERE r > 1',
    'SELECT a AS k, sum(a) OVER (ORDER BY k) FROM t',
    'SELECT a, rank() OVER (ORDER BY a) FROM t GROUP BY 1',
    'SELECT group_concat(a) OVER (ORDER BY a ROWS 1 PRECEDING) FROM t ORDER BY 1',
    'SELECT a, avg(b) FILTER (WHERE a > 1) FROM t GROUP BY a',
    'SELECT count(DISTINCT a) FILTER (WHERE a > 1) FROM t',
    'SELECT a, count(*) FILTER (WHERE b IS NULL) OVER (PARTITION BY a) FROM t',
    "SELECT max(a) FILTER (WHERE b > 'a') FROM t",
    'SELECT a, rank() OVER (ORDER BY (SELECT count(*) FROM t AS u WHERE u.a <= t.a)) FROM t',
    'SELECT a, sum(a) OVER (ORDER BY a GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE CURRENT ROW) FROM t',
    'CREATE VIEW vw AS SELECT a, row_number() OVER (ORDER BY a DESC) AS r, sum(a) OVER (PARTITION BY b IS NULL) AS s FROM t',
    'SELECT * FROM vw',
    'SELECT r, s FROM vw WHERE r > 2',
    'SELECT a, row_number() OVER (ORDER BY a) FROM t ORDER BY row_number() OVER (ORDER BY a DESC)',
    'SELECT a, lag(a, 1, 0) OVER (ORDER BY a), lead(a, 1, 0) OVER (ORDER BY a) FROM t',
    'SELECT sum(sum(a)) OVER () FROM t',
    'SELECT a, sum(a) OVER (PARTITION BY a ORDER BY b ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM t',
    'SELECT min(b) OVER (ORDER BY a ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING) FROM t',
    'SELECT nth_value(a, 1) OVER (), nth_value(a, 6) OVER () FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 0 PRECEDING AND 0 PRECEDING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 1 FOLLOWING AND 1 FOLLOWING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 2 FOLLOWING AND 1 FOLLOWING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a GROUPS BETWEEN 2 FOLLOWING AND 1 FOLLOWING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a RANGE BETWEEN 2 FOLLOWING AND 1 FOLLOWING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a RANGE BETWEEN 1 PRECEDING AND 2 PRECEDING) FROM t',
    'SELECT row_number() OVER (ORDER BY a) FROM t INTERSECT SELECT 2',
    'SELECT a FROM t ORDER BY sum(a) OVER ()',
    'SELECT 1 FROM t HAVING sum(a) OVER () > 0',
    'SELECT sum(a) OVER () FROM t GROUP BY sum(a) OVER ()',
    "SELECT CASE WHEN row_number() OVER (ORDER BY a) = 1 THEN 'first' END FROM t",
    'SELECT a IN (SELECT rank() OVER (ORDER BY a) FROM t) FROM t',
    'SELECT EXISTS (SELECT row_number() OVER () FROM t WHERE a > 10)',
    'SELECT row_number() OVER (ORDER BY a) FROM t WINDOW w AS (x ORDER BY a)',
    'SELECT row_number() OVER w2 FROM t WINDOW w1 AS (PARTITION BY a), w2 AS (w1 ORDER BY b)',
    'SELECT row_number() OVER w2 FROM t WINDOW w2 AS (w1 ORDER BY b), w1 AS (PARTITION BY a)',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE GROUP) FROM t',
    'SELECT sum(a) OVER (ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE TIES) FROM t',
    'SELECT sum(a) OVER (ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING EXCLUDE GROUP) FROM t',
    'SELECT lead(a, 1, a) OVER (ORDER BY a) FROM t',
    'SELECT lag(a) OVER (ORDER BY a ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM t',
    'SELECT first_value(a) OVER (ORDER BY a ROWS 1 PRECEDING EXCLUDE CURRENT ROW) FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS 9223372036854775807 PRECEDING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS BETWEEN 9223372036854775807 FOLLOWING AND 9223372036854775807 FOLLOWING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a RANGE BETWEEN 1e300 PRECEDING AND 1e300 FOLLOWING) FROM t',
    'SELECT sum(a) OVER (ORDER BY a RANGE BETWEEN 9223372036854775807 PRECEDING AND CURRENT ROW) FROM t',
    'SELECT sum(a) OVER (ORDER BY a ROWS ? PRECEDING) FROM t',
]


@pytest.mark.parametrize("queries", [QUERIES, COMBINATIONS], ids=["functions", "combinations"])
def test_window_functions_match_sqlite(queries):
    pair = Pair(check_messages=True)
    for sql in SETUP:
        pair.run(sql)
    for sql in queries:
        pair.run(sql, ordered=True)
    pair.close()


def _value(rng):
    r = rng.random()
    if r < 0.15:
        return "NULL"
    if r < 0.5:
        return str(rng.randint(-5, 8))
    if r < 0.75:
        return repr(rng.choice([0.1, 0.2, 0.3, 1.5, -2.25, 1e16, 3.0, 1e-3, 7.7]))
    if r < 0.9:
        return "'" + rng.choice(["a", "b", "", "ab", "5", "x"]) + "'"
    return rng.choice(["x'00'", "x'61'", "9223372036854775807", "-9223372036854775807"])


def _bound(rng, starting, unit):
    kind = rng.choice(["UNBOUNDED", "PRECEDING", "CURRENT", "FOLLOWING"])
    n = rng.choice(["0", "1", "2", "3"]) if unit != "RANGE" else rng.choice(["0", "1", "2", "0.5", "2.5"])
    if kind == "UNBOUNDED":
        return "UNBOUNDED " + ("PRECEDING" if starting else "FOLLOWING")
    if kind == "CURRENT":
        return "CURRENT ROW"
    return f"{n} {kind}"


def _window(rng):
    parts = []
    if rng.random() < 0.4:
        parts.append("PARTITION BY " + rng.choice(["p", "p % 2", "b IS NULL", "p, q"]))
    if rng.random() < 0.8:
        terms = [rng.choice(["a", "b", "p", "q", "id"]) + rng.choice(["", " DESC", " ASC NULLS LAST", " DESC NULLS FIRST"])
                 for _ in range(rng.randint(1, 2))]
        if rng.random() < 0.5:
            terms.append("id")
        parts.append("ORDER BY " + ", ".join(terms))
    if rng.random() < 0.7:
        unit = rng.choice(["ROWS", "RANGE", "GROUPS"])
        if unit == "RANGE" and rng.random() < 0.7:  # offsets need exactly one ORDER BY term
            parts = [p for p in parts if not p.startswith("ORDER")]
            parts.append("ORDER BY " + rng.choice(["a", "p", "a DESC", "p DESC", "a NULLS LAST", "b"]))
        frame = f"{unit} BETWEEN {_bound(rng, True, unit)} AND {_bound(rng, False, unit)}"
        if rng.random() < 0.25:
            frame += " EXCLUDE " + rng.choice(["NO OTHERS", "CURRENT ROW", "GROUP", "TIES"])
        parts.append(frame)
    return " ".join(parts)


FUNCTIONS = [
    "sum(a)", "total(a)", "avg(a)", "count(*)", "count(b)", "min(a)", "max(b)", "group_concat(b)",
    "group_concat(a, '|')", "string_agg(b, '')", "row_number()", "rank()", "dense_rank()", "percent_rank()",
    "cume_dist()", "ntile(3)", "ntile(p)", "first_value(b)", "last_value(a)", "nth_value(a, 2)", "nth_value(b, q)",
    "lead(a)", "lag(b, 2, 'd')", "lead(a, q)", "lag(a, -1)", "sum(a) FILTER (WHERE p > 1)",
    "count(*) FILTER (WHERE b IS NULL)", "min(a) FILTER (WHERE q)", "sum(p)", "avg(q)",
]


@pytest.mark.parametrize("seed", range(40))
def test_random_windows_match_sqlite(seed):
    """Every frame type and bound, EXCLUDE, partitions, ties in ORDER BY
    and NULLs: results, their order and floating point rounding (sliding
    sums use xInverse in SQLite's order) must be the same."""
    rng = random.Random(seed)
    pair = Pair(check_messages=True)
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, a, b, p, q)")
    rows = [f"({i}, {_value(rng)}, {_value(rng)}, {rng.randint(0, 3)}, {rng.choice([0, 1, 2, 3, 'NULL'])})"
            for i in range(1, rng.randint(0, 30) + 1)]
    if rows:
        pair.run("INSERT INTO t VALUES " + ", ".join(rows))
    for _ in range(25):
        items = ", ".join(f"{rng.choice(FUNCTIONS)} OVER ({_window(rng)})" for _ in range(rng.randint(1, 3)))
        pair.run(f"SELECT id, {items} FROM t", ordered=True)
    pair.close()


def test_parse_windows():
    stmt = parse("SELECT sum(a) FILTER (WHERE b) OVER (w PARTITION BY c ORDER BY d DESC NULLS FIRST "
                 "GROUPS BETWEEN 1 PRECEDING AND UNBOUNDED FOLLOWING EXCLUDE TIES) FROM t "
                 "WINDOW w AS (ORDER BY e), v AS (w ROWS 2 PRECEDING)")
    call = stmt.items[0].expr
    assert call.filter is not None
    assert call.over.base == "w"
    assert call.over.frame == Frame("GROUPS", "PRECEDING", call.over.frame.start_offset, "UNBOUNDED", None, "TIES")
    assert [name for name, _ in stmt.windows] == ["w", "v"]
    assert parse("SELECT rank() OVER w FROM t WINDOW w AS ()").items[0].expr.over == "w"
    assert parse("SELECT a AS over, b window FROM t").items[1].alias == "window"
    assert isinstance(parse("SELECT count(*) OVER () FROM t").items[0].expr.over, WindowDef)
    with pytest.raises(OperationalError, match="unsupported frame specification"):
        parse("SELECT sum(a) OVER (ROWS BETWEEN CURRENT ROW AND 1 PRECEDING) FROM t")
    with pytest.raises(SQLSyntaxError):
        parse("SELECT sum(a) OVER (ROWS UNBOUNDED FOLLOWING) FROM t")
    with pytest.raises(SQLSyntaxError):
        parse("SELECT sum(a) OVER () FILTER (WHERE 1) FROM t")
