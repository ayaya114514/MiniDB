"""Edge cases found by line coverage (tools/coverage.py)."""

import random
import struct
import zlib

import pytest

import minidb
import minidb.executor as executor_module
from minidb.btree import BTree
from minidb.database import Database
from minidb.errors import DatabaseError, IntegrityError, OperationalError
from minidb.pager import PAGE_SIZE, USABLE_SIZE, Pager
from minidb.record import RecordError, decode_record
from minidb.tokenizer import SQLSyntaxError
from sqlcompare import Pair


def test_values_at_the_limits_match_sqlite():
    pair = Pair()
    pair.script([
        "SELECT CAST(1e308 * 10 AS TEXT), CAST(-1e308 * 10 AS TEXT), 1e308 * 10 || ''",
        "SELECT CAST(-1e30 AS INTEGER), CAST(1e30 AS INTEGER), CAST(-9.3e18 AS INTEGER)",
        "SELECT -(-9223372036854775808), typeof(-(-9223372036854775808))",
        "CREATE TABLE f (x INTEGER)",
        "INSERT INTO f VALUES (1e308), (1e308), (NULL), (-5)",
        "SELECT sum(x), total(x), avg(x) FROM f",
        "CREATE TABLE g (k INTEGER, s TEXT)",
        "INSERT INTO g VALUES (1, 'a'), (1, NULL), (1, 'b'), (2, NULL)",
        "SELECT k, group_concat(s) FROM g GROUP BY k",
        "SELECT abs(*) FROM g",
        "SELECT 1 + * FROM g",
    ])
    pair.close()


def test_cast_to_blob():
    assert Database().execute("SELECT CAST(1 AS BLOB), CAST('é' AS BLOB), CAST(x'00' AS BLOB)") == [
        (b"1", "é".encode(), b"\x00")
    ]


def test_explain_needs_a_query():
    with pytest.raises(SQLSyntaxError, match="expected SELECT, UPDATE or DELETE"):
        Database().execute("EXPLAIN CREATE TABLE t (a INTEGER)")


def test_explain_shows_subqueries_and_ordered_index_scans():
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT)")
    db.execute("CREATE INDEX t_a ON t (a)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, {i % 9}, 'b{i}')" for i in range(1, 300)))
    assert db.execute("EXPLAIN SELECT * FROM (SELECT a FROM t) AS d") == [("d", "SCAN SUBQUERY")]
    assert db.execute("EXPLAIN SELECT * FROM t ORDER BY a LIMIT 3") == [("t", "SCAN USING INDEX t_a")]
    assert db.execute("SELECT id FROM t ORDER BY a, id LIMIT 3") == [(9,), (18,), (27,)]


def test_presorted_range_and_multi_index_plans():
    pair = Pair()
    pair.script([
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT)",
        "CREATE INDEX t_a ON t (a)",
        "INSERT INTO t VALUES " + ", ".join(f"({i}, {i % 9}, 'b{i % 4}')" for i in range(1, 300)),
    ])
    db = pair.mini
    for sql in ["SELECT * FROM t WHERE id > 250 ORDER BY id LIMIT 5",
                "SELECT * FROM t WHERE a = 1 OR a = 2 ORDER BY id LIMIT 7",
                "SELECT * FROM t WHERE a IN (3, 4) ORDER BY id LIMIT 7 OFFSET 2"]:
        pair.run(sql)
        assert db.executor.prepare(db.parse(sql)[0]).compiled.presorted
    pair.close()


def test_greedy_join_order_for_many_tables():
    pair = Pair()
    names = [f"t{i}" for i in range(8)]
    for i, name in enumerate(names):
        pair.run(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, next INTEGER, v INTEGER)")
        rows = 5 + 30 * (i % 3)
        pair.run(f"INSERT INTO {name} VALUES " + ", ".join(
            f"({r}, {(r * 7) % rows + 1}, {r % 4})" for r in range(1, rows + 1)))
    joins = " AND ".join(f"{a}.next = {b}.id" for a, b in zip(names, names[1:]))
    sql = f"SELECT count(*), sum(t7.v) FROM {', '.join(reversed(names))} WHERE {joins} AND t0.v = 1"
    pair.run(sql)
    assert len(pair.mini.execute("EXPLAIN " + sql)) == 8
    pair.close()


def test_integrity_check_reports_damage():
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER)")
    db.execute("CREATE INDEX t_a ON t (a)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, {i})" for i in range(200)))
    db.execute("BEGIN")  # damage the trees in memory, inside a transaction
    index = db.catalog.indexes["t_a"]
    db.catalog.index_tree(index).delete(index.key([None, 5], 5))  # an entry goes missing
    assert db.integrity_check() == ["index t_a does not match table t"]
    leaf = db.catalog.table_tree(db.catalog.get_table("t"))._leftmost_leaf()
    leaf.keys.reverse()  # keys out of order
    assert db.integrity_check()[0].startswith("table t: page")


