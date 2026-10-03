"""PRAGMA statements and the pragma_xxx() table-valued functions, as SQLite has them.

A pragma either reports on the schema (``table_info``, ``index_list``,
``foreign_key_list`` ...), reads or sets a value (``user_version``,
``foreign_keys`` ...), or checks the database (``integrity_check``,
``foreign_key_check``).  Unknown pragmas do nothing, as in SQLite.  Each
pragma that returns rows can also be used in FROM as ``pragma_<name>(arg,
schema)``, with the hidden columns ``arg`` and ``schema`` (see
``PragmaSource``).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from minidb.catalog import IndexInfo, TableInfo
from minidb.errors import OperationalError
from minidb.parser import Column, ColumnDef
from minidb.values import ascii_lower, ascii_upper

if TYPE_CHECKING:
    from minidb.executor import Executor
else:
    Executor = Any  # (minidb.executor imports this module)

# Declared types SQLite knows by name (stored and shown in upper case; others as written).
STANDARD_TYPES = ("ANY", "BLOB", "INT", "INTEGER", "REAL", "TEXT")
_SPACE = " \t\n\v\f\r"


_DIGITS = "0123456789"
_HEX_DIGITS = "0123456789abcdefABCDEF"


def int32(text: object) -> int:
    """sqlite3Atoi (sqlite3GetInt32): a 32-bit integer from the start of
    ``text`` - decimal with an optional sign, or 0x hex - else 0."""
    text = "" if text is None else str(text)
    negative = False
    if text[:1] == "-":
        negative, text = True, text[1:]
    elif text[:1] == "+":
        text = text[1:]
    elif text[:2] in ("0x", "0X") and text[2:3] and text[2:3] in _HEX_DIGITS:
        digits = text[2:].lstrip("0")
        n = 0
        while n < len(digits) and n < 8 and digits[n] in _HEX_DIGITS:
            n += 1
        value = int(digits[:n] or "0", 16)
        if value & 0x80000000 or (n < len(digits) and digits[n] in _HEX_DIGITS):
            return 0
        return value
    if not text[:1] or text[0] not in _DIGITS:
        return 0
    text = text.lstrip("0")
    n = 0
    while n < len(text) and n < 11 and text[n] in _DIGITS:
        n += 1
    if n > 10:
        return 0
    value = int(text[:n] or "0")
    if value - negative > 2147483647:
        return 0
    return -value if negative else value


def boolean(text: object) -> int:
    """sqlite3GetBoolean: on / yes / true / a non-zero number."""
    text = str(text) if text is not None else ""
    if text[:1].isdigit():
        return int(int32(text) != 0)
    return int(ascii_lower(text) in ("on", "yes", "true"))


def declared_type(column: ColumnDef) -> str:
    upper = ascii_upper(column.declared)
    return upper if upper in STANDARD_TYPES else column.declared


def default_text(column: ColumnDef) -> str | None:
    """The DEFAULT as written; a parenthesized expression without its parentheses."""
    text = column.default_text
    if text is not None and text.startswith("("):
        text = text[1:-1].strip(_SPACE)
    return text


def collation_text(column: ColumnDef) -> str:
    return column.collation if column.collation is not None else "BINARY"


# ---- reports --------------------------------------------------------------------


def _table(executor: Executor, name: object) -> TableInfo | None:
    """The table a pragma's argument names (in the pragma's schema; else temp first)."""
    if not isinstance(name, str):
        return None
    schema = executor.pragma_schema
    for catalog in executor.catalog.search(schema):
        table = catalog._find_table(name, schema is not None)
        if table is not None:
            return table
    return None


def table_info(executor: Executor, name: object, extended: bool = False) -> list[tuple]:
    table = _table(executor, name)
    hidden = (0,) if extended else ()
    if table is not None:
        key = [ascii_lower(c.name) for c in table.primary_key.columns] if table.primary_key else []
        rows = []
        skipped = 0  # generated columns, which only table_xinfo shows (hidden 2: VIRTUAL, 3: STORED)
        for i, column in enumerate(table.columns):
            if column.generated is not None:
                if not extended:
                    skipped += 1
                    continue
                hidden = (3 if column.stored else 2,)
            elif extended:
                hidden = (0,)
            pk = key.index(ascii_lower(column.name)) + 1 if ascii_lower(column.name) in key else 0
            rows.append((i - skipped, column.name, declared_type(column), int(column.not_null),
                         default_text(column), pk) + hidden)
        return rows
    view = executor.catalog.find_view(name, executor.pragma_schema) if isinstance(name, str) else None
    if view is None:
        return []
    source = executor.view_source(view)
    types = source_types(source.compiled)
    return [(cid, column.name, types[cid], 0, None, 0) + hidden for cid, column in enumerate(source.columns)]


def source_types(compiled: Any) -> list[str]:
    """The declared types of a query's result columns (as SQLite gives a view's
    columns): a column reference's declared type, else ''."""
    from minidb.executor import AliasReference, CompiledCompound, DerivedSource

    if isinstance(compiled, CompiledCompound):
        compiled = compiled.parts[0]
    compiler = getattr(compiled, "collation_compiler", None)
    types = []
    for expr in compiled.exprs:
        kind = ""
        if compiler is not None and isinstance(expr, Column):
            try:
                slot, _, index, depth = compiler.scope.resolve(expr)
            except (AliasReference, OperationalError):
                depth = None
            if depth == 0:
                entry = compiler.scope.entries[index]
                position = slot - entry.offset
                if isinstance(entry.table, TableInfo) and position < len(entry.table.columns):
                    kind = declared_type(entry.table.columns[position])
                elif isinstance(entry.table, DerivedSource) and position < len(entry.table.columns):
                    kind = source_types(entry.table.compiled)[position]
        types.append(kind)
    return types


