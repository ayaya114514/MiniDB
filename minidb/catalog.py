"""Schema management.

The schema lives in the database file itself, in a B+ tree rooted at page 1
(like SQLite's ``sqlite_schema``).  Each entry is a record

    (type, name, table name, root page, sql)

where ``type`` is "table", "index", "view" or "trigger" and ``sql`` is the
CREATE statement (as written; a trigger's as SQLite keeps it); on open the
statements are parsed again to rebuild the in-memory ``TableInfo``,
``IndexInfo``, ``ViewInfo`` and ``TriggerInfo`` objects.  Tables, views and
indexes share one namespace, triggers have their own.

Every UNIQUE column and every PRIMARY KEY that is not an INTEGER PRIMARY KEY
gets an automatic unique index named ``minidb_autoindex_<table>_<n>``.

In a database in SQLite's format (``minidb.sqlite_pager``) the schema table
is SQLite's ``sqlite_schema``: automatic indexes are named
``sqlite_autoindex_<table>_<n>`` with no SQL (SQLite numbers them in the
order the constraints appear, which for MiniDB's column constraints is
column order), DESC index columns are kept, ``sqlite_`` names are reserved,
and the schema cookie changes with every schema change.  Objects whose SQL
MiniDB cannot parse (written by SQLite) are kept as they are: using them
raises ``NotSupportedError``, and a table with such an index or trigger
cannot be changed (MiniDB could not keep the index up to date).

``ANALYZE`` stores planner statistics as entries of type "stat" (named after
the table or index, sql = the numbers as text): a table's row count, and for
an index the average number of rows per distinct value of each prefix of its
columns (like SQLite's sqlite_stat1).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

from minidb import values
from minidb.btree import BTree
from minidb.errors import DatabaseError, NotSupportedError, OperationalError
from minidb.pager import Pager
from minidb.parser import (
    GENERATED_KEY_ERROR, CheckConstraint, ColumnDef, CreateIndex, CreateTable, CreateTrigger, CreateView, ForeignKey,
    KeyConstraint, Literal, Unary, parse,
)
from minidb.parser import Column as ColumnRef
from minidb.record import decode_record, encode_record, encoded_size
from minidb.values import SQLValue, ascii_lower

SCHEMA_ROOT = 1
RESERVED_PREFIX = "minidb_"
AUTO_INDEX_PREFIX = "minidb_autoindex_"
SQLITE_RESERVED_PREFIX = "sqlite_"
SQLITE_AUTO_INDEX_PREFIX = "sqlite_autoindex_"
# The schema table can be read (only) as sqlite_schema or sqlite_master, as in SQLite;
# the temp database's also as sqlite_temp_schema or sqlite_temp_master.
SCHEMA_TABLE_NAMES = ("sqlite_schema", "sqlite_master")
TEMP_SCHEMA_TABLE_NAMES = ("sqlite_temp_schema", "sqlite_temp_master")
SCHEMA_TABLE_SQL = "CREATE TABLE sqlite_master (type text, name text, tbl_name text, rootpage int, sql text)"


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


class CollatedKeyCodec(IndexKeyCodec):
    """IndexKeyCodec for an index with NOCASE or RTRIM columns: their keys
    are collation keys (values.Collated), the records hold the values."""

    def __init__(self, key_functions: list) -> None:
        self.key_functions = key_functions + [values.sort_key]  # (the last: the row id)

    def decode(self, data: bytes, pos: int) -> tuple[IndexKey, int]:
        row, end = decode_record(data, pos)
        functions = self.key_functions
        return tuple(functions[i](v) if i < len(functions) else values.sort_key(v)
                     for i, v in enumerate(row)), end


# ---- schema objects ----------------------------------------------------------------


class AutoIndex:
    """The index a PRIMARY KEY or UNIQUE constraint needs (SQLite's
    ``sqlite_autoindex_<table>_<n>``)."""

    def __init__(self, positions: list[int], collations: list[str], descending: list[bool],
                 conflict: str | None, primary: bool, written: list[str | None]) -> None:
        self.positions = positions
        self.collations = collations
        self.written = written  # the COLLATE names as the constraint wrote them
        self.descending = descending
        self.conflict = conflict
        self.origin = "pk" if primary else "u"


class TableInfo:
    has_rowid = True
    temp = False  # in the connection's temp database (CREATE TEMP TABLE)

    def __init__(self, name: str, columns: list[ColumnDef], root: int, schema_key: int | None = None,
                 constraints: Sequence[object] = (), sql: str | None = None, without_rowid: bool = False) -> None:
        self.name = name
        # WITHOUT ROWID: the rows are kept in the PRIMARY KEY's order, keyed
        # by it (the "row id" of such a row is the tuple of its PRIMARY KEY
        # sort keys); the table's tree is that of its PRIMARY KEY index.
        self.without_rowid = without_rowid
        self.has_rowid = not without_rowid
        self.pk_index = None  # (WITHOUT ROWID: the IndexInfo of the PRIMARY KEY, see Catalog)
        self.columns = columns
        self.root = root
        self.schema_key = schema_key
        self.sql = sql  # the CREATE TABLE statement the schema stores
        self.indexes = []  # newest first, the order SQLite checks UNIQUE constraints in
        self.stat_rows = None  # row count from ANALYZE
        self.stat_key = None
        self.positions = {ascii_lower(column.name): i for i, column in enumerate(columns)}
        # The constraints in the order SQLite meets them: each column's, then the table's.
        everything = [c for column in columns for c in column.constraints] + list(constraints)
        self.table_constraints = list(constraints)
        self.keys = [c for c in everything if isinstance(c, KeyConstraint)]
        self.checks = [c for c in everything if isinstance(c, CheckConstraint)]
        self.foreign_keys = [c for c in everything if isinstance(c, ForeignKey)]
        self.primary_key = next((k for k in self.keys if k.primary), None)
        # An INTEGER PRIMARY KEY column is an alias for the row id (as in
        # SQLite): only for the type name INTEGER itself ("INT PRIMARY KEY"
        # is an ordinary column), and not for a column's PRIMARY KEY DESC.
        self.rowid_column = None
        key = self.primary_key
        # (as SQLite's sqlite3AddPrimaryKey: an INTEGER PRIMARY KEY, which a
        # WITHOUT ROWID table makes an ordinary column, its index last)
        self.integer_key = None
        if key is not None and len(key.columns) == 1 and not (key.column_level and key.columns[0].descending):
            position = self.column_index(key.columns[0].name)
            if position is not None and columns[position].type == "INTEGER":
                self.integer_key = position
        if not without_rowid:
            self.rowid_column = self.integer_key
        elif key is not None:
            for column in key.columns:  # (the PRIMARY KEY of a WITHOUT ROWID table is NOT NULL)
                position = self.column_index(column.name)
                if position is not None and not columns[position].not_null:
                    columns[position].not_null = True
        self.autoincrement = self.rowid_column is not None and key.autoincrement
        self.compiled_checks = None  # (the executor's, see Executor.check_violation)
        self.collations = [values.collation_name(c.collation) if c.collation else "BINARY" for c in columns]
        self.affinities = [values.type_affinity(c.type) for c in columns]
        self.read_only = None  # why the table cannot be changed (SQLite files), or None
        self.is_schema = False  # sqlite_schema / sqlite_master
        # Values of columns missing from a record (added by ALTER TABLE ADD
        # COLUMN after the row was written): their constant defaults.
        self.padding = [values.apply_affinity(constant_default(c.default), a)
                        for c, a in zip(columns, self.affinities)]
        # Generated columns: a record holds the others and the STORED ones,
        # in column order (SQLite's sqlite3TableColumnToStorage); a VIRTUAL
        # column is computed when the row is read (Executor.load_row).
        self.generated = [i for i, c in enumerate(columns) if c.generated is not None]
        self.virtual = [i for i in self.generated if not columns[i].stored]
        self.storage = [i for i, c in enumerate(columns) if c.generated is None or c.stored] if self.virtual else None
        self.fill_virtual = None  # (the executor's, see executor.expand_virtual)
        self.fill_virtual_except = {}  # (likewise, with some VIRTUAL columns given)
        self.fill_generated = None  # (the executor's, see Executor.generate)

    def column_index(self, name: str) -> int | None:
        return self.positions.get(ascii_lower(name))

    def rowid_conflict(self) -> str | None:
        """The ON CONFLICT clause of an INTEGER PRIMARY KEY."""
        return self.primary_key.conflict if self.rowid_column is not None else None

    def auto_indexes(self, check: bool = False) -> list[AutoIndex]:
        """The indexes the PRIMARY KEY and UNIQUE constraints need, in the
        order SQLite numbers them: a constraint whose columns and collations
        an earlier one has gets none (a PRIMARY KEY then takes over the
        earlier index, and an ON CONFLICT clause goes to it).  ``check``:
        raise on conflicting ON CONFLICT clauses and unknown columns."""
        found = []
        keys = self.keys
        if self.without_rowid and self.integer_key is not None:
            # (SQLite makes the index of such a key last: convertToWithoutRowidTable)
            keys = [k for k in keys if k is not self.primary_key] + [self.primary_key]
        for key in keys:
            if key is self.primary_key and self.rowid_column is not None:
                continue
            positions = []
            for column in key.columns:
                position = self.column_index(column.name)
                if position is None:
                    if not check:
                        return found  # (cannot happen in a schema SQLite wrote)
                    raise OperationalError(f"no such column: {column.name}")
                positions.append(position)
            # (convertToWithoutRowidTable makes the index of an INTEGER
            # PRIMARY KEY from the column's name: a COLLATE there is lost.)
            written = [None] if key is self.primary_key and self.without_rowid and self.integer_key is not None \
                else [c.collation for c in key.columns]
            collations = [values.collation_name(c) if c else self.collations[p]
                          for c, p in zip(written, positions)]
            for earlier in found:
                if earlier.positions == positions and earlier.collations == collations:
                    if earlier.conflict != key.conflict and earlier.conflict is not None \
                            and key.conflict is not None and check:
                        raise OperationalError("conflicting ON CONFLICT clauses specified")
                    if earlier.conflict is None:
                        earlier.conflict = key.conflict
                    if key.primary:
                        earlier.origin = "pk"
                    break
            else:
                found.append(AutoIndex(positions, collations, [c.descending for c in key.columns],
                                       key.conflict, key.primary, written))
        return found

    def validate(self) -> None:
        """The checks of CREATE TABLE (a schema SQLite wrote passes them)."""
        seen = set()
        for column in self.columns:
            if ascii_lower(column.name) in seen:
                raise OperationalError(f"duplicate column name: {column.name}")
            seen.add(ascii_lower(column.name))
        if self.primary_key is not None and any(
                self.column_index(c.name) in self.generated for c in self.primary_key.columns):
            raise OperationalError(GENERATED_KEY_ERROR)
        if sum(k.primary for k in self.keys) > 1:
            raise OperationalError(f'table "{self.name}" has more than one primary key')
        if any(k.autoincrement for k in self.keys) and (self.integer_key is None or not self.primary_key.autoincrement):
            raise OperationalError("AUTOINCREMENT is only allowed on an INTEGER PRIMARY KEY")
        if self.without_rowid:
            if any(k.autoincrement for k in self.keys):
                raise OperationalError("AUTOINCREMENT not allowed on WITHOUT ROWID tables")
            if self.primary_key is None:
                raise OperationalError(f"PRIMARY KEY missing on table {self.name}")
        self.auto_indexes(check=True)
        for key in self.foreign_keys:
            for name in key.columns:
                if self.column_index(name) is None:
                    raise OperationalError(f'unknown column "{name}" in foreign key definition')
            if key.parent_columns and len(key.parent_columns) != len(key.columns):
                if len(key.columns) == 1 and any(key is c for c in self.columns[self.column_index(key.columns[0])].constraints):
                    raise OperationalError(f"foreign key on {key.columns[0]} should reference only one "
                                           f"column of table {key.parent}")
                raise OperationalError("number of columns in foreign key does not match the number of "
                                       "columns in the referenced table")

    def canonical_sql(self) -> str:
        """A CREATE TABLE statement for the columns alone (MiniDB's REPL
        before tables kept their SQL)."""
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
    temp = False

    def __init__(self, name: str, table: TableInfo, column_names: list[str], unique: bool, root: int,
                 schema_key: int | None = None, auto: bool = False, descending: list[bool] | None = None,
                 collations: list[str | None] | None = None, conflict: str | None = None,
                 origin: str = "c", sql: str | None = None, declared: list[bool] | None = None,
                 table_pk: bool = False) -> None:
        self.name = name
        # The PRIMARY KEY of a WITHOUT ROWID table: its tree is the table's.
        self.table_pk = table_pk
        self.auto = auto
        # DESC columns (kept only in SQLite-format files): the index is then
        # maintained in that order but not used for lookups or ordering.
        self.descending = descending if descending and any(descending) else None
        self.table = table
        self.column_names = [table.columns[table.column_index(c)].name for c in column_names]
        self.positions = [table.column_index(c) for c in column_names]
        # Each column's collation: its own COLLATE, else the table column's.
        written = collations or [None] * len(self.positions)
        self.collations = [values.collation_name(c) if c else table.collations[p]
                           for c, p in zip(written, self.positions)]
        # As PRAGMA index_xinfo shows them: the COLLATE names as written (in
        # the index, else on the column) and the DESC flags as declared.
        self.collation_names = [c if c is not None else table.columns[p].collation or "BINARY"
                                for c, p in zip(written, self.positions)]
        self.declared_descending = list(declared or descending or [False] * len(self.positions))
        # Each column's sort key function (values.collation_sort_key).
        self.key_functions = [values.collation_sort_key(c) for c in self.collations]
        self.collated = any(c != "BINARY" for c in self.collations)
        # A key ends with the row id - or, for a WITHOUT ROWID table, with
        # the PRIMARY KEY columns the index does not have already (same
        # column, same collation), in PRIMARY KEY order: ``extra`` lists their
        # places in the table's key, ``pk_parts`` where each PRIMARY KEY
        # column is in this index's key (SQLite's sqlite3CreateIndex).
        self.extra, self.pk_parts = [], None
        key_functions = self.key_functions
        if table_pk:
            self.pk_parts = list(range(len(self.positions)))
        elif not table.has_rowid:
            pk = table.pk_index
            self.pk_parts = []
            for j, (position, collation) in enumerate(zip(pk.positions, pk.collations)):
                same = next((i for i, (p, c) in enumerate(zip(self.positions, self.collations))
                             if p == position and c == collation), None)
                if same is None:
                    self.extra.append(j)
                    same = len(self.positions) + len(self.extra) - 1
                self.pk_parts.append(same)
            key_functions = key_functions + [pk.key_functions[j] for j in self.extra]
            self.collated = self.collated or any(pk.collations[j] != "BINARY" for j in self.extra)
        self.codec = CollatedKeyCodec(key_functions) if self.collated else IndexKeyCodec
        # A generated column that is just another column of REAL affinity
        # holds that column's value as SQLite reads it when CREATE INDEX
        # fills the index from the table: a whole REAL as an integer (unless
        # the column is REAL itself).  Entries INSERT / UPDATE add keep the REAL.
        self.raw_reals = [i for i, p in enumerate(self.positions) if _real_copy(table, p)]
        self.unique = unique
        self.conflict = conflict  # ON CONFLICT of its PRIMARY KEY or UNIQUE constraint
        self.origin = origin  # "c" (CREATE INDEX), "u" (UNIQUE) or "pk", as PRAGMA index_list says
        self.sql = sql  # the CREATE INDEX statement the schema stores (None for automatic indexes)
        self.root = root
        self.schema_key = schema_key
        self.stat_average = None  # rows per distinct prefix value, from ANALYZE
        self.stat_key = None

    @property
    def is_auto(self) -> bool:
        return self.auto

    @property
    def ordered(self) -> bool:
        """Whether the index tree is in the order of the keys (no DESC)."""
        return self.descending is None

    def build_key(self, row: Sequence[SQLValue], rowid: int | tuple) -> IndexKey:
        """The key CREATE INDEX / REINDEX gives a row's entry (see raw_reals)."""
        if self.raw_reals:
            row = list(row)
            for i in self.raw_reals:
                value = row[self.positions[i]]
                if isinstance(value, float) and value.is_integer() and -2**63 <= value < 2**63:
                    row[self.positions[i]] = int(value)
        return self.key(row, rowid)

    def key(self, row: Sequence[SQLValue], rowid: int | tuple) -> IndexKey:
        if self.pk_parts is not None:  # (WITHOUT ROWID: ``rowid`` is the PRIMARY KEY's key)
            return self.prefix([row[p] for p in self.positions]) + tuple(rowid[j] for j in self.extra)
        if not self.collated:
            return index_key([row[p] for p in self.positions], rowid)
        return self.prefix([row[p] for p in self.positions]) + ((1, rowid),)

    def row_id(self, key: IndexKey) -> int | tuple:
        """The row id (WITHOUT ROWID: the PRIMARY KEY's key) of the row an
        entry of this index stands for."""
        if self.pk_parts is None:
            return key[-1][1]
        return tuple(key[i] for i in self.pk_parts)

    @property
    def entry_positions(self) -> list[int]:
        """The table columns an entry holds (WITHOUT ROWID: with the PRIMARY KEY's)."""
        if self.pk_parts is None:
            return self.positions
        return self.positions + [self.table.pk_index.positions[j] for j in self.extra]

    def prefix(self, key_values: Sequence[SQLValue]) -> IndexKey:
        """The start of the keys of entries with these values (in column order)."""
        return tuple(f(v) for f, v in zip(self.key_functions, key_values))


