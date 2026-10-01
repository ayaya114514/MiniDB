"""The public entry point: a connection to one database file."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from minidb.catalog import Catalog
from minidb.errors import DatabaseError, OperationalError, ProgrammingError
from minidb.executor import Executor, Result
from minidb.locking import LockTimeout
from minidb.pager import Pager
from minidb.record import decode_record
from minidb.sqlite_format import MAGIC as SQLITE_MAGIC
from minidb.sqlite_pager import SqlitePager
from minidb.parser import (
    Analyze, Begin, Commit, CreateIndex, CreateTable, CreateView, Delete, DropIndex, DropTable,
    AlterTable, DropView, Insert, Pragma, Reindex, Rollback, Update, Vacuum, parse_script,
)
from minidb import pragmas
from minidb.values import INT_MAX, INT_MIN, SQLValue

# Values for ?-parameters: by position, or by name.
Parameters = Sequence[object] | Mapping[str, object]

STATEMENT_CACHE_SIZE = 256
SPILL_PAGES = 1000  # dirty pages a transaction may hold before they go to the log
CACHE_PAGES_AFTER_SPILL = 2000
WRITE_STATEMENTS = (
    Insert, Update, Delete, CreateTable, DropTable, CreateIndex, DropIndex, CreateView, DropView,
    Analyze, Reindex, AlterTable,
)


def file_format(path: str) -> str | None:
    """The format of an existing database file: "sqlite", "minidb", or None
    for a missing or empty file."""
    try:
        with open(path, "rb") as f:
            start = f.read(16)
    except FileNotFoundError:
        return None
    if not start:
        return None
    return "sqlite" if start == SQLITE_MAGIC else "minidb"


def open_pager(path: str | None, timeout: float, format: str | None) -> Pager | SqlitePager:
    if format not in (None, "minidb", "sqlite"):
        raise ProgrammingError(f'unknown database format "{format}" (use "minidb" or "sqlite")')
    found = file_format(path) if path is not None else None
    if found is not None and format is not None and found != format:
        raise OperationalError(f"{path} is a database in {found} format, not {format}")
    if (found or format) == "sqlite":
        return SqlitePager(path, timeout)
    return Pager(path, timeout)


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

    def __init__(self, path: str | None = None, timeout: float = 5.0, format: str | None = None) -> None:
        """``timeout``: seconds to wait for a lock held by another connection.

        ``format``: "minidb" (the default for new databases) or "sqlite"
        (SQLite's own file format, see ``minidb.sqlite_pager``).  An existing
        file is opened in the format its header says."""
        self.pager = open_pager(path, timeout, format)  # starts inside a read transaction
        try:
            if self.pager.is_new:
                # Creating the file: become the writer first (RESERVED before the snapshot).
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
        self.executor.integrity_problems = self._integrity_check
        self.executor.in_transaction = lambda: self.in_transaction
        self.in_transaction = False
        self.broken = False
        self.total_changes = 0
        self._statements = OrderedDict()  # SQL text -> parsed statements

    @property
    def last_insert_rowid(self) -> int:
        return self.executor.last_insert_rowid

    def parse(self, sql: str) -> list[Any]:
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

    def execute(self, sql: str, parameters: Parameters | None = None) -> Result:
        result = Result()
        for result in self.execute_each(sql, parameters):
            pass
        return result

    def execute_each(self, sql: str, parameters: Parameters | None = None) -> Iterator[Result]:
        """Run the statements in ``sql`` one by one, yielding each result."""
        statements = self.parse(sql)
        if parameters is not None and len(statements) > 1:
            raise ProgrammingError("You can only execute one statement at a time.")
        for stmt in statements:
            values = ()
            if stmt.param_count or parameters is not None:
                values = resolve_parameters(stmt, parameters)
            yield self.execute_statement(stmt, values)

    def execute_statement(self, stmt: Any, parameters: Sequence[SQLValue] = ()) -> Result:
        """Run one parsed statement.

        Locking: a statement or explicit transaction reads a snapshot (see
        ``Pager.begin_read``).  A writing statement outside a transaction
        takes RESERVED first (waiting for another writer), then its snapshot,
        which is therefore the newest.  Inside a transaction RESERVED is not
        waited for: once that writer commits, our snapshot is outdated and
        could not write anyway, so we fail with "database is locked" at
        once, as SQLite does.
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
        writes = isinstance(stmt, WRITE_STATEMENTS) or (
            isinstance(stmt, Pragma) and pragmas.is_write(stmt.name, stmt.value))
        if isinstance(stmt, Vacuum):
            if self.in_transaction:
                raise OperationalError("cannot VACUUM from within a transaction")
            writes = stmt.schema == "main" and stmt.into is None
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
            result = self.executor.execute(stmt, parameters)
        except BaseException as exc:
            # A constraint violation under INSERT/UPDATE OR FAIL keeps the
            # statement's earlier changes; OR ROLLBACK ends the transaction.
            # Inside a transaction, SQLite undoes a statement that failed for
            # another reason only if it compiled it with a statement journal
            # (Executor.statement_journal); otherwise the changes stay.
            resolution = getattr(exc, "resolution", None)
            if isinstance(stmt, (Insert, Update, Delete)):
                self.executor.changes = 0
            if resolution == "FAIL":
                self.total_changes += exc.changes  # SQLite counts them only for FAIL
                self.executor.changes = exc.changes
                self.executor.total_changes = self.total_changes
            elif resolution is None and self.in_transaction and not self.executor.statement_journal:
                resolution = "FAIL"
            if resolution == "FAIL":
                self._end_statement()
                raise
            pager.rollback_statement()
            self.catalog.load()
            if resolution == "ROLLBACK" and self.in_transaction:
                self.in_transaction = False
                self.rollback()
                pager.end_transaction()
            elif not self.in_transaction:
                pager.end_transaction()
            raise
        self._end_statement()
        if isinstance(stmt, Vacuum) and writes and not self.broken:
            pager.checkpoint()  # shrinks the file, unless another connection is reading
        if result.rowcount > 0:
            self.total_changes += result.rowcount
        if isinstance(stmt, (Insert, Update, Delete)):
            self.executor.changes = max(result.rowcount, 0)
            self.executor.total_changes = self.total_changes
        return result

    def _end_statement(self) -> None:
        """Keep a statement's changes; outside a transaction, commit them."""
        pager = self.pager
        pager.end_statement()
        if self.in_transaction and len(pager.dirty) > SPILL_PAGES:
            # Keep big transactions out of memory: move their pages to the log.
            pager.spill()
            pager.shrink_cache(CACHE_PAGES_AFTER_SPILL)
        if not self.in_transaction:
            try:
                self._commit()
            except LockTimeout:
                self.rollback()
                raise
            finally:
                if not self.broken:
                    pager.end_transaction()

    def _begin_read(self) -> None:
        if self.pager.begin_read():
            self.catalog.load()  # another connection committed: the schema may differ
            self.executor.data_version += 1

    def _commit(self) -> None:
        try:
            self.pager.commit()
            if self.pager.committed >= self.pager.checkpoint_frames:
                self.pager.checkpoint()  # keeps the log short; skipped while others read
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

    def rollback(self) -> None:
        """Discard all uncommitted changes."""
        self.pager.rollback()
        self.catalog.load()

    def integrity_check(self) -> list[str]:
        """Check page checksums, every B+ tree and every index; returns a list
        of problems (empty if all is well)."""
        if self.in_transaction:
            return self._integrity_check()
        self._begin_read()
        try:
            return self._integrity_check()
        finally:
            self.pager.end_transaction()

    def _integrity_check(self) -> list[str]:
        problems = [f"page {pgno}: bad checksum" for pgno in self.pager.check_checksums()]
        if problems:
            return problems
        if self.catalog.sqlite:
            roots = [1] + [decode_record(value)[0][3] for _, value in self.catalog.schema.scan()]
            problems = self.pager.check_pages([root for root in roots if isinstance(root, int) and root > 0])
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

    def close(self) -> None:
        """Close the database; an open transaction is rolled back."""
        if self.broken or self.pager.closed:
            return
        if self.in_transaction:
            self.in_transaction = False
            self.rollback()
            self.pager.end_transaction()
        try:
            self.pager.checkpoint()  # only if nobody else is using the database
        finally:
            self.pager.close_files()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()



# ---- parameters ----------------------------------------------------------------


def adapt(value: object, position: int) -> SQLValue:
    """Check a bound Python value and convert it to a SQL value."""
    if value is None or isinstance(value, (float, str, bytes)):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if not INT_MIN <= value <= INT_MAX:
            raise OverflowError("Python int too large to convert to SQLite INTEGER")
        return value
    raise ProgrammingError(
        f"Error binding parameter {position}: type '{type(value).__name__}' is not supported"
    )


def resolve_parameters(stmt: Any, parameters: Parameters | None) -> list[SQLValue]:
    """The statement's parameter values as a list indexed by parameter number
    - 1 (sqlite3's rules and messages)."""
    count = stmt.param_count
    if parameters is None:
        parameters = ()
    values = [None] * count
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
            values[index - 1] = adapt(parameters[name[1:]], index)
        return values
    parameters = list(parameters)
    if len(parameters) != count:
        raise ProgrammingError(
            "Incorrect number of bindings supplied. The current statement uses "
            f"{count}, and there are {len(parameters)} supplied."
        )
    for index, value in enumerate(parameters, 1):
        name = stmt.param_names.get(index)
        if name is not None:  # Python 3.14's rule (3.12 and 3.13 only warn)
            raise ProgrammingError(
                f"Binding {index} ('{name}') is a named parameter, but you supplied a "
                "sequence which requires nameless (qmark) placeholders."
            )
        values[index - 1] = adapt(value, index)
    return values