def index_list(executor: Executor, name: object) -> list[tuple]:
    table = _table(executor, name)
    if table is None:
        return []
    return [(seq, index.name, int(index.unique), index.origin, 0) for seq, index in enumerate(table.indexes)]


def index_info(executor: Executor, name: object, extended: bool = False) -> list[tuple]:
    index = next((c.indexes[ascii_lower(name)] for c in executor.catalog.search(executor.pragma_schema)
                  if ascii_lower(name) in c.indexes), None) if isinstance(name, str) else None
    if index is None:
        # (a WITHOUT ROWID table's name stands for its PRIMARY KEY index)
        table = _table(executor, name)
        index = table.pk_index if table is not None and not table.has_rowid else None
    if index is None:
        return []
    rows = []
    for seq, (position, column_name) in enumerate(zip(index.positions, index.column_names)):
        if extended:
            rows.append((seq, position, column_name, int(index.declared_descending[seq]),
                         index.collation_names[seq], 1))
        else:
            rows.append((seq, position, column_name))
    if not extended:
        return rows
    table = index.table
    if index.table_pk:  # (then the other stored columns: its entries are the rows)
        stored = table.storage if table.storage is not None else range(len(table.columns))
        extra = [(p, 0, table.columns[p].collation or "BINARY") for p in stored if p not in index.positions]
    elif not table.has_rowid:  # (then the PRIMARY KEY's columns it does not have)
        pk = table.pk_index
        extra = [(pk.positions[j], int(pk.declared_descending[j] and not index.auto), pk.collation_names[j])
                 for j in index.extra]  # (a UNIQUE constraint's index: ascending, see Catalog.index_tree)
    else:
        return rows + [(len(rows), -1, None, 0, "BINARY", 0)]
    for position, descending, collation in extra:
        rows.append((len(rows), position, table.columns[position].name, descending, collation, 0))
    return rows


