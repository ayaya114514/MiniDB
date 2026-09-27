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

import os

from minidb.database import Database
from minidb.errors import (  # noqa: F401 - re-exported as PEP 249 requires
    DatabaseError, DataError, Error, IntegrityError, InterfaceError, InternalError,
    NotSupportedError, OperationalError, ProgrammingError, Warning,
)
from minidb.parser import Delete, Insert, Update

apilevel = "2.0"
threadsafety = 1  # threads may share the module but not connections
paramstyle = "qmark"  # "?"; ":name", "@name", "$name" and "?NNN" work too


def connect(database=":memory:", autocommit=False):
    """Open a connection; ``database`` is a file path or ":memory:"."""
    return Connection(database, autocommit)


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

    def __init__(self, database=":memory:", autocommit=False):
        path = None if database == ":memory:" else os.fspath(database)
        self._db = Database(path)
        self.autocommit = autocommit
        if not autocommit:
            self._db.execute("BEGIN")

    @property
    def database(self):
        """The underlying ``minidb.database.Database``."""
        if self._db is None:
            raise ProgrammingError("Cannot operate on a closed database.")
        return self._db

    @property
    def in_transaction(self):
        return self.database.in_transaction

    @property
    def total_changes(self):
        return self.database.total_changes

    def cursor(self):
        self.database  # raises if closed
        return Cursor(self)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql):
        return self.cursor().executescript(sql)

    def commit(self):
        self._end_transaction("COMMIT")

    def rollback(self):
        self._end_transaction("ROLLBACK")

    def _end_transaction(self, sql):
        db = self.database
        if self.autocommit:
            return
        if db.in_transaction:
            db.execute(sql)
        db.execute("BEGIN")

    def close(self):
        """Close the connection; an uncommitted transaction is rolled back."""
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        """Commit if the block succeeded, roll back if it raised (like sqlite3)."""
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.arraysize = 1
        self.description = None
        self.rowcount = -1
        self.lastrowid = None
        self._rows = []
        self._position = 0
        self._closed = False

    def _database(self):
        if self._closed:
            raise ProgrammingError("Cannot operate on a closed cursor.")
        return self.connection.database

    def _single_statement(self, sql):
        statements = self._database().parse(sql)
        if len(statements) > 1:
            raise ProgrammingError("You can only execute one statement at a time.")
        return statements[0] if statements else None

    def _reset(self):
        self.description = None
        self.rowcount = -1
        self._rows = []
        self._position = 0

    def execute(self, sql, parameters=()):
        statement = self._single_statement(sql)
        self._reset()
        if statement is None:
            return self
        db = self._database()
        result = db.execute(sql, parameters)
        self._rows = list(result)
        if result.columns:
            self.description = tuple((name, None, None, None, None, None, None) for name in result.columns)
        self.rowcount = result.rowcount
        self.lastrowid = db.last_insert_rowid
        return self

    def executemany(self, sql, seq_of_parameters):
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

    def executescript(self, sql):
        """Run several statements (no implicit transaction handling)."""
        self._reset()
        self._database().execute(sql)
        return self

    # ---- fetching -------------------------------------------------------

    def fetchone(self):
        self._database()
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchmany(self, size=None):
        self._database()
        size = self.arraysize if size is None else size
        rows = self._rows[self._position:self._position + size]
        self._position += len(rows)
        return rows

    def fetchall(self):
        self._database()
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self):
        self._closed = True
        self._rows = []

    def setinputsizes(self, sizes):
        pass

    def setoutputsize(self, size, column=None):
        pass
