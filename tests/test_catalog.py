import pytest

from minidb.database import Database
from minidb.errors import OperationalError


def test_schema_and_data_persist(tmp_path):
    path = str(tmp_path / "db")
    with Database(path) as db:
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER)")
        db.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, author INTEGER, title TEXT UNIQUE)")
        db.execute("INSERT INTO users VALUES (1, 'alice', 30), (2, 'bob', 25)")
        db.execute("INSERT INTO posts (author, title) VALUES (1, 'hello'), (2, 'world')")
    with Database(path) as db:
        assert sorted(db.catalog.tables) == ["posts", "users"]
        users = db.catalog.get_table("USERS")
        assert [c.name for c in users.columns] == ["id", "name", "age"]
        assert users.rowid_column == 0
        assert users.columns[1].not_null
        assert db.catalog.get_table("posts").columns[2].unique
        assert db.execute("SELECT * FROM users") == [(1, "alice", 30), (2, "bob", 25)]
        assert db.execute("SELECT id, title FROM posts WHERE author = 2") == [(2, "world")]
        with pytest.raises(Exception):
            db.execute("INSERT INTO users (id) VALUES (3)")  # NOT NULL survives reopening


def test_many_tables(tmp_path):
    path = str(tmp_path / "db")
    with Database(path) as db:
        for i in range(60):
            db.execute(f"CREATE TABLE t{i} (a INTEGER, b TEXT)")
            db.execute(f"INSERT INTO t{i} VALUES ({i}, 'row of t{i}')")
    with Database(path) as db:
        assert len(db.catalog.tables) == 60
        for i in range(60):
            assert db.execute(f"SELECT * FROM t{i}") == [(i, f"row of t{i}")]


def test_drop_table_frees_pages_and_persists(tmp_path):
    path = str(tmp_path / "db")
    with Database(path) as db:
        db.execute("CREATE TABLE keep (a INTEGER)")
        db.execute("CREATE TABLE big (a INTEGER, b TEXT)")
        for i in range(0, 3000, 100):
            values = ", ".join(f"({j}, '{'x' * 100}')" for j in range(i, i + 100))
            db.execute(f"INSERT INTO big VALUES {values}")
        pages = db.pager.page_count
        db.execute("DROP TABLE big")
        assert db.pager.free_page_count() > pages - 10
    with Database(path) as db:
        assert list(db.catalog.tables) == ["keep"]
        with pytest.raises(OperationalError):
            db.execute("SELECT * FROM big")
        db.execute("CREATE TABLE big2 (a INTEGER)")
        db.execute("INSERT INTO big2 VALUES (1)")
        assert db.pager.page_count == pages  # freed pages were reused


def test_failed_ddl_is_rolled_back():
    db = Database()
    db.execute("CREATE TABLE t (a INTEGER)")
    with pytest.raises(OperationalError):
        db.execute("CREATE TABLE u (a INTEGER, a TEXT)")
    assert list(db.catalog.tables) == ["t"]
    assert db.catalog.schema.check() == 1


def test_quoted_names_round_trip(tmp_path):
    path = str(tmp_path / "db")
    with Database(path) as db:
        db.execute('CREATE TABLE "odd ""name""" ("col one" INTEGER, [select] TEXT)')
        db.execute("INSERT INTO \"odd \"\"name\"\"\" VALUES (1, 'x')")
    with Database(path) as db:
        table = db.catalog.get_table('odd "name"')
        assert [c.name for c in table.columns] == ["col one", "select"]
        assert db.execute('SELECT "col one", "select" FROM "odd ""name"""') == [(1, "x")]


def test_result_column_names():
    db = Database()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    assert db.execute("SELECT * FROM t").columns == ["id", "name"]
    assert db.execute("SELECT name AS n, id + 1, t.id FROM t").columns == ["n", "id + 1", "id"]


def test_large_rows_use_overflow_pages(tmp_path):
    path = str(tmp_path / "db")
    big = "y" * 50_000
    with Database(path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        db.execute(f"INSERT INTO t VALUES (1, '{big}'), (2, 'small')")
    with Database(path) as db:
        assert db.execute("SELECT length(body) FROM t") == [(50_000,), (5,)]
        db.execute("UPDATE t SET body = 'short' WHERE id = 1")
        assert db.execute("SELECT body FROM t WHERE id = 1") == [("short",)]
        assert db.pager.free_page_count() >= 12