def foreign_key_list(executor: Executor, name: object) -> list[tuple]:
    table = _table(executor, name)
    if table is None:
        return []
    rows = []
    for number, key in enumerate(reversed(table.foreign_keys)):  # (SQLite lists the last one first)
        for seq, column in enumerate(key.columns):
            parent = key.parent_columns[seq] if seq < len(key.parent_columns) else None
            rows.append((number, seq, key.parent, column, parent, key.on_update, key.on_delete, key.match))
    return rows


def table_list(executor: Executor, name: object = None) -> list[tuple]:
    """Tables and views, newest first (the order SQLite's schema hash gives
    while it is small), then the schema tables."""
    catalog = executor.catalog
    rows = []
    objects = sorted([*catalog.tables.values(), *catalog.views.values()],
                     key=lambda o: o.schema_key or 0, reverse=True)
    for item in objects:
        if isinstance(item, TableInfo):
            rows.append(("main", item.name, "table", len(item.columns), int(not item.has_rowid), 0))
        else:
            rows.append(("main", item.name, "view", _view_width(executor, item), 0, 0))
    rows.append(("main", "sqlite_schema", "table", 5, 0, 0))
    temp = catalog.temp
    objects = [] if temp is None else sorted([*temp.tables.values(), *temp.views.values()],
                                             key=lambda o: o.schema_key or 0, reverse=True)
    for item in objects:
        if isinstance(item, TableInfo):
            rows.append(("temp", item.name, "table", len(item.columns), int(not item.has_rowid), 0))
        else:
            rows.append(("temp", item.name, "view", _view_width(executor, item), 0, 0))
    rows.append(("temp", "sqlite_temp_schema", "table", 5, 0, 0))
    if executor.pragma_schema is not None:
        rows = [row for row in rows if row[0] == executor.pragma_schema]
    if name is not None:
        rows = [row for row in rows if ascii_lower(row[1]) == ascii_lower(str(name))]
    return rows


def _view_width(executor: Executor, view: object) -> int:
    """A view's number of columns (0 if its SELECT does not compile, as SQLite shows it)."""
    try:
        return len(executor.view_source(view).columns)
    except OperationalError:
        return 0


def integrity_check(executor: Executor, arg: object, quick: bool = False) -> list[tuple]:
    """The structural problems (Database.integrity_check), then each row's
    NOT NULL and CHECK violations as SQLite words them; 'ok' if none."""
    limit = 100
    only = None
    if arg is not None:
        only = _table(executor, arg)
        if only is None:
            limit = int32(arg) if int32(arg) > 0 else 100
    problems = list(executor.integrity_problems()) if executor.integrity_problems is not None else []
    tables = []
    for catalog in reversed(executor.catalog.search(executor.pragma_schema)):  # (main, then temp)
        tables += sorted(catalog.tables.values(), key=lambda t: t.schema_key or 0, reverse=True)
    for table in tables:
        if only is not None and table is not only:
            continue
        checks = executor.compile_checks(table) if table.checks and not executor.settings["ignore_check_constraints"] \
            else []
        not_null = [(i, c) for i, c in enumerate(table.columns) if c.not_null]
        if not checks and not not_null:
            continue
        for rowid, record in executor.catalog.table_tree(table).scan():
            row = executor.load_row(table, rowid, record)
            for i, column in not_null:
                if row[i] is None:
                    problems.append(f"NULL value in {table.name}.{column.name}")
            if any(failed(row) for _, failed, _ in checks):
                problems.append(f"CHECK constraint failed in {table.name}")
    return [(problem,) for problem in problems[:limit]] or [("ok",)]


# ---- values --------------------------------------------------------------------------


def _header_field(executor: Executor, field: str) -> str:
    if field == "schema_version" and executor.catalog.sqlite:
        return "schema_cookie"
    return field


def _header_value(field: str) -> Callable[[Executor], int]:
    """A 32-bit header field, read as a signed integer (as SQLite does)."""
    def read(executor: Executor) -> int:
        value = getattr(executor.catalog.pager.header, _header_field(executor, field)) & 0xFFFFFFFF
        return value - (1 << 32) if value >= 1 << 31 else value
    return read


