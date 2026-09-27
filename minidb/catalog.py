"""Schema management.

The schema lives in the database file itself, in a B+ tree rooted at page 1
(like SQLite's ``sqlite_schema``).  Each entry is a record

    (type, name, table name, root page, sql)

where ``type`` is "table" or "index" and ``sql`` is the canonical CREATE
statement; on open the statements are parsed again to rebuild the in-memory
``TableInfo`` and ``IndexInfo`` objects.

Every UNIQUE column and every PRIMARY KEY that is not an INTEGER PRIMARY KEY
gets an automatic unique index named ``minidb_autoindex_<table>_<n>``.
"""

from minidb import values
from minidb.btree import BTree
from minidb.errors import DatabaseError, OperationalError
from minidb.parser import CreateIndex, CreateTable, parse
from minidb.record import decode_record, encode_record

SCHEMA_ROOT = 1
RESERVED_PREFIX = "minidb_"
AUTO_INDEX_PREFIX = "minidb_autoindex_"


def quote(name):
    return '"' + name.replace('"', '""') + '"'


# ---- index keys ------------------------------------------------------------------

# Sentinels that sort below / above every (rank, value) pair of an index key.
LOW = (-1,)
HIGH = (3,)


def index_key(key_values, rowid):
    """The B+ tree key for an index entry: the sort keys of the indexed values
    followed by the row id, so that every key is unique."""
    return tuple(values.sort_key(v) for v in key_values) + ((1, rowid),)


class IndexKeyCodec:
    """Serializes index keys (tuples of sort-key pairs) as records."""

    @staticmethod
    def _plain(pair):
        return None if pair[0] == 0 else pair[1]

    @classmethod
    def encode(cls, key):
        return encode_record([cls._plain(pair) for pair in key])

    @staticmethod
    def decode(data, pos):
        row, end = decode_record(data, pos)
        return tuple(values.sort_key(v) for v in row), end

    @classmethod
    def size(cls, key):
        return len(cls.encode(key))


# ---- schema objects ----------------------------------------------------------------


class TableInfo:
    def __init__(self, name, columns, root, schema_key=None):
        self.name = name
        self.columns = columns
        self.root = root
        self.schema_key = schema_key
        self.indexes = []  # newest first, the order SQLite checks UNIQUE constraints in
        self.positions = {column.name.lower(): i for i, column in enumerate(columns)}
        # An INTEGER PRIMARY KEY column is an alias for the row id (as in SQLite).
        self.rowid_column = next(
            (i for i, c in enumerate(columns) if c.primary_key and c.type == "INTEGER"), None
        )
        self.affinities = [values.INTEGER if c.type == "INTEGER" else values.TEXT for c in columns]

    def column_index(self, name):
        return self.positions.get(name.lower())

    def auto_index_columns(self):
        """Columns that need an automatic unique index, in column order."""
        return [
            c.name for i, c in enumerate(self.columns)
            if (c.unique or c.primary_key) and i != self.rowid_column
        ]

    def sql(self):
        parts = []
        for column in self.columns:
            text = f"{quote(column.name)} {column.type}"
            if column.primary_key:
                text += " PRIMARY KEY"
            if column.not_null:
                text += " NOT NULL"
            if column.unique:
                text += " UNIQUE"
            parts.append(text)
        return f"CREATE TABLE {quote(self.name)} ({', '.join(parts)})"


class IndexInfo:
    def __init__(self, name, table, column_names, unique, root, schema_key=None):
        self.name = name
        self.table = table
        self.column_names = [table.columns[table.column_index(c)].name for c in column_names]
        self.positions = [table.column_index(c) for c in column_names]
        self.unique = unique
        self.root = root
        self.schema_key = schema_key

    @property
    def is_auto(self):
        return self.name.lower().startswith(AUTO_INDEX_PREFIX)

    def key(self, row, rowid):
        return index_key([row[p] for p in self.positions], rowid)

    def sql(self):
        columns = ", ".join(quote(c) for c in self.column_names)
        unique = "UNIQUE " if self.unique else ""
        return f"CREATE {unique}INDEX {quote(self.name)} ON {quote(self.table.name)} ({columns})"


