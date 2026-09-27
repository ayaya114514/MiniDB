import pytest

from minidb.database import Database


@pytest.fixture(scope="module")
def db():
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, n INTEGER)")
    db.execute("CREATE TABLE plain (v TEXT)")
    for start in range(0, 20_000, 500):
        rows = ", ".join(f"({i}, 'v{i}', {i % 7})" for i in range(start, start + 500))
        db.execute(f"INSERT INTO t VALUES {rows}")
    return db


def plan(db, sql):
    return db.execute("EXPLAIN " + sql)[0][1]


@pytest.mark.parametrize(
    "where, expected",
    [
        ("id = 5", "SEARCH USING ROWID (=)"),
        ("5 = id", "SEARCH USING ROWID (=)"),
        ("rowid = 5", "SEARCH USING ROWID (=)"),
        ("id IN (1, 2)", "SEARCH USING ROWID (=)"),
        ("id = 5 AND n > 1", "SEARCH USING ROWID (=)"),
        ("id > 5", "SEARCH USING ROWID (range)"),
        ("id <= 5", "SEARCH USING ROWID (range)"),
        ("10 < id AND id < 20", "SEARCH USING ROWID (range)"),
        ("id BETWEEN 1 AND 3", "SCAN"),
        ("id = 5 OR id = 6", "MULTI-INDEX OR (SEARCH USING ROWID (=); SEARCH USING ROWID (=))"),
        ("id != 5", "SCAN"),
        ("n = 5", "SCAN"),
        ("id + 0 = 5", "SCAN"),
        ("id = n", "SCAN"),
        ("", "SCAN"),
    ],
)
def test_access_path_choice(db, where, expected):
    sql = "SELECT * FROM t" + (f" WHERE {where}" if where else "")
    assert plan(db, sql) == expected
    assert plan(db, "DELETE FROM t" + (f" WHERE {where}" if where else "")) == expected
    assert plan(db, "UPDATE t SET n = 1" + (f" WHERE {where}" if where else "")) == expected


def test_explain_query_plan_syntax(db):
    assert db.execute("EXPLAIN QUERY PLAN SELECT * FROM t WHERE id = 1") == [
        ("t", "SEARCH USING ROWID (=)")
    ]
    assert db.execute("EXPLAIN SELECT 1") == []
    assert plan(db, "SELECT * FROM plain WHERE rowid > 3") == "SEARCH USING ROWID (range)"


def count_page_reads(db, sql):
    pager = db.pager
    pager.shrink_cache(limit=0)
    reads = 0
    original = pager._read

    def counting_read(pgno):
        nonlocal reads
        reads += 1
        return original(pgno)

    pager._read = counting_read
    try:
        result = db.execute(sql)
    finally:
        pager._read = original
    return reads, result


def test_point_lookup_reads_one_path(db):
    total_pages = db.pager.page_count
    reads, result = count_page_reads(db, "SELECT v FROM t WHERE id = 12345")
    assert result == [("v12345",)]
    assert reads <= 4  # schema is cached; root-to-leaf path only
    scan_reads, _ = count_page_reads(db, "SELECT v FROM t WHERE n = 12345")
    assert scan_reads > total_pages // 2


def test_range_scan_reads_only_needed_leaves(db):
    reads, result = count_page_reads(db, "SELECT id FROM t WHERE id >= 1000 AND id < 1100")
    assert [r[0] for r in result] == list(range(1000, 1100))
    assert reads <= 8
