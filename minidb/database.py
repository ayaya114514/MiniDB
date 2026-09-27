"""The public entry point: a connection to one database file."""

import dataclasses
from collections import OrderedDict
from collections.abc import Mapping

from minidb.catalog import Catalog
from minidb.errors import DatabaseError, OperationalError, ProgrammingError
from minidb.executor import Executor, Result
from minidb.locking import LockTimeout
from minidb.pager import Pager
from minidb.parser import (
    Begin, Bound, Commit, CreateIndex, CreateTable, Delete, DropIndex, DropTable, Insert,
    Parameter, Rollback, Update, parse_script,
)
from minidb.values import INT_MAX, INT_MIN

STATEMENT_CACHE_SIZE = 256
WRITE_STATEMENTS = (Insert, Update, Delete, CreateTable, DropTable, CreateIndex, DropIndex)


class Database:
    """A MiniDB database stored in the file ``path`` (``None`` keeps it in memory).

    ``execute`` runs one or more SQL statements and returns the result of the
    last one.  Every statement is atomic: if it fails, none of its changes
    remain.  Outside an explicit transaction (BEGIN ... COMMIT/ROLLBACK) every
    statement commits at once.

    Values for ``?``, ``?NNN``, ``:name``, ``@name`` and ``$name`` placeholders
    are passed as ``parameters``: a sequence (by position) or a mapping (by
    name, without the prefix character).  Parsed statements are cached by SQL
    text, so executing the same SQL again skips tokenizing and parsing.
    """

    def __init__(self, path=None, timeout=5.0):
        """``timeout``: seconds to wait for a lock held by another connection."""
        self.pager = Pager(path, timeout)  # starts inside a read transaction
        try:
            if self.pager.is_new:
                # Creating the file: become the writer first (RESERVED before SHARED).
                self.pager.end_transaction()
                self.pager.begin_write()
                self.pager.begin_read()
            self.catalog = Catalog(self.pager)
            self.pager.commit()
            self.pager.end_transaction()
        except BaseException:
            self.pager.close_files()
            raise
        self.executor = Executor(self.catalog)
        self.in_transaction = False
        self.broken = False
        self.total_changes = 0
        self._statements = OrderedDict()  # SQL text -> parsed statements

    @property
    def last_insert_rowid(self):
        return self.executor.last_insert_rowid

    def parse(self, sql):
        """Parse ``sql`` into statements, using the statement cache."""
        statements = self._statements.get(sql)
        if statements is None:
            statements = parse_script(sql)
            self._statements[sql] = statements
            if len(self._statements) > STATEMENT_CACHE_SIZE:
                self._statements.popitem(last=False)
        else:
            self._statements.move_to_end(sql)
        return statements

    def execute(self, sql, parameters=None):
        result = Result()
        for result in self.execute_each(sql, parameters):
            pass
        return result

    def execute_each(self, sql, parameters=None):
        """Run the statements in ``sql`` one by one, yielding each result."""
        statements = self.parse(sql)
        if parameters is not None and len(statements) > 1:
            raise ProgrammingError("You can only execute one statement at a time.")
        for stmt in statements:
            if stmt.param_count or parameters is not None:
                stmt = bind(stmt, resolve_parameters(stmt, parameters))
            yield self.execute_statement(stmt)

    def execute_statement(self, stmt):
        """Run one parsed statement.

        Locking: a statement or explicit transaction reads under SHARED.  A
        writing statement outside a transaction takes RESERVED first (waiting
        for another writer), then SHARED.  Inside a transaction, which already
        holds SHARED, RESERVED is not waited for: that writer may itself be
        waiting for our SHARED to go away, so we fail with "database is
        locked" at once, as SQLite does.
        """
        if self.broken:
            raise DatabaseError("a commit failed: reopen the database to recover")
        pager = self.pager
        if isinstance(stmt, Begin):
            if self.in_transaction:
                raise OperationalError("cannot start a transaction within a transaction")
            try:
                if stmt.mode != "DEFERRED":
                    pager.begin_write()
                self._begin_read()
            except BaseException:
                pager.end_transaction()
                raise
            self.in_transaction = True
            return Result()
        if isinstance(stmt, Commit):
            if not self.in_transaction:
                raise OperationalError("cannot commit - no transaction is active")
            self._commit()  # a lock timeout leaves the transaction open
            self.in_transaction = False
            pager.end_transaction()
            return Result()
        if isinstance(stmt, Rollback):
            if not self.in_transaction:
                raise OperationalError("cannot rollback - no transaction is active")
            self.in_transaction = False
            self.rollback()
            pager.end_transaction()
            return Result()
        writes = isinstance(stmt, WRITE_STATEMENTS)
        if not self.in_transaction:
            try:
                if writes:
                    pager.begin_write()
                self._begin_read()
            except BaseException:
                pager.end_transaction()
                raise
        elif writes:
            pager.begin_write(wait=False)
        pager.begin_statement()
        try:
            result = self.executor.execute(stmt)
        except BaseException:
            pager.rollback_statement()
            self.catalog.load()
            if not self.in_transaction:
                pager.end_transaction()
            raise
        pager.end_statement()
        if not self.in_transaction:
            try:
                self._commit()
            except LockTimeout:
                self.rollback()
                raise
            finally:
                if not self.broken:
                    pager.end_transaction()
        if result.rowcount > 0:
            self.total_changes += result.rowcount
        return result

    def _begin_read(self):
        if self.pager.begin_read():
            self.catalog.load()  # another connection committed: the schema may differ

    def _commit(self):
        try:
            self.pager.commit()
        except LockTimeout:
            raise  # nothing was written; the transaction is intact
        except BaseException:
            # The commit may or may not have reached the WAL's commit record,
            # so the in-memory state cannot be trusted.  Abandon it; reopening
            # the file lets recovery decide.
            self.broken = True
            self.pager.close_files()
            raise
        self.pager.shrink_cache()

    def rollback(self):
        """Discard all uncommitted changes."""
        self.pager.rollback()
        self.catalog.load()

    def integrity_check(self):
        """Check page checksums, every B+ tree and every index; returns a list
        of problems (empty if all is well)."""
        if self.in_transaction:
            return self._integrity_check()
        self._begin_read()
        try:
            return self._integrity_check()
        finally:
            self.pager.end_transaction()

    def _integrity_check(self):
        problems = [f"page {pgno}: bad checksum" for pgno in self.pager.check_checksums()]
        if problems:
            return problems
        catalog = self.catalog
        trees = [("schema", catalog.schema)]
        for table in catalog.tables.values():
            trees.append((f"table {table.name}", catalog.table_tree(table)))
            for index in table.indexes:
                trees.append((f"index {index.name}", catalog.index_tree(index)))
        for name, tree in trees:
            try:
                tree.check()
            except AssertionError as exc:
                problems.append(f"{name}: {exc}")
        if problems:
            return problems
        for table in catalog.tables.values():
            rows = [
                (rowid, self.executor.load_row(table, rowid, record))
                for rowid, record in catalog.table_tree(table).scan()
            ]
            for index in table.indexes:
                expected = sorted(index.key(row, rowid) for rowid, row in rows)
                if catalog.index_tree(index).keys() != expected:
                    problems.append(f"index {index.name} does not match table {table.name}")
        return problems

    def close(self):
        """Close the database; an open transaction is rolled back."""
        if self.broken or self.pager.file.closed:
            return
        if self.in_transaction:
            self.in_transaction = False
            self.rollback()
        self.pager.close_files()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()



