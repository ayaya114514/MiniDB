import io
import random
import sqlite3

from minidb.executor import ExecutionError, Table
from minidb.pager import Pager
from minidb.parser import parse
from minidb.repl import run

import pytest


def run_script(lines, path=None):
    out = io.StringIO()
    run(io.StringIO("".join(line + "\n" for line in lines)), out, path=path, interactive=False)
    return out.getvalue().splitlines()


def test_select_returns_rows_in_id_order():
    assert run_script(["insert 3 c 1", "insert 1 a 1", "insert 2 b 1", "select"]) == [
        "1|a|1", "2|b|1", "3|c|1",
    ]


def test_delete():
    assert run_script(["insert 1 a 1", "insert 2 b 2", "delete 1", "select", "delete 1"]) == [
        "2|b|2",
        "Error: no row with id 1",
    ]


def test_btree_command():
    lines = [f"insert {i} name{i} {i}" for i in range(300)] + [".btree"]
    output = run_script(lines)
    assert output[0].startswith("- internal (page 1")
    assert sum(1 for line in output if "- leaf" in line) >= 2


def test_btree_command_on_empty_table():
    assert run_script([".btree"]) == ["- leaf (page 1, 0 keys): "]


def test_random_workload_matches_sqlite(tmp_path):
    rng = random.Random(3)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    path = str(tmp_path / "users.db")
    pager = Pager(path)
    table = Table(pager)
    for step in range(20_000):
        row_id = rng.randint(0, 5000)
        if rng.random() < 0.6:
            name, age = f"n{rng.randint(0, 99)}", rng.randint(0, 99)
            try:
                conn.execute("INSERT INTO users VALUES (?, ?, ?)", (row_id, name, age))
                expected_error = False
            except sqlite3.IntegrityError:
                expected_error = True
            try:
                table.execute(parse(f"insert {row_id} {name} {age}"))
                assert not expected_error
            except ExecutionError:
                assert expected_error
        else:
            deleted = conn.execute("DELETE FROM users WHERE id = ?", (row_id,)).rowcount
            try:
                table.execute(parse(f"delete {row_id}"))
                assert deleted == 1
            except ExecutionError:
                assert deleted == 0
        if step == 10_000:
            pager.close()
            pager = Pager(path)
            table = Table(pager)
    expected = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    assert table.execute(parse("select")) == expected
    assert table.tree.check() == len(expected)
    pager.close()
