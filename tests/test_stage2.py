import io
import subprocess
import sys

from minidb.executor import Table
from minidb.pager import PAGE_SIZE, Pager
from minidb.parser import parse
from minidb.repl import run


def run_script(lines, path):
    out = io.StringIO()
    run(io.StringIO("".join(line + "\n" for line in lines)), out, path=path, interactive=False)
    return out.getvalue().splitlines()


def test_data_survives_close_and_reopen(tmp_path):
    path = str(tmp_path / "users.db")
    assert run_script(["insert 1 alice 30", "insert 2 bob 25"], path) == []
    assert run_script(["select"], path) == ["1|alice|30", "2|bob|25"]


def test_many_rows_span_pages_and_persist(tmp_path):
    path = str(tmp_path / "users.db")
    pager = Pager(path)
    table = Table(pager)
    for i in range(3000):
        table.execute(parse(f"insert {i} name{i} {i % 100}"))
    pager.close()
    assert (tmp_path / "users.db").stat().st_size > 10 * PAGE_SIZE

    pager = Pager(path)
    rows = Table(pager).execute(parse("select"))
    pager.close()
    assert rows == [(i, f"name{i}", i % 100) for i in range(3000)]


def test_duplicate_detected_after_reopen(tmp_path):
    path = str(tmp_path / "users.db")
    run_script(["insert 7 x 1"], path)
    assert run_script(["insert 7 y 2", "select"], path) == ["Error: duplicate id 7", "7|x|1"]


def test_unicode_and_long_names_persist(tmp_path):
    path = str(tmp_path / "users.db")
    name = "名前" * 300
    run_script([f"insert 1 {name} 3"], path)
    assert run_script(["select"], path) == [f"1|{name}|3"]


def test_command_line_with_file(tmp_path):
    path = str(tmp_path / "cli.db")
    for script, expected in [("insert 1 a 2\n.exit\n", ""), ("select\n", "1|a|2\n")]:
        result = subprocess.run(
            [sys.executable, "-m", "minidb", path],
            input=script, capture_output=True, text=True, check=True,
        )
        assert result.stdout == expected


def test_command_line_rejects_bad_file(tmp_path):
    path = tmp_path / "bad.db"
    path.write_bytes(b"not a database")
    result = subprocess.run(
        [sys.executable, "-m", "minidb", str(path)], input="", capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "Error" in result.stderr