def _real_copy(table: TableInfo, position: int) -> bool:
    """Whether column ``position`` is VIRTUAL, generated as a copy of a REAL
    column (``AS (r)`` or ``AS (+r)``) without REAL affinity of its own."""
    expr = table.columns[position].generated
    while isinstance(expr, Unary) and expr.op == "+":
        expr = expr.operand
    if (not isinstance(expr, ColumnRef) or table.affinities[position] == values.REAL
            or table.columns[position].stored):
        return False
    source = table.column_index(expr.name)
    return source is not None and table.affinities[source] == values.REAL


def add_index(table: TableInfo, index: IndexInfo) -> None:
    """Add an index to the table's list, newest first - except that, as in
    SQLite, indexes whose constraint says ON CONFLICT REPLACE come last."""
    indexes = table.indexes
    if index.conflict != "REPLACE" or not indexes or indexes[0].conflict == "REPLACE":
        indexes.insert(0, index)
        return
    position = next((i for i, other in enumerate(indexes) if other.conflict == "REPLACE"), len(indexes))
    indexes.insert(position, index)


class ViewInfo:
    """A view: a stored SELECT, expanded where the view is used."""

    temp = False

    def __init__(self, stmt: CreateView, schema_key: int | None = None) -> None:
        self.name = stmt.name
        self.columns = stmt.columns  # declared column names, or None
        self.query = stmt.query
        self.sql = stmt.sql
        self.schema_key = schema_key


