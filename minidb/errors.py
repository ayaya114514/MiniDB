"""Exception hierarchy shared by all MiniDB modules (the PEP 249 one)."""


class Warning(Exception):  # noqa: A001 - name required by PEP 249
    """Important warnings (not used by MiniDB itself; required by PEP 249)."""


class Error(Exception):
    """Base class of every error MiniDB reports to the user."""


class InterfaceError(Error):
    """Misuse of the database interface rather than of the database."""


class DatabaseError(Error):
    """An error related to the database; also raised for a file that is
    missing, corrupt or not a MiniDB database."""


class DataError(DatabaseError):
    """A problem with the processed data (unused; required by PEP 249)."""


class OperationalError(DatabaseError):
    """A statement refers to something that does not exist or cannot be done
    (including SQL syntax errors, as in sqlite3)."""


class IntegrityError(DatabaseError):
    """A constraint (PRIMARY KEY, UNIQUE, NOT NULL, type) was violated."""


class InternalError(DatabaseError):
    """MiniDB reached an inconsistent internal state."""


class ProgrammingError(DatabaseError):
    """Wrong use of the API: bad parameter bindings, a closed connection..."""


class NotSupportedError(DatabaseError):
    """A feature MiniDB does not support."""
