"""Exception hierarchy shared by all MiniDB modules."""


class Error(Exception):
    """Base class of every error MiniDB reports to the user."""


class DatabaseError(Error):
    """The database file is missing, corrupt or not a MiniDB database."""


class OperationalError(Error):
    """A statement refers to something that does not exist or cannot be done."""


class IntegrityError(Error):
    """A constraint (PRIMARY KEY, UNIQUE, NOT NULL, type) was violated."""
