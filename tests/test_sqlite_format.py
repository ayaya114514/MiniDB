"""Databases in SQLite's file format: MiniDB and sqlite3 read and write each
other's files, recover each other's crashes and respect each other's locks."""

import os
import random
import sqlite3
import subprocess
import sys
import textwrap
from contextlib import closing

import pytest

import minidb
from minidb import sqlite_format as F
from minidb.database import Database
from minidb.errors import DatabaseError, IntegrityError, NotSupportedError, OperationalError, ProgrammingError
from sqlcompare import REFERENCE_VERSION, typed
from test_transactions import SimulatedCrash, crash_at

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lite(path):
    connection = sqlite3.connect(path, isolation_level=None, timeout=0.2)
    connection.text_factory = lambda data: data.decode("utf-8", "surrogateescape")
    return connection


def integrity(path):
    with closing(lite(path)) as connection:
        return connection.execute("PRAGMA integrity_check").fetchall()


def same_results(path, db, queries):
    with closing(lite(path)) as other:
        for sql in queries:
            expected = [typed(r) for r in other.execute(sql).fetchall()]
            found = [typed(r) for r in db.execute(sql)]
            assert found == expected, sql


# ---- the byte layout ------------------------------------------------------------


def test_varints():
    for value, encoded in [(0, b"\x00"), (127, b"\x7f"), (128, b"\x81\x00"), (16383, b"\xff\x7f"),
                           (-1, b"\xff" * 9), (2**56, b"\x80\xc0\x80\x80\x80\x80\x80\x80\x00")]:
        assert F.put_varint(value) == encoded
        assert F.get_signed_varint(encoded, 0) == (value, len(encoded))
        assert F.varint_size(value) == len(encoded)
    rng = random.Random(1)
    for _ in range(2000):
        value = rng.randint(-2**63, 2**63 - 1) >> rng.randint(0, 63)
        data = F.put_varint(value)
        assert F.get_signed_varint(data, 0) == (value, len(data))


def test_records_are_byte_for_byte_sqlites(tmp_path):
    path = str(tmp_path / "db")
    samples = [None, 0, 1, -1, 127, 128, -128, -129, 32767, 32768, 2**23, 2**31, 2**47, 2**48, 2**63 - 1,
               -2**63, 1.5, -0.0, "text", "", "é", b"", b"\x00\xff", "x" * 300, [3, "a", None, 2.5]]
    with closing(lite(path)) as connection:
        connection.execute("CREATE TABLE t (x, y)")  # no affinity: values are stored as given
        for value in samples:
            row = value if isinstance(value, list) else [value, None]
            connection.execute("INSERT INTO t VALUES (?, ?)", row[:2])
    with open(path, "rb") as f:
        data = f.read()
    page = F.BtreePage.from_bytes(2, data[F.PAGE_SIZE:2 * F.PAGE_SIZE])
    for cell, value in zip(page.cells, samples):
        row = value[:2] if isinstance(value, list) else [value, None]
        assert cell.local == F.encode_record(row)
        assert F.decode_record(cell.local) == row


# ---- reading each other's files ---------------------------------------------------

SCHEMA = """
CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER, score REAL, note BLOB);
CREATE TABLE tags (tag TEXT PRIMARY KEY, weight NUMERIC UNIQUE);
CREATE INDEX people_age ON people (age, name);
CREATE INDEX people_score ON people (score DESC);
CREATE VIEW adults AS SELECT name, age FROM people WHERE age >= 18;
"""


def fill(execute, rng):
    for i in range(1500):
        name = "p%d" % i + "é" * (i % 3) + "x" * rng.choice([0, 0, 20, 3000])
        execute("INSERT INTO people (name, age, score, note) VALUES (?, ?, ?, ?)",
                (name, rng.choice([None, rng.randint(0, 90)]), rng.choice([None, 2.0, rng.random() * 100]),
                 rng.choice([None, b"\x00\x01", rng.randbytes(rng.choice([1, 5000]))])))
    for i in range(300):
        execute("INSERT INTO tags VALUES (?, ?)", (f"tag{i}", rng.choice([None, i * 3, i * 3 + 0.5, str(i * 3 + 1)])))
    execute("DELETE FROM people WHERE id % 3 = 0")
    execute("UPDATE people SET score = score * 2, name = name || '!' WHERE id % 5 = 1")


