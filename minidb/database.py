"""The public entry point: a connection to one database file."""

import dataclasses
from collections import OrderedDict
from collections.abc import Mapping

from minidb.catalog import Catalog
from minidb.errors import DatabaseError, OperationalError, ProgrammingError
from minidb.executor import Executor, Result
from minidb.pager import Pager
from minidb.parser import Begin, Bound, Commit, Parameter, Rollback, parse_script
from minidb.values import INT_MAX, INT_MIN

STATEMENT_CACHE_SIZE = 256


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

    def __init__(self, path=None):
        self.pager = Pager(path)
        try:
            self.catalog = Catalog(self.pager)
            self.pager.commit()
        except BaseException:
            self.pager.file.close()
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
        if self.broken:
            raise DatabaseError("a commit failed: reopen the database to recover")
        if isinstance(stmt, Begin):
            if self.in_transaction:
                raise OperationalError("cannot start a transaction within a transaction")
            self.in_transaction = True
            return Result()
        if isinstance(stmt, Commit):
            if not self.in_transaction:
                raise OperationalError("cannot commit - no transaction is active")
            self.in_transaction = False
            self._commit()
            return Result()
        if isinstance(stmt, Rollback):
            if not self.in_transaction:
                raise OperationalError("cannot rollback - no transaction is active")
            self.in_transaction = False
            self.rollback()
            return Result()
        self.pager.begin_statement()
        try:
            result = self.executor.execute(stmt)
        except BaseException:
            self.pager.rollback_statement()
            self.catalog.load()
            raise
        self.pager.end_statement()
        if not self.in_transaction:
            self._commit()
        if result.rowcount > 0:
            self.total_changes += result.rowcount
        return result

    def _commit(self):
        try:
            self.pager.commit()
        except BaseException:
            # The commit may or may not have reached the WAL's commit record,
            # so the in-memory state cannot be trusted.  Abandon it; reopening
            # the file lets recovery decide.
            self.broken = True
            self.pager.file.close()
            raise
        self.pager.shrink_cache()

    def rollback(self):
        """Discard all uncommitted changes."""
        self.pager.rollback()
        self.catalog.load()

    def integrity_check(self):
        """Check every B+ tree and index; returns a list of problems (empty if OK)."""
        problems = []
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
        self.pager.close()

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