def _set_header(field: str) -> Callable[[Executor, object], None]:
    def write(executor: Executor, value: object) -> None:
        pager = executor.catalog.pager
        pager.write(pager.header)
        number = int32(value)
        name = _header_field(executor, field)
        unsigned = executor.catalog.sqlite or field == "schema_version"
        setattr(pager.header, name, number & 0xFFFFFFFF if unsigned else number)
        if field == "schema_version":
            executor.catalog.load()
    return write


def _page_size(executor: Executor) -> int:
    catalog = executor.catalog
    return catalog.pager.geometry.page_size if catalog.sqlite else 4096


def _set_page_size(executor: Executor, value: object) -> None:
    if executor.catalog.sqlite:  # (MiniDB's own format has 4096-byte pages only)
        executor.catalog.pager.set_page_size(int32(value))


def _auto_vacuum(executor: Executor) -> int:
    catalog = executor.catalog
    return catalog.pager.auto_vacuum if catalog.sqlite else 0


def auto_vacuum_mode(value: object) -> int:
    """SQLite's getAutoVacuum: NONE / FULL / INCREMENTAL or 0-2 (anything else is 0)."""
    text = ascii_lower(str(value))
    if text in ("none", "full", "incremental"):
        return ("none", "full", "incremental").index(text)
    number = int32(value)
    return number if 0 <= number <= 2 else 0


def _set_auto_vacuum(executor: Executor, value: object) -> None:
    if executor.catalog.sqlite:  # (MiniDB's own format has no auto_vacuum)
        executor.catalog.pager.set_auto_vacuum(auto_vacuum_mode(value))


def incremental_vacuum(executor: Executor, arg: object) -> list[tuple]:
    """Up to ``arg`` steps (all, if none or not positive), one empty row each."""
    catalog = executor.catalog
    if not catalog.sqlite or not catalog.pager.auto_vacuum:
        return []
    limit = int32(arg) if arg is not None else 0
    steps = catalog.pager.vacuum_pages(limit if limit > 0 else 0x7FFFFFFF)
    return [()] * steps


def _setting(name: str) -> Callable[[Executor], object]:
    return lambda executor: executor.settings[name]


PLAN_SETTINGS = frozenset(("foreign_keys", "defer_foreign_keys", "recursive_triggers"))


def _set_flag(name: str) -> Callable[[Executor, object], None]:
    def write(executor: Executor, value: object) -> None:
        if name == "foreign_keys" and executor.in_transaction():
            return  # (SQLite ignores it inside a transaction)
        if executor.settings[name] != boolean(value) and name in PLAN_SETTINGS:
            executor.catalog.version += 1  # (compiled plans depend on it: SQLite expires its statements)
        executor.settings[name] = boolean(value)
    return write


def _set_number(name: str) -> Callable[[Executor, object], None]:
    def write(executor: Executor, value: object) -> None:
        executor.settings[name] = int32(value)
    return write


def _journal_mode(executor: Executor) -> str:
    if executor.catalog.sqlite:
        return "memory" if executor.catalog.pager.path is None else "delete"
    return "memory" if executor.catalog.pager.path is None else "wal"


def _page_count(executor: Executor) -> int:
    return executor.catalog.pager.page_count


def _freelist_count(executor: Executor) -> int:
    return executor.catalog.pager.free_page_count()


def _database_list(executor: Executor, _arg: object = None) -> list[tuple]:
    path = executor.catalog.pager.path
    rows = [(0, "main", os.path.abspath(path) if path else "")]
    return rows if executor.catalog.temp is None else rows + [(1, "temp", "")]


