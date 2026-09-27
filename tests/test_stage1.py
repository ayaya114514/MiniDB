import io
import sqlite3
import subprocess
import sys

import pytest

from minidb.executor import ExecutionError, Table
from minidb.pager import Pager
from minidb.parser import ParseError, parse
from minidb.repl import run


def run_script(lines):
    out = io.StringIO()
    run(io.StringIO("".join(line + "\n" for line in lines)), out, interactive=False)
    return out.getvalue().splitlines()


def test_insert_and_select():
    assert run_script(["insert 1 alice 30", "insert 2 bob 25", "select"]) == [
        "1|alice|30",
        "2|bob|25",
    ]


def test_select_empty_table():
    assert run_script(["select"]) == []


def test_exit_stops_processing():
    assert run_script(["insert 1 a 1", ".exit", "select"]) == []


def test_unknown_meta_command():
    assert run_script([".foo"]) == ["Error: unknown command: .foo"]


@pytest.mark.parametrize(
    "text",
    ["", "insert 1 a", "insert x a 1", "insert 1 a y", "insert -1 a 1", "select 1", "update", "delete", "delete x"],
)
def test_parse_errors(text):
    with pytest.raises(ParseError):
        parse(text)


def test_duplicate_id_rejected():
    table = Table(Pager())
    table.execute(parse("insert 1 a 1"))
    with pytest.raises(ExecutionError):
        table.execute(parse("insert 1 b 2"))


def test_errors_are_reported_and_repl_continues():
    output = run_script(["insert 1 a", "insert 1 a 1", "insert 1 b 2", "select"])
    assert output[0].startswith("Error:")
    assert output[1].startswith("Error:")
    assert output[2] == "1|a|1"


def test_many_rows():
    lines = [f"insert {i} user{i} {i % 90}" for i in range(1000)] + ["select"]
    output = run_script(lines)
    assert len(output) == 1000
    assert output[999] == "999|user999|9"


def test_matches_sqlite():
    rows = [(5, "eve", 40), (1, "alice", 30), (3, "carol", 22)]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    conn.executemany("INSERT INTO users VALUES (?, ?, ?)", rows)
    expected = conn.execute("SELECT * FROM users ORDER BY id").fetchall()

    table = Table(Pager())
    for row in rows:
        table.execute(parse("insert %d %s %d" % row))
    assert table.execute(parse("select")) == expected


def test_command_line_entry_point():
    result = subprocess.run(
        [sys.executable, "-m", "minidb"],
        input="insert 1 alice 30\nselect\n.exit\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "1|alice|30\n"
