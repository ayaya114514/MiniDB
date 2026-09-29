"""Schema management.

The schema lives in the database file itself, in a B+ tree rooted at page 1
(like SQLite's ``sqlite_schema``).  Each entry is a record

    (type, name, table name, root page, sql)

where ``type`` is "table", "index" or "view" and ``sql`` is the CREATE
statement (canonical for tables and indexes, as written for views); on open
the statements are parsed again to rebuild the in-memory ``TableInfo``,
``IndexInfo`` and ``ViewInfo`` objects.  Tables, views and indexes share one
namespace.

Every UNIQUE column and every PRIMARY KEY that is not an INTEGER PRIMARY KEY
gets an automatic unique index named ``minidb_autoindex_<table>_<n>``.

``ANALYZE`` stores planner statistics as entries of type "stat" (named after
the table or index, sql = the numbers as text): a table's row count, and for
an index the average number of rows per distinct value of each prefix of its
columns (like SQLite's sqlite_stat1).
"""

from __future__ import annotations

from collections.abc import Sequence

from minidb import values
from minidb.btree import BTree
from minidb.errors import DatabaseError, OperationalError
from minidb.pager import Pager
from minidb.parser import ColumnDef, CreateIndex, CreateTable, CreateView, Literal, Unary, parse
from minidb.record import decode_record, encode_record, encoded_size
from minidb.values import SQLValue, ascii_lower

SCHEMA_ROOT = 1
RESERVED_PREFIX = "minidb_"
AUTO_INDEX_PREFIX = "minidb_autoindex_"


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---- index keys ------------------------------------------------------------------

# An index key: the sort keys of the indexed values, then (1, rowid).
IndexKey = tuple

# Sentinels that sort below / above every (rank, value) pair of an index key.
LOW = (-1,)
HIGH = (4,)


def index_key(key_values: Sequence[SQLValue], rowid: int) -> IndexKey:
    """The B+ tree key for an index entry: the sort keys of the indexed values
    followed by the row id, so that every key is unique."""
    return tuple(values.sort_key(v) for v in key_values) + ((1, rowid),)


class IndexKeyCodec:
    """Serializes index keys (tuples of sort-key pairs) as records."""

    _plain = staticmethod(values.plain_value)

    @classmethod
    def encode(cls, key: IndexKey) -> bytes:
        return encode_record([cls._plain(pair) for pair in key])

    @staticmethod
    def decode(data: bytes, pos: int) -> tuple[IndexKey, int]:
        row, end = decode_record(data, pos)
        return tuple(values.sort_key(v) for v in row), end

    @classmethod
    def size(cls, key: IndexKey) -> int:
        return encoded_size([cls._plain(pair) for pair in key])


# ---- schema objects ----------------------------------------------------------------


class TableInfo:
    has_rowid = True

    def __init__(self, name: str, columns: list[ColumnDef], root: int, schema_key: int | None = None) -> None:
        self.name = name
        self.columns = columns
        self.root = root
        self.schema_key = schema_key
        self.indexes = []  # newest first, the order SQLite checks UNIQUE constraints in
        self.stat_rows = None  # row count from ANALYZE
        self.stat_key = None
        self.positions = {ascii_lower(column.name): i for i, column in enumerate(columns)}
        # An INTEGER PRIMARY KEY column is an alias for the row id (as in SQLite).
        self.rowid_column = next(
            (i for i, c in enumerate(columns) if c.primary_key and c.type == "INTEGER"), None
        )  # only the type name INTEGER itself: "INT PRIMARY KEY" is an ordinary column
        self.affinities = [values.type_affinity(c.type) for c in columns]
        # Values of columns missing from a record (added by ALTER TABLE ADD
        # COLUMN after the row was written): their constant defaults.
        self.padding = [values.apply_affinity(constant_default(c.default), a)
                        for c, a in zip(columns, self.affinities)]

    def column_index(self, name: str) -> int | None:
        return self.positions.get(ascii_lower(name))

    def auto_index_columns(self) -> list[str]:
        """Columns that need an automatic unique index, in column order."""
        return [
            c.name for i, c in enumerate(self.columns)
            if (c.unique or c.primary_key) and i != self.rowid_column
        ]

    def sql(self) -> str:
        parts = []
        for column in self.columns:
            text = f"{quote(column.name)} {column.type}".rstrip()
            if column.default_text is not None:
                text += f" DEFAULT {column.default_text}"
            if column.primary_key:
                text += " PRIMARY KEY"
            if column.not_null:
                text += " NOT NULL"
            if column.unique:
                text += " UNIQUE"
            parts.append(text)
        return f"CREATE TABLE {quote(self.name)} ({', '.join(parts)})"


