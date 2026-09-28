"""The DB-API 2.0 interface and parameter binding, compared with the sqlite3 module."""

import sqlite3
import sys

import pytest

import minidb
from minidb.database import Database
from sqlcompare import Pair


PEP_249_ERRORS = {
    "Warning", "Error", "InterfaceError", "DatabaseError", "DataError", "OperationalError",
    "IntegrityError", "InternalError", "ProgrammingError", "NotSupportedError",
}


def error_class(exc):
    """The PEP 249 class name of ``exc`` (MiniDB's SQLSyntaxError is an
    OperationalError, which is what sqlite3 raises), or its builtin name."""
    for cls in type(exc).__mro__:
        if cls.__name__ in PEP_249_ERRORS or cls.__module__ == "builtins":
            return cls.__name__
    return type(exc).__name__


def error_message(exc):
    """The message without MiniDB's ``(line L, column C)`` suffix."""
    return getattr(exc, "message", str(exc))


# The reference is the sqlite3 module of Python 3.12+, whose ``autocommit``
# parameter MiniDB mirrors.  Python 3.11 lacks it: there, isolation_level=None
# stands in for autocommit=True in the tests that do not depend on transaction
# handling, and the transaction tests are skipped.
HAS_AUTOCOMMIT = sys.version_info >= (3, 12)
needs_autocommit = pytest.mark.skipif(not HAS_AUTOCOMMIT, reason="sqlite3 autocommit needs Python 3.12+")


def connect(module, path, autocommit=True):
    if module is sqlite3 and not HAS_AUTOCOMMIT:
        assert autocommit is True
        return sqlite3.connect(path, isolation_level=None)
    return module.connect(path, autocommit=autocommit)


def outcome(module, script, autocommit=True):
    """Run ``script(connection)`` and describe everything it observed."""
    conn = connect(module, ":memory:", autocommit)
    observed = []

    def note(value):
        observed.append(value)

    try:
        script(conn, note)
    except Exception as exc:  # compare the error, whatever it is
        observed.append(("error", error_class(exc), error_message(exc)))
    finally:
        conn.close()
    return observed


def same_behaviour(script, autocommit=True):
    expected = outcome(sqlite3, script, autocommit)
    actual = outcome(minidb, script, autocommit)
    assert actual == expected


def cursor_state(cursor):
    return (cursor.rowcount, cursor.lastrowid, cursor.description)


# ---- module level -------------------------------------------------------------


def test_module_attributes():
    assert (minidb.apilevel, minidb.threadsafety, minidb.paramstyle) == ("2.0", 1, "qmark")
    for name in ["Warning", "Error", "InterfaceError", "DatabaseError", "DataError",
                 "OperationalError", "IntegrityError", "InternalError", "ProgrammingError",
                 "NotSupportedError"]:
        assert getattr(minidb, name) is getattr(minidb.Connection, name)
    assert issubclass(minidb.IntegrityError, minidb.DatabaseError)
    assert issubclass(minidb.OperationalError, minidb.DatabaseError)
    assert issubclass(minidb.ProgrammingError, minidb.DatabaseError)
    assert issubclass(minidb.DatabaseError, minidb.Error)
    assert issubclass(minidb.InterfaceError, minidb.Error)
    from minidb.tokenizer import SQLSyntaxError
    assert issubclass(SQLSyntaxError, minidb.OperationalError)  # like sqlite3


# ---- parameter binding -------------------------------------------------------------


@pytest.mark.parametrize("sql, parameters", [
    ("SELECT ?, ?3, ?, :x, ?", (1, 2, 3, 4, 5, 6)),
    ("SELECT ?, ?3, ?, :x, ?", (1, 2, 3, 4, 5)),
    ("SELECT ?2, ?", (1, 2, 3)),
    ("SELECT ?, ?", (1,)),
    ("SELECT ?", (1, 2)),
    ("SELECT ?", ()),
    ("SELECT :a, :b, :a", {"a": 1, "b": 2}),
    ("SELECT :a, @b, $c", {"a": 1, "b": "two", "c": None, "extra": 5}),
    ("SELECT :a", {"b": 1}),
    ("SELECT ?", {"a": 1}),
    ("SELECT ?1, ?1, ?2", (7, 8)),
    ("SELECT ?0", (1,)),
    ("SELECT ?32767", (1,)),
    ("SELECT ?", (True,)),
    ("SELECT ?", (2**63,)),
    ("SELECT ?", (-(2**63),)),
    ("SELECT ?", (object(),)),
    ("SELECT ?", ([1],)),
    ("SELECT typeof(?), typeof(?), typeof(?), typeof(?)", (1, 1.5, "x", None)),
    ("SELECT ? = '1', ? || 'x', ? + 1", (1, 2.5, "7")),
    ("SELECT 1; SELECT 2", ()),
    ("SELECT 1; SELECT 2", None),
])
def test_binding_matches_sqlite3(sql, parameters):
    def script(conn, note):
        cursor = conn.execute(sql) if parameters is None else conn.execute(sql, parameters)
        note(cursor.fetchall())
        note(cursor.description)

    if sql == "SELECT ?, ?3, ?, :x, ?" and len(parameters) == 6 and sys.version_info < (3, 14):
        # a named placeholder bound from a sequence: sqlite3 before 3.14
        # only warns; MiniDB already raises like 3.14
        assert outcome(minidb, script) == [("error", "ProgrammingError", NAMED_FROM_SEQUENCE)]
        return
    same_behaviour(script)


