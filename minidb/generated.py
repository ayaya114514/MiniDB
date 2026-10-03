"""Generated columns and other per-table helpers of the executor: the
order SQLite computes generated columns in, the compiled function that
fills them, VIRTUAL columns computed when a row is read (``load_row``), and
which columns, triggers and constraints a change involves."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Iterator

from minidb import dates, functions, values
from minidb.catalog import TableInfo, TriggerInfo
from minidb.errors import OperationalError
from minidb.parser import Call, Column, Exists, InSelect, Parameter, Subquery
from minidb.parser import CheckConstraint, is_true_false_name
from minidb.values import SQLValue, ascii_lower
from minidb.record import decode_row
from minidb.expressions import Compiler, Row, Scope, walk


# ---- statements ------------------------------------------------------------------


def load_row(table: TableInfo, rowid: int, record: bytes | list) -> Row:
    """A table's row as queries see it (its values, the VIRTUAL columns
    computed, then its row id) from a stored record (or a SQLite file's row)."""
    row = record if type(record) is list else decode_row(record)  # (SQLite files: a row)
    if table.virtual:
        return expand_virtual(table, row, rowid)
    if len(row) < len(table.columns):  # written before ALTER TABLE ADD COLUMN
        row.extend(table.padding[len(row):])
    if table.rowid_column is not None:
        row[table.rowid_column] = rowid
    row.append(rowid)
    return row


def walk_nodes(node: object) -> Iterator[object]:
    """Every syntax tree node under ``node``, subqueries included."""
    yield node
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from walk_nodes(item)
    elif dataclasses.is_dataclass(node):
        for f in dataclasses.fields(node):
            if f.compare:
                yield from walk_nodes(getattr(node, f.name))


def replace_possible(table: TableInfo, conflict: str | None, rowid_checked: bool,
                     changed: set[int] | None = None, handled: list | None = None) -> bool:
    """Whether a REPLACE could delete rows of ``table`` (a uniqueness
    constraint resolved by REPLACE that the statement checks; an UPDATE that
    changes the row id, or a WITHOUT ROWID table's PRIMARY KEY, rewrites, so
    checks, every index).  ``handled``: the constraints an upsert takes over
    (None among them: all)."""
    handled = handled or []
    if None in handled:
        return False
    if changed is not None and (rowid_checked or (
            table.without_rowid and changed & set(table.pk_index.positions))):
        changed = None
    rowid_checked = rowid_checked and "rowid" not in handled
    indexes = [index for index in table.indexes if index.unique and index not in handled
               and (changed is None or changed & set(index.positions))]
    if conflict is not None:
        return conflict == "REPLACE" and (rowid_checked or bool(indexes))
    if rowid_checked and table.rowid_conflict() == "REPLACE":
        return True
    return any(index.conflict == "REPLACE" for index in indexes)


# What SQLite's resolver refuses in a generated column as non-deterministic
# (the date and time functions are refused only when they read the clock:
# dates.pure_context).
GENERATED_NONDETERMINISTIC = functions.NONDETERMINISTIC | {"CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP"}


def check_generated(table: TableInfo, loops: bool = True) -> None:
    """The errors CREATE TABLE reports for generated columns (SQLite's
    sqlite3EndTable, each expression as its resolver walks it); ``loops``:
    also a loop among VIRTUAL columns (ADD COLUMN does not look)."""
    if not table.generated:
        return
    if len(table.generated) == len(table.columns):
        raise OperationalError("must have at least one non-generated column")  # (SQLite's last word)
    scope = Scope()
    scope.add(table)
    compiler = Compiler(scope)
    for position in table.generated:
        expr = table.columns[position].generated
        for node in walk(expr):
            if isinstance(node, (Subquery, InSelect, Exists)):
                raise OperationalError("subqueries prohibited in generated columns")
            if isinstance(node, Parameter):
                raise OperationalError("parameters prohibited in generated columns")
            if isinstance(node, Column):
                if node.table is not None:
                    if ascii_lower(node.table) != ascii_lower(table.name):
                        raise OperationalError(f"no such column: {node.table}.{node.name}")
                    raise OperationalError('the "." operator prohibited in generated columns')
                if table.column_index(node.name) is None and not is_true_false_name(node):
                    raise OperationalError(f"no such column: {node.name}")
            if isinstance(node, Call) and node.name in GENERATED_NONDETERMINISTIC:
                raise OperationalError("non-deterministic functions prohibited in generated columns")
        compiler.compile(expr)  # (aggregates and window functions)
    if loops:
        generated_order(table, table.virtual)


def generated_order(table: TableInfo, pending: list[int]) -> list[int]:
    """The order to compute the generated columns ``pending`` in, as SQLite's
    sqlite3ComputeGeneratedColumns finds it: passes over the columns, each
    computing those whose expression uses no column still to be computed; a
    pass that computes none is a loop, reported on the last it put off."""
    uses = {p: {table.column_index(n.name) for n in walk(table.columns[p].generated) if isinstance(n, Column)}
            for p in pending}
    waiting = set(pending)
    order = []
    while waiting:
        last, progress = None, False
        for position in pending:
            if position not in waiting:
                continue
            if uses[position] & waiting:
                last = position
            else:
                order.append(position)
                waiting.discard(position)
                progress = True
        if not progress:
            raise OperationalError(f'generated column loop on "{table.columns[last].name}"')
    return order


def compile_generated(table: TableInfo, positions: list[int]) -> Callable[[Row], None]:
    """A function computing the generated columns at ``positions`` (in that
    order) of a row in place, each with its column's affinity, as SQLite's
    registers hold them: with the JSON subtype, a whole REAL as an IntReal
    (Executor.stored_row makes the record's values)."""
    scope = Scope()
    scope.add(table)
    compiler = Compiler(scope)
    steps = [(p, compiler.compile(table.columns[p].generated), table.affinities[p]) for p in positions]
    apply, int_real, real = values.apply_affinity, values.int_real, values.REAL

    def fill(row):
        for position, function, affinity in steps:
            value = apply(function(row), affinity)
            row[position] = int_real(value) if affinity == real else value  # (SQLite's OP_Affinity)

    if not any(isinstance(n, Call) and n.name in dates.DATE_FUNCTIONS
               for p in positions for n in walk(table.columns[p].generated)):
        return fill

    def checked(row):
        context = dates.pure_context
        saved, context[0] = context[0], "a generated column"
        try:
            fill(row)
        finally:
            context[0] = saved
    return checked


def expand_virtual(table: TableInfo, stored: list, rowid: int, fixed: dict[int, SQLValue] | None = None) -> Row:
    """The row (with its row id) of a record of a table with VIRTUAL
    columns: the stored values in their places, the others computed
    (but for those ``fixed`` gives)."""
    storage = table.storage
    if len(stored) < len(storage):  # written before ALTER TABLE ADD COLUMN
        stored.extend(table.padding[p] for p in storage[len(stored):])
    row = [None] * len(table.columns)
    for position, value in zip(storage, stored):
        row[position] = value
    if table.rowid_column is not None:
        row[table.rowid_column] = rowid
    if fixed:
        for position, value in fixed.items():
            row[position] = value
        key = frozenset(fixed)
        cache = table.fill_virtual_except
        fill = cache.get(key)
        if fill is None:
            fill = cache[key] = compile_generated(
                table, generated_order(table, [p for p in table.virtual if p not in key]))
    else:
        fill = table.fill_virtual
        if fill is None:
            fill = table.fill_virtual = compile_generated(table, generated_order(table, table.virtual))
    fill(row)
    row.append(rowid)
    return row


def unused_virtual(table: TableInfo, used: set[int]) -> dict[int, None]:
    """The VIRTUAL columns that neither the columns ``used`` are nor need."""
    needed = set(used)
    while True:
        more = {table.column_index(n.name) for p in needed if p in table.virtual
                for n in walk(table.columns[p].generated) if isinstance(n, Column)} - needed
        if not more:
            break
        needed |= more
    return {p: None for p in table.virtual if p not in needed}


def new_columns_used(table: TableInfo, triggers: list[TriggerInfo]) -> set[int] | None:
    """The columns the triggers' programs name as new.x (None: all, a
    column past the 32nd among them, as SQLite's mask)."""
    used = set()
    for trigger in triggers:
        for node in walk_nodes([trigger.stmt.when, trigger.stmt.body]):
            if isinstance(node, Column) and node.table is not None and ascii_lower(node.table) == "new":
                position = table.column_index(node.name)
                if position is not None:
                    if position >= 32:
                        return None
                    used.add(position)
    return used


def trigger_names(table: TableInfo, changed: Iterable[int]) -> list[str]:
    """The names an UPDATE setting ``changed`` (the row id as len(columns))
    matches UPDATE OF triggers by: not those of the generated columns that
    change with them."""
    width = len(table.columns)
    return [table.columns[p].name if p < width else "rowid" for p in changed
            if p >= width or table.columns[p].generated is None]


def generated_dependents(table: TableInfo, changed: set[int]) -> set[int]:
    """The generated columns whose value changes when the columns ``changed``
    do, directly or through other generated columns (update.c's aXRef)."""
    found = set()
    progress = True
    while progress:
        progress = False
        for position in table.generated:
            if position in found or position in changed:
                continue
            if any(isinstance(n, Column) and table.column_index(n.name) in changed | found
                   for n in walk(table.columns[position].generated)):
                found.add(position)
                progress = True
    return found


def check_positions(table: TableInfo, check: CheckConstraint) -> set[int]:
    """The columns a CHECK constraint uses (the row id as len(columns))."""
    positions = set()
    for node in walk(check.expr):
        if isinstance(node, Column):
            position = table.column_index(node.name)
            if position is None or position == table.rowid_column:
                position = len(table.columns)
            positions.add(position)
    return positions
