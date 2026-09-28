import io
import subprocess
import sys

from minidb.repl import format_row, run, statement_complete


def run_script(text, path=None, interactive=False):
    out = io.StringIO()
    run(io.StringIO(text), out, path=path, interactive=interactive)
    return out.getvalue().splitlines()


def test_statements_and_results():
    assert run_script(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, score INTEGER);\n"
        "INSERT INTO t VALUES (1, 'a', 10), (2, NULL, 2.5);\n"
        "SELECT * FROM t;\n"
    ) == ["1|a|10", "2||2.5"]


def test_multi_line_statement_and_several_per_line():
    assert run_script("SELECT 1,\n  2\n;SELECT 3; SELECT 4;\n") == ["1|2", "3", "4"]


def test_semicolon_inside_string_does_not_end_statement():
    assert run_script("SELECT 'a;\nb';\n") == ["a;", "b"]


def test_statement_without_semicolon_at_end_of_input_runs():
    assert run_script("SELECT 42") == ["42"]


def test_errors_are_reported_and_shell_continues():
    output = run_script("SELECT * FROM nope;\nSELECT 1 +;\nSELECT 2;\n")
    assert output == [
        "Error: no such table: nope",
        'Error: syntax error near ";": expected expression (line 1, column 11)',
        "SELECT 1 +;",
        "          ^",
        "2",
    ]


def test_meta_commands():
    output = run_script(
        "CREATE TABLE b (x INTEGER);\nCREATE TABLE a (y TEXT NOT NULL);\n"
        ".tables\n.schema\n.schema a\n.btree a\n.help\n.nope\n.btree\n.btree zz\n"
    )
    assert output[0] == "a b"
    assert output[1] == 'CREATE TABLE "a" ("y" TEXT NOT NULL);'
    assert output[2] == 'CREATE TABLE "b" ("x" INTEGER);'
    assert output[3] == 'CREATE TABLE "a" ("y" TEXT NOT NULL);'
    assert output[4].startswith("- leaf (page")
    assert any(line.startswith(".btree") for line in output)
    assert 'Error: unknown command: .nope. Enter ".help" for help' in output
    assert "Usage: .btree TABLE" in output
    assert output[-1] == "Error: no such table: zz"


def test_schema_lists_indexes():
    output = run_script(
        "CREATE TABLE t (a INTEGER, b TEXT UNIQUE);\nCREATE INDEX t_a ON t (a);\n"
        "CREATE UNIQUE INDEX t_ab ON t (a, b);\n.schema t\n"
    )
    assert output == [
        'CREATE TABLE "t" ("a" INTEGER, "b" TEXT UNIQUE);',
        'CREATE INDEX "t_a" ON "t" ("a");',
        'CREATE UNIQUE INDEX "t_ab" ON "t" ("a", "b");',
    ]


def test_btree_command_shows_levels():
    inserts = "".join(f"INSERT INTO t VALUES ({i}, '{'x' * 50}');\n" for i in range(300))
    output = run_script("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);\n" + inserts + ".btree t\n")
    assert output[0].startswith("- internal")
    assert sum("- leaf" in line for line in output) > 3


def test_exit_stops_processing():
    assert run_script("SELECT 1;\n.exit\nSELECT 2;\n") == ["1"]


def test_prompts_in_interactive_mode():
    out = io.StringIO()
    run(io.StringIO("SELECT\n1;\n"), out, interactive=True)
    assert out.getvalue() == "minidb>    ...> 1\nminidb> "


def test_persistence_across_sessions(tmp_path):
    path = str(tmp_path / "shell.db")
    run_script("CREATE TABLE t (a TEXT);\nINSERT INTO t VALUES ('kept');\n", path)
    assert run_script("SELECT * FROM t;\n", path) == ["kept"]


def test_format_row():
    assert format_row((1, None, 2.5, "x", 1e20)) == "1||2.5|x|1.0e+20"


def test_statement_complete():
    assert statement_complete("SELECT 1;")
    assert statement_complete("SELECT 1; -- done")
    assert not statement_complete("SELECT 1")
    assert not statement_complete("SELECT ';")
    assert not statement_complete("SELECT 1 /* ; */")
    assert statement_complete("SELECT # ;")  # a lexical error: let the parser report it


def test_command_line(tmp_path):
    path = str(tmp_path / "cli.db")
    for script, expected in [
        ("CREATE TABLE t (a INTEGER);\nINSERT INTO t VALUES (7);\n.exit\n", ""),
        ("SELECT a * 6 FROM t;\n", "42\n"),
    ]:
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
    assert result.stderr.startswith("Error:")


def test_views_in_tables_and_schema():
    output = run_script(
        "CREATE TABLE t (a INTEGER);\nCREATE VIEW v AS SELECT a FROM t;\n.tables\n.schema\n.schema v\n"
    )
    assert output == [
        "t v",
        'CREATE TABLE "t" ("a" INTEGER);',
        "CREATE VIEW v AS SELECT a FROM t;",
        "CREATE VIEW v AS SELECT a FROM t;",
    ]