QUERIES = [
    "SELECT * FROM people ORDER BY id",
    "SELECT count(*), sum(age), total(score), max(name), min(note) FROM people",
    "SELECT * FROM people WHERE age = 30 ORDER BY id",
    "SELECT name, age FROM people WHERE age BETWEEN 10 AND 20 AND name > 'p5' ORDER BY id",
    "SELECT * FROM tags ORDER BY tag",
    "SELECT * FROM tags WHERE weight = 7 OR weight = 9",
    "SELECT * FROM tags WHERE tag = 'tag42'",
    "SELECT count(*) FROM adults",
    "SELECT score FROM people WHERE score > 50 ORDER BY score DESC, id",
    "SELECT typeof(score), count(*) FROM people GROUP BY 1 ORDER BY 1",
    "SELECT name, type, tbl_name, rootpage FROM sqlite_schema ORDER BY rowid",
]


def test_minidb_reads_a_database_written_by_sqlite(tmp_path):
    path = str(tmp_path / "db")
    with closing(lite(path)) as connection:
        connection.executescript(SCHEMA)
        connection.execute("BEGIN")
        fill(connection.execute, random.Random(2))
        connection.execute("COMMIT")
    with Database(path) as db:
        assert db.pager.format == "sqlite"
        same_results(path, db, QUERIES)
        assert db.integrity_check() == []
        assert db.execute("EXPLAIN SELECT * FROM people WHERE age = 3") == [
            ("people", "SEARCH USING INDEX people_age (age=?)")]
        # an index with a DESC column is kept up to date but not used to search
        assert db.execute("EXPLAIN SELECT * FROM people WHERE score = 3") == [("people", "SCAN")]


