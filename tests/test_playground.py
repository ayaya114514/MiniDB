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


def sqlite_file(path):
    """An SQLite file written by sqlite3 with what real files have: free
    blocks and fragments (deleted rows, shorter updates), overflow pages,
    freelist pages (a dropped table), an index."""
    import sqlite3

    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, data BLOB);
        CREATE INDEX t_name ON t (name);
        CREATE TABLE gone (x);
        WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1500)
        INSERT INTO t (name, data) SELECT printf('name %05d %.*c', i, i % 40, 'x'), randomblob(CASE WHEN i % 100 = 0 THEN 9000 ELSE i % 50 END) FROM n;
        INSERT INTO gone SELECT randomblob(500) FROM t;
        DELETE FROM t WHERE id % 3 = 0;
        UPDATE t SET name = substr(name, 1, 10) WHERE id % 7 = 0;
        DROP TABLE gone;
    """)
    connection.commit()
    connection.close()
    with open(path, "rb") as f:
        return f.read()


def test_sqlite_files_pages_and_export(tmp_path):
    import sqlite3

    image = sqlite_file(tmp_path / "f.sqlite")
    info = json.loads(bridge.open_file(image, "f.sqlite"))
    assert info == {"name": "f.sqlite", "format": "sqlite", "pages": len(image) // 4096, "bytes": len(image),
                    "page_size": 4096}
    names = [o["name"] for o in json.loads(bridge.objects())]
    assert names == ["sqlite_schema", "t", "t_name"]
    reference = sqlite3.connect(tmp_path / "f.sqlite")

    # Every page is accounted for, the freelist as SQLite counts it.
    pages = json.loads(bridge.file_map())["pages"]
    kinds = [kind for kind, _ in pages]
    assert "unknown" not in kinds and len(pages) == info["pages"]
    free = kinds.count("freelist-trunk") + kinds.count("freelist-leaf")
    assert free == reference.execute("PRAGMA freelist_count").fetchone()[0] > 0
    assert kinds.count("overflow") == 10 * 2  # (the 10 rows left with a 9000-byte blob: 2 pages each)

    # Each B-tree page: the regions cover its bytes exactly once, the
    # fragments add up to what its header says.
    fragments_seen = freeblocks_seen = 0
    for pgno, kind in enumerate(kinds, 1):
        if kind not in ("table-leaf", "table-interior", "index-leaf", "index-interior"):
            continue
        layout = json.loads(bridge.page(pgno))
        assert layout["kind"] == kind
        position = 0
        for region in layout["regions"]:
            assert region["start"] == position, (pgno, region)
            position = region["end"]
        assert position == 4096
        fragments = sum(r["end"] - r["start"] for r in layout["regions"] if r["kind"] == "fragment")
        assert fragments == layout["fragmented"], pgno
        fragments_seen += fragments
        freeblocks_seen += sum(r["kind"] == "freeblock" for r in layout["regions"])
    assert fragments_seen and freeblocks_seen  # (the file has both)

    # The table's leaves, in tree order, hold sqlite3's row ids.
    tree = json.loads(bridge.tree("t"))
    assert tree["keys"] == reference.execute("SELECT count(*) FROM t").fetchone()[0]
    leaves = []

    def walk(pgno):
        layout = json.loads(bridge.page(pgno))
        if layout["kind"] == "table-leaf":
            leaves.extend(int(cell["key"]) for cell in layout["listed"])
        else:
            for cell in layout["listed"]:
                walk(cell["child"])
            walk(layout["right"])
    walk(tree["root"])
    assert leaves == [r[0] for r in reference.execute("SELECT id FROM t ORDER BY id")]
    index = json.loads(bridge.tree("t_name"))
    assert index["keys"] == tree["keys"]

    # Change it, export it: sqlite3 finds the changes and a sound file.
    results = json.loads(bridge.run("DELETE FROM t WHERE id < 100; UPDATE t SET name = upper(name) WHERE id % 5 = 0; "
                                    "CREATE TABLE u (v); INSERT INTO u VALUES (zeroblob(20000))"))
    assert all("error" not in r for r in results), results
    exported = bridge.export()
    check = sqlite3.connect(":memory:")
    check.deserialize(exported)
    assert check.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    reference.executescript("DELETE FROM t WHERE id < 100; UPDATE t SET name = upper(name) WHERE id % 5 = 0;")
    assert check.execute("SELECT * FROM t ORDER BY id").fetchall() == reference.execute("SELECT * FROM t ORDER BY id").fetchall()
    assert check.execute("SELECT length(v) FROM u").fetchall() == [(20000,)]
    assert json.loads(bridge.info())["pages"] == len(exported) // 4096


def test_files_the_playground_refuses(tmp_path):
    import sqlite3

    bridge.reset()
    with pytest.raises(ValueError, match="不是 SQLite 数据库文件"):
        bridge.open_file(b"MiniDB or anything else", "x.db")
    utf16 = tmp_path / "utf16.sqlite"
    connection = sqlite3.connect(utf16)
    connection.executescript("PRAGMA encoding = 'UTF-16le'; CREATE TABLE t (x); INSERT INTO t VALUES (1);")
    connection.close()
    with pytest.raises(Exception, match="UTF-16"):
        bridge.open_file(utf16.read_bytes(), "utf16.sqlite")
    assert json.loads(bridge.info())["format"] == "minidb"  # (still the database it had)
    with pytest.raises(Exception, match="SQLite's file format"):
        bridge.export()


def test_the_file_kinds_of_stage_25(tmp_path):
    # 1024-byte pages, auto_vacuum, WAL mode (the file alone: what was
    # checkpointed into it), WITHOUT ROWID and generated columns.
    import sqlite3

    path = tmp_path / "w.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA page_size = 1024; PRAGMA auto_vacuum = FULL; PRAGMA journal_mode = WAL;
        CREATE TABLE w (k TEXT PRIMARY KEY, n INT, g AS (n * 2)) WITHOUT ROWID;
        CREATE INDEX wg ON w (g);
    """)
    connection.executemany("INSERT INTO w (k, n) VALUES (?, ?)", [(f"key{i:04}", i) for i in range(500)])
    connection.commit()
    connection.close()
    image = path.read_bytes()
    info = json.loads(bridge.open_file(image, "w.sqlite"))
    assert info["page_size"] == 1024 and info["pages"] == len(image) // 1024
    kinds = [kind for kind, _ in json.loads(bridge.file_map())["pages"]]
    assert "unknown" not in kinds[2:] and "index-leaf" in kinds
    results = json.loads(bridge.run("SELECT count(*), sum(g) FROM w; DELETE FROM w WHERE n % 2 = 0"))
    assert results[0]["rows"] == [[500, 249500]]
    exported = bridge.export()
    copy = sqlite3.connect(":memory:")
    copy.deserialize(exported[:18] + b"\x01\x01" + exported[20:])  # (sqlite3 opens no WAL image in memory)
    assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert copy.execute("SELECT count(*), sum(g) FROM w").fetchall() == [(250, 2 * sum(range(1, 500, 2)))]