def test_page_with_valid_checksum_but_garbage_content(tmp_path):
    path = str(tmp_path / "db")
    db = Database(path)
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    root = db.catalog.get_table("t").root
    db.close()
    garbage = bytes([7]) + bytes(USABLE_SIZE - 1)  # node type 7 does not exist
    with open(path, "r+b") as f:
        f.seek(root * PAGE_SIZE)
        f.write(garbage + struct.pack(">I", zlib.crc32(garbage)))
    db = Database(path)
    with pytest.raises(DatabaseError, match="malformed"):
        db.execute("SELECT * FROM t")
    db.close()


def test_truncated_long_text_header():
    with pytest.raises(RecordError, match="truncated record header"):
        decode_record(bytes([2, 8, 0]))


def test_rowids_run_out(monkeypatch):
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (9223372036854775807), (5)")
    monkeypatch.setattr(executor_module.random, "randint", lambda a, b: 5)  # always taken
    with pytest.raises(OperationalError, match="database or disk is full"):
        db.execute("INSERT INTO t VALUES (NULL)")


def test_update_rowid_of_table_without_alias():
    pair = Pair()
    pair.script([
        "CREATE TABLE t (v TEXT)",
        "INSERT INTO t VALUES ('a'), ('b')",
        "UPDATE t SET rowid = 'x' WHERE v = 'a'",
        "UPDATE t SET rowid = NULL WHERE v = 'a'",
        "UPDATE t SET rowid = '7' WHERE v = 'a'",
        "SELECT rowid, v FROM t",
    ])
    pair.close()


def test_redistribution_can_split_the_parent():
    """With variable-length separators, borrowing between two leaves may
    lengthen the parent's separator enough to overflow the parent: the
    parent must then split.  The situation is built by hand: a root with
    13 short separators (150 of 160 bytes) whose first leaf underflows next
    to a leaf full of long keys."""
    from minidb.btree import Internal, Leaf
    from test_btree import TextKey

    pager = Pager()
    tree = BTree.create(pager, TextKey, 160)
    groups = [["a0", "a1", "a2", "a3"], ["b" + "z" * 14 + str(i) for i in range(6)]]
    groups += [[f"{c}{i}" for i in range(4)] for c in "cdefghijklmn"]
    leaves = [pager.allocate(Leaf, TextKey, list(keys), [b""] * len(keys)) for keys in groups]
    for leaf, following in zip(leaves, leaves[1:]):
        leaf.next_leaf = following.pgno
    separators = ["b"] + [c for c in "cdefghijklmn"]
    pager.write(Internal(tree.root, TextKey, separators, [leaf.pgno for leaf in leaves]))
    assert tree.check() == sum(len(g) for g in groups)
    root_size = tree.node(tree.root).size
    assert root_size + 15 > tree.capacity >= root_size  # one longer separator overflows it
    tree.delete("a3")  # underflow: borrowing from the long-key leaf moves a long key up
    assert tree.depth() == 3  # the root had to split
    assert tree.check() == sum(len(g) for g in groups) - 1
    assert tree.keys() == sorted(k for g in groups for k in g if k != "a3")


def test_small_api_corners(tmp_path):
    conn = minidb.connect(":memory:")
    cursor = conn.cursor()
    assert cursor.executemany("   ", []) is cursor
    cursor.setinputsizes([1])
    cursor.setoutputsize(10)
    conn.close()
    conn.close()  # closing twice is fine
    db = Database(str(tmp_path / "db"))
    db.close()
    db.close()
    with pytest.raises(minidb.ProgrammingError):
        Database().execute("SELECT ?; SELECT 2", [1])


def test_rollback_of_a_database_that_was_never_committed(tmp_path):
    pager = Pager(str(tmp_path / "db"))
    assert pager.is_new
    pager.rollback()
    assert pager.is_new and pager.page_count == 1
    assert not pager.checkpoint()  # nothing in the log yet
    pager.close_files()


def test_shell_entry_point_in_process(tmp_path, capsys):
    import io

    from minidb import repl
    assert repl.main(["a.db", "b.db"]) == 2
    assert capsys.readouterr().err.startswith("usage:")
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database")
    assert repl.main([str(bad)]) == 1
    assert capsys.readouterr().err.startswith("Error:")
    out = io.StringIO()
    repl.run(io.StringIO("\n   \nSELECT 1;\n"), out)  # blank lines are skipped
    assert out.getvalue() == "1\n"


def test_missing_parameters_match_sqlite():
    import sqlite3
    for module in (sqlite3, minidb):
        conn = module.connect(":memory:")
        with pytest.raises(module.ProgrammingError, match="uses 1, and there are 0 supplied"):
            conn.execute("SELECT ?")
        conn.close()
    with pytest.raises(minidb.ProgrammingError, match="uses 1, and there are 0 supplied"):
        Database().execute("SELECT ?")  # no parameters at all (None)
