"""Schema management.

The schema lives in the database file itself, in a B+ tree rooted at page 1
(like SQLite's ``sqlite_schema``).  Each entry is a record

    (type, name, table name, root page, sql)

where ``sql`` is the canonical CREATE statement; on open the statements are
parsed again to rebuild the in-memory ``TableInfo`` objects.
"""

from minidb import values
from minidb.btree import BTree
from minidb.errors import DatabaseError, OperationalError
from minidb.parser import CreateTable, parse
from minidb.record import decode_record, encode_record

SCHEMA_ROOT = 1


def quote(name):
    return '"' + name.replace('"', '""') + '"'


class TableInfo:
    def __init__(self, name, columns, root, schema_key=None):
        self.name = name
        self.columns = columns
        self.root = root
        self.schema_key = schema_key
        self.positions = {column.name.lower(): i for i, column in enumerate(columns)}
        # An INTEGER PRIMARY KEY column is an alias for the row id (as in SQLite).
        self.rowid_column = next(
            (i for i, c in enumerate(columns) if c.primary_key and c.type == "INTEGER"), None
        )
        self.affinities = [values.INTEGER if c.type == "INTEGER" else values.TEXT for c in columns]

    def column_index(self, name):
        return self.positions.get(name.lower())

    def unique_columns(self):
        """Positions of columns that must hold distinct values (besides the rowid)."""
        return [
            i for i, c in enumerate(self.columns)
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
        for key, value in self.schema.scan():
            kind, name, _table_name, root, sql = decode_record(value)[0]
            if kind == "table":
                stmt = parse(sql)
                self.tables[name.lower()] = TableInfo(name, stmt.columns, root, key)

    def get_table(self, name):
        table = self.tables.get(name.lower())
        if table is None:
            raise OperationalError(f"no such table: {name}")
        return table

    def has_table(self, name):
        return name.lower() in self.tables

    def create_table(self, stmt: CreateTable):
        if self.has_table(stmt.name):
            if stmt.if_not_exists:
                return None
            raise OperationalError(f"table {stmt.name} already exists")
        seen = set()
        for column in stmt.columns:
            if column.name.lower() in seen:
                raise OperationalError(f"duplicate column name: {column.name}")
            seen.add(column.name.lower())
        if sum(column.primary_key for column in stmt.columns) > 1:
            raise OperationalError(f'table "{stmt.name}" has more than one primary key')
        root = BTree.create(self.pager).root
        key = (self.schema.last_key() or 0) + 1
        table = TableInfo(stmt.name, stmt.columns, root, key)
        self.schema.insert(key, encode_record(["table", table.name, table.name, root, table.sql()]))
        self.tables[stmt.name.lower()] = table
        return table

    def drop_table(self, name, if_exists=False):
        if not self.has_table(name):
            if if_exists:
                return
            raise OperationalError(f"no such table: {name}")
        table = self.tables.pop(name.lower())
        BTree(self.pager, table.root).destroy()
        self.schema.delete(table.schema_key)

    def table_tree(self, table):
        return BTree(self.pager, table.root)