def constant_default(expr: object) -> SQLValue:
    """The value of a constant DEFAULT (a literal, possibly signed), or None."""
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, Unary) and expr.op in ("-", "+") and isinstance(expr.operand, (Literal, Unary)):
        value = constant_default(expr.operand)
        return values.subtract(0, value) if expr.op == "-" else value
    return None


def is_constant_default(expr: object) -> bool:
    return expr is None or isinstance(expr, Literal) or (
        isinstance(expr, Unary) and expr.op in ("-", "+") and is_constant_default(expr.operand))


class IndexInfo:
    def __init__(self, name: str, table: TableInfo, column_names: list[str], unique: bool, root: int, schema_key: int | None = None) -> None:
        self.name = name
        self.table = table
        self.column_names = [table.columns[table.column_index(c)].name for c in column_names]
        self.positions = [table.column_index(c) for c in column_names]
        self.unique = unique
        self.root = root
        self.schema_key = schema_key
        self.stat_average = None  # rows per distinct prefix value, from ANALYZE
        self.stat_key = None

    @property
    def is_auto(self) -> bool:
        return ascii_lower(self.name).startswith(AUTO_INDEX_PREFIX)

    def key(self, row: Sequence[SQLValue], rowid: int) -> IndexKey:
        return index_key([row[p] for p in self.positions], rowid)

    def sql(self) -> str:
        columns = ", ".join(quote(c) for c in self.column_names)
        unique = "UNIQUE " if self.unique else ""
        return f"CREATE {unique}INDEX {quote(self.name)} ON {quote(self.table.name)} ({columns})"


class ViewInfo:
    """A view: a stored SELECT, expanded where the view is used."""

    def __init__(self, stmt: CreateView, schema_key: int | None = None) -> None:
        self.name = stmt.name
        self.columns = stmt.columns  # declared column names, or None
        self.query = stmt.query
        self.sql = stmt.sql
        self.schema_key = schema_key