NAMED_FROM_SEQUENCE = ("Binding 5 (':x') is a named parameter, but you supplied a sequence "
                       "which requires nameless (qmark) placeholders.")


def test_parameters_in_every_clause():
    pair = Pair()
    pair.run("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, city TEXT)")
    for row in [(1, "ann", 30, "oslo"), (2, "bob", 25, None), (3, "cat", 41, "rome"), (4, "dan", 25, "oslo")]:
        pair.run("INSERT INTO t VALUES (?, ?, ?, ?)", parameters=row)
    pair.run("INSERT INTO t (name, age) VALUES (:n, :a)", parameters={"n": "eve", "a": "33"})
    pair.run("CREATE INDEX t_age ON t (age)")
    cases = [
        ("SELECT * FROM t WHERE id = ?", (3,)),
        ("SELECT * FROM t WHERE id = ?", ("3",)),
        ("SELECT * FROM t WHERE age = ?", ("25",)),
        ("SELECT * FROM t WHERE age > ? AND age < ?", (24, 40)),
        ("SELECT * FROM t WHERE name IN (?, ?, ?)", ("ann", "zed", None)),
        ("SELECT * FROM t WHERE name LIKE ?", ("%a%",)),
        ("SELECT * FROM t WHERE age BETWEEN ?1 AND ?2 OR id = ?1", (25, 30)),
        ("SELECT * FROM t ORDER BY ?, id", (2,)),
        ("SELECT * FROM t ORDER BY age, id LIMIT ? OFFSET ?", (2, 1)),
        ("SELECT * FROM t ORDER BY id LIMIT ?", ("2",)),
        ("SELECT * FROM t ORDER BY id LIMIT ?", (2.5,)),
        ("SELECT city, count(*) FROM t GROUP BY city HAVING count(*) > ?", (1,)),
        ("SELECT name, ? FROM t WHERE ? IS NULL", (None, None)),
        ("SELECT a.name, b.name FROM t a JOIN t b ON b.age = a.age + ? ", (5,)),
        ("SELECT coalesce(city, ?) FROM t", ("nowhere",)),
        ("SELECT * FROM t WHERE age = ?", (25.0,)),
        ("SELECT * FROM t WHERE id = ?", (None,)),
        ("UPDATE t SET age = age + ? WHERE city = ?", (1, "oslo")),
        ("SELECT * FROM t", None),
        ("DELETE FROM t WHERE age < ?", (30,)),
        ("SELECT * FROM t", None),
        ("INSERT INTO t VALUES (?, ?, ?, ?)", (1, "dup", 1, "x")),
    ]
    for sql, parameters in cases:
        pair.run(sql, parameters=parameters)
    pair.close()


def test_bound_zero_is_not_folded_like_a_literal():
    pair = Pair()
    pair.run("CREATE TABLE t (a INTEGER, b TEXT)")
    pair.run("INSERT INTO t VALUES (1, 'z'), (2, 'y'), (3, 'x')")
    for sql, parameters in [
        ("SELECT a, b FROM t ORDER BY (a AND ?) DESC, b", (0,)),
        ("SELECT a, b FROM t ORDER BY (? IS NULL) DESC, b", (2,)),
        ("SELECT a, b FROM t ORDER BY +?, b", (1,)),
        ("SELECT a, b FROM t ORDER BY ? DESC, b", (1,)),
    ]:
        pair.run(sql, parameters=parameters)
    pair.close()


# ---- cursors ----------------------------------------------------------------------


