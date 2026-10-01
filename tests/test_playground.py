"""The browser playground's Python side (playground/bridge.py), run natively,
and every example of playground/app.js."""

import json
import os
import re
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "playground"))

import bridge  # noqa: E402


def examples():
    with open(os.path.join(ROOT, "playground", "app.js"), encoding="utf-8") as f:
        return re.findall(r'\["([^"]+)", `(.*?)`\]', f.read(), re.S)


@pytest.mark.parametrize("name, sql", examples())
def test_examples_run_without_errors(name, sql):
    bridge.reset()
    results = json.loads(bridge.run(sql))
    assert results and all("error" not in r for r in results), results
    for entry in json.loads(bridge.objects()):
        tree = json.loads(bridge.tree(entry["name"]))
        assert tree["depth"] >= 1 and tree["keys"] >= 0


def test_run_reports_results_plans_and_errors():
    bridge.reset()
    results = json.loads(bridge.run(
        "CREATE TABLE t (a INTEGER, b); CREATE INDEX ta ON t (a); "
        "INSERT INTO t VALUES (1, x'00ff'), (2, 1e999), (3, NULL); "
        "SELECT * FROM t WHERE a BETWEEN 1 AND 2; SELECT nope; SELECT 1"))
    assert [r["sql"] for r in results] == [
        "CREATE TABLE t (a INTEGER, b)", "CREATE INDEX ta ON t (a)",
        "INSERT INTO t VALUES (1, x'00ff'), (2, 1e999), (3, NULL)",
        "SELECT * FROM t WHERE a BETWEEN 1 AND 2", "SELECT nope",
    ]  # stops at the first error
    assert results[2]["rowcount"] == 3
    assert results[3]["plan"] == [["t", "SEARCH USING INDEX ta (a>=? AND a<=?)"]]
    assert results[3]["rows"] == [[1, {"blob": "00ff"}], [2, {"real": "Inf"}]]
    assert results[4]["error"] == "no such column: nope"
    assert json.loads(bridge.run("SELECT 'x"))[0]["error"].startswith("unterminated string")


def test_tree_levels():
    bridge.reset()
    bridge.run("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); CREATE INDEX tv ON t (v); "
               "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3000) "
               "INSERT INTO t (v) SELECT 'value ' || i FROM n")
    assert json.loads(bridge.objects()) == [
        {"name": "t", "kind": "table"}, {"name": "tv", "kind": "index", "table": "t"}]
    tree = json.loads(bridge.tree("t"))
    assert tree["keys"] == 3000 and tree["depth"] == len(tree["levels"]) >= 2
    root = tree["levels"][0][0]
    assert not root["leaf"] and len(root["children"]) == len(tree["levels"][1])
    leaf = tree["levels"][-1][0]
    assert leaf["leaf"] and leaf["keys"][0] == "1" and 0 < leaf["fill"] <= 1
    index = json.loads(bridge.tree("TV"))
    assert index["levels"][-1][0]["keys"][0] == "'value 1' | 1"
    assert "error" in json.loads(bridge.tree("nope"))


def test_build(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import build_playground

    version = build_playground.build(str(tmp_path))
    names = zipfile.ZipFile(tmp_path / "minidb.zip").namelist()
    assert "bridge.py" in names and "minidb/database.py" in names
    for name in ("index.html", "app.js", "worker.js"):
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert "__BUILD__" not in text and version in text