class Catalog:
    def __init__(self, pager):
        self.pager = pager
        if pager.page_count == 1:
            tree = BTree.create(pager)
            if tree.root != SCHEMA_ROOT:
                raise DatabaseError("could not create the schema table")
        self.schema = BTree(pager, SCHEMA_ROOT)
        self.load()

    def load(self):
        """(Re)build the in-memory schema from the schema table."""
        self.tables = {}
        self.indexes = {}
        entries = [decode_record(value)[0] + [key] for key, value in self.schema.scan()]
        for kind, name, _table_name, root, sql, key in entries:
            if kind == "table":
                stmt = parse(sql)
                self.tables[name.lower()] = TableInfo(name, stmt.columns, root, key)
        for kind, name, table_name, root, sql, key in entries:
            if kind == "index":
                stmt = parse(sql)
                table = self.tables[table_name.lower()]
                index = IndexInfo(name, table, stmt.columns, stmt.unique, root, key)
                self.indexes[name.lower()] = index
                table.indexes.insert(0, index)

    # ---- lookups ----------------------------------------------------------

    def get_table(self, name):
        table = self.tables.get(name.lower())
        if table is None:
            raise OperationalError(f"no such table: {name}")
        return table

    def has_table(self, name):
        return name.lower() in self.tables

    def table_tree(self, table):
        return BTree(self.pager, table.root)

    def index_tree(self, index):
        return BTree(self.pager, index.root, IndexKeyCodec)

    # ---- changes ------------------------------------------------------------

    def _add_entry(self, kind, name, table_name, root, sql):
        key = (self.schema.last_key() or 0) + 1
        self.schema.insert(key, encode_record([kind, name, table_name, root, sql]))
        return key

    def _check_new_name(self, name):
        lowered = name.lower()
        if lowered.startswith(RESERVED_PREFIX):
            raise OperationalError(f"object name reserved for internal use: {name}")
        if lowered in self.indexes:
            raise OperationalError(f"there is already an index named {name}")

    def create_table(self, stmt: CreateTable):
        if self.has_table(stmt.name):
            if stmt.if_not_exists:
                return None
            raise OperationalError(f"table {stmt.name} already exists")
        self._check_new_name(stmt.name)
        seen = set()
        for column in stmt.columns:
            if column.name.lower() in seen:
                raise OperationalError(f"duplicate column name: {column.name}")
            seen.add(column.name.lower())
        if sum(column.primary_key for column in stmt.columns) > 1:
            raise OperationalError(f'table "{stmt.name}" has more than one primary key')
        root = BTree.create(self.pager).root
        table = TableInfo(stmt.name, stmt.columns, root)
        table.schema_key = self._add_entry("table", table.name, table.name, root, table.sql())
        self.tables[stmt.name.lower()] = table
        for n, column in enumerate(table.auto_index_columns(), 1):
            name = f"{AUTO_INDEX_PREFIX}{table.name}_{n}"
            self._create_index(name, table, [column], unique=True)
        return table

    def drop_table(self, name, if_exists=False):
        if not self.has_table(name):
            if if_exists:
                return
            raise OperationalError(f"no such table: {name}")
        table = self.tables[name.lower()]
        for index in list(table.indexes):
            self._drop_index(index)
        del self.tables[name.lower()]
        self.table_tree(table).destroy()
        self.schema.delete(table.schema_key)

    def create_index(self, stmt: CreateIndex):
        """Create an index; returns it (still empty) or None if it already exists."""
        lowered = stmt.name.lower()
        if lowered in self.indexes:
            if stmt.if_not_exists:
                return None
            raise OperationalError(f"index {stmt.name} already exists")
        if lowered in self.tables:
            raise OperationalError(f"there is already a table named {stmt.name}")
        if lowered.startswith(RESERVED_PREFIX):
            raise OperationalError(f"object name reserved for internal use: {stmt.name}")
        table = self.tables.get(stmt.table.lower())
        if table is None:
            raise OperationalError(f"no such table: main.{stmt.table}")
        for column in stmt.columns:
            if table.column_index(column) is None:
                raise OperationalError(f"no such column: {column}")
        return self._create_index(stmt.name, table, stmt.columns, stmt.unique)

    def _create_index(self, name, table, columns, unique):
        root = BTree.create(self.pager, IndexKeyCodec).root
        index = IndexInfo(name, table, columns, unique, root)
        index.schema_key = self._add_entry("index", name, table.name, root, index.sql())
        self.indexes[name.lower()] = index
        table.indexes.insert(0, index)
        return index

    def drop_index(self, name, if_exists=False):
        index = self.indexes.get(name.lower())
        if index is None:
            if if_exists:
                return
            raise OperationalError(f"no such index: {name}")
        if index.is_auto:
            raise OperationalError(
                "index associated with UNIQUE or PRIMARY KEY constraint cannot be dropped"
            )
        self._drop_index(index)

    def _drop_index(self, index):
        self.index_tree(index).destroy()
        self.schema.delete(index.schema_key)
        del self.indexes[index.name.lower()]
        index.table.indexes.remove(index)