def test_cursor_attributes_match_sqlite3():
    def script(conn, note):
        cursor = conn.cursor()
        note(cursor_state(cursor))
        cursor.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        note(cursor_state(cursor))
        cursor.execute("INSERT INTO t (v) VALUES ('a'), ('b')")
        note(cursor_state(cursor))
        cursor.execute("INSERT INTO t VALUES (10, 'c')")
        note(cursor_state(cursor))
        cursor.execute("UPDATE t SET v = v || '!' WHERE id > 1")
        note(cursor_state(cursor))
        cursor.execute("SELECT id, v AS value, id * 2 FROM t WHERE 0")
        note(cursor_state(cursor))
        cursor.execute("DELETE FROM t")
        note(cursor_state(cursor))
        cursor.execute("   ")
        note(cursor_state(cursor))
        cursor.executemany("INSERT INTO t (v) VALUES (?)", [("x",), ("y",), ("z",)])
        note(cursor_state(cursor))
        cursor.executemany("UPDATE t SET v = ? WHERE id = ?", [("p", 1), ("q", 2), ("r", 99)])
        note(cursor_state(cursor))
        note(conn.total_changes)

    same_behaviour(script)


def test_fetching_matches_sqlite3():
    def script(conn, note):
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(10)])
        cursor = conn.execute("SELECT a FROM t ORDER BY a")
        note(cursor.fetchone())
        note(cursor.fetchmany())
        cursor.arraysize = 3
        note(cursor.fetchmany())
        note(cursor.fetchmany(2))
        note(list(cursor))
        note(cursor.fetchone())
        note(cursor.fetchall())
        note(cursor.fetchmany(5))
        note([row for row in conn.execute("SELECT a * a FROM t WHERE a < 4")])

    same_behaviour(script)


def test_api_errors_match_sqlite3():
    def script(conn, note):
        conn.execute("CREATE TABLE t (a INTEGER UNIQUE)")
        for call in [
            lambda: conn.executemany("SELECT ?", [(1,)]),
            lambda: conn.execute("INSERT INTO t VALUES (1); INSERT INTO t VALUES (2)"),
            lambda: conn.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (1,)]),
            lambda: conn.execute("SELECT nope FROM t"),
            lambda: conn.execute("SELEC 1"),
        ]:
            try:
                call()
                note("ok")
            except Exception as exc:
                note((error_class(exc), isinstance(exc, (sqlite3.Error, minidb.Error))))
        note(conn.execute("SELECT a FROM t ORDER BY a").fetchall())
        cursor = conn.cursor()
        cursor.close()
        try:
            cursor.execute("SELECT 1")
        except Exception as exc:
            note((type(exc).__name__, str(exc)))
        conn.close()
        for call in [conn.cursor, lambda: conn.execute("SELECT 1"), conn.commit]:
            try:
                call()
            except Exception as exc:
                note((type(exc).__name__, str(exc)))

    same_behaviour(script)


# ---- transactions ---------------------------------------------------------------


@needs_autocommit
@pytest.mark.parametrize("autocommit", [False, True])
def test_transaction_behaviour_matches_sqlite3(autocommit, tmp_path):
    def run(module):
        path = str(tmp_path / f"{module.__name__}.db")
        observed = []
        conn = module.connect(path, autocommit=autocommit)
        observed.append(conn.in_transaction)
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()
        observed.append(conn.in_transaction)
        conn.execute("INSERT INTO t VALUES (1)")
        observed.append(conn.in_transaction)
        conn.rollback()
        observed.append((conn.in_transaction, conn.execute("SELECT count(*) FROM t").fetchall()))
        conn.execute("INSERT INTO t VALUES (2)")
        conn.executescript("INSERT INTO t VALUES (3)")
        observed.append(conn.in_transaction)
        conn.commit()
        conn.execute("INSERT INTO t VALUES (4)")
        conn.close()  # uncommitted work is rolled back
        conn = module.connect(path, autocommit=autocommit)
        observed.append(conn.execute("SELECT a FROM t ORDER BY a").fetchall())
        with conn:
            conn.execute("INSERT INTO t VALUES (5)")
        try:
            with conn:
                conn.execute("INSERT INTO t VALUES (6)")
                raise KeyError("boom")
        except KeyError:
            pass
        observed.append(conn.execute("SELECT a FROM t ORDER BY a").fetchall())
        if not autocommit:
            conn.execute("COMMIT")  # plain SQL COMMIT ends the implicit transaction
            observed.append(conn.in_transaction)
            conn.execute("INSERT INTO t VALUES (7)")  # ... and nothing reopens it
            observed.append(conn.in_transaction)
        conn.close()
        conn = module.connect(path, autocommit=True)
        observed.append(conn.execute("SELECT a FROM t ORDER BY a").fetchall())
        conn.close()
        return observed

    assert run(minidb) == run(sqlite3)