class Catalog:
    def __init__(self, pager: Pager) -> None:
        self.pager = pager
        self.version = 0  # bumped by every schema change; prepared plans check it
        self.temp_views = {}  # CREATE TEMP VIEW: this connection only, never stored
        if pager.page_count == 1:
            tree = BTree.create(pager)
            if tree.root != SCHEMA_ROOT:
                raise DatabaseError("could not create the schema table")
        self.schema = BTree(pager, SCHEMA_ROOT)
        self.load()

    def load(self) -> None:
        """(Re)build the in-memory schema from the schema table."""
        self.version += 1
        self.tables = {}
        self.indexes = {}
        self.views = {}
        entries = [decode_record(value)[0] + [key] for key, value in self.schema.scan()]
        for kind, name, _table_name, root, sql, key in entries:
            if kind == "table":
                stmt = parse(sql)
                self.tables[ascii_lower(name)] = TableInfo(name, stmt.columns, root, key)
            elif kind == "view":
                self.views[ascii_lower(name)] = ViewInfo(parse(sql), key)
        for kind, name, table_name, root, sql, key in entries:
            if kind == "index":
                stmt = parse(sql)
                table = self.tables[ascii_lower(table_name)]
                index = IndexInfo(name, table, stmt.columns, stmt.unique, root, key)
                self.indexes[ascii_lower(name)] = index
                table.indexes.insert(0, index)
        for kind, name, _table_name, _root, sql, key in entries:
            if kind == "stat":
                numbers = [float(n) for n in sql.split()]
                if ascii_lower(name) in self.indexes:
                    index = self.indexes[ascii_lower(name)]
                    index.stat_average, index.stat_key = numbers[1:], key
                elif ascii_lower(name) in self.tables:
                    table = self.tables[ascii_lower(name)]
                    table.stat_rows, table.stat_key = int(numbers[0]), key

    # ---- lookups ----------------------------------------------------------

    def get_table(self, name: str) -> TableInfo:
        table = self.tables.get(ascii_lower(name))
        if table is None:
            raise OperationalError(f"no such table: {name}")
        return table

    def has_table(self, name: str) -> bool:
        return ascii_lower(name) in self.tables

    def find_view(self, name: str) -> ViewInfo | None:
        """The view called ``name``: temporary views come first, as SQLite
        searches the temp schema before main."""
        lowered = ascii_lower(name)
        return self.temp_views.get(lowered) or self.views.get(lowered)

    def table_to_modify(self, name: str) -> TableInfo:
        """The table an INSERT, UPDATE or DELETE changes (not a view)."""
        if self.find_view(name) is not None:
            raise OperationalError(f"cannot modify {name} because it is a view")
        return self.get_table(name)

    def check_index_hint(self, table: TableInfo, index_name: str | None) -> None:
        """INDEXED BY must name an index of the table."""
        if index_name is not None:
            index = self.indexes.get(ascii_lower(index_name))
            if index is None or index.table is not table:
                raise OperationalError(f"no such index: {index_name}")

    def table_tree(self, table: TableInfo) -> BTree:
        return BTree(self.pager, table.root)

    def index_tree(self, index: IndexInfo) -> BTree:
        return BTree(self.pager, index.root, IndexKeyCodec)

    # ---- changes ------------------------------------------------------------

    def _add_entry(self, kind: str, name: str, table_name: str, root: int, sql: str) -> int:
        key = (self.schema.last_key() or 0) + 1
        self.schema.insert(key, encode_record([kind, name, table_name, root, sql]))
        return key

    def _check_new_name(self, name: str) -> None:
        lowered = ascii_lower(name)
        if lowered.startswith(RESERVED_PREFIX):
            raise OperationalError(f"object name reserved for internal use: {name}")
        if lowered in self.indexes:
            raise OperationalError(f"there is already an index named {name}")

    def _exists(self, name: str, if_not_exists: bool) -> bool:
        """Whether a table or view called ``name`` exists (an error unless
        IF NOT EXISTS was given)."""
        lowered = ascii_lower(name)
        if lowered not in self.tables and lowered not in self.views:
            return False
        if if_not_exists:
            return True
        kind = "table" if lowered in self.tables else "view"
        raise OperationalError(f"{kind} {name} already exists")

    def create_table(self, stmt: CreateTable) -> TableInfo | None:
        if self._exists(stmt.name, stmt.if_not_exists):
            return None
        self._check_new_name(stmt.name)
        seen = set()
        for column in stmt.columns:
            if ascii_lower(column.name) in seen:
                raise OperationalError(f"duplicate column name: {column.name}")
            seen.add(ascii_lower(column.name))
        if sum(column.primary_key for column in stmt.columns) > 1:
            raise OperationalError(f'table "{stmt.name}" has more than one primary key')
        self.version += 1
        root = BTree.create(self.pager).root
        table = TableInfo(stmt.name, stmt.columns, root)
        table.schema_key = self._add_entry("table", table.name, table.name, root, table.sql())
        self.tables[ascii_lower(stmt.name)] = table
        for n, column in enumerate(table.auto_index_columns(), 1):
            name = f"{AUTO_INDEX_PREFIX}{table.name}_{n}"
            self._create_index(name, table, [column], unique=True)
        return table

    def create_view(self, stmt: CreateView) -> None:
        """Store a view.  Like SQLite, the SELECT is not checked until the
        view is used (it may name tables that do not exist yet)."""
        if stmt.temp:
            if ascii_lower(stmt.name) in self.temp_views:
                if stmt.if_not_exists:
                    return
                raise OperationalError(f"view {stmt.name} already exists")
            self.version += 1
            self.temp_views[ascii_lower(stmt.name)] = ViewInfo(stmt)
            return
        if self._exists(stmt.name, stmt.if_not_exists):
            return
        self._check_new_name(stmt.name)
        self.version += 1
        view = ViewInfo(stmt)
        view.schema_key = self._add_entry("view", stmt.name, stmt.name, 0, stmt.sql)
        self.views[ascii_lower(stmt.name)] = view

    def drop_view(self, name: str, if_exists: bool = False) -> None:
        if ascii_lower(name) in self.temp_views:
            self.version += 1
            del self.temp_views[ascii_lower(name)]
            return
        if ascii_lower(name) in self.tables:
            raise OperationalError(f"use DROP TABLE to delete table {name}")
        view = self.views.get(ascii_lower(name))
        if view is None:
            if if_exists:
                return
            raise OperationalError(f"no such view: {name}")
        self.version += 1
        self.schema.delete(view.schema_key)
        del self.views[ascii_lower(name)]

    def drop_table(self, name: str, if_exists: bool = False) -> None:
        if self.find_view(name) is not None:
            raise OperationalError(f"use DROP VIEW to delete view {name}")
        if not self.has_table(name):
            if if_exists:
                return
            raise OperationalError(f"no such table: {name}")
        self.version += 1
        table = self.tables[ascii_lower(name)]
        for index in list(table.indexes):
            self._drop_index(index)
        if table.stat_key is not None:
            self.schema.delete(table.stat_key)
        del self.tables[ascii_lower(name)]
        self.table_tree(table).destroy()
        self.schema.delete(table.schema_key)

    def create_index(self, stmt: CreateIndex) -> IndexInfo | None:
        """Create an index; returns it (still empty) or None if it already exists."""
        lowered = ascii_lower(stmt.name)
        if lowered in self.indexes:
            if stmt.if_not_exists:
                return None
            raise OperationalError(f"index {stmt.name} already exists")
        if lowered in self.tables or lowered in self.views:
            raise OperationalError(f"there is already a table named {stmt.name}")
        if lowered.startswith(RESERVED_PREFIX):
            raise OperationalError(f"object name reserved for internal use: {stmt.name}")
        table = self.tables.get(ascii_lower(stmt.table))
        if table is None:
            if ascii_lower(stmt.table) in self.views:
                raise OperationalError("views may not be indexed")
            raise OperationalError(f"no such table: main.{stmt.table}")
        for column in stmt.columns:
            if table.column_index(column) is None:
                raise OperationalError(f"no such column: {column}")
        return self._create_index(stmt.name, table, stmt.columns, stmt.unique)

    def _create_index(self, name: str, table: TableInfo, columns: list[str], unique: bool) -> IndexInfo:
        self.version += 1
        root = BTree.create(self.pager, IndexKeyCodec).root
        index = IndexInfo(name, table, columns, unique, root)
        index.schema_key = self._add_entry("index", name, table.name, root, index.sql())
        self.indexes[ascii_lower(name)] = index
        table.indexes.insert(0, index)
        return index

    def drop_index(self, name: str, if_exists: bool = False) -> None:
        index = self.indexes.get(ascii_lower(name))
        if index is None:
            if if_exists:
                return
            raise OperationalError(f"no such index: {name}")
        if index.is_auto:
            raise OperationalError(
                "index associated with UNIQUE or PRIMARY KEY constraint cannot be dropped"
            )
        self._drop_index(index)

    def _drop_index(self, index: IndexInfo) -> None:
        self.version += 1
        self.index_tree(index).destroy()
        self.schema.delete(index.schema_key)
        if index.stat_key is not None:
            self.schema.delete(index.stat_key)
        del self.indexes[ascii_lower(index.name)]
        index.table.indexes.remove(index)

    # ---- ALTER TABLE ------------------------------------------------------------

    def rewrite_table_entries(self, table: TableInfo) -> None:
        """Store a table's (changed) definition, its indexes' and their
        statistics again, under their old keys; then reload the schema."""
        self.version += 1
        self.schema.insert(table.schema_key, encode_record(
            ["table", table.name, table.name, table.root, table.sql()]), replace=True)
        for index in table.indexes:
            self.schema.insert(index.schema_key, encode_record(
                ["index", index.name, table.name, index.root, index.sql()]), replace=True)
            if index.stat_key is not None:
                text = " ".join(f"{n:g}" if isinstance(n, float) else str(n)
                                for n in [table.stat_rows or 0] + index.stat_average)
                self.schema.insert(index.stat_key, encode_record(
                    ["stat", index.name, table.name, 0, text]), replace=True)
        if table.stat_key is not None:
            self.schema.insert(table.stat_key, encode_record(
                ["stat", table.name, table.name, 0, str(table.stat_rows)]), replace=True)

    def rewrite_view(self, view: ViewInfo, sql: str) -> None:
        self.version += 1
        if view.schema_key is None:  # a temporary view
            self.temp_views[ascii_lower(view.name)] = ViewInfo(parse(sql))
            return
        self.schema.insert(view.schema_key, encode_record(["view", view.name, view.name, 0, sql]), replace=True)

    # ---- statistics ---------------------------------------------------------

    def analyze(self, name: str | None = None) -> None:
        """Gather statistics for one table (or the table of an index) or all."""
        if name is None:
            tables = list(self.tables.values())
        elif ascii_lower(name) in self.tables:
            tables = [self.tables[ascii_lower(name)]]
        elif ascii_lower(name) in self.indexes:
            tables = [self.indexes[ascii_lower(name)].table]
        else:
            raise OperationalError(f"no such table or index: {name}")
        self.version += 1
        for table in tables:
            rows = len(self.table_tree(table))
            self._set_stat(table, table.name, [rows])
            table.stat_rows = rows
            for index in table.indexes:
                distinct = [0] * len(index.positions)
                previous = None
                for key in self.index_tree(index).keys():
                    values = key[:-1]  # without the row id
                    for depth in range(len(values)):
                        if previous is None or previous[:depth + 1] != values[:depth + 1]:
                            distinct[depth] += 1
                    previous = values
                average = [rows / d if d else 1.0 for d in distinct]
                self._set_stat(index, index.name, [rows] + average)
                index.stat_average = average

    def _set_stat(self, owner: TableInfo | IndexInfo, name: str, numbers: list[int | float]) -> None:
        if owner.stat_key is not None:
            self.schema.delete(owner.stat_key)
        text = " ".join(f"{n:g}" if isinstance(n, float) else str(n) for n in numbers)
        table_name = owner.name if isinstance(owner, TableInfo) else owner.table.name
        owner.stat_key = self._add_entry("stat", name, table_name, 0, text)
