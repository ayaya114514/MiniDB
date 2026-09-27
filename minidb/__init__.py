"""MiniDB: a small SQLite-like relational database written from scratch.

The DB-API 2.0 interface is available at the package level::

    import minidb
    conn = minidb.connect("app.db")
"""

from minidb.dbapi import (  # noqa: F401
    Connection, Cursor, DatabaseError, DataError, Error, IntegrityError, InterfaceError,
    InternalError, NotSupportedError, OperationalError, ProgrammingError, Warning, apilevel,
    connect, paramstyle, threadsafety,
)
from minidb.database import Database  # noqa: F401