class TriggerInfo:
    """A trigger: when it fires and its program (compiled by minidb.triggers)."""

    temp = False  # a TEMP trigger (on a temp table, or on a main one)
    on_temp = False  # on a table or view of the temp database

    def __init__(self, stmt: CreateTrigger, schema_key: int | None = None) -> None:
        self.name = stmt.name
        self.table_name = stmt.table
        self.timing = stmt.timing  # BEFORE, AFTER or INSTEAD OF
        self.event = stmt.event  # INSERT, UPDATE or DELETE
        self.columns = None if stmt.columns is None else [ascii_lower(c) for c in stmt.columns]
        self.when = stmt.when
        self.body = stmt.body
        self.sql = stmt.sql
        self.stmt = stmt  # parsed from ``sql`` (positions in it, for ALTER TABLE)
        self.schema_key = schema_key
        self.programs = {}  # (catalog version, ...) -> compiled program (see minidb.triggers)


class Catalog:
    """The schema of the main database; and through it (``temp``) that of
    the connection's temp database, a second Catalog over an in-memory
    pager in the same format, created by the first temporary object.

    Lookups by name take a schema - "main", "temp", or None for both, temp
    first (as SQLite searches them) - and so do the statements that create
    and drop objects; called on the temp database's own Catalog they see
    only it."""

    def __init__(self, pager: Pager, parent: Catalog | None = None) -> None:
        self.pager = pager
        self.parent = parent  # for the temp database: the main database's catalog
        self.temp = None  # the temp database's catalog, once there is one
        self._version = 0
        self.sqlite = getattr(pager, "format", None) == "sqlite"
        self.schema_names = SCHEMA_TABLE_NAMES if parent is None else TEMP_SCHEMA_TABLE_NAMES
        if self.sqlite:
            from minidb.sqlite_btree import SqliteTable

            self.reserved_prefixes, self.auto_prefix = (SQLITE_RESERVED_PREFIX,), SQLITE_AUTO_INDEX_PREFIX
            self.schema = SqliteTable(pager, SCHEMA_ROOT, on_change=pager.note_schema_change)
        else:
            # (sqlite_ as well, as SQLite reserves it)
            self.reserved_prefixes, self.auto_prefix = (RESERVED_PREFIX, SQLITE_RESERVED_PREFIX), AUTO_INDEX_PREFIX
            if pager.page_count == 1:
                tree = BTree.create(pager)
                if tree.root != SCHEMA_ROOT:
                    raise DatabaseError("could not create the schema table")
            self.schema = _SchemaTree(pager, SCHEMA_ROOT)
        self.load()

    @property
    def version(self) -> int:
        """Bumped by every schema change (of either database); prepared plans check it."""
        return self._version if self.parent is None else self.parent.version

    @version.setter
    def version(self, value: int) -> None:
        if self.parent is None:
            self._version = value
        else:
            self.parent.version = value

    @property
    def is_temp(self) -> bool:
        return self.parent is not None

    def temp_catalog(self) -> Catalog:
        """The temp database's catalog, created (empty) when first needed."""
        if self.parent is not None:
            return self
        if self.temp is None:
            from minidb.sqlite_pager import SqlitePager

            pager = SqlitePager(None) if self.sqlite else Pager(None)
            self.temp = Catalog(pager, self)
            pager.commit()  # (its empty schema; a rollback goes back to this)
            if self.pager.journal is not None:
                pager.begin_statement()  # (created by a statement, which may yet fail)
        return self.temp

    def search(self, schema: str | None = None) -> list[Catalog]:
        """The databases to look a name up in, in order."""
        if self.parent is not None:
            return [self]
        if schema is None:
            return [self] if self.temp is None else [self.temp, self]
        if schema == "main":
            return [self]
        if schema == "temp":
            return [self.temp_catalog()]
        return []  # (an unknown schema: nothing is found)

    def owner(self, item: TableInfo | IndexInfo | ViewInfo | TriggerInfo) -> Catalog:
        """The catalog of the database an object belongs to."""
        return self.temp if item.temp and self.parent is None else self

    def all_tables(self) -> list[TableInfo]:
        """The tables of both databases (temp first)."""
        return [t for catalog in self.search() for t in catalog.tables.values()]

    def all_views(self) -> list[ViewInfo]:
        return [v for catalog in self.search() for v in catalog.views.values()]

    def all_triggers(self) -> list[TriggerInfo]:
        return [t for catalog in self.search() for t in catalog.triggers.values()]

    def all_indexes(self) -> list[IndexInfo]:
        return [i for catalog in self.search() for i in catalog.indexes.values()]

    @property
    def any_triggers(self) -> bool:
        return bool(self.triggers or self.temp is not None and self.temp.triggers)

    def _mark(self, item: TableInfo | IndexInfo | ViewInfo | TriggerInfo) -> None:
        if self.parent is not None:
            item.temp = True

    def load(self) -> None:
        """(Re)build the in-memory schema from the schema table (both
        databases', called on the main one)."""
        self.version += 1
        self.tables = {}
        self.indexes = {}
        self.views = {}
        self.triggers = {}  # lowered name -> TriggerInfo (a namespace of their own)
        self.unsupported = {}  # lowered name -> why (objects only SQLite understands)
        entries = [decode_record(value)[0][:5] + [key] for key, value in self.schema.scan()]
        for kind, name, _table_name, root, sql, key in entries:
            try:
                if kind == "table":
                    stmt = parse(sql)
                    self.tables[ascii_lower(name)] = TableInfo(name, stmt.columns, root, key, stmt.constraints, sql,
                                                               stmt.without_rowid)
                elif kind == "view":
                    self.views[ascii_lower(name)] = ViewInfo(parse(sql), key)
            except Exception as exc:  # (only SQLite files can hold such SQL)
                if not self.sqlite:
                    raise
                self.unsupported[ascii_lower(name)] = f"{kind} {name}: {exc}"
        # The PRIMARY KEY index of a WITHOUT ROWID table has no entry: it
        # takes its place among the table's automatic indexes (by number).
        pending = {table: self._make_pk_index(table) for table in self.tables.values() if not table.has_rowid}
        for kind, name, table_name, root, sql, key in entries:
            if kind not in ("index", "trigger"):
                continue
            table = self.tables.get(ascii_lower(table_name))
            if kind == "index" and table in pending:
                prefix = self.auto_prefix + table.name + "_"
                number = name[len(prefix):] if ascii_lower(name).startswith(ascii_lower(prefix)) else ""
                if not number.isdigit() or int(number) > pending[table]:
                    pending.pop(table)
                    self._add_pk_index(table)
            if table is None and kind == "trigger" and (ascii_lower(table_name) in self.views or self.is_temp):
                # (on a view; or a TEMP trigger on a table of the main database)
                try:
                    self.triggers[ascii_lower(name)] = TriggerInfo(parse(sql), key)
                except Exception as exc:
                    if not self.sqlite:
                        raise
                    self.unsupported[ascii_lower(name)] = f"{kind} {name}: {exc}"
                continue
            if table is None:
                continue  # belongs to an unsupported table
            try:
                if kind == "trigger":
                    self.triggers[ascii_lower(name)] = TriggerInfo(parse(sql), key)
                    continue
                if ascii_lower(name).startswith(self.auto_prefix):
                    # The automatic index of a UNIQUE / PRIMARY KEY (no SQL;
                    # MiniDB's files once stored some, for column constraints)
                    number = int(name[len(self.auto_prefix) + len(table.name) + 1:])
                    auto = table.auto_indexes()[number - 1]
                    columns = [table.columns[p].name for p in auto.positions]
                    descending = auto.descending if self.sqlite else None
                    index = IndexInfo(name, table, columns, True, root, key, True, descending,
                                      auto.written, auto.conflict, auto.origin, declared=auto.descending)
                else:
                    stmt = parse(sql)
                    for column in stmt.columns:
                        if table.column_index(column) is None:
                            raise OperationalError(f"no such column: {column}")
                    descending = stmt.descending if self.sqlite else None
                    index = IndexInfo(name, table, stmt.columns, stmt.unique, root, key, False, descending,
                                      stmt.collations, sql=sql, declared=stmt.descending)
            except Exception as exc:
                if not self.sqlite:
                    raise
                table.read_only = f"{kind} {name} is not supported ({exc})"
                self.unsupported[ascii_lower(name)] = f"{kind} {name}: {exc}"
                continue
            self.indexes[ascii_lower(name)] = index
            add_index(table, index)
        for table in pending:
            self._add_pk_index(table)
        if self.sqlite:
            self._load_sqlite_stats()
        for kind, name, _table_name, _root, sql, key in entries:
            if kind == "stat":
                numbers = [float(n) for n in sql.split()]
                if ascii_lower(name) in self.indexes:
                    index = self.indexes[ascii_lower(name)]
                    index.stat_average, index.stat_key = numbers[1:], key
                elif ascii_lower(name) in self.tables:
                    table = self.tables[ascii_lower(name)]
                    table.stat_rows, table.stat_key = int(numbers[0]), key
        if self.is_temp:
            for item in [*self.tables.values(), *self.indexes.values(), *self.views.values()]:
                item.temp = True
            for trigger in self.triggers.values():
                trigger.temp = True
                lowered = ascii_lower(trigger.table_name)
                trigger.on_temp = trigger.stmt.table_schema != "main" and (
                    lowered in self.tables or lowered in self.views)
        elif self.temp is not None:
            self.temp.load()

    # ---- lookups ----------------------------------------------------------

    def triggers_on(self, name: str) -> list[TriggerInfo]:
        """The triggers of the table or view the name finds (temp first), in
        the order SQLite fires them: TEMP triggers on a main table first
        (sqlite3TriggerList), each database's newest first."""
        lowered = ascii_lower(name)
        on_temp = self._temp_object(lowered)
        found = []
        for catalog in self.search():
            own = [t for t in catalog.triggers.values()
                   if t.on_temp == on_temp and ascii_lower(t.table_name) == lowered]
            own.sort(key=lambda t: t.schema_key, reverse=True)
            found += own
        return found

    def _temp_object(self, lowered: str) -> bool:
        """Whether the temp database has a table or view of this (lower-case) name."""
        temp = self if self.is_temp else self.temp
        return temp is not None and (lowered in temp.tables or lowered in temp.views)

    def _find_table(self, name: str, qualified: bool = False) -> TableInfo | None:
        """This database's table called ``name`` (its schema table too), or None."""
        lowered = ascii_lower(name)
        table = self.tables.get(lowered)
        if table is None and (lowered in self.schema_names or (qualified and lowered in SCHEMA_TABLE_NAMES)):
            table = TableInfo(lowered, parse(SCHEMA_TABLE_SQL).columns, SCHEMA_ROOT)
            table.is_schema = True
            table.read_only = f"table {self.schema_names[1]} may not be modified"
            self._mark(table)
        return table

    def get_table(self, name: str, schema: str | None = None) -> TableInfo:
        if self.temp is None and schema is None:
            table = self.tables.get(ascii_lower(name))  # (the usual case, quickly)
            if table is not None:
                return table
        if schema is None and self.parent is None and ascii_lower(name) in TEMP_SCHEMA_TABLE_NAMES:
            self.temp_catalog()  # (sqlite_temp_master is there, empty, before anything is)
        for catalog in self.search(schema):
            table = catalog._find_table(name, schema is not None)
            if table is not None:
                return table
        reason = self.unsupported.get(ascii_lower(name)) if schema in (None, "main") else None
        if reason is not None:
            raise NotSupportedError(f"MiniDB cannot use {reason}")
        raise OperationalError(f"no such table: {name}" if schema is None else f"no such table: {schema}.{name}")

    def has_table(self, name: str, schema: str | None = None) -> bool:
        return any(ascii_lower(name) in catalog.tables for catalog in self.search(schema))

    def find_view(self, name: str, schema: str | None = None) -> ViewInfo | None:
        """The view called ``name`` (temporary views first, as SQLite
        searches the temp schema before main) - unless a table of that name
        comes first."""
        lowered = ascii_lower(name)
        if self.temp is None and schema is None:
            return None if lowered in self.tables else self.views.get(lowered)
        for catalog in self.search(schema):
            if lowered in catalog.tables:
                return None
            if lowered in catalog.views:
                return catalog.views[lowered]
        return None

    def table_to_modify(self, name: str, schema: str | None = None) -> TableInfo:
        """The table an INSERT, UPDATE or DELETE changes (not a view)."""
        if self.find_view(name, schema) is not None:
            raise OperationalError(f"cannot modify {name} because it is a view")
        table = self.get_table(name, schema)
        self.check_writable(table)
        return table

    def check_writable(self, table: TableInfo, verb: str = "modified") -> None:
        if table.is_schema:
            raise OperationalError(f"table {'sqlite_temp_master' if table.temp else 'sqlite_master'} may not be {verb}")
        if table.read_only is not None:
            raise NotSupportedError(f"MiniDB cannot change table {table.name}: {table.read_only}")

    def check_index_hint(self, table: TableInfo, index_name: str | None) -> None:
        """INDEXED BY must name an index of the table."""
        if index_name is not None:
            index = self.owner(table).indexes.get(ascii_lower(index_name))
            if index is None or index.table is not table:
                raise OperationalError(f"no such index: {index_name}")

    def table_tree(self, table: TableInfo) -> BTree:
        if table.temp and self.parent is None:
            return self.temp.table_tree(table)
        if table.is_schema and not self.sqlite:
            return _SchemaRows(self)
        if not table.has_rowid:
            # Keyed by the PRIMARY KEY's sort keys: SQLite's format keeps the
            # rows as the entries of that index (WithoutRowidTable); MiniDB's
            # as the values of a tree keyed by them.
            if self.sqlite:
                from minidb.sqlite_btree import WithoutRowidTable

                return WithoutRowidTable(self.index_tree(table.pk_index), table)
            return BTree(self.pager, table.root, table.pk_index.codec)
        if self.sqlite:
            from minidb.sqlite_btree import SqliteTable

            affinities = table.affinities if table.storage is None else [table.affinities[p] for p in table.storage]
            return SqliteTable(self.pager, table.root, affinities, rows=True)
        return BTree(self.pager, table.root)

    def index_tree(self, index: IndexInfo) -> BTree:
        if index.table.temp and self.parent is None:
            return self.temp.index_tree(index)
        if self.sqlite:
            from minidb.sqlite_btree import SqliteIndex

            table = index.table
            positions = index.entry_positions
            descending = list(index.descending or [False] * len(index.positions))
            if index.extra and not index.auto:
                # CREATE INDEX keeps the order of the PRIMARY KEY's columns;
                # a UNIQUE constraint's index has them ascending (SQLite's
                # convertToWithoutRowidTable, its "bAscKeyBug").
                pk = table.pk_index
                descending += [bool(pk.descending and pk.descending[j]) for j in index.extra]
            if index.table_pk:  # (its entries are the rows: the other stored columns follow)
                stored = table.storage if table.storage is not None else range(len(table.columns))
                rest = [p for p in stored if p not in positions]
                positions, descending = positions + rest, descending + [False] * len(rest)
            functions = index.codec.key_functions[:-1] if index.collated else None
            return SqliteIndex(self.pager, index.root, descending if any(descending) else None,
                               [table.affinities[p] for p in positions], functions, table.has_rowid)
        if index.table_pk:
            return self.table_tree(index.table)
        return BTree(self.pager, index.root, index.codec)

    def _new_tree(self, index: bool) -> int:
        """Create an empty table (or index) tree; returns its root page."""
        if self.sqlite:
            from minidb.sqlite_btree import IndexTree, TableTree

            return (IndexTree if index else TableTree).create(self.pager)
        return BTree.create(self.pager, IndexKeyCodec).root if index else BTree.create(self.pager).root

    # ---- changes ------------------------------------------------------------

    def _add_entry(self, kind: str, name: str, table_name: str, root: int, sql: str) -> int:
        key = (self.schema.last_key() or 0) + 1
        self.schema.insert(key, encode_record([kind, name, table_name, root, sql]))
        return key

    def _check_reserved(self, name: str) -> None:
        if ascii_lower(name).startswith(self.reserved_prefixes):
            raise OperationalError(f"object name reserved for internal use: {name}")

    def _check_new_name(self, name: str) -> None:
        """(After _check_reserved and _exists, as in SQLite.)"""
        if ascii_lower(name) in self.indexes:
            raise OperationalError(f"there is already an index named {name}")

    def _exists(self, name: str, if_not_exists: bool) -> bool:
        """Whether a table or view called ``name`` exists (an error unless
        IF NOT EXISTS was given)."""
        lowered = ascii_lower(name)
        if lowered in self.unsupported:
            raise OperationalError(f"table {name} already exists")
        if lowered not in self.tables and lowered not in self.views:
            return False
        if if_not_exists:
            return True
        kind = "table" if lowered in self.tables else "view"
        raise OperationalError(f"{kind} {name} already exists")

    def create_table(self, stmt: CreateTable, check: Callable[[TableInfo], None] | None = None) -> TableInfo | None:
        """Create a table; ``check`` (the executor's) checks its CHECK constraints."""
        if stmt.temp and not self.is_temp:
            return self.temp_catalog().create_table(stmt, check)
        self._check_reserved(stmt.name)
        if self._exists(stmt.name, stmt.if_not_exists):
            return None
        self._check_new_name(stmt.name)
        table = TableInfo(stmt.name, stmt.columns, 0, None, stmt.constraints, stmt.sql, stmt.without_rowid)
        table.validate()
        if check is not None:
            check(table)
        self.version += 1
        table.root = root = self._new_tree(index=not table.has_rowid)
        table.schema_key = self._add_entry("table", table.name, table.name, root, table.sql)
        self._mark(table)
        self.tables[ascii_lower(stmt.name)] = table
        pk_number = None if table.has_rowid else self._make_pk_index(table)
        for n, auto in enumerate(table.auto_indexes(), 1):
            if n == pk_number:
                self._add_pk_index(table)
                continue
            name = f"{self.auto_prefix}{table.name}_{n}"
            columns = [table.columns[p].name for p in auto.positions]
            descending = auto.descending if self.sqlite else None
            self._create_index(name, table, columns, True, True, descending, auto.written, auto.conflict,
                               auto.origin, declared=auto.descending)
        if table.autoincrement and "sqlite_sequence" not in self.tables:
            sql = "CREATE TABLE sqlite_sequence(name,seq)"
            root = self._new_tree(index=False)
            sequence = TableInfo("sqlite_sequence", parse(sql).columns, root, None, (), sql)
            sequence.schema_key = self._add_entry("table", sequence.name, sequence.name, root, sql)
            self._mark(sequence)
            self.tables["sqlite_sequence"] = sequence
        return table

    def create_view(self, stmt: CreateView) -> None:
        """Store a view.  Like SQLite, the SELECT is not checked until the
        view is used (it may name tables that do not exist yet)."""
        if stmt.temp and not self.is_temp:
            return self.temp_catalog().create_view(stmt)
        self._check_reserved(stmt.name)
        if self._exists(stmt.name, stmt.if_not_exists):
            return
        self._check_new_name(stmt.name)
        self.version += 1
        view = ViewInfo(parse(stmt.sql))  # (positions in its own text, for ALTER TABLE)
        view.schema_key = self._add_entry("view", stmt.name, stmt.name, 0, stmt.sql)
        self._mark(view)
        self.views[ascii_lower(stmt.name)] = view

    def create_trigger(self, stmt: CreateTrigger) -> None:
        """Store a trigger, with SQLite's checks (sqlite3BeginTrigger) in its
        order.  Like SQLite, its program is not checked until it first runs.
        It goes in the temp database if TEMP (or temp.name) says so, or if
        its (unqualified) name meets a table of the temp database; a trigger
        of the main database is on a main table, a TEMP one on either."""
        lowered = ascii_lower(stmt.table)
        if stmt.temp or stmt.schema == "temp" or (
                stmt.schema is None and stmt.table_schema != "main" and self._temp_object(lowered)):
            database = self.temp_catalog()
            search = self.search(stmt.table_schema)
        else:
            database = self
            if stmt.table_schema not in (None, "main"):
                raise OperationalError(f"trigger {stmt.name} cannot reference objects in database "
                                       f"{stmt.table_schema}")
            search = [self]
        table = view = None
        for catalog in search:
            table, view = catalog.tables.get(lowered), catalog.views.get(lowered)
            if table is not None or view is not None:
                break
        if table is None and view is None:
            if lowered in SCHEMA_TABLE_NAMES or lowered in TEMP_SCHEMA_TABLE_NAMES:
                raise OperationalError("cannot create trigger on system table")
            reason = self.unsupported.get(lowered)
            if reason is not None:
                raise NotSupportedError(f"MiniDB cannot use {reason}")
            written = stmt.table if stmt.table_schema is None else f"{stmt.table_schema}.{stmt.table}"
            raise OperationalError(f"no such table: {'main.' + stmt.table if database is self else written}")
        database._add_trigger(stmt, table, view)

    def _add_trigger(self, stmt: CreateTrigger, table: TableInfo | None, view: ViewInfo | None) -> None:
        target = table.name if table is not None else view.name
        if ascii_lower(stmt.name).startswith(self.reserved_prefixes):
            raise OperationalError(f"object name reserved for internal use: {stmt.name}")
        if ascii_lower(stmt.name) in self.triggers:
            if stmt.if_not_exists:
                return
            raise OperationalError(f"trigger {stmt.name} already exists")
        if ascii_lower(target).startswith(SQLITE_RESERVED_PREFIX):
            raise OperationalError("cannot create trigger on system table")
        if view is not None and stmt.timing != "INSTEAD OF":
            raise OperationalError(f"cannot create {stmt.timing} trigger on view: {view.name}")
        if table is not None and stmt.timing == "INSTEAD OF":
            raise OperationalError(f"cannot create INSTEAD OF trigger on table: {table.name}")
        if table is not None:
            self.check_writable(table)
        self.version += 1
        trigger = TriggerInfo(parse(stmt.sql))  # (positions in its own text, for ALTER TABLE)
        trigger.table_name = target
        trigger.schema_key = self._add_entry("trigger", stmt.name, target, 0, stmt.sql)
        self._mark(trigger)
        trigger.on_temp = (table or view).temp
        self.triggers[ascii_lower(stmt.name)] = trigger

    def drop_trigger(self, name: str, if_exists: bool = False, schema: str | None = None) -> None:
        for catalog in self.search(schema):
            trigger = catalog.triggers.get(ascii_lower(name))
            if trigger is not None:
                catalog._drop_trigger(trigger)
                return
        if if_exists:
            return
        raise OperationalError(f"no such trigger: {name if schema is None else schema + '.' + name}")

    def _drop_trigger(self, trigger: TriggerInfo) -> None:
        catalog = self.owner(trigger)
        self.version += 1
        catalog.schema.delete(trigger.schema_key)
        del catalog.triggers[ascii_lower(trigger.name)]

    def triggers_of(self, item: TableInfo | ViewInfo) -> list[TriggerInfo]:
        """The triggers of a table or view: its own database's and TEMP ones."""
        main = self.parent or self
        lowered = ascii_lower(item.name)
        return [t for catalog in main.search() for t in catalog.triggers.values()
                if t.on_temp == item.temp and ascii_lower(t.table_name) == lowered]

    def drop_view(self, name: str, if_exists: bool = False, schema: str | None = None) -> None:
        lowered = ascii_lower(name)
        for catalog in self.search(schema):
            if lowered in catalog.tables:
                raise OperationalError(f"use DROP TABLE to delete table {name}")
            if lowered in catalog.views:
                catalog._drop_view(catalog.views[lowered])
                return
        if if_exists:
            return
        raise OperationalError(f"no such view: {name if schema is None else schema + '.' + name}")

    def _drop_view(self, view: ViewInfo) -> None:
        self.version += 1
        for trigger in self.triggers_of(view):
            self._drop_trigger(trigger)
        self.schema.delete(view.schema_key)
        del self.views[ascii_lower(view.name)]

    def drop_table(self, name: str, if_exists: bool = False, schema: str | None = None) -> None:
        lowered = ascii_lower(name)
        for catalog in self.search(schema):
            if lowered in catalog.views:
                raise OperationalError(f"use DROP VIEW to delete view {name}")
            if lowered in catalog.tables:
                catalog._drop_table(catalog.tables[lowered])
                return
            if lowered in catalog.schema_names or (schema is not None and lowered in SCHEMA_TABLE_NAMES):
                raise OperationalError(f"table {catalog.schema_names[1]} may not be dropped")
        if lowered in SCHEMA_TABLE_NAMES and schema is None:
            raise OperationalError("table sqlite_master may not be dropped")
        if if_exists:
            return
        raise OperationalError(f"no such table: {name if schema is None else schema + '.' + name}")

    def _drop_table(self, table: TableInfo) -> None:
        name = table.name
        if ascii_lower(table.name) == "sqlite_sequence":
            raise OperationalError("table sqlite_sequence may not be dropped")
        self.version += 1
        self.check_writable(table, "dropped")
        for trigger in self.triggers_of(table):
            self._drop_trigger(trigger)
        self._destroy_trees([i for i in table.indexes if not i.table_pk] + [table])
        for index in list(table.indexes):
            self._drop_index(index, destroy=False)
        if self.sqlite:
            self._delete_sqlite_stats(table.name)
        if table.stat_key is not None:
            self.schema.delete(table.stat_key)
        del self.tables[ascii_lower(name)]
        self.schema.delete(table.schema_key)
        sequence = self.tables.get("sqlite_sequence")
        if table.autoincrement and sequence is not None:
            tree = self.table_tree(sequence)
            for rowid, record in list(tree.scan()):
                row = record if type(record) is list else decode_record(record)[0]
                if row and row[0] == table.name:
                    tree.delete(rowid)

    def create_index(self, stmt: CreateIndex) -> IndexInfo | None:
        """Create an index; returns it (still empty) or None if it already exists.
        It goes in the database its name says (main if unqualified), or the
        temp database when an unqualified name meets a temp table (as
        sqlite3CreateIndex); there the table may only be a temp one."""
        if not self.is_temp:
            if stmt.schema == "temp" or (stmt.schema is None and self.temp is not None
                                         and ascii_lower(stmt.table) in self.temp.tables):
                table = self.get_table(stmt.table)
                if not table.temp:
                    raise OperationalError(f'cannot create a TEMP index on non-TEMP table "{table.name}"')
                return self.temp.create_index(stmt)
            if stmt.schema not in (None, "main"):
                raise OperationalError(f"unknown database {stmt.schema}")
        lowered = ascii_lower(stmt.name)
        if lowered in self.indexes:
            if stmt.if_not_exists:
                return None
            raise OperationalError(f"index {stmt.name} already exists")
        if lowered in self.tables or lowered in self.views:
            raise OperationalError(f"there is already a table named {stmt.name}")
        if lowered.startswith(self.reserved_prefixes):
            raise OperationalError(f"object name reserved for internal use: {stmt.name}")
        table = self.tables.get(ascii_lower(stmt.table))
        if table is None and ascii_lower(stmt.table) in self.schema_names:
            raise OperationalError(f"table {self.schema_names[1]} may not be indexed")
        if table is None:
            if ascii_lower(stmt.table) in self.views:
                raise OperationalError("views may not be indexed")
            raise OperationalError(f"no such table: {'temp' if self.is_temp else 'main'}.{stmt.table}")
        for column in stmt.columns:
            if table.column_index(column) is None:
                raise OperationalError(f"no such column: {column}")
        for collation in stmt.collations:
            if collation is not None:
                values.collation_name(collation)
        self.check_writable(table, "indexed")
        descending = stmt.descending if self.sqlite else None
        return self._create_index(stmt.name, table, stmt.columns, stmt.unique, descending=descending,
                                  collations=stmt.collations, sql=stmt.sql, declared=stmt.descending)

    def _create_index(self, name: str, table: TableInfo, columns: list[str], unique: bool, auto: bool = False,
                      descending: list[bool] | None = None, collations: list[str | None] | None = None,
                      conflict: str | None = None, origin: str = "c", sql: str | None = None,
                      declared: list[bool] | None = None) -> IndexInfo:
        self.version += 1
        root = self._new_tree(index=True)
        index = IndexInfo(name, table, columns, unique, root, None, auto, descending, collations, conflict,
                          origin, sql, declared)
        index.schema_key = self._add_entry("index", name, table.name, root, sql)
        self._mark(index)
        self.indexes[ascii_lower(name)] = index
        add_index(table, index)
        return index

    def _make_pk_index(self, table: TableInfo) -> int:
        """Make the PRIMARY KEY index of a WITHOUT ROWID table (table.pk_index:
        no tree or schema entry of its own - it is the table's); returns its
        number among the table's automatic indexes, where _add_pk_index
        puts it in the table's list (the other indexes' keys need it first)."""
        number, auto = next((n, a) for n, a in enumerate(table.auto_indexes(), 1) if a.origin == "pk")
        # (A column the PRIMARY KEY repeats with the same collation counts
        # once: SQLite's convertToWithoutRowidTable.)
        seen, kept = set(), []
        for i, part in enumerate(zip(auto.positions, auto.collations)):
            if part not in seen:
                seen.add(part)
                kept.append(i)
        columns = [table.columns[auto.positions[i]].name for i in kept]
        descending = [auto.descending[i] for i in kept]
        table.pk_index = IndexInfo(f"{self.auto_prefix}{table.name}_{number}", table, columns, True, table.root,
                                   None, True, descending if self.sqlite else None, [auto.written[i] for i in kept],
                                   auto.conflict, auto.origin, declared=descending, table_pk=True)
        self._mark(table.pk_index)
        return number

    def _add_pk_index(self, table: TableInfo) -> None:
        index = table.pk_index
        self.indexes[ascii_lower(index.name)] = index
        add_index(table, index)

    def drop_index(self, name: str, if_exists: bool = False, schema: str | None = None) -> None:
        catalog = next((c for c in self.search(schema) if ascii_lower(name) in c.indexes), None)
        if catalog is None:
            if if_exists:
                return
            raise OperationalError(f"no such index: {name if schema is None else schema + '.' + name}")
        index = catalog.indexes[ascii_lower(name)]
        if index.is_auto:
            raise OperationalError(
                "index associated with UNIQUE or PRIMARY KEY constraint cannot be dropped"
            )
        catalog._drop_index(index)

    def _drop_index(self, index: IndexInfo, destroy: bool = True) -> None:
        self.version += 1
        if self.sqlite:
            self._delete_sqlite_stats(index.table.name, index.name)
        if destroy:
            self._destroy_trees([index])
        if index.schema_key is not None:  # (a WITHOUT ROWID table's PRIMARY KEY has none)
            self.schema.delete(index.schema_key)
        if index.stat_key is not None:
            self.schema.delete(index.stat_key)
        del self.indexes[ascii_lower(index.name)]
        index.table.indexes.remove(index)

    # ---- ALTER TABLE ------------------------------------------------------------

    def rewrite_table_entries(self, table: TableInfo) -> None:
        """Store a table's (changed) definition, its indexes' and their
        statistics again, under their old keys; then reload the schema."""
        if table.temp and not self.is_temp:
            return self.temp.rewrite_table_entries(table)
        self.version += 1
        self.schema.insert(table.schema_key, encode_record(
            ["table", table.name, table.name, table.root, table.sql]), replace=True)
        for index in table.indexes:
            if index.schema_key is None:
                continue
            self.schema.insert(index.schema_key, encode_record(
                ["index", index.name, table.name, index.root, index.sql]), replace=True)
            if index.stat_key is not None:
                text = " ".join(f"{n:g}" if isinstance(n, float) else str(n)
                                for n in [table.stat_rows or 0] + index.stat_average)
                self.schema.insert(index.stat_key, encode_record(
                    ["stat", index.name, table.name, 0, text]), replace=True)
        if table.stat_key is not None:
            self.schema.insert(table.stat_key, encode_record(
                ["stat", table.name, table.name, 0, str(table.stat_rows)]), replace=True)

    def _destroy_trees(self, objects: list[TableInfo | IndexInfo]) -> None:
        """Free the trees of tables and indexes, the largest root first (as
        SQLite's destroyTable): with auto_vacuum the largest root of the
        database then moves into each freed root page, and the schema says so."""
        objects = list(objects)
        while objects:
            target = max(objects, key=lambda o: o.root)
            objects.remove(target)
            (self.index_tree(target) if isinstance(target, IndexInfo) else self.table_tree(target)).destroy()
            moved = self.pager.release_root(target.root) if self.sqlite else None
            if moved is not None:
                self._root_moved(*moved)

    def _root_moved(self, old: int, new: int) -> None:
        for key, record in list(self.schema.scan()):
            row = record if type(record) is list else decode_record(record)[0]
            if row[0] in ("table", "index") and row[3] == old:
                row[3] = new
                self.schema.insert(key, encode_record(row), replace=True)
        for table in self.tables.values():
            for item in [table] + table.indexes:
                if item.root == old and not table.is_schema:
                    item.root = new

    def rewrite_trigger(self, trigger: TriggerInfo, sql: str, table_name: str) -> None:
        self.version += 1
        self.owner(trigger).schema.insert(trigger.schema_key, encode_record(["trigger", trigger.name, table_name, 0, sql]),
                           replace=True)

    def rewrite_view(self, view: ViewInfo, sql: str) -> None:
        self.version += 1
        self.owner(view).schema.insert(view.schema_key, encode_record(["view", view.name, view.name, 0, sql]), replace=True)

    # ---- statistics ---------------------------------------------------------

    def analyze(self, name: str | None = None) -> None:
        """Gather statistics for one table (or the table of an index) or all
        (of the main database: MiniDB keeps none for temporary tables)."""
        if name is not None and self.temp is not None and (
                ascii_lower(name) in self.temp.tables or ascii_lower(name) in self.temp.indexes):
            return self.temp.analyze(name)
        if self.sqlite:
            self._analyze_sqlite(name)
            return
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
                width = len(index.positions)
                for key in self.index_tree(index).keys():
                    values = key[:width]  # (not the row id, or the PRIMARY KEY's columns)
                    for depth in range(len(values)):
                        if previous is None or previous[:depth + 1] != values[:depth + 1]:
                            distinct[depth] += 1
                    previous = values
                average = [rows / d if d else 1.0 for d in distinct]
                self._set_stat(index, index.name, [rows] + average)
                index.stat_average = average

    # ---- statistics in SQLite's format: the table sqlite_stat1(tbl, idx, stat) ----

    def _load_sqlite_stats(self) -> None:
        stat = self.tables.get("sqlite_stat1")
        if stat is None:
            return
        for _, value in self.table_tree(stat).scan():
            row = value + [None, None, None]  # (a SQLite table tree gives rows)
            table, index_name, text = self.tables.get(ascii_lower(str(row[0]))), row[1], row[2]
            numbers = []
            for word in str(text).split():
                try:
                    numbers.append(float(word))
                except ValueError:
                    break  # SQLite allows words like "unordered" after the numbers
            if table is None or not numbers:
                continue
            table.stat_rows = int(numbers[0])
            index = self.indexes.get(ascii_lower(str(index_name))) if index_name is not None else None
            if index is None and not table.has_rowid and ascii_lower(str(index_name)) == ascii_lower(table.name):
                index = table.pk_index  # (named after its table)
            if index is not None and index.table is table:
                index.stat_average = numbers[1:]

    def _stat_tree(self, create: bool) -> BTree | None:
        """sqlite_stat1's tree (created as SQLite creates it, if asked)."""
        stat = self.tables.get("sqlite_stat1")
        if stat is None:
            if not create:
                return None
            sql = "CREATE TABLE sqlite_stat1(tbl,idx,stat)"
            root = self._new_tree(index=False)
            stat = TableInfo("sqlite_stat1", parse(sql).columns, root)
            stat.schema_key = self._add_entry("table", stat.name, stat.name, root, sql)
            self._mark(stat)
            self.tables["sqlite_stat1"] = stat
        return self.table_tree(stat)

    def _delete_sqlite_stats(self, table: str, index: str | None = None) -> None:
        """Remove the statistics of a table (or of one of its indexes)."""
        tree = self._stat_tree(create=False)
        if tree is None:
            return
        for rowid, value in list(tree.scan()):
            row = value + [None, None]
            if ascii_lower(str(row[0])) == ascii_lower(table) and (
                    index is None or (row[1] is not None and ascii_lower(str(row[1])) == ascii_lower(index))):
                tree.delete(rowid)

    def _analyze_sqlite(self, name: str | None) -> None:
        """ANALYZE as SQLite does it: per index "rows avg1 avg2 ..." where
        avgN is the rows per distinct value of the first N columns, rounded
        up; "rows" for a table without indexes; nothing for an empty table."""
        if name is None:
            # (in the order SQLite's schema hash yields them: newest first,
            # while there are fewer than 10 tables; only the row order of
            # sqlite_stat1 depends on it)
            targets = [(t, None) for t in reversed(self.tables.values())
                       if not ascii_lower(t.name).startswith(SQLITE_RESERVED_PREFIX)]
        elif ascii_lower(name) in self.tables:
            targets = [(self.tables[ascii_lower(name)], None)]
        elif ascii_lower(name) in self.indexes:
            index = self.indexes[ascii_lower(name)]
            targets = [(index.table, index)]
        else:
            raise OperationalError(f"no such table or index: {name}")
        self.version += 1
        for table, index in targets:
            self._delete_sqlite_stats(table.name, index.name if index is not None else None)
        tree = self._stat_tree(create=True)

        def add(table_name: str, index_name: str | None, numbers: list[int]) -> None:
            text = " ".join(str(n) for n in numbers)
            tree.insert((tree.last_key() or 0) + 1, encode_record([table_name, index_name, text]))

        for table, only in targets:
            rows = len(self.table_tree(table))
            table.stat_rows = rows
            if rows == 0:
                continue
            if not table.indexes:
                add(table.name, None, [rows])
            for index in table.indexes if only is None else [only]:
                distinct, previous = [0] * len(index.positions), None
                for key in self.index_tree(index).keys():
                    key = key[:len(index.positions)]  # (not the row id, or the PRIMARY KEY's columns)
                    for depth in range(len(key)):
                        if previous is None or previous[:depth + 1] != key[:depth + 1]:
                            distinct[depth] += 1
                    previous = key
                averages = [(rows + d - 1) // d if d else rows for d in distinct]
                add(table.name, table.name if index.table_pk else index.name, [rows] + averages)
                index.stat_average = [float(a) for a in averages]

    def _set_stat(self, owner: TableInfo | IndexInfo, name: str, numbers: list[int | float]) -> None:
        if owner.stat_key is not None:
            self.schema.delete(owner.stat_key)
        text = " ".join(f"{n:g}" if isinstance(n, float) else str(n) for n in numbers)
        table_name = owner.name if isinstance(owner, TableInfo) else owner.table.name
        owner.stat_key = self._add_entry("stat", name, table_name, 0, text)


class _SchemaTree(BTree):
    """MiniDB's schema table: a change makes the commit bump the schema version."""

    def insert(self, *args: Any, **kwargs: Any) -> None:
        self.pager.note_schema_change()
        super().insert(*args, **kwargs)

    def delete(self, key: int) -> bool:
        self.pager.note_schema_change()
        return super().delete(key)


class _SchemaRows:
    """sqlite_schema in MiniDB's own format: the schema table without the
    statistics rows, with no SQL for automatic indexes (as SQLite shows them).
    Read only."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def scan(self, start: int | None = None, end: int | None = None, start_inclusive: bool = True,
             end_inclusive: bool = True) -> Iterator[tuple[int, bytes]]:
        for key, value in self.catalog.schema.scan(start, end, start_inclusive, end_inclusive):
            row = decode_record(value)[0]
            if row[0] == "stat":
                continue
            if row[0] == "index" and ascii_lower(row[1]) in self.catalog.indexes \
                    and self.catalog.indexes[ascii_lower(row[1])].is_auto:
                row[4] = None
            yield key, encode_record(row)

    def get(self, key: int, default: bytes | None = None) -> bytes | None:
        return next((value for k, value in self.scan(key, key)), default)

    def __contains__(self, key: int) -> bool:
        return self.get(key) is not None

    def keys(self) -> list:
        return [key for key, _ in self.scan()]

    def __len__(self) -> int:
        return len(self.keys())

    def last_key(self) -> int | None:
        keys = self.keys()
        return keys[-1] if keys else None

    def estimated_count(self) -> int:
        return max(1, len(self))