def test_sqlite_reads_a_database_written_by_minidb(tmp_path):
    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute("BEGIN")
        fill(db.execute, random.Random(2))
        db.execute("COMMIT")
        db.execute("ALTER TABLE tags ADD COLUMN extra TEXT DEFAULT 'none'")
        db.execute("CREATE TABLE gone (a UNIQUE)")
        db.execute("INSERT INTO gone VALUES (1), (2)")
        db.execute("DROP TABLE gone")
        db.execute("DROP INDEX people_age")
        db.execute("CREATE INDEX people_age ON people (age, name)")
        assert db.integrity_check() == []
        with closing(lite(path)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("SELECT name, sql IS NULL FROM sqlite_schema WHERE type = 'index' "
                                      "ORDER BY name").fetchall() == [
                ("people_age", 0), ("people_score", 0), ("sqlite_autoindex_tags_1", 1),
                ("sqlite_autoindex_tags_2", 1)]
        same_results(path, db, QUERIES + ["SELECT * FROM tags WHERE extra = 'none' ORDER BY tag"])
    assert os.listdir(tmp_path) == ["db"]  # no journal left


def test_alternating_writers(tmp_path):
    path = str(tmp_path / "db")
    reference = lite(":memory:")
    statements = ["CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b)", "CREATE INDEX tb ON t (b)"]
    rng = random.Random(3)
    for round_ in range(40):
        statements.append(rng.choice([
            f"INSERT INTO t (a, b) VALUES ('{'v' * rng.randint(1, 3000)}', {rng.randint(0, 50)})",
            f"UPDATE t SET b = b + 1 WHERE id % {rng.randint(2, 5)} = 0",
            f"DELETE FROM t WHERE b = {rng.randint(0, 50)}",
            "INSERT INTO t (a, b) SELECT a || 'x', b FROM t WHERE id % 7 = 1",
        ]))
    for n, sql in enumerate(statements):
        reference.execute(sql)
        if n % 2:
            with closing(lite(path)) as connection:
                connection.execute(sql)
        else:
            with Database(path, format="sqlite") as db:
                db.execute(sql)
    assert integrity(path) == [("ok",)]
    with Database(path) as db:
        assert db.execute("SELECT * FROM t ORDER BY id") == reference.execute("SELECT * FROM t ORDER BY id").fetchall()
        assert db.integrity_check() == []
    reference.close()


@pytest.mark.skipif(sqlite3.sqlite_version != REFERENCE_VERSION,
                    reason="compares results with the reference SQLite (Windows CI has another version)")
@pytest.mark.parametrize("seed", [7, 8, 9])
def test_fuzz_in_sqlite_format(tmp_path, seed):
    from fuzz import run_seed

    failure = run_seed(seed, 300, str(tmp_path / "fuzz.db"), format="sqlite")
    assert failure is None, failure


def test_in_memory_sqlite_format():
    with Database(format="sqlite") as db:
        db.execute("CREATE TABLE t (a TEXT UNIQUE)")
        db.execute("INSERT INTO t VALUES ('x'), ('y')")
        assert db.execute("SELECT * FROM t ORDER BY a") == [("x",), ("y",)]
        assert db.integrity_check() == []


# ---- ANALYZE and VACUUM, as SQLite does them ------------------------------------------


def test_analyze_and_vacuum_match_sqlite(tmp_path):
    path = str(tmp_path / "db")
    statements = [
        "CREATE TABLE a (x, y)", "CREATE INDEX ax ON a (x)", "CREATE INDEX axy ON a (x, y)",
        "CREATE TABLE b (p UNIQUE, q)", "CREATE TABLE c (z)", "CREATE TABLE e (w)", "CREATE INDEX ew ON e (w)",
        "INSERT INTO a VALUES (1, 1), (1, 2), (2, 2), (3, NULL), (NULL, NULL), (1, 1), (7, 8)",
        "INSERT INTO b VALUES (1, 2), (2, 3), (NULL, 4), (NULL, 5)", "INSERT INTO c VALUES (1), (2), (3)",
        "DELETE FROM c WHERE z = 2", "ANALYZE", "INSERT INTO c VALUES (9)", "ANALYZE c", "ANALYZE ax",
        "DROP TABLE b", "VACUUM",
    ]
    reference = lite(":memory:")
    with Database(path, format="sqlite") as db:
        for sql in statements:
            reference.execute(sql)
            db.execute(sql)
        for query in ["SELECT rowid, * FROM sqlite_stat1", "SELECT rowid, * FROM c", "SELECT rowid, * FROM a"]:
            assert db.execute(query) == reference.execute(query).fetchall(), query
    assert integrity(path) == [("ok",)]
    target = str(tmp_path / "copy")
    with Database(path) as db:
        db.execute("VACUUM INTO ?", [target])
    with closing(lite(target)) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert copy.execute("SELECT rowid, * FROM c").fetchall() == reference.execute("SELECT rowid, * FROM c").fetchall()
    reference.close()


# ---- crashes ------------------------------------------------------------------------


@pytest.mark.parametrize("point, detail", [
    ("journal_header", None), ("journal_page", 1), ("journal_sync", None),
    ("db_page", 0), ("db_page", 3), ("db_sync", None), ("journal_delete", None),
])
@pytest.mark.parametrize("recovered_by", ["sqlite", "minidb"])
def test_crash_during_a_commit(tmp_path, point, detail, recovered_by):
    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
        db.execute("CREATE INDEX ta ON t (a)")
        db.execute("INSERT INTO t (a) VALUES " + ", ".join(f"('{'v' * (i % 50)}{i}')" for i in range(300)))
        before = db.execute("SELECT * FROM t")
    db = Database(path)
    db.pager.crash_hook = crash_at(point, detail)
    with pytest.raises(SimulatedCrash):
        db.execute("UPDATE t SET a = a || 'changed' WHERE id % 2 = 0")
    db.pager.close_files()
    # Until the journal is deleted the commit has not happened: either side
    # rolls it back (a crash before anything reached the file leaves it as it was).
    if recovered_by == "sqlite":
        with closing(lite(path)) as connection:
            assert connection.execute("SELECT * FROM t").fetchall() == before
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    with Database(path) as db:
        assert db.execute("SELECT * FROM t") == before
        assert db.integrity_check() == []
    assert integrity(path) == [("ok",)]
    assert not os.path.exists(path + "-journal")


HOT_JOURNAL = textwrap.dedent("""
    import os, sqlite3, sys
    connection = sqlite3.connect(sys.argv[1], isolation_level=None)
    connection.execute("PRAGMA cache_size = 2")  # dirty pages spill into the file before the commit
    connection.execute("BEGIN")
    connection.execute("UPDATE t SET a = a || 'changed'")
    os._exit(3)  # the process dies with the transaction half written
""")


def test_minidb_rolls_back_a_journal_sqlite_left(tmp_path):
    path = str(tmp_path / "db")
    with closing(lite(path)) as connection:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
        connection.execute("INSERT INTO t (a) SELECT printf('%.500c', 'x') FROM "
                           "(WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 400) SELECT i FROM n)")
        before = connection.execute("SELECT * FROM t").fetchall()
    with open(path, "rb") as f:
        image = f.read()
    script = tmp_path / "hot.py"
    script.write_text(HOT_JOURNAL)
    assert subprocess.run([sys.executable, str(script), path]).returncode == 3
    assert os.path.getsize(path + "-journal") > 0
    with open(path, "rb") as f:
        assert f.read() != image  # sqlite3 did change the file
    with Database(path) as db:
        assert db.execute("SELECT * FROM t") == before
        assert db.integrity_check() == []
    assert not os.path.exists(path + "-journal")
    assert integrity(path) == [("ok",)]


# ---- locks --------------------------------------------------------------------------

HOLD = textwrap.dedent("""
    import sqlite3, sys, time
    connection = sqlite3.connect(sys.argv[1], isolation_level=None)
    connection.execute(sys.argv[2])
    connection.execute("SELECT count(*) FROM t").fetchall()
    print("holding", flush=True)
    sys.stdin.readline()
    connection.execute("COMMIT")
""")


def holder(tmp_path, path, begin):
    script = tmp_path / "hold.py"
    script.write_text(HOLD)
    process = subprocess.Popen([sys.executable, str(script), path, begin],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline() == "holding\n"
    return process


def release(process):
    process.communicate("\n", timeout=30)
    assert process.returncode == 0


def test_locks_against_a_sqlite_process(tmp_path):
    path = str(tmp_path / "db")
    with Database(path, format="sqlite") as db:
        db.execute("CREATE TABLE t (a)")
        db.execute("INSERT INTO t VALUES (1)")
    db = Database(path, timeout=0.2)
    # sqlite3 holds EXCLUSIVE: MiniDB cannot even read
    process = holder(tmp_path, path, "BEGIN EXCLUSIVE")
    with pytest.raises(OperationalError, match="database is locked"):
        db.execute("SELECT * FROM t")
    release(process)
    assert db.execute("SELECT * FROM t") == [(1,)]
    # sqlite3 reads: MiniDB may write but not commit; the transaction stays intact
    process = holder(tmp_path, path, "BEGIN")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (2)")
    with pytest.raises(OperationalError, match="database is locked"):
        db.execute("COMMIT")
    release(process)
    db.execute("COMMIT")
    assert integrity(path) == [("ok",)]
    # MiniDB reads: sqlite3 (in another process: POSIX locks belong to a
    # process) cannot commit
    db.execute("BEGIN")
    db.execute("SELECT * FROM t")
    write = [sys.executable, "-c", "import sqlite3, sys; sqlite3.connect(sys.argv[1], timeout=0.2)"
             ".execute('INSERT INTO t VALUES (3)').connection.commit()", path]
    failed = subprocess.run(write, capture_output=True, text=True)
    assert failed.returncode != 0 and "database is locked" in failed.stderr
    db.execute("COMMIT")
    with closing(lite(path)) as connection:
        connection.execute("INSERT INTO t VALUES (3)")
    assert db.execute("SELECT * FROM t") == [(1,), (2,), (3,)]
    db.close()


# ---- what is not supported ------------------------------------------------------------


@pytest.mark.parametrize("setting, message", [
    ("PRAGMA journal_mode = WAL", "WAL mode"),
    ("PRAGMA page_size = 1024", "page size of 1024"),
    ("PRAGMA encoding = 'UTF-16le'", "UTF-16"),
    ("PRAGMA auto_vacuum = FULL", "auto_vacuum"),
])
def test_files_minidb_refuses(tmp_path, setting, message):
    path = str(tmp_path / "db")
    with closing(lite(path)) as connection:
        connection.execute(setting)
        connection.execute("CREATE TABLE t (a)")
        connection.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(DatabaseError, match=message):
        Database(path)


def test_objects_minidb_cannot_parse(tmp_path):
    path = str(tmp_path / "db")
    with closing(lite(path)) as connection:
        connection.executescript("""
            CREATE TABLE plain (a, b);
            CREATE TABLE checked (a CHECK (a > 0));
            CREATE TABLE logged (a);
            CREATE TRIGGER log AFTER INSERT ON logged BEGIN INSERT INTO plain VALUES (new.a, 'trigger'); END;
            CREATE TABLE odd (a);
            CREATE TRIGGER odd_one AFTER DELETE ON odd BEGIN UPDATE plain SET b = 'z' FROM checked; END;
            CREATE TABLE expressed (a);
            CREATE INDEX lower_a ON expressed (lower(a));
            INSERT INTO plain VALUES (1, 'x');
            INSERT INTO checked VALUES (5);
            INSERT INTO expressed VALUES ('A');
        """)
    with Database(path) as db:
        assert db.execute("SELECT * FROM plain") == [(1, "x")]
        db.execute("INSERT INTO plain VALUES (2, 'y')")
        with pytest.raises(IntegrityError, match="CHECK constraint failed: a > 0"):
            db.execute("INSERT INTO checked VALUES (0)")  # (CHECK is supported now)
        assert db.execute("SELECT * FROM logged") == []
        db.execute("INSERT INTO logged VALUES (1)")  # (triggers are supported now)
        with pytest.raises(NotSupportedError, match="odd_one"):
            db.execute("DELETE FROM odd")  # (MiniDB cannot parse UPDATE ... FROM: the trigger would not run)
        assert db.execute("SELECT * FROM expressed") == [("A",)]
        with pytest.raises(NotSupportedError, match="lower_a"):
            db.execute("DELETE FROM expressed")  # (the index could not be kept up to date)
        db.execute("VACUUM")  # copies what it does not understand as it is
    with closing(lite(path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        connection.execute("INSERT INTO logged VALUES (7)")
        assert connection.execute("SELECT * FROM plain ORDER BY a, b").fetchall() == [
            (1, "trigger"), (1, "x"), (2, "y"), (7, "trigger")]


def test_choosing_the_format(tmp_path):
    path = str(tmp_path / "db")
    with Database(path) as db:
        db.execute("CREATE TABLE t (a)")
    with pytest.raises(OperationalError, match="minidb format, not sqlite"):
        Database(path, format="sqlite")
    with pytest.raises(ProgrammingError, match="unknown database format"):
        Database(format="oracle")
    lite_path = str(tmp_path / "lite")
    connection = minidb.connect(lite_path, format="sqlite")
    connection.execute("CREATE TABLE t (a)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    connection.close()
    with open(lite_path, "rb") as f:
        assert f.read(16) == F.MAGIC
    assert integrity(lite_path) == [("ok",)]


def test_cli_creates_sqlite_files(tmp_path):
    import io

    from minidb import repl

    path = str(tmp_path / "cli.db")
    out = io.StringIO()
    repl.run(io.StringIO("CREATE TABLE t (a);\nINSERT INTO t VALUES (1), (2);\n.btree t\n"), out, path,
             interactive=False, format="sqlite")
    assert "- leaf (page 2, 2 keys): 1, 2" in out.getvalue()
    assert integrity(path) == [("ok",)]
    assert repl.main(["--sqlite", "a", "b"]) == 2


def test_many_processes_on_a_sqlite_file(tmp_path):
    """test_concurrency's writers and reader, on a file in SQLite's format
    (readers and writers exclude each other there, as in SQLite's
    rollback-journal mode)."""
    import test_concurrency as tc

    path = str(tmp_path / "shared.db")
    with Database(path, format="sqlite") as db:
        db.execute("CREATE TABLE counter (n INTEGER)")
        db.execute("INSERT INTO counter VALUES (0)")
        db.execute("CREATE TABLE log (id INTEGER PRIMARY KEY, worker INTEGER, i INTEGER)")
        db.execute("CREATE INDEX log_worker ON log (worker)")
        db.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, balance INTEGER)")
        db.execute("INSERT INTO account VALUES (0, 1000), (1, 1000), (2, 1000), (3, 1000), (4, 1000)")
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(tc.WORKER.format(root=tc.ROOT))
    reader_script = tmp_path / "reader.py"
    reader_script.write_text(tc.READER.format(root=tc.ROOT))
    workers, rounds = 4, 40
    procs = [subprocess.Popen([sys.executable, str(worker_script), path, str(w), str(rounds)]) for w in range(workers)]
    reader = subprocess.Popen([sys.executable, str(reader_script), path, "2", str(workers)],
                              stdout=subprocess.PIPE, text=True)
    assert [p.wait(timeout=180) for p in procs] == [0] * workers
    out, _ = reader.communicate(timeout=180)
    # Readers and writers exclude each other here: how often the reader gets
    # in depends on the machine (once in 2 s on Windows CI, with slow fsync).
    assert reader.returncode == 0 and int(out) >= 1
    with Database(path) as db:
        assert db.execute("SELECT n FROM counter") == [(workers * rounds,)]
        assert db.execute("SELECT count(*), count(DISTINCT worker) FROM log") == [(workers * rounds, workers)]
        assert db.execute("SELECT sum(balance) FROM account") == [(5000,)]
        assert db.integrity_check() == []
    assert integrity(path) == [("ok",)]


@pytest.mark.parametrize("seed", range(3))
def test_btrees_against_a_model(seed):
    """SQLite-format table and index trees under random inserts, replacements
    and deletes (small and overflowing payloads, ascending and DESC index
    columns): contents match a dict, structure checks pass, every page is
    accounted for."""
    from minidb import values
    from minidb.btree import DuplicateKeyError
    from minidb.sqlite_btree import IndexTree, SqliteIndex, SqliteTable, TableTree
    from minidb.sqlite_pager import SqlitePager

    rng = random.Random(seed)
    pager = SqlitePager(None)
    pager.begin_read()
    pager.begin_write()
    table = SqliteTable(pager, TableTree.create(pager), rows=True)
    descending = [rng.random() < 0.5, False]
    index = SqliteIndex(pager, IndexTree.create(pager), descending)
    rows, keys = {}, set()

    def key(value, rowid):
        return (values.sort_key(value), values.sort_key(rowid))

    def payload():
        return rng.choice([rng.randrange(-5, 5), "t" * rng.choice([1, 30, 3000, 9000]), b"\x01" * rng.choice([2, 5000])])

    def check():
        assert dict((rowid, row) for rowid, row in table.scan()) == rows
        assert table.check() == len(rows) == len(table)
        assert table.last_key() == (max(rows) if rows else None)
        assert [k for k, _ in index.scan()] == sorted(keys)
        assert index.check() == len(keys) == len(index)
        assert pager.check_pages([1, table.root, index.root]) == []

    for step in range(1500):
        rowid = rng.randrange(400) if rng.random() < 0.7 else rng.randrange(-50, 10**6)
        action = rng.random()
        if action < 0.55:
            row = [payload(), rng.randrange(30)]
            if rowid in rows:
                with pytest.raises(DuplicateKeyError):
                    table.insert(rowid, row)
                table.insert(rowid, row, replace=True)
                keys.discard(key(rows[rowid][1], rowid))
                index.delete(key(rows[rowid][1], rowid))
            else:
                table.insert(rowid, row)
            rows[rowid] = row
            keys.add(key(row[1], rowid))
            index.insert(key(row[1], rowid))
            with pytest.raises(DuplicateKeyError):
                index.insert(key(row[1], rowid))
            index.insert(key(row[1], rowid), replace=True)
        else:
            assert table.delete(rowid) == (rowid in rows)
            if rowid in rows:
                old = key(rows.pop(rowid)[1], rowid)
                keys.remove(old)
                assert index.delete(old)
            assert not index.delete(key(-1, rowid))
        if step % 300 == 299:
            check()
            low, high = key(rng.randrange(30), 0), key(rng.randrange(30), 10**6)
            expected = [k for k in sorted(keys) if low <= k <= high]
            assert [k for k, _ in index.scan(low, high)] == expected
            assert [k for k, _ in index.scan(low, high, False, False)] == [k for k in expected if low < k < high]
            assert [r for r, _ in table.scan(100, 200, False)] == sorted(r for r in rows if 100 < r <= 200)
    check()
    assert table.depth() > 1 and index.tree.depth() > 1
    copy = SqliteTable(pager, TableTree.create(pager), rows=True)
    copy.bulk_load(sorted(rows.items()))  # bottom-up build
    assert dict(copy.scan()) == rows and copy.check() == len(rows)
    for tree in (table, index, copy):
        tree.clear()
        assert len(tree) == 0 and tree.check() == 0
    table.destroy()
    index.destroy()
    copy.destroy()
    assert pager.check_pages([1]) == []
    pager.rollback()


def test_deep_index_deletes():
    """Long keys make a three-level index B-tree, so deleting an interior
    entry takes its predecessor from a leaf two levels down."""
    from minidb import values
    from minidb.sqlite_btree import IndexTree, SqliteIndex
    from minidb.sqlite_pager import SqlitePager

    rng = random.Random(5)
    pager = SqlitePager(None)
    pager.begin_read()
    pager.begin_write()
    index = SqliteIndex(pager, IndexTree.create(pager), None)
    keys = [(values.sort_key(f"{i:05}" + "k" * 400), values.sort_key(i)) for i in range(600)]
    for key in rng.sample(keys, len(keys)):
        index.insert(key)
    assert index.tree.depth() >= 3 and index.check() == 600
    assert index.last_key() == max(keys)
    rng.shuffle(keys)
    for n, key in enumerate(keys, 1):
        assert index.delete(key)
        if n % 50 == 0:
            assert [k for k, _ in index.scan()] == sorted(keys[n:]) and index.check() == len(keys) - n
            assert pager.check_pages([1, index.root]) == []
    assert index.last_key() is None
    pager.rollback()