# ---- parameters ----------------------------------------------------------------


def adapt(value, position):
    """Check a bound Python value and convert it to a SQL value."""
    if value is None or isinstance(value, (float, str)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise OverflowError("Python int too large to convert to SQLite INTEGER")
        return value
    raise ProgrammingError(
        f"Error binding parameter {position}: type '{type(value).__name__}' is not supported"
    )


def resolve_parameters(stmt, parameters):
    """Map the statement's parameter indexes to values (sqlite3's rules and messages)."""
    count = stmt.param_count
    if parameters is None:
        parameters = ()
    values = {}
    if isinstance(parameters, Mapping):
        for index in range(1, count + 1):
            name = stmt.param_names.get(index)
            if name is None:
                raise ProgrammingError(
                    f"Binding {index} has no name, but you supplied a dictionary "
                    "(which has only names)."
                )
            if name[1:] not in parameters:
                raise ProgrammingError(f"You did not supply a value for binding parameter {name}.")
            values[index] = adapt(parameters[name[1:]], index)
        return values
    parameters = list(parameters)
    if len(parameters) != count:
        raise ProgrammingError(
            "Incorrect number of bindings supplied. The current statement uses "
            f"{count}, and there are {len(parameters)} supplied."
        )
    for index, value in enumerate(parameters, 1):
        values[index] = adapt(value, index)
    return values


def bind(node, values):
    """A copy of the syntax tree ``node`` with every Parameter replaced by its value."""
    if isinstance(node, Parameter):
        return Bound(values[node.index])
    if isinstance(node, (list, tuple)):
        items = [bind(item, values) for item in node]
        if all(new is old for new, old in zip(items, node)):
            return node
        return type(node)(items)
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        changes = {}
        for field in dataclasses.fields(node):
            old = getattr(node, field.name)
            new = bind(old, values)
            if new is not old:
                changes[field.name] = new
        return dataclasses.replace(node, **changes) if changes else node
    return node