@needs_autocommit
def test_explicit_transactions_with_autocommit():
    def script(conn, note):
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.execute("BEGIN")
        note(conn.in_transaction)
        conn.execute("INSERT INTO t VALUES (1)")
        conn.execute("ROLLBACK")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO t VALUES (2)")
        conn.commit()
        note(conn.in_transaction)
        note(conn.execute("SELECT * FROM t").fetchall())
        conn.executescript("BEGIN; INSERT INTO t VALUES (3); INSERT INTO t VALUES (4); COMMIT;")
        note(conn.execute("SELECT * FROM t").fetchall())

    same_behaviour(script, autocommit=True)


# ---- statement cache ---------------------------------------------------------------


def test_statement_cache_parses_once(monkeypatch):
    import minidb.database as database_module

    calls = []
    original = database_module.parse_script
    monkeypatch.setattr(database_module, "parse_script", lambda sql: calls.append(sql) or original(sql))
    db = Database()
    db.execute("CREATE TABLE t (a INTEGER)")
    for i in range(100):
        db.execute("INSERT INTO t VALUES (?)", (i,))
    assert db.execute("SELECT count(*), sum(a) FROM t") == [(100, 4950)]
    assert calls.count("INSERT INTO t VALUES (?)") == 1


def test_statement_cache_is_bounded():
    db = Database()
    for i in range(1000):
        db.execute(f"SELECT {i}")
    assert len(db._statements) <= 256


def test_cached_statement_survives_schema_changes():
    db = Database()
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (?)", (1,))
    db.execute("DROP TABLE t")
    db.execute("CREATE TABLE t (b TEXT, a INTEGER)")
    db.execute("INSERT INTO t (a) VALUES (?)", (2,))
    with pytest.raises(minidb.OperationalError):
        db.execute("INSERT INTO t VALUES (?)", (3,))
    assert db.execute("SELECT * FROM t") == [(None, 2)]


def test_returning_matches_sqlite3():
    def script(conn, note):
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE)")
        cursor = conn.execute("INSERT INTO t (v) VALUES ('a'), ('b') RETURNING id, v || '!' AS bang")
        note(cursor_state(cursor))
        note(cursor.fetchall())
        note(cursor_state(cursor))
        cursor = conn.execute("INSERT INTO t VALUES (1, 'z') ON CONFLICT (id) DO UPDATE SET v = excluded.v RETURNING *")
        note(cursor.fetchall())
        note(cursor_state(cursor))
        cursor = conn.execute("INSERT OR IGNORE INTO t VALUES (5, 'z') RETURNING id")
        note(cursor.fetchall())
        note(cursor_state(cursor))
        cursor = conn.execute("UPDATE t SET v = v || v WHERE id = 2 RETURNING v")
        note(cursor.fetchone())
        note(cursor_state(cursor))
        cursor = conn.execute("DELETE FROM t WHERE id = 1 RETURNING *")
        note(cursor.fetchall())
        note(cursor_state(cursor))
        note(conn.total_changes)

    same_behaviour(script)


def test_returning_rowcount_appears_after_the_last_row():
    def script(conn, note):
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE)")
        cursor = conn.execute("INSERT INTO t (v) VALUES ('a'), ('b'), ('c') RETURNING id, v || '!' AS bang")
        note(cursor_state(cursor))
        for _ in range(4):
            note(cursor.fetchone())
            note(cursor_state(cursor))
        cursor = conn.execute("UPDATE t SET v = v || v WHERE id < 3 RETURNING v")
        note(cursor.rowcount)
        note(cursor.fetchmany(1))
        note(cursor.rowcount)
        note(list(cursor))
        note(cursor.rowcount)
        cursor = conn.execute("DELETE FROM t WHERE id = 99 RETURNING id")
        note(cursor_state(cursor))
        cursor = conn.execute("DELETE FROM t RETURNING id")
        note(cursor.rowcount)
        cursor.close()
        note(conn.total_changes)
        note(conn.execute("SELECT count(*) FROM t").fetchall())

    same_behaviour(script)


def test_blobs_match_sqlite3():
    def script(conn, note):
        conn.execute("CREATE TABLE t (a BLOB, b)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", [
            (b"\x00\x01", bytearray(b"ab")), (memoryview(b"xyz"), b""), (b"\xff", "text"),
        ])
        note(conn.execute("SELECT a, b, typeof(a), typeof(b), length(a) FROM t").fetchall())
        note(conn.execute("SELECT count(*) FROM t WHERE a = ?", (b"xyz",)).fetchall())
        note(conn.execute("SELECT CAST(a AS TEXT) FROM t WHERE a = x'0001'").fetchall())
        conn.execute("SELECT CAST(a AS TEXT) FROM t WHERE a = x'ff'").fetchall()  # not UTF-8

    same_behaviour(script)
