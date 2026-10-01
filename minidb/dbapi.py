"""A PEP 249 (DB-API 2.0) interface, modelled on Python's sqlite3 module.

    import minidb

    with minidb.connect("app.db") as conn:        # commits on success
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "x"), (2, "y")])
        for row in conn.execute("SELECT * FROM t WHERE a > :min", {"min": 1}):
            print(row)

Transactions follow Python 3.12's sqlite3 ``autocommit`` attribute:

* ``autocommit=False`` (the default, as PEP 249 asks): a transaction is opened
  when connecting and again after every ``commit()`` / ``rollback()``;
  closing the connection rolls back uncommitted work.
* ``autocommit=True``: every statement commits by itself unless the SQL uses
  BEGIN ... COMMIT; ``commit()`` and ``rollback()`` do nothing.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from minidb.database import Database, Parameters
from minidb.errors import (  # noqa: F401 - re-exported as PEP 249 requires
    DatabaseError, DataError, Error, IntegrityError, InterfaceError, InternalError,
    NotSupportedError, OperationalError, ProgrammingError, Warning,
)
from minidb.parser import Delete, Insert, Update

apilevel = "2.0"
threadsafety = 1  # threads may share the module but not connections
paramstyle = "qmark"  # "?"; ":name", "@name", "$name" and "?NNN" work too


def connect(database: str | os.PathLike = ":memory:", autocommit: bool = False, format: str | None = None) -> Connection:
    """Open a connection; ``database`` is a file path or ":memory:".
    ``format``: see ``Database``."""
    return Connection(database, autocommit, format)


class Connection:
    Warning = Warning
    Error = Error
    InterfaceError = InterfaceError
    DatabaseError = DatabaseError
    DataError = DataError
    OperationalError = OperationalError
    IntegrityError = IntegrityError
    InternalError = InternalError
    ProgrammingError = ProgrammingError
    NotSupportedError = NotSupportedError

    def __init__(self, database: str | os.PathLike = ":memory:", autocommit: bool = False, format: str | None = None) -> None:
        path = None if database == ":memory:" else os.fspath(database)
        self._db = Database(path, format=format)
        self.autocommit = autocommit
        if not autocommit:
            self._db.execute("BEGIN")

    @property
    def database(self) -> Database:
        """The underlying ``minidb.database.Database``."""
        if self._db is None:
            raise ProgrammingError("Cannot operate on a closed database.")
        return self._db

    @property
    def in_transaction(self) -> bool:
        return self.database.in_transaction

    @property
    def total_changes(self) -> int:
        return self.database.total_changes

    def cursor(self) -> Cursor:
        self.database  # raises if closed
        return Cursor(self)

    def execute(self, sql: str, parameters: Parameters = ()) -> Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Iterable[Parameters]) -> Cursor:
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql: str) -> Cursor:
        return self.cursor().executescript(sql)

    def commit(self) -> None:
        self._end_transaction("COMMIT")

    def rollback(self) -> None:
        self._end_transaction("ROLLBACK")

    def _end_transaction(self, sql: str) -> None:
        db = self.database
        if self.autocommit:
            return
        if db.in_transaction:
            db.execute(sql)
        db.execute("BEGIN")

    def close(self) -> None:
        """Close the connection; an uncommitted transaction is rolled back."""
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, traceback: object) -> bool:
        """Commit if the block succeeded, roll back if it raised (like sqlite3)."""
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def check_utf8(rows: list[tuple], columns: list[str]) -> None:
    """Fail like sqlite3 on text that is not valid UTF-8 (made from a BLOB):
    inside MiniDB such bytes are kept as lone surrogates."""
    for row in rows:
        for name, value in zip(columns, row):
            if type(value) is str and not value.isascii():
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError:
                    shown = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
                    raise OperationalError(
                        f"Could not decode to UTF-8 column '{name}' with text '{shown}'"
                    ) from None


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.arraysize = 1
        self.description = None
        self.rowcount = -1
        self.lastrowid = None
        self._rows = []
        self._position = 0
        self._pending_rowcount = None  # see execute()
        self._closed = False

    def _database(self) -> Database:
        if self._closed:
            raise ProgrammingError("Cannot operate on a closed cursor.")
        return self.connection.database

    def _single_statement(self, sql: str) -> Any:
        statements = self._database().parse(sql)
        if len(statements) > 1:
            raise ProgrammingError("You can only execute one statement at a time.")
        return statements[0] if statements else None

    def _reset(self) -> None:
        self.description = None
        self.rowcount = -1
        self._rows = []
        self._position = 0
        self._pending_rowcount = None

    def execute(self, sql: str, parameters: Parameters = ()) -> Cursor:
        statement = self._single_statement(sql)
        self._reset()
        if statement is None:
            return self
        db = self._database()
        result = db.execute(sql, parameters)
        self._rows = list(result)
        check_utf8(self._rows, result.columns)
        if result.columns:
            self.description = tuple((name, None, None, None, None, None, None) for name in result.columns)
        self.rowcount = result.rowcount
        if result.columns and self._rows and isinstance(statement, (Insert, Update, Delete)):
            # RETURNING: sqlite3 reports the count only once the last row has
            # been fetched (when the statement has run to completion).
            self.rowcount, self._pending_rowcount = 0, result.rowcount
        self.lastrowid = db.last_insert_rowid
        return self

    def executemany(self, sql: str, seq_of_parameters: Iterable[Parameters]) -> Cursor:
        statement = self._single_statement(sql)
        self._reset()
        if statement is None:
            return self
        if not isinstance(statement, (Insert, Update, Delete)):
            raise ProgrammingError("executemany() can only execute DML statements.")
        db = self._database()
        total = 0
        for parameters in seq_of_parameters:
            total += db.execute(sql, parameters).rowcount
        self.rowcount = total  # lastrowid is left alone, as in sqlite3
        return self

    def executescript(self, sql: str) -> Cursor:
        """Run several statements (no implicit transaction handling)."""
        self._reset()
        self._database().execute(sql)
        return self

    # ---- fetching -------------------------------------------------------

    def fetchone(self) -> tuple | None:
        self._database()
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._advance(1)
        return row

    def fetchmany(self, size: int | None = None) -> list[tuple]:
        self._database()
        size = self.arraysize if size is None else size
        rows = self._rows[self._position:self._position + size]
        self._advance(len(rows))
        return rows

    def fetchall(self) -> list[tuple]:
        self._database()
        rows = self._rows[self._position:]
        self._advance(len(rows))
        return rows

    def _advance(self, count: int) -> None:
        self._position += count
        if self._pending_rowcount is not None and self._position >= len(self._rows):
            self.rowcount, self._pending_rowcount = self._pending_rowcount, None

    def __iter__(self) -> Cursor:
        return self

    def __next__(self) -> tuple:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self) -> None:
        self._closed = True
        self._rows = []

    def setinputsizes(self, sizes: object) -> None:
        pass

    def setoutputsize(self, size: object, column: object = None) -> None:
        pass