class Spec:
    """A pragma: its result columns; ``report(executor, arg)`` -> rows for a
    pragma that reports (``arg``: "required", "optional" or None); ``get`` /
    ``put`` for one that reads or sets a value."""

    def __init__(self, columns: list[str], report: Callable | None = None, arg: str | None = None,
                 get: Callable | None = None, put: Callable | None = None, writes: bool = False,
                 returns_on_set: bool = False) -> None:
        self.columns = columns
        self.report = report
        self.arg = arg
        self.get = get
        self.put = put
        self.writes = writes  # setting it changes the database file
        self.returns_on_set = returns_on_set

    def rows(self, executor: Executor, arg: object) -> list[tuple]:
        if self.report is not None:
            if self.arg == "required" and arg is None:
                return []
            return self.report(executor, arg) if self.arg else self.report(executor)
        return [(self.get(executor),)]


def _value(name: str, get: Callable, put: Callable | None = None, **options: Any) -> Spec:
    return Spec([name], get=get, put=put, **options)


PRAGMAS = {
    "table_info": Spec(["cid", "name", "type", "notnull", "dflt_value", "pk"], table_info, "required"),
    "table_xinfo": Spec(["cid", "name", "type", "notnull", "dflt_value", "pk", "hidden"],
                        lambda e, a: table_info(e, a, True), "required"),
    "index_list": Spec(["seq", "name", "unique", "origin", "partial"], index_list, "required"),
    "index_info": Spec(["seqno", "cid", "name"], index_info, "required"),
    "index_xinfo": Spec(["seqno", "cid", "name", "desc", "coll", "key"],
                        lambda e, a: index_info(e, a, True), "required"),
    "foreign_key_list": Spec(["id", "seq", "table", "from", "to", "on_update", "on_delete", "match"],
                             foreign_key_list, "required"),
    "foreign_key_check": Spec(["table", "rowid", "parent", "fkid"],
                              lambda e, a: e.foreign_key_violations(a), "optional"),
    "integrity_check": Spec(["integrity_check"], integrity_check, "optional"),
    "quick_check": Spec(["quick_check"], lambda e, a: integrity_check(e, a, True), "optional"),
    "table_list": Spec(["schema", "name", "type", "ncol", "wr", "strict"], table_list, "optional"),
    "database_list": Spec(["seq", "name", "file"], _database_list, "optional"),
    "collation_list": Spec(["seq", "name"], lambda e: [(0, "RTRIM"), (1, "NOCASE"), (2, "BINARY")]),
    "user_version": _value("user_version", _header_value("user_version"), _set_header("user_version"), writes=True),
    "application_id": _value("application_id", _header_value("application_id"), _set_header("application_id"),
                             writes=True),
    "schema_version": _value("schema_version", _header_value("schema_version"), _set_header("schema_version"),
                             writes=True),
    "page_size": _value("page_size", _page_size, _set_page_size, writes=True),
    "auto_vacuum": _value("auto_vacuum", _auto_vacuum, _set_auto_vacuum, writes=True),
    "incremental_vacuum": Spec([], incremental_vacuum, "optional", writes=True),
    "page_count": _value("page_count", _page_count),
    "freelist_count": _value("freelist_count", _freelist_count),
    "journal_mode": _value("journal_mode", _journal_mode, lambda e, v: None, returns_on_set=True),
    "encoding": _value("encoding", lambda e: "UTF-8", lambda e, v: None),
    "foreign_keys": _value("foreign_keys", _setting("foreign_keys"), _set_flag("foreign_keys")),
    "defer_foreign_keys": _value("defer_foreign_keys", _setting("defer_foreign_keys"), _set_flag("defer_foreign_keys")),
    "ignore_check_constraints": _value("ignore_check_constraints", _setting("ignore_check_constraints"),
                                       _set_flag("ignore_check_constraints")),
    "recursive_triggers": _value("recursive_triggers", _setting("recursive_triggers"), _set_flag("recursive_triggers")),
    "cache_size": _value("cache_size", _setting("cache_size"), _set_number("cache_size")),
    "synchronous": _value("synchronous", _setting("synchronous"), lambda e, v: None),
    "temp_store": _value("temp_store", lambda e: 0, lambda e, v: None),
    "locking_mode": _value("locking_mode", lambda e: "normal", lambda e, v: None, returns_on_set=True),
    "data_version": _value("data_version", lambda e: e.data_version),
    "busy_timeout": _value("busy_timeout", lambda e: int(e.catalog.pager.timeout * 1000)
                           if hasattr(e.catalog.pager, "timeout") else 5000, lambda e, v: None),
}


def check_schema(schema: str | None, quoted: bool = False) -> None:
    if schema is not None and ascii_lower(schema) not in ("main", "temp"):
        raise OperationalError(f"unknown database '{schema}'" if quoted else f"unknown database {schema}")


def run(executor: Executor, name: str, value: object, schema: str | None) -> tuple[list[tuple], list[str]]:
    """Execute ``PRAGMA [schema.]name [= value]``: (rows, column names)."""
    spec = PRAGMAS.get(name)
    if spec is None:
        return [], []
    check_schema(schema)
    executor.pragma_schema = None if schema is None else ascii_lower(schema)
    if schema is not None and ascii_lower(schema) == "temp" and spec.get is not None:
        if spec.writes:
            return ([], []) if value is not None else ([(0,)], spec.columns)
    if spec.get is not None and value is not None:
        if spec.put is None:
            return [], []
        spec.put(executor, value)
        if spec.returns_on_set:
            return [(spec.get(executor),)], spec.columns
        return [], []
    return spec.rows(executor, value), spec.columns


# The pragmas whose SQLite program always reads the database file (it has an
# OP_Transaction): outside a transaction, running one ends an implicit one.
READS_FILE = frozenset((
    "integrity_check", "quick_check", "table_list", "user_version", "application_id", "schema_version",
    "page_count", "freelist_count", "journal_mode", "data_version",
))


def reads_file(executor: Executor, name: str, value: object) -> bool:
    """Whether SQLite's program for ``PRAGMA name [= value]`` reads the
    database file: the ones above, and the reports on a table or index
    that exists (table_info on any name)."""
    if name in READS_FILE:
        return True
    if value is None:
        return False
    if name in ("table_info", "table_xinfo"):
        return True
    if name == "index_list":
        return executor.catalog.has_table(str(value))
    if name in ("index_info", "index_xinfo"):
        return any(ascii_lower(str(value)) in c.indexes for c in executor.catalog.search())
    return False


def is_write(name: str, value: object) -> bool:
    spec = PRAGMAS.get(name)
    return spec is not None and spec.writes and (value is not None or name == "incremental_vacuum")


# ---- pragma_xxx() in FROM ------------------------------------------------------------


def function_spec(name: str) -> Spec | None:
    """The pragma behind a table-valued function name pragma_<name>."""
    if not ascii_lower(name).startswith("pragma_"):
        return None
    spec = PRAGMAS.get(ascii_lower(name)[7:])
    if spec is None or (spec.report is None and spec.get is None):
        return None
    return spec


def argument_domain(executor: Executor, spec: Spec) -> list[str]:
    """Every value the argument of a schema-reporting pragma can usefully have
    (for a call whose argument comes from an earlier table of the FROM clause)."""
    catalog = executor.catalog
    if spec.report in (index_info,) or spec is PRAGMAS["index_xinfo"]:
        return [index.name for index in catalog.all_indexes()]
    return [*(t.name for t in catalog.all_tables()), *(v.name for v in catalog.all_views()),
            "sqlite_schema", "sqlite_master"]


def index_collation_names(index: IndexInfo, written: list[str | None]) -> list[str]:
    """What PRAGMA index_xinfo shows as each column's collation: as written,
    in the index or on the table column, else BINARY."""
    table = index.table
    return [w if w is not None else collation_text(table.columns[p]) for w, p in zip(written, index.positions)]
