"""Query execution.

Expressions are compiled into Python closures that take a *row*: a list
holding, for every table in the query, its column values followed by its row
id.  A ``Scope`` maps column names to positions in that list.  Scopes of
subqueries have their enclosing query's scope as parent; a reference to an
outer column reads the outer row that the subquery was invoked with.

For each table the planner looks at the WHERE conjuncts and picks an access
path: a lookup or range scan on the row id when a conjunct constrains the
INTEGER PRIMARY KEY (or ``rowid``), an index search, otherwise a full scan.
The access path only narrows the candidate rows; the complete WHERE clause is
still applied to every candidate, so planning can never change a result.

A SELECT is compiled once into a ``CompiledSelect`` (or ``CompiledCompound``)
whose ``run()`` can be called many times: a correlated subquery runs once per
row of its outer query.
"""

from __future__ import annotations

import contextlib
import dataclasses
import heapq
import itertools
import math
import os
import random
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from operator import itemgetter
from typing import Any, Protocol, Union

from minidb import dates, functions, jsonfuncs, pragmas, values, window
from minidb.foreign_keys import ForeignKeys, Link
from minidb.triggers import Program, TriggerIgnore, Triggers, raise_error
from minidb.btree import BTree, IntKey
from minidb.catalog import (
    HIGH, Catalog, IndexInfo, IndexKeyCodec, TableInfo, TriggerInfo, ViewInfo,
    constant_default,
    is_constant_default, quote,
)
from minidb.errors import Error, IntegrityError, NotSupportedError, OperationalError
from minidb.parser import (
    AlterTable, Analyze, Between, Binary, Call, Case, Cast, Collate, Column, Compound, CreateIndex, CreateTable,
    CreateTrigger, CreateView, Cte, Delete, DerivedTable, DropIndex, DropTable, DropTrigger, DropView, Exists,
    Explain, InList, Raise,
    InSelect, Insert, Join, Like, Literal, Parameter, Pragma, Reindex, Select, SelectItem, Star, Subquery,
    TableFunction, TableRef, Unary, Update, Upsert, Vacuum, Values, Frame, WindowDef,
)
from minidb.parser import (
    CheckConstraint, ColumnDef, Expr, ForeignKey, KeyConstraint, Statement, is_true_false_name, parse,
)
from minidb.tokenizer import tokenize
from minidb.jsonb import JSONBlob, JSONText
from minidb.values import IntReal
from minidb.values import SQLValue, ascii_lower, ascii_upper
from minidb.record import decode_record, decode_row, encode_record
from minidb.pager import Pager
from minidb.sqlite_pager import SqlitePager

ROWID_NAMES = ("rowid", "oid", "_rowid_")
# Functions that read the connection's state: name -> Executor attribute.
CONNECTION_FUNCTIONS = {
    "LAST_INSERT_ROWID": "last_insert_rowid", "CHANGES": "changes", "TOTAL_CHANGES": "total_changes",
}

Row = list  # the values of every table of a query, each followed by its row id
RowFunction = Callable[[Row], Any]  # a compiled expression
Record = tuple[tuple, tuple]  # (result row, extra ORDER BY values)
OrderTerm = tuple[str, int, bool, bool, Union[str, None]]  # (source, index, descending, NULLs first, collation)
Bound = Union[tuple[RowFunction, bool], None]  # (key function, inclusive)
Source = Union[TableInfo, "DerivedSource"]  # a table or a subquery in FROM
CompiledQuery = Union["CompiledSelect", "CompiledCompound"]
PreparedStatement = Union["PreparedSelect", "PreparedInsert", "PreparedUpdate", "PreparedDelete"]


class AccessPath(Protocol):
    """How one table of a query is read (see the access path classes)."""

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]: ...

    def order(self) -> tuple[list[int], set[int]] | None: ...

    def estimate(self) -> tuple[float, float]: ...

    def describe(self) -> str: ...


class Result(list):
    """Rows (a list of tuples) plus the result column names.

    ``rowcount`` is the number of rows an INSERT, UPDATE or DELETE changed
    (-1 for other statements), as in sqlite3."""

    def __init__(self, rows: Iterable[tuple] = (), columns: Iterable[str] = (), rowcount: int = -1) -> None:
        super().__init__(rows)
        self.columns = list(columns)
        self.rowcount = rowcount


# ---- name resolution --------------------------------------------------------


class AliasReference(OperationalError):
    """Scope.resolve found a column name to be the alias of a result column of
    an enclosing query (SQLite resolves those in WHERE, ON, GROUP BY, HAVING
    and ORDER BY, subqueries there included)."""

    def __init__(self, item: SelectItem, depth: int) -> None:
        super().__init__(f"no such column: {item.alias}")
        self.item = item
        self.depth = depth


class NeedsAggregate(Exception):
    """A subquery in the result columns of the query owning ``scope`` has an
    aggregate of that query: CompiledSelect compiles it again as an
    aggregate query."""

    def __init__(self, scope: Scope) -> None:
        super().__init__()
        self.scope = scope


class ScopeEntry:
    """One table (or derived table) of a FROM clause."""

    __slots__ = ("name", "table", "offset", "hidden", "using", "hint")

    def __init__(self, name: str, table: Source, offset: int) -> None:
        self.name = name  # alias or table name, lower case
        self.table = table
        self.offset = offset  # position of its first column in a row
        self.hidden = set()  # columns only reachable when qualified (see Executor.using_condition)
        self.using = set()  # the columns of its own USING / NATURAL join (left out of *)
        self.hint = None  # NOT_INDEXED, or the IndexInfo of INDEXED BY

    def indexes(self) -> list[IndexInfo]:
        """The indexes the planner may use for this table."""
        if self.hint is None:
            return self.table.indexes
        return [] if self.hint is NOT_INDEXED else [self.hint]


NOT_INDEXED = object()  # ScopeEntry.hint of a table marked NOT INDEXED


class Merge:
    """A USING column of a FULL JOIN: unqualified, it is the first non-NULL
    of the joined tables' columns (with the first one's affinity, as SQLite
    does), kept in a slot of its own."""

    __slots__ = ("slot", "index", "parts", "affinity")

    def __init__(self, slot: int, index: int, parts: list[int], affinity: str | None) -> None:
        self.slot = slot
        self.index = index  # the FULL JOIN's table, where the slot is set
        self.parts = parts  # slots of the columns, in order
        self.affinity = affinity


class Scope:
    """The tables visible to expressions and where their values sit in a row."""

    def __init__(self, parent: Scope | None = None) -> None:
        self.entries = []
        self.width = 0
        self.parent = parent  # scope of the enclosing query, for correlated subqueries
        self.cell = [None]  # the row of this scope while one of its subqueries runs
        self.uses_outer = False  # some expression here refers to an enclosing query
        self.used = set()  # (table index, column position) pairs referenced so far
        self.last_right = -1  # the index of the last RIGHT or FULL JOIN's table
        self.merged = {}  # lower case name -> Merge
        self.aliases = {}  # lower case alias -> SelectItem, once the result columns are compiled
        self.aggregates = None  # the AggregateCollector of an aggregate query
        self.phase = None  # the clause of the query being compiled: "outputs", "where", "group", ...
        self.in_aggregate = False  # compiling the arguments of one of its aggregate calls
        self.watch = None  # a set to collect the indexes of the tables references resolve to
        self.has_windows = lambda: False  # whether its query has window functions

    def add(self, table: Source, alias: str | None = None) -> None:
        name = ascii_lower(alias if alias is not None else table.name)
        self.entries.append(ScopeEntry(name, table, self.width))
        self.width += len(table.columns) + 1

    def add_merge(self, name: str, index: int, parts: list[int], affinity: str | None) -> None:
        self.merged[ascii_lower(name)] = Merge(self.width, index, parts, affinity)
        self.width += 1

    def rowid_slot(self, index: int) -> int:
        entry = self.entries[index]
        return entry.offset + len(entry.table.columns)

    def _matches(self, column: Column) -> list[tuple[int, str | None, int]]:
        matches = []
        lowered = ascii_lower(column.name)
        if column.table is None and lowered in self.merged:
            merge = self.merged[lowered]
            matches.append((merge.slot, merge.affinity, merge.index))
        for index, entry in enumerate(self.entries):
            if column.table is not None:
                if ascii_lower(column.table) != entry.name:
                    continue
            elif lowered in entry.hidden:
                continue
            table = entry.table
            position = table.column_index(column.name)
            if position is not None:
                matches.append((entry.offset + position, table.affinities[position], index))
            elif lowered in ROWID_NAMES and table.has_rowid:
                matches.append((entry.offset + len(table.columns), values.INTEGER, index))
        return matches

    def resolve(self, column: Column) -> tuple[int, str | None, int, int]:
        """Return (slot, affinity, table index, depth) for a column reference;
        ``depth`` counts how many enclosing queries up the column was found."""
        scope, depth, passed = self, 0, []
        while scope is not None:
            matches = scope._matches(column)
            if len(matches) > 1:
                raise OperationalError(f"ambiguous column name: {column.name}")
            if not matches and depth and column.table is None and ascii_lower(column.name) in scope.aliases:
                for inner in passed:
                    inner.uses_outer = True
                raise AliasReference(scope.aliases[ascii_lower(column.name)], depth)
            if matches:
                for inner in passed:
                    inner.uses_outer = True
                slot, _, index = matches[0]
                if column.table is not None or ascii_lower(column.name) not in scope.merged:
                    scope.used.add((index, slot - scope.entries[index].offset))
                if scope.watch is not None:
                    scope.watch.add(index)
                return (*matches[0], depth)
            passed.append(scope)
            scope, depth = scope.parent, depth + 1
        full_name = f"{column.table}.{column.name}" if column.table else column.name
        raise OperationalError(f"no such column: {full_name}")

    def ancestor(self, depth: int) -> Scope:
        scope = self
        for _ in range(depth):
            scope = scope.parent
        return scope

    def star_columns(self, table_name: str | None = None) -> list[tuple[str | None, str]]:
        """(table name, column name) pairs that ``*`` or ``table.*`` expands to.

        As in SQLite, a column of a table left of a RIGHT or FULL JOIN that a
        later USING joins on is taken by its unqualified name (table None),
        which may mean another table's column or a Merge."""
        if not self.entries:
            raise OperationalError("no tables specified")
        result = []
        found = False
        for i, entry in enumerate(self.entries):
            if table_name is not None and ascii_lower(table_name) != entry.name:
                continue
            found = True
            hidden = getattr(entry.table, "hidden_columns", ())
            for column in entry.table.columns:
                lowered = ascii_lower(column.name)
                if (table_name is None and lowered in entry.using) or lowered in hidden:
                    continue
                if i < self.last_right and any(lowered in e.using for e in self.entries[i + 1:]):
                    result.append((None, column.name))
                else:
                    result.append((entry.name, column.name))
        if not found:
            raise OperationalError(f"no such table: {table_name}")
        return result


def walk(expr: Expr) -> Iterator[Expr]:
    """Yield ``expr`` and all of its sub-expressions (not entering subqueries)."""
    yield expr
    if isinstance(expr, Unary):
        yield from walk(expr.operand)
    elif isinstance(expr, Binary):
        yield from walk(expr.left)
        yield from walk(expr.right)
    elif isinstance(expr, Between):
        for part in (expr.expr, expr.low, expr.high):
            yield from walk(part)
    elif isinstance(expr, InList):
        yield from walk(expr.expr)
        for item in expr.items:
            yield from walk(item)
    elif isinstance(expr, InSelect):
        yield from walk(expr.expr)
    elif isinstance(expr, Like):
        yield from walk(expr.expr)
        yield from walk(expr.pattern)
    elif isinstance(expr, Call):
        for arg in expr.args:
            yield from walk(arg)
        if expr.filter is not None:
            yield from walk(expr.filter)
        if isinstance(expr.over, WindowDef):
            for part in expr.over.partition:
                yield from walk(part)
            for item, _, _ in expr.over.order_by:
                yield from walk(item)
            frame = expr.over.frame
            for offset in (frame.start_offset, frame.end_offset) if frame is not None else ():
                if offset is not None:
                    yield from walk(offset)
    elif isinstance(expr, (Cast, Collate)):
        yield from walk(expr.expr)
    elif isinstance(expr, Case):
        if expr.base is not None:
            yield from walk(expr.base)
        for condition, result in expr.whens:
            yield from walk(condition)
            yield from walk(result)
        if expr.else_ is not None:
            yield from walk(expr.else_)


def strip_collate(expr: Expr) -> Expr:
    while isinstance(expr, Collate):
        expr = expr.expr
    return expr


def has_collate(expr: Expr) -> bool:
    """Whether an explicit COLLATE is part of ``expr`` (SQLite's EP_Collate),
    not counting subqueries."""
    return any(isinstance(node, Collate) for node in walk(expr))


def collation_children(expr: Expr) -> list[Expr]:
    """The operands sqlite3ExprCollSeq looks into, in its order: the left
    operand, the list (arguments, IN items, BETWEEN bounds, CASE parts), the
    right operand.  (LIKE / GLOB are calls whose first argument is the pattern.)"""
    if isinstance(expr, Binary):
        return [expr.left, expr.right]
    if isinstance(expr, Unary):
        return [expr.operand]
    if isinstance(expr, Between):
        return [expr.expr, expr.low, expr.high]
    if isinstance(expr, InList):
        return [expr.expr, *expr.items]
    if isinstance(expr, InSelect):
        return [expr.expr]
    if isinstance(expr, Like):
        return [p for p in (expr.pattern, expr.expr, expr.escape) if p is not None]
    if isinstance(expr, Call):
        return list(expr.args)
    if isinstance(expr, Case):
        parts = [] if expr.base is None else [expr.base]
        parts += [part for when in expr.whens for part in when]
        return parts + ([] if expr.else_ is None else [expr.else_])
    return []


def is_true_false(expr: Column) -> bool:
    """Whether a column name that did not resolve is TRUE or FALSE (which
    SQLite takes as 1 and 0 unless there is such a column)."""
    return expr.table is None and ascii_lower(expr.name) in ("true", "false")


def tables_referenced(expr: Expr, scope: Scope) -> set[int]:
    """Indexes of the tables of ``scope`` that ``expr`` uses.  An expression
    with a subquery counts as using all of them (it may be correlated)."""
    tables = set()
    for e in walk(expr):
        if isinstance(e, Column):
            try:
                _, _, index, depth = scope.resolve(e)
            except AliasReference:
                continue  # an enclosing query's result column
            except OperationalError:
                if is_true_false(e):
                    continue
                raise
            if depth == 0:
                tables.add(index)
        elif isinstance(e, (Subquery, InSelect, Exists)):
            return set(range(len(scope.entries)))
    return tables


_COMPOUND_EXPRESSIONS = (Unary, Binary, Between, InList, InSelect, Like, Call, Cast, Case, Collate)


def substitute_columns(expr: object, replace: Callable[[Column], Expr | None]) -> object:
    """``expr`` with each Column for which ``replace`` returns an expression
    replaced by it (not inside subqueries); unchanged parts are shared."""
    if isinstance(expr, Column):
        new = replace(expr)
        return expr if new is None else new
    if isinstance(expr, tuple):
        new = tuple(substitute_columns(e, replace) for e in expr)
        return expr if all(a is b for a, b in zip(new, expr)) else new
    if isinstance(expr, _COMPOUND_EXPRESSIONS):
        changes = {}
        for f in dataclasses.fields(expr):
            if f.name != "query":
                value = getattr(expr, f.name)
                new = substitute_columns(value, replace)
                if new is not value:
                    changes[f.name] = new
        return dataclasses.replace(expr, **changes) if changes else expr
    return expr


def split_conjuncts(expr: Expr | None) -> list[Expr]:
    if expr is None:
        return []
    if isinstance(expr, Binary) and expr.op == "AND":
        return split_conjuncts(expr.left) + split_conjuncts(expr.right)
    return [expr]


# ---- expression compilation ---------------------------------------------------

_TESTS = {
    "=": lambda c: c == 0,
    "!=": lambda c: c != 0,
    "<": lambda c: c < 0,
    "<=": lambda c: c <= 0,
    ">": lambda c: c > 0,
    ">=": lambda c: c >= 0,
}

_ARITHMETIC = {
    "+": values.add,
    "-": values.subtract,
    "*": values.multiply,
    "/": values.divide,
    "%": values.remainder,
    "||": values.concat,
    "&": values.bit_and,
    "|": values.bit_or,
    "<<": values.shift_left,
    ">>": values.shift_right,
}

# Conversions for comparisons: the numeric affinities all convert text to numbers.
_AFFINITY_FUNCTIONS = {
    values.INTEGER: values.numeric_affinity, values.REAL: values.numeric_affinity,
    values.NUMERIC: values.numeric_affinity, values.TEXT: values.text_affinity,
}

_FLIPPED = {"=": "=", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


def value_comparator(op: str, left_affinity: str | None, right_affinity: str | None,
                     collation: str | None = None) -> Callable[[SQLValue, SQLValue], int | None]:
    """A function (a, b) -> 1, 0 or None comparing two values with SQLite's
    rules: the comparison affinity applies to both operands, TEXT only when
    one of them is text (as in SQLite's OP_Eq and friends); two texts
    compare by the collation."""
    affinity = values.comparison_affinity(left_affinity, right_affinity)
    compare = values.collation_compare(collation)
    if affinity in values.NUMERIC_AFFINITIES:
        numeric = values.numeric_affinity

        def order(a, b):
            if isinstance(a, str):
                a = numeric(a)
            if isinstance(b, str):
                b = numeric(b)
            return compare(a, b)
    elif affinity == values.TEXT:
        text = values.text_affinity

        def order(a, b):
            if isinstance(a, str) or isinstance(b, str):
                return compare(text(a), text(b))
            return compare(a, b)
    else:
        order = compare
    if op in ("IS", "IS NOT"):
        want = op == "IS"

        def is_test(a, b):
            if a is None or b is None:
                return int((a is None and b is None) == want)
            return int((order(a, b) == 0) == want)

        return is_test
    test = _TESTS[op]

    def comparator(a, b):
        if a is None or b is None:
            return None
        return int(test(order(a, b)))

    return comparator


def tuple_function(functions: list[RowFunction]) -> Callable[[Row], tuple]:
    """A function row -> the tuple of the functions' values."""
    if not functions:
        return lambda row: ()
    source = _Source()
    parts = [f"{source.value(f)}(row)" for f in functions]
    return source.function(f"({', '.join(parts)},)")


_PYTHON_COMPARISONS = {"=": "==", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
_NUMBER_TYPES = (int, float)
_CODE_CACHE = {}  # source text -> code object: statements of the same shape compile once


class _Source:
    """The values that generated source refers to (by name) and fresh
    names for its temporaries."""

    def __init__(self) -> None:
        self.env = {}
        self.names = 0

    def value(self, value: object) -> str:
        """A name for ``value``.  (Constants, too, are not written into the
        source, which then depends only on the shape of the expression.)"""
        if value is None:
            return "None"
        name = f"_k{len(self.env)}"
        self.env[name] = value
        return name

    def name(self) -> str:
        self.names += 1
        return f"_v{self.names}"

    def truth(self, operand: str) -> str:
        """values.truth(operand): None, or whether the number is not 0."""
        t = self.name()
        return (f"(None if ({t} := {operand}) is None else ({t} != 0 if type({t}) is int "
                f"else {self.value(values.truth)}({t})))")

    def function(self, text: str) -> RowFunction:
        if text.startswith("_k") and text.endswith("(row)") and text[2:-5].isdigit():
            return self.env[text[:-5]]  # a single closure: no need to wrap it
        code = _CODE_CACHE.get(text)
        if code is None:
            if len(_CODE_CACHE) > 4096:
                _CODE_CACHE.clear()
            code = _CODE_CACHE[text] = compile("lambda row: " + text, "<expression>", "eval")
        return eval(code, self.env)


class Compiler:
    """Compiles expression trees into closures ``fn(row) -> value``.

    With an ``AggregateCollector`` the compiler accepts aggregate function
    calls: each becomes a lookup of the aggregate's result, which the executor
    appends to the group's representative row.  Without one, an aggregate
    call is an error reported with ``misuse`` (formatted with the name).

    An aggregate whose arguments use only columns of an enclosing query
    belongs to that query (as in SQLite): it is collected there, and read
    from that query's current group row.  ``allow_aggregates`` says whether
    such a call may appear here at all (SQLite's NC_AllowAgg).
    """

    def __init__(self, scope: Scope, aggregates: AggregateCollector | None = None, misuse: str = "misuse of aggregate function {name}()", executor: Executor | None = None, allow_aggregates: bool | None = None, windows: WindowCollector | None = None) -> None:
        self.scope = scope
        self.aggregates = aggregates
        self.misuse = misuse
        self.executor = executor  # needed to compile subqueries
        self.allow_aggregates = aggregates is not None if allow_aggregates is None else allow_aggregates
        self.windows = windows  # where window functions may appear (result columns, ORDER BY)

    def compile(self, expr: Expr) -> RowFunction:
        return self.compile_with_affinity(expr)[0]

    # ---- collations ---------------------------------------------------------

    def collation(self, expr: Expr) -> str | None:
        """The collation of ``expr`` (SQLite's sqlite3ExprCollSeq): its
        COLLATE, a column's own (BINARY if it has none), through CAST and
        unary +; else that of the operand holding an explicit COLLATE, if
        any (None: no collation, which comparisons take as BINARY)."""
        while True:
            if isinstance(expr, Collate):
                return values.collation_name(expr.collation)
            if isinstance(expr, Cast):
                expr = expr.expr
            elif isinstance(expr, Unary) and expr.op == "+":
                expr = expr.operand
            elif isinstance(expr, Column):
                return self.column_collation(expr)
            elif isinstance(expr, Call) and expr.defer_affinity:
                expr = expr.args[0]  # (a RIGHT JOIN's USING coalesce(): its first table's, as SQLite)
            else:
                expr = next((child for child in collation_children(expr) if has_collate(child)), None)
                if expr is None:
                    return None

    def column_collation(self, expr: Column) -> str | None:
        try:
            slot, _, index, depth = self.scope.resolve(expr)
        except AliasReference as reference:
            outer = self.scope.ancestor(reference.depth)
            return Compiler(outer, executor=self.executor).collation(reference.item.expr)
        except OperationalError:
            return None  # TRUE / FALSE
        scope = self.scope.ancestor(depth)
        entry = scope.entries[index]
        position = slot - entry.offset
        if not 0 <= position < len(entry.table.columns):
            merge = next((m for m in scope.merged.values() if m.slot == slot), None)
            if merge is None:
                return None  # the row id: none (SQLite's sqlite3ExprCollSeq for a column numbered -1)
            first = scope.entries[merge.index]  # (a FULL JOIN's USING column: its first table's)
            for e in scope.entries:
                if e.offset <= merge.parts[0] < e.offset + len(e.table.columns):
                    first = e
            entry, position = first, merge.parts[0] - first.offset
        if position == getattr(entry.table, "rowid_column", None):
            return None  # (an INTEGER PRIMARY KEY is the row id)
        collation = entry.table.collations[position]
        if collation is None and (position in getattr(entry.table, "bare", ())
                                  or getattr(entry.table, "uncollated", False)):
            return None  # (a flattened subquery's row id: SQLite puts the column itself in its place)
        return collation or "BINARY"

    def comparison_collation(self, left: Expr, right: Expr) -> str:
        """The collation comparing ``left`` with ``right`` (SQLite's
        sqlite3BinaryCompareCollSeq): an explicit COLLATE on the left, else
        on the right, else the left's collation, else the right's."""
        if has_collate(left):
            collation = self.collation(left)
        elif has_collate(right):
            collation = self.collation(right)
        else:
            collation = self.collation(left) or self.collation(right)
        return collation or "BINARY"

    def compile_with_affinity(self, expr: Expr) -> tuple[RowFunction, str | None]:
        """Return (function, affinity); only column references have an affinity.

        Operators are turned into Python source (see _Source) and compiled
        into one function; other expressions are closures."""
        if isinstance(expr, (Binary, Unary)):
            source = _Source()
            text, affinity = self._source(expr, source, 0)
            return source.function(text), affinity
        return self._closure_with_affinity(expr)

    def compile_tuple(self, exprs: list[Expr]) -> tuple[Callable[[Row], tuple], list[str | None]]:
        """One function computing the tuple of ``exprs`` (result columns),
        and their affinities."""
        source = _Source()
        parts, affinities = [], []
        for expr in exprs:
            text, affinity = self._source(expr, source, 0)
            parts.append(text)
            affinities.append(affinity)
        return source.function(f"({', '.join(parts)}{',' if len(parts) == 1 else ''})"), affinities

    def _source(self, expr: Expr, source: _Source, depth: int) -> tuple[str, str | None]:
        """Python source computing ``expr`` from ``row``, and its affinity.
        The operators have fast paths for integers and numbers; anything
        else calls the same functions as the closures do."""
        if depth > 12 or not isinstance(expr, (Binary, Unary, Literal, Column)):
            function, affinity = self._closure_with_affinity(expr)
            return f"{source.value(function)}(row)", affinity
        if isinstance(expr, Literal):
            return source.value(expr.value), None
        if isinstance(expr, Column):
            try:
                slot, affinity, index, column_depth = self.scope.resolve(expr)
            except OperationalError:
                column_depth = None  # an alias, TRUE / FALSE, or an error: see the closure
            if column_depth != 0:
                function, affinity = self._closure_with_affinity(expr)
                return f"{source.value(function)}(row)", affinity
            hook = self.executor.column_hook if self.executor is not None else None
            if hook is not None:
                hook(expr, self.scope.entries[index].table)
            return f"row[{slot}]", affinity
        depth += 1
        if isinstance(expr, Unary):
            if expr.op == "-" and isinstance(expr.operand, Literal) and type(expr.operand.value) in (int, float):
                return source.value(values.negate(expr.operand.value)), None  # (-0.0 stays -0.0)
            operand, _ = self._source(expr.operand, source, depth)
            if expr.op == "+":
                return operand, None
            a = source.name()
            if expr.op == "-":  # 0 - X, as SQLite (never -0.0)
                return (f"(-{a} if type({a} := {operand}) is int and {a} != {values.INT_MIN} "
                        f"else {source.value(values.subtract)}(0, {a}))"), None
            if expr.op == "~":
                return f"{source.value(values.bit_not)}({operand})", None
            t = source.name()
            return f"(None if ({t} := {source.truth(operand)}) is None else (0 if {t} else 1))", None
        op = expr.op
        if op == "AND" and folded_literal(expr) == Literal(0):
            return "0", None  # see _binary
        test = self._truth_test(expr)
        if test is not None:
            return f"{source.value(test)}(row)", None
        left, left_affinity = self._source(expr.left, source, depth)
        right, right_affinity = self._source(expr.right, source, depth)
        x, y = source.name(), source.name()
        if op == "AND":
            return (f"(0 if ({x} := {source.truth(left)}) is False else "
                    f"(0 if ({y} := {source.truth(right)}) is False else "
                    f"(None if {x} is None or {y} is None else 1)))"), None
        if op == "OR":
            return (f"(1 if ({x} := {source.truth(left)}) else (1 if ({y} := {source.truth(right)}) else "
                    f"(None if {x} is None or {y} is None else 0)))"), None
        both = f"(({x} := {left}), ({y} := {right}))"
        if op in ("+", "-", "*"):
            s_ = source.name()
            function = source.value(_ARITHMETIC[op])
            return (f"({s_} if {both} and type({x}) is int and type({y}) is int and "
                    f"{values.INT_MIN} <= ({s_} := {x} {op} {y}) <= {values.INT_MAX} else {function}({x}, {y}))"), None
        if op in _ARITHMETIC:
            return f"{source.value(_ARITHMETIC[op])}({left}, {right})", None
        collation = self.comparison_collation(expr.left, expr.right)
        comparator = source.value(value_comparator(op, left_affinity, right_affinity, collation))
        if op in _PYTHON_COMPARISONS:
            numbers = source.value(_NUMBER_TYPES)
            return (f"((1 if {x} {_PYTHON_COMPARISONS[op]} {y} else 0) if {both} and type({x}) in {numbers} "
                    f"and type({y}) in {numbers} else {comparator}({x}, {y}))"), None
        return f"{comparator}({left}, {right})", None

    def _closure_with_affinity(self, expr: Expr) -> tuple[RowFunction, str | None]:
        if isinstance(expr, Literal):
            value = expr.value
            return (lambda row: value), None
        if isinstance(expr, Parameter):
            # Read at run time, so a prepared plan works for any bound values.
            parameters, i = self.executor.parameters, expr.index - 1
            return (lambda row: parameters[i]), None
        if isinstance(expr, Column):
            try:
                slot, affinity, index, depth = self.scope.resolve(expr)
            except AliasReference as reference:
                return self._outer_alias(reference)
            except OperationalError:
                if is_true_false(expr):
                    value = int(ascii_lower(expr.name) == "true")
                    return (lambda row: value), None
                raise
            hook = self.executor.column_hook if self.executor is not None else None
            if hook is not None:
                hook(expr, self.scope.ancestor(depth).entries[index].table)
            if depth == 0:
                return itemgetter(slot), affinity
            cell = self.scope.ancestor(depth).cell  # the enclosing query's current row
            return (lambda row: cell[0][slot]), affinity
        if isinstance(expr, Cast):
            return self._cast(expr)
        if isinstance(expr, Collate):
            return self.compile_with_affinity(expr.expr)
        if isinstance(expr, Case):
            return self._case(expr), None
        if isinstance(expr, Subquery):
            return self._scalar_subquery(expr)
        if isinstance(expr, InSelect):
            return self._in_select(expr), None
        if isinstance(expr, Exists):
            return self._exists(expr), None
        if isinstance(expr, Unary):
            return self._unary(expr), None
        if isinstance(expr, Binary):
            return self._binary(expr), None
        if isinstance(expr, Between):
            return self._between(expr), None
        if isinstance(expr, InList):
            return self._in_list(expr), None
        if isinstance(expr, Like):
            return self._like(expr), None
        if isinstance(expr, Call):
            if expr.defer_affinity:
                return self.call(expr), self.compile_with_affinity(expr.args[0])[1]
            return self.call(expr), None
        if isinstance(expr, Raise):
            return self._raise(expr), None
        if isinstance(expr, Star):
            raise OperationalError("* is only allowed in a select list or COUNT(*)")
        raise OperationalError(f"cannot evaluate {expr!r}")

    def _outer_alias(self, reference: AliasReference) -> tuple[RowFunction, str | None]:
        """An enclosing query's result column by its alias: its expression,
        evaluated with that query's current row."""
        if contains_window(reference.item.expr):
            raise OperationalError(f"misuse of aliased window function {reference.item.alias}")
        outer = self.scope.ancestor(reference.depth)
        # (An aliased aggregate works where the query's own aggregates do.)
        aggregates = outer.aggregates if outer.phase in ("having", "order") and not outer.in_aggregate else None
        compiler = Compiler(outer, aggregates, misuse="misuse of aggregate: {name}()", executor=self.executor)
        function, affinity = compiler.compile_with_affinity(reference.item.expr)
        cell = outer.cell
        return (lambda row: function(cell[0])), affinity

    def _unary(self, expr: Unary) -> RowFunction:
        operand = self.compile(expr.operand)
        if expr.op == "-":
            if isinstance(expr.operand, Literal) and type(expr.operand.value) in (int, float):
                value = values.negate(expr.operand.value)  # a negative literal (-0.0 stays -0.0)
                return lambda row: value
            # SQLite computes -X as 0 - X, which never gives -0.0.
            subtract = values.subtract
            return lambda row: subtract(0, operand(row))
        if expr.op == "+":
            return operand
        if expr.op == "~":
            bit_not = values.bit_not
            return lambda row: bit_not(operand(row))
        logical_not = values.logical_not
        return lambda row: logical_not(operand(row))

    def _truth_test(self, expr: Binary) -> RowFunction | None:
        """``x IS [NOT] TRUE`` and ``x IS [NOT] FALSE`` test the truth of x
        (SQLite's TK_TRUTH / OP_IsTrue) - when TRUE / FALSE is not a column
        name.  A NULL x IS TRUE / FALSE is 0, IS NOT TRUE / FALSE is 1.
        Like SQLite (sqlite3ExprSkipCollateAndLikely), the right side may be
        wrapped in COLLATE - not in likely(), which is unresolved (so not yet
        marked as likely()) when SQLite looks."""
        right = strip_collate(expr.right)
        if expr.op not in ("IS", "IS NOT") or not isinstance(right, Column) or not is_true_false(right):
            return None
        try:
            self.scope.resolve(right)
            return None  # a column named TRUE or FALSE
        except AliasReference:
            return None
        except OperationalError:
            pass
        operand = self.compile(expr.left)
        is_true = ascii_lower(right.name) == "true"
        invert = int(is_true != (expr.op == "IS"))
        if_null = int(not is_true)
        truth = values.truth

        def test(row):
            value = truth(operand(row))
            return (if_null if value is None else int(value)) ^ invert
        return test

    def _binary(self, expr: Binary) -> RowFunction:
        op = expr.op
        if op == "AND" and folded_literal(expr) == Literal(0):
            # SQLite's parser replaces this by 0: the operands are never
            # resolved, so e.g. a missing table in a subquery there is no error.
            return lambda row: 0
        test = self._truth_test(expr)
        if test is not None:
            return test
        left, left_affinity = self.compile_with_affinity(expr.left)
        right, right_affinity = self.compile_with_affinity(expr.right)
        truth = values.truth
        if op == "AND":
            def and_(row):
                a = truth(left(row))
                if a is False:
                    return 0
                b = truth(right(row))
                if b is False:
                    return 0
                return None if a is None or b is None else 1
            return and_
        if op == "OR":
            def or_(row):
                a = truth(left(row))
                if a:
                    return 1
                b = truth(right(row))
                if b:
                    return 1
                return None if a is None or b is None else 0
            return or_
        if op in _ARITHMETIC:
            function = _ARITHMETIC[op]
            return lambda row: function(left(row), right(row))
        collation = self.comparison_collation(expr.left, expr.right)
        comparator = value_comparator(op, left_affinity, right_affinity, collation)
        return lambda row: comparator(left(row), right(row))

    def _between(self, expr: Between) -> RowFunction:
        value, affinity = self.compile_with_affinity(expr.expr)
        low, low_affinity = self.compile_with_affinity(expr.low)
        high, high_affinity = self.compile_with_affinity(expr.high)
        at_least = value_comparator(">=", affinity, low_affinity, self.comparison_collation(expr.expr, expr.low))
        at_most = value_comparator("<=", affinity, high_affinity, self.comparison_collation(expr.expr, expr.high))
        logical_and, logical_not = values.logical_and, values.logical_not
        negated = expr.negated

        def between(row):
            v = value(row)
            result = logical_and(at_least(v, low(row)), at_most(v, high(row)))
            return logical_not(result) if negated else result

        return between

    def _in_list(self, expr: InList) -> RowFunction:
        value, affinity = self.compile_with_affinity(expr.expr)
        if not expr.items:  # x IN (): false even when x is NULL, as in SQLite
            result = int(expr.negated)
            return lambda row: result
        items = [self.compile(item) for item in expr.items]
        # SQLite makes x IN (<constant>) x = +<constant>; otherwise the left side's collation applies.
        if len(expr.items) == 1 and is_parse_constant(expr.items[0]):
            collation = self.comparison_collation(expr.expr, expr.items[0])
        else:
            collation = self.collation(expr.expr)
        # Each item is compared as by "=" with the left side's affinity (SQLite's
        # OP_Eq: TEXT converts only when one side is text).
        equal = value_comparator("=", affinity, None, collation)
        found, missing = (0, 1) if expr.negated else (1, 0)

        def in_list(row):
            v = value(row)
            if v is None:
                return None
            saw_null = False
            for item in items:
                candidate = item(row)
                if candidate is None:
                    saw_null = True
                    continue
                if equal(v, candidate) == 1:
                    return found
            return None if saw_null else missing

        return in_list

    def _like(self, expr: Like) -> RowFunction:
        value = self.compile(expr.expr)
        pattern = self.compile(expr.pattern)
        logical_not = values.logical_not
        if expr.op == "GLOB":
            glob = functions.glob
            match = lambda row: glob(pattern(row), value(row))  # noqa: E731
        elif expr.escape is not None:
            escape, like_escape = self.compile(expr.escape), functions.like_escape
            match = lambda row: like_escape(value(row), pattern(row), escape(row))  # noqa: E731
        else:
            like = values.like
            match = lambda row: like(value(row), pattern(row))  # noqa: E731
        if expr.negated:
            return lambda row: logical_not(match(row))
        return match

    def _cast(self, expr: Cast) -> tuple[RowFunction, str | None]:
        target = values.type_affinity(expr.type_name)
        operand = self.compile(expr.expr)
        cast = values.cast
        return (lambda row: cast(operand(row), target)), target

    def _case(self, expr: Case) -> RowFunction:
        whens = []
        truth = values.truth
        if expr.base is None:
            for condition, result in expr.whens:
                whens.append((self.compile(condition), self.compile(result)))
        else:
            base, base_affinity = self.compile_with_affinity(expr.base)
            for when, result in expr.whens:
                value, value_affinity = self.compile_with_affinity(when)
                collation = self.comparison_collation(expr.base, when)
                whens.append(((value, value_comparator("=", base_affinity, value_affinity, collation)),
                              self.compile(result)))
        otherwise = self.compile(expr.else_) if expr.else_ is not None else (lambda row: None)
        if expr.base is None:
            def searched_case(row):
                for condition, result in whens:
                    if truth(condition(row)):
                        return result(row)
                return otherwise(row)
            return searched_case

        def simple_case(row):
            b = base(row)
            for (value, equal), result in whens:
                if equal(b, value(row)) == 1:
                    return result(row)
            return otherwise(row)
        return simple_case

    # ---- subqueries -------------------------------------------------------

    def _subquery(self, query: Select | Compound, columns: int | None = None) -> CompiledQuery:
        """Compile ``query`` as a subquery of this scope; returns a function
        ``rows(outer_row)`` and the compiled query.  Uncorrelated subqueries
        run once; correlated ones run for every outer row."""
        if self.executor is None:
            raise OperationalError("subqueries are not allowed here")
        compiled = self.executor.compile_query(query, parent=self.scope)
        if columns is not None and len(compiled.names) != columns:
            raise OperationalError(
                f"sub-select returns {len(compiled.names)} columns - expected {columns}"
            )
        return compiled

    def _runner(self, compiled: CompiledQuery, transform: Callable[[list[tuple]], Any], max_rows: int | None = None) -> Callable[[Row], Any]:
        """A function outer_row -> transform(rows of the subquery)."""
        cell = self.scope.cell
        if compiled.correlated:
            def run_correlated(row):
                cell[0] = row
                return transform(compiled.run(max_rows))
            return run_correlated
        cache = []
        self.executor.once_caches.append(cache)  # emptied before every execution

        def run_once(row):
            if not cache:
                cache.append(transform(compiled.run(max_rows)))
            return cache[0]
        return run_once

    def _scalar_subquery(self, expr: Subquery) -> tuple[RowFunction, str | None]:
        compiled = self._subquery(expr.query, columns=1)
        run = self._runner(compiled, lambda rows: rows[0][0] if rows else None, max_rows=1)
        return run, compiled.affinities[0]

    def _exists(self, expr: Exists) -> RowFunction:
        compiled = self._subquery(expr.query)
        return self._runner(compiled, lambda rows: int(bool(rows)), max_rows=1)

    def _in_select(self, expr: InSelect) -> RowFunction:
        compiled = self._subquery(expr.query, columns=1)
        value, value_affinity = self.compile_with_affinity(expr.expr)
        affinity = in_select_affinity(value_affinity, compiled.affinities[-1])
        convert = _AFFINITY_FUNCTIONS.get(affinity)
        # The collation comparing x with the subquery's column (SQLite's
        # sqlite3BinaryCompareCollSeq of x and that column's expression).
        right, explicit = compiled.result_collation(0)
        if has_collate(expr.expr):
            collation = self.collation(expr.expr)
        elif explicit:
            collation = right
        else:
            collation = self.collation(expr.expr) or right
        sort_key = values.collation_sort_key(collation)

        def summarize(rows):
            keys, has_null = set(), False
            for (candidate,) in rows:
                if candidate is None:
                    has_null = True
                    continue
                keys.add(sort_key(convert(candidate) if convert else candidate))
            return keys, has_null, bool(rows)

        members = self._runner(compiled, summarize)
        found, missing = (0, 1) if expr.negated else (1, 0)

        def in_select(row):
            keys, has_null, nonempty = members(row)
            if not nonempty:
                return missing  # x IN (empty) is false even for NULL x
            v = value(row)
            if v is None:
                return None
            if sort_key(convert(v) if convert else v) in keys:
                return found
            return None if has_null else missing
        return in_select

    def _raise(self, expr: Raise) -> RowFunction:
        if self.executor is None or not self.executor.compiling_trigger:
            raise OperationalError("RAISE() may only be used within a trigger-program")
        kind = expr.kind
        if kind == "IGNORE":
            def ignore(row):
                raise TriggerIgnore()
            return ignore
        message = self.compile(expr.message)

        def raise_(row):
            text = message(row)
            raise raise_error(kind, "" if text is None else values.to_text(text))
        return raise_

    def call(self, expr: Call) -> RowFunction:
        name = expr.name
        if expr.over is not None:
            return self._window(expr)
        if name in window.WINDOW_FUNCTIONS:
            raise OperationalError(f"misuse of window function {ascii_lower(name)}()")
        if values.is_aggregate_call(name, len(expr.args)):
            return self._aggregate(expr)
        if name in CONNECTION_FUNCTIONS:
            if expr.args:
                raise OperationalError(f"wrong number of arguments to function {ascii_lower(name)}()")
            executor, attribute = self.executor, CONNECTION_FUNCTIONS[name]
            return lambda row: getattr(executor, attribute)
        if name not in functions.SCALAR_FUNCTIONS:
            raise OperationalError(f"no such function: {ascii_lower(name)}")
        function, min_args, max_args = functions.SCALAR_FUNCTIONS[name]
        # DISTINCT means nothing to a scalar function; SQLite ignores it.
        if len(expr.args) < min_args or (max_args is not None and len(expr.args) > max_args):
            raise OperationalError(f"wrong number of arguments to function {ascii_lower(name)}()")
        if expr.filter is not None:
            raise OperationalError(f"FILTER may not be used with non-aggregate {ascii_lower(name)}()")
        if name == "LIKELIHOOD":
            # SQLite wants a floating-point literal (its TK_FLOAT) from 0.0 to 1.0.
            probability = expr.args[1]
            if not (isinstance(probability, Literal) and type(probability.value) is float
                    and 0.0 <= probability.value <= 1.0):
                raise OperationalError("second argument to likelihood() must be a constant between 0.0 and 1.0")
        args = [self.compile(arg) for arg in expr.args]
        if name in values.COLLATING_FUNCTIONS:
            # The first argument with a collation decides how texts compare.
            collation = next((c for c in map(self.collation, expr.args) if c is not None), None)
            if collation not in (None, "BINARY"):
                function = values.COLLATING_FUNCTIONS[name](values.collation_compare(collation))
        if not args:
            return lambda row: function()
        if len(args) == 1:
            (arg,) = args
            return lambda row: function(arg(row))
        return lambda row: function(*[arg(row) for arg in args])

    def aggregate_depth(self, expr: Call) -> int:
        """How many queries up the aggregate call ``expr`` belongs: to the
        innermost one whose columns its arguments use (0 when they use none).
        (Arguments with a subquery are taken to belong here.)"""
        depths = []
        for arg in expr.args:
            for e in walk(arg):
                if isinstance(e, (Subquery, InSelect, Exists)):
                    return 0
                if isinstance(e, Column):
                    try:
                        depths.append(self.scope.resolve(e)[3])
                    except AliasReference as reference:
                        depths.append(reference.depth)
                    except OperationalError:
                        return 0  # reported when compiled
        return min(depths, default=0)

    def _outer_aggregate(self, expr: Call, depth: int) -> RowFunction:
        name = ascii_lower(expr.name)
        if not self.allow_aggregates:
            raise OperationalError(f"misuse of aggregate function {name}()")
        owner = self.scope.ancestor(depth)
        if owner.phase == "group":
            raise OperationalError("aggregate functions are not allowed in the GROUP BY clause")
        if owner.has_windows():
            # (SQLite moves a query with window functions into a subquery, which the aggregate cannot reach)
            raise OperationalError(f"misuse of aggregate: {name}()")
        if owner.aggregates is None:
            if owner.phase == "outputs":
                raise NeedsAggregate(owner)
            raise OperationalError(f"misuse of aggregate: {name}()")
        if owner.phase == "where" or owner.in_aggregate:
            raise OperationalError(f"misuse of aggregate: {name}()")
        scope = self.scope
        for _ in range(depth):  # the queries in between depend on the owner's row
            scope.uses_outer = True
            scope = scope.parent
        function = Compiler(owner, owner.aggregates, executor=self.executor)._aggregate(expr)
        cell = owner.cell
        return lambda row: function(cell[0])

    def _aggregate(self, expr: Call) -> RowFunction:
        name = expr.name
        depth = self.aggregate_depth(expr)
        if depth:
            return self._outer_aggregate(expr, depth)
        if self.aggregates is None:
            raise OperationalError(self.misuse.format(name=ascii_lower(name)))
        _, min_args, max_args = values.AGGREGATE_FUNCTIONS[name]
        star = expr.args == (Star(),)
        if star and name != "COUNT" or not min_args <= len(expr.args) <= max_args:
            raise OperationalError(f"wrong number of arguments to function {ascii_lower(name)}()")
        if expr.distinct and len(expr.args) != 1:
            raise OperationalError("DISTINCT aggregates must have exactly one argument")
        if star or not expr.args:
            args = []  # COUNT(*) and COUNT()
        else:
            inner = Compiler(self.scope, executor=self.executor)  # aggregates may not be nested
            self.scope.in_aggregate = True
            try:
                args = [inner.compile(arg) for arg in expr.args]
            finally:
                self.scope.in_aggregate = False
        filter_ = None
        if expr.filter is not None:
            filter_ = Compiler(self.scope, executor=self.executor).compile(expr.filter)
        # MIN / MAX and DISTINCT compare by the argument's collation.
        collation = None
        if expr.args and not star and (expr.distinct or name in ("MIN", "MAX")):
            collation = self.collation(expr.args[0])
        return itemgetter(self.aggregates.add(name, args, expr.distinct, filter_, collation))

    def _window(self, expr: Call) -> RowFunction:
        """A window function call: its result is read from the row, where
        CompiledSelect puts it (WindowCollector)."""
        name, lowered = expr.name, ascii_lower(expr.name)
        if expr.distinct:
            raise OperationalError("DISTINCT is not supported for window functions")
        builtin = name in window.WINDOW_FUNCTIONS
        if not builtin and not values.is_aggregate_call(name, len(expr.args)):
            if name in functions.SCALAR_FUNCTIONS or name in CONNECTION_FUNCTIONS or name in ("MIN", "MAX"):
                raise OperationalError(f"{lowered}() may not be used as a window function")
            raise OperationalError(f"no such function: {lowered}")
        if self.windows is None:
            raise OperationalError(f"misuse of window function {lowered}()")
        min_args, max_args = window.WINDOW_FUNCTIONS[name] if builtin else values.AGGREGATE_FUNCTIONS[name][1:]
        star = expr.args == (Star(),)
        if star and name != "COUNT" or not star and not min_args <= len(expr.args) <= max_args:
            raise OperationalError(f"wrong number of arguments to function {lowered}()")
        definition = self.windows.resolve(expr.over)
        frame = definition.frame or Frame("RANGE", "UNBOUNDED", None, "CURRENT", None, None)
        if frame.unit == "RANGE" and (frame.start_offset is not None or frame.end_offset is not None) \
                and len(definition.order_by) != 1:
            raise OperationalError("RANGE with offset PRECEDING/FOLLOWING requires one ORDER BY expression")
        if builtin and expr.filter is not None:
            raise OperationalError("FILTER clause may only be used with aggregate window functions")
        if name in window.BUILTIN_FRAMES:
            unit, start, start_offset, end = window.BUILTIN_FRAMES[name]
            frame = Frame(unit, start, None if start_offset is None else Literal(start_offset), end, None, None)
        # As SQLite: an offset that is not a constant is NULL (an error once
        # there is a row).
        frame = dataclasses.replace(
            frame,
            start_offset=frame.start_offset if frame.start_offset is None or is_parse_constant(frame.start_offset)
            else Literal(None),
            end_offset=frame.end_offset if frame.end_offset is None or is_parse_constant(frame.end_offset)
            else Literal(None),
        )
        inner = Compiler(self.scope, self.aggregates, misuse=self.misuse, executor=self.executor,
                         allow_aggregates=self.allow_aggregates)  # no window functions inside
        args = [] if star else [inner.compile(arg) for arg in expr.args]
        filter_ = inner.compile(expr.filter) if expr.filter is not None else None
        collation = inner.collation(expr.args[0]) if name in ("MIN", "MAX") and args else None
        number = self.windows.add(definition, frame, name, args, filter_, inner, collation)
        windows = self.windows
        return lambda row: row[windows.base + number]


def in_select_affinity(left: str | None, right: str | None) -> str | None:
    """Affinity for ``x IN (SELECT y ...)`` (SQLite's sqlite3CompareAffinity):
    both columns: numeric if either is, else none; otherwise whichever exists.
    It is applied to both sides."""
    if left is not None and right is not None:
        numeric = left in values.NUMERIC_AFFINITIES or right in values.NUMERIC_AFFINITIES
        return values.NUMERIC if numeric else None
    return left if left is not None else right


def is_aggregate(e: object) -> bool:
    """An aggregate function call (not a window function)."""
    return isinstance(e, Call) and e.over is None and values.is_aggregate_call(e.name, len(e.args))


def contains_aggregate(expr: Expr) -> bool:
    return any(is_aggregate(e) for e in walk(expr))


def contains_window(expr: Expr) -> bool:
    return any(isinstance(e, Call) and e.over is not None for e in walk(expr))


def owns_aggregate(expr: Expr, scope: Scope) -> bool:
    """Whether ``expr`` (outside its subqueries) has an aggregate call that
    belongs to the query of ``scope`` rather than to an enclosing one."""
    compiler = Compiler(scope)
    return any(is_aggregate(e) and compiler.aggregate_depth(e) == 0 for e in walk(expr))


def is_parse_constant(expr: Expr) -> bool:
    """Whether SQLite's parser sees ``expr`` as a constant (sqlite3ExprIsConstant
    before names are resolved): no columns, function calls or subqueries."""
    for e in walk(expr):
        if isinstance(e, Column):
            if e.table is not None or ascii_lower(e.name) not in ("true", "false"):
                return False
        elif isinstance(e, (Call, Subquery, InSelect, Exists)):
            return False
    return True


class WindowCollector:
    """The window function calls of a query, grouped by window definition,
    and the named windows of its WINDOW clause."""

    def __init__(self, definitions: list[tuple[str, WindowDef]]) -> None:
        # As SQLite's parser: each definition but the first is based on the
        # ones before it; the first keeps its base name, which is ignored.
        # The last definition of a name wins.
        self.named = {}
        for i, (name, definition) in enumerate(definitions):
            self.named[ascii_lower(name)] = self.chain(definition) if i else definition
        self.groups = []  # [((partition, ORDER BY, frame), window.WindowGroup)]
        self.count = 0
        self.base = 0

    def find(self, name: str) -> WindowDef:
        definition = self.named.get(ascii_lower(name))
        if definition is None:
            raise OperationalError(f"no such window: {name}")
        return definition

    def chain(self, definition: WindowDef) -> WindowDef:
        """A definition based on a named window (sqlite3WindowChain)."""
        if definition.base is None:
            return definition
        base = self.find(definition.base)
        base = dataclasses.replace(base, base=None)
        if definition.partition:
            clause = "PARTITION clause"
        elif base.order_by and definition.order_by:
            clause = "ORDER BY clause"
        elif base.frame is not None:
            clause = "frame specification"
        else:
            return WindowDef(None, base.partition, definition.order_by or base.order_by, definition.frame)
        raise OperationalError(f"cannot override {clause} of window: {definition.base}")

    def resolve(self, over: WindowDef | str) -> WindowDef:
        return self.find(over) if isinstance(over, str) else self.chain(over)

    def add(self, definition: WindowDef, frame: Frame, name: str, args: list[RowFunction], filter_: RowFunction | None,
            compiler: Compiler, collation: str | None = None) -> int:
        """Register a call; returns its number.  ``collation``: its first argument's."""
        key = (definition.partition, definition.order_by, frame)
        group = next((g for k, g in self.groups if k == key), None)  # (the key may not be hashable)
        if group is None:
            group = window.WindowGroup(
                [compiler.compile(e) for e in definition.partition],
                [(compiler.compile(e), descending, nulls_first) for e, descending, nulls_first in definition.order_by],
                frame.unit, frame.start,
                None if frame.start_offset is None else compiler.compile(frame.start_offset),
                frame.end,
                None if frame.end_offset is None else compiler.compile(frame.end_offset),
                frame.exclude,
                [compiler.collation(e) for e in definition.partition],
                [compiler.collation(e) for e, _, _ in definition.order_by],
            )
            self.groups.append((key, group))
        number = self.count
        self.count += 1
        group.functions.append(window.WindowFunction(name, args, filter_, number, collation))
        return number

    def apply(self, rows: Iterable[Row]) -> list[Row]:
        """The rows with the window functions' results appended, in the order
        SQLite returns them: the window seen first is computed last (SQLite
        nests the others in subqueries)."""
        padding = [None] * self.count
        rows = [list(row) + padding for row in rows]
        for _, group in reversed(self.groups):
            rows = group.run(rows, self.base)
        return rows


class AggregateCollector:
    """The aggregate calls of a query and their per-group state."""

    def __init__(self, base_width: int) -> None:
        self.base_width = base_width  # aggregate results follow the row's slots
        self.calls = []  # (name, argument functions, distinct, FILTER function or None)
        self.collations = []  # of each call's argument, for MIN / MAX and DISTINCT
        self.loop = None  # the generated grouping loop

    def add(self, name: str, args: list[RowFunction], distinct: bool, filter_: RowFunction | None = None,
            collation: str | None = None) -> int:
        self.calls.append((name, args, distinct, filter_))
        self.collations.append(collation)
        return self.base_width + len(self.calls) - 1

    def new_state(self) -> list[tuple[Any, set | None]]:
        state = []
        for (name, args, distinct, _), collation in zip(self.calls, self.collations):
            if name == "COUNT" and not args:
                aggregate = values.CountStarAggregate()
            elif name in ("MIN", "MAX") and collation not in (None, "BINARY"):
                aggregate = values.MinMaxAggregate(-1 if name == "MIN" else 1, values.collation_compare(collation))
            else:
                aggregate = values.AGGREGATE_FUNCTIONS[name][0]()
            state.append((aggregate, set() if distinct else None))
        return state

    @staticmethod
    def results(state: list[tuple[Any, set | None]]) -> list[SQLValue]:
        return [aggregate.result() for aggregate, _ in state]

    def grouping_loop(self, group_functions: list[RowFunction],
                      group_keys: list[Callable[[SQLValue], tuple]]) -> Callable[[Iterable[Row], dict, Callable], None]:
        """A generated function (rows, groups, new_state) that puts each row in
        its group (key -> [representative row, state]) and steps the
        group's aggregates, as ``step`` does, in straight-line code.
        ``group_keys``: the sort key function of each GROUP BY term (its collation)."""
        if self.loop is not None:
            return self.loop
        env = {"_truth": values.truth}
        for i, sort_key in enumerate(group_keys):
            env[f"_key{i}"] = sort_key
        key = ", ".join(f"_key{i}(_group{i}(row))" for i in range(len(group_functions)))
        # Which row a group's bare columns (and its GROUP BY values, which a
        # collation may let differ within the group) come from, as SQLite's
        # updateAccumulator decides: a register "hit", kept from row to row,
        # is set by each MIN / MAX step (0: load this row's values; 1: it
        # skipped - no new extreme, or a NULL once there is one); before the
        # FILTER of a MIN / MAX it is set to "not the group's first row"
        # (without GROUP BY only if every MIN / MAX has a FILTER).  Without
        # MIN / MAX: only the group's first row.
        extreme = [name in ("MIN", "MAX") for name, _, _, _ in self.calls]
        grouped = bool(group_functions)
        use_flag = grouped or not any(e and f is None for e, (_, _, _, f) in zip(extreme, self.calls))
        lines = ["def loop(rows, groups, new_state):",
                 "    hit = 0",
                 "    for row in rows:",
                 f"        key = ({key}{',' if len(group_functions) == 1 else ''})",
                 "        group = groups.get(key)",
                 "        if group is None:",
                 "            group = groups[key] = [None, new_state()]",
                 "            used = 0",
                 "        else:",
                 "            used = 1",
                 "        state = group[1]"]
        for i, function in enumerate(group_functions):
            env[f"_group{i}"] = function
        for i, (_, args, distinct, filter_) in enumerate(self.calls):
            indent = "        "
            if filter_ is not None:
                if extreme[i] and use_flag:
                    lines.append(f"{indent}hit = used")
                env[f"_filter{i}"] = filter_
                lines.append(f"{indent}_v = _filter{i}(row)")
                lines.append(f"{indent}if _v is not None and ((_v != 0) if type(_v) is int else _truth(_v)):")
                indent += "    "
            for j, arg in enumerate(args):
                env[f"_arg{i}_{j}"] = arg
            arguments = ", ".join(f"_arg{i}_{j}(row)" for j in range(len(args)))
            call = f"state[{i}][0].step({arguments})"
            if distinct:
                env[f"_distinct{i}"] = values.collation_sort_key(self.collations[i])
                lines.append(f"{indent}_a = _arg{i}_0(row)")
                lines.append(f"{indent}_seen = state[{i}][1]")
                # (one NULL passes too: json_group_array() records it, the others skip it)
                lines.append(f"{indent}if (_key := _distinct{i}(_a)) not in _seen:")
                lines.append(f"{indent}    _seen.add(_key)")
                indent += "    "
                call = f"state[{i}][0].step(_a)"
            if extreme[i]:
                lines.append(f"{indent}hit = 0 if {call} else 1")
            else:
                lines.append(f"{indent}{call}")
        if not any(extreme):
            lines.append("        hit = used")
        lines.append("        if not hit or group[0] is None:")
        lines.append("            group[0] = list(row)")
        text = "\n".join(lines)
        code = _CODE_CACHE.get(text)
        if code is None:
            code = _CODE_CACHE[text] = compile(text, "<grouping loop>", "exec")
        exec(code, env)
        self.loop = env["loop"]
        return self.loop


# ---- access paths --------------------------------------------------------------
#
# Every access path yields (rowid, record or row) candidates for one table,
# reports the order it yields rows in, and estimates (rows, cost) so the
# planner can compare them.  Costs are in "row visits": reading a row from a
# table scan costs 1, finding one through an index costs a seek plus a table
# lookup.

SEEK_COST = 4  # descending a B+ tree
FULL_SCAN_PENALTY = 3  # like SQLite, choose a full scan only if lookups cost 3 times more
FETCH_COST = 2  # looking a row up in the table after finding it in an index
RANGE_FACTOR = 4  # a range condition keeps a quarter of the rows (one bound)
# Without ANALYZE statistics, like SQLite: a table has at least this many rows
# and an equality on an index column matches about this many.
DEFAULT_MIN_ROWS = 100
DEFAULT_EQUAL_ROWS = 10


class DerivedScan:
    """All rows of a derived table (a subquery in FROM), materialized per run."""

    def __init__(self, source: DerivedSource) -> None:
        self.source = source

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        return ((r[-1], r) for r in self.source.rows)

    def order(self) -> tuple[list[int], set[int]] | None:
        return None

    def estimate(self) -> tuple[float, float]:
        return 100, 100  # unknown until the subquery runs

    def describe(self) -> str:
        return "SCAN SUBQUERY"


class FullScan:
    def __init__(self, tree: BTree, rows: int, key_order: list | None = None) -> None:
        self.tree = tree
        self.rows = rows
        self.key_order = key_order  # a WITHOUT ROWID table's: its PRIMARY KEY columns ([] if DESC)

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        return self.tree.scan()

    def order(self) -> tuple[list[int], set[int]] | None:
        """(columns the rows come ordered by, columns that are constant)."""
        return ([ROWID] if self.key_order is None else self.key_order), set()

    def estimate(self) -> tuple[float, float]:
        return self.rows, self.rows

    def describe(self) -> str:
        return "SCAN"


class RowidLookup:
    """Rows whose row id equals one of the given expressions (``=`` or ``IN``)."""

    def __init__(self, tree: BTree, key_functions: list[RowFunction]) -> None:
        self.tree = tree
        self.key_functions = key_functions

    def rowids(self, row: Row) -> list[int]:
        keys = set()
        for key_function in self.key_functions:
            key = values.numeric_affinity(key_function(row))
            if isinstance(key, int):
                keys.add(key)
        return sorted(keys)

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        for key in self.rowids(row):
            value = self.tree.get(key)
            if value is not None:
                yield key, value

    def order(self) -> tuple[list[int], set[int]] | None:
        return [ROWID], set()

    def estimate(self) -> tuple[float, float]:
        count = len(self.key_functions)
        return count, count * SEEK_COST

    def describe(self) -> str:
        return "SEARCH USING ROWID (=)"


class RowidRange:
    """Rows whose row id lies between optional lower and upper bounds."""

    def __init__(self, tree: BTree, lower: Bound, upper: Bound, rows: int) -> None:
        self.tree = tree
        self.lower = lower  # (key function, inclusive) or None
        self.upper = upper
        self.rows = rows

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        start = end = None
        start_inclusive = end_inclusive = True
        if self.lower:
            start = values.numeric_affinity(self.lower[0](row))
            if start is None or isinstance(start, (str, bytes)):
                return iter(())  # rowid > NULL, 'text' or a BLOB is never true
            start_inclusive = self.lower[1]
        if self.upper:
            end = values.numeric_affinity(self.upper[0](row))
            if end is None:
                return iter(())
            if isinstance(end, (str, bytes)):
                end = None  # every number is below any text or BLOB
            end_inclusive = self.upper[1]
        return self.tree.scan(start, end, start_inclusive, end_inclusive)

    def rowids(self, row: Row) -> list[int]:
        return [key for key, _ in self.candidates(row)]

    def order(self) -> tuple[list[int], set[int]] | None:
        return [ROWID], set()

    def estimate(self) -> tuple[float, float]:
        rows = max(1, self.rows // (RANGE_FACTOR ** (bool(self.lower) + bool(self.upper))))
        return rows, rows + SEEK_COST

    def describe(self) -> str:
        return "SEARCH USING ROWID (range)"


class IndexScan:
    """Rows found through a secondary index: equality on a prefix of its
    columns, optionally followed by a range on the next column."""

    def __init__(self, index: IndexInfo, index_tree: BTree, table_tree: BTree, equal: list[RowFunction], lower: Bound, upper: Bound, table_rows: int) -> None:
        self.index = index
        self.grouping = None  # see grouping_index_scan
        self.index_tree = index_tree
        self.table_tree = table_tree
        self.equal = equal  # key functions for the leading columns
        self.lower = lower  # (key function, inclusive) or None
        self.upper = upper
        self.table_rows = table_rows
        self.covering = False  # rows are built from index keys alone
        self.index_values = True  # a SELECT's: indexed VIRTUAL columns come from the index (candidates)
        self.unused = {}  # VIRTUAL columns the query does not use (left NULL)

    @property
    def yields_rows(self) -> bool:
        return self.covering or (self.index_values and bool(self.index.virtual))

    def cover_if_possible(self, scope: Scope, table_index: int) -> None:
        """Use the index alone if it holds every column the query uses."""
        table = self.index.table
        available = set(self.index.entry_positions) - set(table.virtual) | {len(table.columns)}  # plus the row id
        if table.rowid_column is not None:
            available.add(table.rowid_column)
        used = {position for index, position in scope.used if index == table_index}
        self.covering = used <= available

    def keys(self, row: Row) -> Iterator[tuple]:
        """The index keys in range, in order."""
        key_functions = self.index.key_functions
        prefix = []
        for key_function, sort_key in zip(self.equal, key_functions):
            value = key_function(row)
            if value is None:
                return iter(())  # col = NULL is never true
            prefix.append(sort_key(value))
        prefix = tuple(prefix)
        sort_key = key_functions[len(prefix)] if len(prefix) < len(key_functions) else values.sort_key
        start, start_inclusive = prefix, True
        end, end_inclusive = prefix + (HIGH,), True
        if self.lower or self.upper:
            start, start_inclusive = prefix + ((0, 0), HIGH), False  # skip NULLs
        if self.lower:
            value = self.lower[0](row)
            if value is None:
                return iter(())
            if self.lower[1]:
                start, start_inclusive = prefix + (sort_key(value),), True
            else:
                start, start_inclusive = prefix + (sort_key(value), HIGH), False
        if self.upper:
            value = self.upper[0](row)
            if value is None:
                return iter(())
            if self.upper[1]:
                end = prefix + (sort_key(value), HIGH)
            else:
                end, end_inclusive = prefix + (sort_key(value),), False
        return (key for key, _ in self.index_tree.scan(start, end, start_inclusive, end_inclusive))

    def rowids(self, row: Row) -> list[int]:
        if self.index.pk_parts is not None:
            return [self.index.row_id(key) for key in self.keys(row)]
        return [key[-1][1] for key in self.keys(row)]

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        keys = self.keys(row)
        if self.index.pk_parts is not None:  # (WITHOUT ROWID)
            row_id = self.index.row_id
            keys = ((row_id(key), key) for key in keys)
        else:
            keys = ((key[-1][1], key) for key in keys)
        if self.covering:
            table = self.index.table
            width, positions, alias = len(table.columns), self.index.entry_positions, table.rowid_column
            plain_value = values.plain_value
            for rowid, key in keys:
                built = [None] * width
                for position, pair in zip(positions, key):
                    if pair[0]:
                        built[position] = plain_value(pair)
                if alias is not None:
                    built[alias] = rowid
                built.append(rowid)
                yield rowid, built
            return
        get = self.table_tree.get
        if self.index_values and self.index.virtual:
            # SQLite reads an indexed VIRTUAL column from the index entry
            # (where.c's pIdxEpr), also where another generated column uses
            # it: CREATE INDEX may have stored a whole REAL as an integer,
            # and an entry has no JSON subtype.
            table, positions = self.index.table, self.index.positions
            fixed = [positions[i] for i in self.index.virtual]
            plain_value = values.plain_value
            for rowid, key in keys:
                record = get(rowid)
                stored = record if type(record) is list else decode_row(record)
                yield rowid, expand_virtual(table, stored, rowid, {
                    **self.unused, **{p: plain_value(key[i]) for i, p in zip(self.index.virtual, fixed)}})
            return
        for rowid, _ in keys:
            yield rowid, get(rowid)

    def order(self) -> tuple[list, set] | None:
        """(Index columns as (position, collation).)"""
        columns = list(zip(self.index.positions, self.index.collations))
        return columns[len(self.equal):] + [ROWID], set(columns[:len(self.equal)])

    def estimate(self) -> tuple[float, float]:
        rows = self.table_rows
        matched = len(self.equal)
        if matched:
            if self.index.unique and matched == len(self.index.positions):
                rows = 1
            elif self.index.stat_average:
                rows = self.index.stat_average[matched - 1]
            else:
                rows = min(rows, DEFAULT_EQUAL_ROWS / 2 ** (matched - 1))
        rows = max(1, rows / RANGE_FACTOR ** (bool(self.lower) + bool(self.upper)))
        per_row = 1 if self.covering else 1 + FETCH_COST
        return rows, SEEK_COST + rows * per_row

    def describe(self) -> str:
        names = self.index.column_names
        covering = "COVERING " if self.covering else ""
        if not (self.equal or self.lower or self.upper):
            return f"SCAN USING {covering}INDEX {self.index.name}"
        parts = [f"{name}=?" for name in names[:len(self.equal)]]
        if self.lower:
            parts.append(f"{names[len(self.equal)]}>{'=' if self.lower[1] else ''}?")
        if self.upper:
            parts.append(f"{names[len(self.equal)]}<{'=' if self.upper[1] else ''}?")
        return f"SEARCH USING {covering}INDEX {self.index.name} ({' AND '.join(parts)})"


class HashLookup:
    """The rows whose column equals a key, for an equality join on a column
    without an index: like SQLite's automatic index, the table's rows are
    hashed by that column (with the key's affinity conversion) the first
    time a run of the join looks for one, then found by the key."""

    yields_rows = True

    def __init__(self, table: Source, tree: BTree | None, position: int, key: RowFunction, convert: Callable[[SQLValue], SQLValue] | None, scan: AccessPath, collation: str = "BINARY") -> None:
        self.table = table
        self.tree = tree  # None for a derived table
        self.position = position
        self.key = key
        self.convert = convert
        self.scan = scan  # the full scan it replaces (for its estimate)
        self.sort_key = values.collation_sort_key(collation)  # (the equality's collation)
        self.hashed = None

    def reset(self) -> None:
        self.hashed = None  # the table may have changed since the last run

    def build(self) -> dict:
        if self.tree is None:
            rows = self.table.rows
        else:
            load_row, table = Executor.load_row, self.table
            rows = (load_row(table, rowid, record) for rowid, record in self.tree.scan())
        hashed = {}
        position, convert, sort_key = self.position, self.convert, self.sort_key
        for row in rows:
            value = row[position]
            if value is not None and convert is not None:
                value = convert(value)
            if value is not None:
                hashed.setdefault(sort_key(value), []).append(row)
        self.hashed = hashed
        return hashed

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        hashed = self.hashed if self.hashed is not None else self.build()
        key = self.key(row)
        if key is None:
            return iter(())
        return ((found[-1], found) for found in hashed.get(self.sort_key(key), ()))

    def order(self) -> tuple[list[int], set[int]] | None:
        return None

    def estimate(self) -> tuple[float, float]:
        rows = min(self.scan.estimate()[0], DEFAULT_EQUAL_ROWS)
        return rows, 1 + rows

    def describe(self) -> str:
        return f"SEARCH USING AUTOMATIC INDEX ({self.table.columns[self.position].name}=?)"


class MultiScan:
    """The union of several row id / index lookups, in SQLite's order:
    ``col IN (...)`` on an index walks the index in order (SQLite sorts the
    IN values); the terms of an OR come one after another, each row once
    (SQLite's MULTI-INDEX OR with its RowSet)."""

    def __init__(self, parts: list[AccessPath], table_tree: BTree, label: str) -> None:
        self.parts = parts
        self.table_tree = table_tree
        self.label = label

    def rowids(self, row: Row) -> list[int]:
        if self.label == "IN":
            keys = set()
            for part in self.parts:
                keys.update(part.keys(row))
            if self.parts and self.parts[0].index.pk_parts is not None:  # (IN on one index)
                return [self.parts[0].index.row_id(key) for key in sorted(keys)]
            return [key[-1][1] for key in sorted(keys)]
        seen = set()
        rowids = []
        for part in self.parts:
            for rowid in part.rowids(row):
                if rowid not in seen:
                    seen.add(rowid)
                    rowids.append(rowid)
        return rowids

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        get = self.table_tree.get
        for rowid in self.rowids(row):
            record = get(rowid)
            if record is not None:
                yield rowid, record

    def order(self) -> tuple[list[int], set[int]] | None:
        if self.label == "IN":
            index = self.parts[0].index
            return list(zip(index.positions, index.collations)) + [ROWID], set()
        return None

    def estimate(self) -> tuple[float, float]:
        rows = cost = 0
        for part in self.parts:
            part_rows, part_cost = part.estimate()
            rows += part_rows
            cost += part_cost
        return rows, cost + rows * FETCH_COST

    def describe(self) -> str:
        return f"MULTI-INDEX {self.label} (" + "; ".join(p.describe() for p in self.parts) + ")"


ROWID = -1  # column position standing for the row id in constraints


class Constraint:
    """A WHERE/ON conjunct of the form ``column op key`` usable by an access path."""

    def __init__(self, position: int, op: str, key: RowFunction | list[RowFunction], convert: Callable[[SQLValue], SQLValue] | None = None, joined: bool = False, collation: str = "BINARY") -> None:
        self.position = position  # column position in the table, or ROWID
        self.op = op  # "=", "<", "<=", ">", ">=" or "IN"
        self.key = key  # key function(s) evaluated on the outer row
        self.convert = convert  # the affinity conversion the key gets
        self.joined = joined  # the key uses a table joined before this one
        self.collation = collation  # of the comparison: an index must have the same


def find_constraints(scope: Scope, index: int, conjuncts: list[Expr], compiler: Compiler, bound: set[int] | frozenset[int]) -> list[Constraint]:
    """Constraints on table ``index`` whose other side only uses the tables in
    ``bound`` (already joined) or constants.

    Keys are converted with the comparison affinity SQLite would apply to
    them; comparisons that would convert the *column* side are unusable.
    """
    entry = scope.entries[index]
    table, offset = entry.table, entry.offset
    rowid_slot = scope.rowid_slot(index)
    constraints = []

    def column_position(expr):
        expr = strip_collate(expr)  # (as SQLite's sqlite3ExprSkipCollate)
        if not isinstance(expr, Column):
            return None
        try:
            slot, _, table_index, depth = scope.resolve(expr)
        except AliasReference:
            return None
        except OperationalError:
            if is_true_false(expr):
                return None
            raise
        if depth or table_index != index:
            return None
        if slot == rowid_slot or slot - offset == table.rowid_column:
            return ROWID
        return slot - offset

    conversions = {}  # key function -> the conversion it applies

    def key_function(position, expr, key_affinity=None):
        if tables_referenced(expr, scope) - bound:
            return None
        function, affinity = compiler.compile_with_affinity(expr)
        if position == ROWID:
            return function  # row id lookups apply numeric affinity themselves
        column_affinity = table.affinities[position]
        if key_affinity is not None:  # IN: the column's affinity applies to the items
            convert = _AFFINITY_FUNCTIONS.get(column_affinity)
        else:
            # The comparison affinity applies to both sides; the index can be
            # used when that leaves the column's values as they are.
            shared = values.comparison_affinity(column_affinity, affinity)
            if shared in values.NUMERIC_AFFINITIES and column_affinity not in values.NUMERIC_AFFINITIES:
                return None
            if shared == values.TEXT and column_affinity != values.TEXT:
                return None
            convert = _AFFINITY_FUNCTIONS.get(shared)
        if convert is None:
            return function
        converted = lambda row: convert(function(row))  # noqa: E731
        conversions[converted] = convert
        return converted

    expanded = []
    for conjunct in conjuncts:
        if isinstance(conjunct, Between) and not conjunct.negated:
            # As SQLite: x BETWEEN a AND b also gives the (virtual) terms
            # x >= a and x <= b; the BETWEEN itself is still tested.
            expanded += [Binary(">=", conjunct.expr, conjunct.low), Binary("<=", conjunct.expr, conjunct.high)]
        else:
            expanded.append(conjunct)
            terms = split_disjuncts(conjunct)
            if len(terms) > 1:
                expanded += or_to_in(terms, column_position, table.affinities, compiler)
    for conjunct in expanded:
        if isinstance(conjunct, InList) and not conjunct.negated:
            position = column_position(conjunct.expr)
            if position is not None:
                keys = [key_function(position, item, "IN") for item in conjunct.items]
                if all(keys):
                    if len(conjunct.items) == 1 and is_parse_constant(conjunct.items[0]):
                        collation = compiler.comparison_collation(conjunct.expr, conjunct.items[0])
                    else:
                        collation = compiler.collation(conjunct.expr) or "BINARY"
                    constraints.append(Constraint(position, "IN", keys, collation=collation))
            continue
        if not isinstance(conjunct, Binary) or conjunct.op not in _FLIPPED or conjunct.op == "!=":
            continue
        op, left, right = conjunct.op, conjunct.left, conjunct.right
        if column_position(left) is None and column_position(right) is not None:
            op, left, right = _FLIPPED[op], right, left
        position = column_position(left)
        if position is None:
            continue
        key = key_function(position, right)
        if key is not None:
            joined = bool(tables_referenced(right, scope) & bound)
            collation = compiler.comparison_collation(conjunct.left, conjunct.right)
            constraints.append(Constraint(position, op, key, conversions.get(key), joined, collation))
    return constraints


def or_to_in(terms: list[Expr], column_position: Callable[[Expr], int | None], affinities: list[str | None], compiler: Compiler) -> list[InList]:
    """As SQLite (exprAnalyzeOrTerm), ``x = a OR x = b ...`` on one column
    also gives the (virtual) term ``x IN (a, b, ...)`` when no right-hand
    side has an affinity other than the column's; the OR is still tested."""
    column, position, items = None, None, []
    for term in terms:
        if not isinstance(term, Binary) or term.op != "=":
            return []
        left, right = term.left, term.right
        if column_position(left) is None or (position is not None and column_position(left) != position):
            left, right = right, left
        if column_position(left) is None or (position is not None and column_position(left) != position):
            return []
        column, position = column or left, column_position(left)
        if has_collate(left) or has_collate(right):
            return []  # (the IN would compare by the column's collation)
        affinity = compiler.compile_with_affinity(right)[1]
        if affinity is not None and affinity != (values.INTEGER if position == ROWID else affinities[position]):
            return []
        items.append(right)
    return [InList(column, tuple(items))]


def _bounds(constraints: list[Constraint], position: int) -> tuple[Bound, Bound]:
    """The first lower and upper bound constraints on ``position``."""
    lower = upper = None
    for c in constraints:
        if c.position != position:
            continue
        if c.op in (">", ">=") and lower is None:
            lower = (c.key, c.op == ">=")
        elif c.op in ("<", "<=") and upper is None:
            upper = (c.key, c.op == "<=")
    return lower, upper


def split_disjuncts(expr: Expr) -> list[Expr]:
    if isinstance(expr, Binary) and expr.op == "OR":
        return split_disjuncts(expr.left) + split_disjuncts(expr.right)
    return [expr]


def table_rows(catalog: Catalog, table: TableInfo) -> int:
    """Rows in ``table``: from ANALYZE if available, else a cheap estimate."""
    if table.stat_rows is not None:
        return max(1, table.stat_rows)
    return max(DEFAULT_MIN_ROWS, catalog.table_tree(table).estimated_count())


def index_rooted(pager: SqlitePager, root: int) -> bool:
    """Whether a tree of a SQLite file is an index tree (a WITHOUT ROWID
    table's, when the schema calls it a table)."""
    from minidb.sqlite_format import INDEX_INTERIOR, INDEX_LEAF
    from minidb.sqlite_btree import IndexTree

    return IndexTree(pager, root).page(root).kind in (INDEX_LEAF, INDEX_INTERIOR)


def full_scan(tree: BTree, table: TableInfo, rows: int) -> FullScan:
    """A scan of a whole table: in row id order, or a WITHOUT ROWID table's
    in its PRIMARY KEY's order."""
    if table.has_rowid:
        return FullScan(tree, rows)
    pk = table.pk_index
    return FullScan(tree, rows, list(zip(pk.positions, pk.collations)) if pk.ordered else [])


def access_candidates(scope: Scope, index: int, catalog: Catalog, conjuncts: list[Expr], compiler: Compiler, bound: set[int] | frozenset[int], rows: int) -> list[AccessPath]:
    """Every access path the conjuncts allow for table ``index``, full scan first."""
    table = scope.entries[index].table
    tree = catalog.table_tree(table)
    constraints = find_constraints(scope, index, conjuncts, compiler, bound)
    candidates = [full_scan(tree, table, rows)]
    for c in constraints:
        if c.position == ROWID and c.op in ("=", "IN"):
            candidates.append(RowidLookup(tree, [c.key] if c.op == "=" else c.key))
    lower, upper = _bounds(constraints, ROWID)
    if lower or upper:
        candidates.append(RowidRange(tree, lower, upper, rows))
    for info in scope.entries[index].indexes():
        if not info.ordered:
            continue  # (DESC columns in a SQLite file: not in key order)
        # Only comparisons with the index column's collation can use it.
        usable = [c for c in constraints if c.position == ROWID or c.position not in info.positions
                  or c.collation == info.collations[info.positions.index(c.position)]]
        equal = []
        for position in info.positions:
            key = next((c.key for c in usable if c.position == position and c.op == "="), None)
            if key is None:
                break
            equal.append(key)
        lower = upper = None
        if len(equal) < len(info.positions):
            lower, upper = _bounds(usable, info.positions[len(equal)])
        if equal or lower or upper:
            candidates.append(IndexScan(info, catalog.index_tree(info), tree, equal, lower, upper, rows))
        first = info.positions[0]
        for c in usable:
            if c.position == first and c.op == "IN":
                index_tree = catalog.index_tree(info)
                parts = [IndexScan(info, index_tree, tree, [key], None, None, rows) for key in c.key]
                candidates.append(MultiScan(parts, tree, "IN"))
                break
    return candidates


def hash_lookup(scope: Scope, index: int, catalog: Catalog, conjuncts: list[Expr], compiler: Compiler, bound: set[int] | frozenset[int], scan: AccessPath) -> HashLookup | None:
    """A HashLookup for an equality with a table joined before, if any."""
    for c in find_constraints(scope, index, conjuncts, compiler, bound):
        if c.op == "=" and c.position != ROWID and c.joined:
            table = scope.entries[index].table
            tree = None if isinstance(table, DerivedSource) else catalog.table_tree(table)
            return HashLookup(table, tree, c.position, c.key, c.convert, scan, c.collation)
    return None


def size_estimate(type_name: str) -> int:
    """SQLite's estimate of a column's width (an integer is 1), from its
    declared type, as sqlite3AffinityType computes Column.szEst."""
    name = type_name.encode("utf-8", "surrogateescape").lower()  # (ASCII letters only)
    h, affinity, size_from = 0, "NUMERIC", None
    for i, byte in enumerate(name):
        h = ((h << 8) + byte) & 0xFFFFFFFF
        if h == 0x63686172:  # char
            affinity, size_from = "TEXT", i + 1
        elif h in (0x636C6F62, 0x74657874):  # clob, text
            affinity = "TEXT"
        elif h == 0x626C6F62 and affinity in ("NUMERIC", "REAL"):  # blob
            affinity = "BLOB"
            if name[i + 1:i + 2] == b"(":
                size_from = i + 1
        elif h in (0x7265616C, 0x666C6F61, 0x646F7562) and affinity == "NUMERIC":  # real, floa, doub
            affinity = "REAL"
        elif h & 0xFFFFFF == 0x696E74:  # int
            affinity = "INTEGER"
            break
    v = 0
    if affinity in ("TEXT", "BLOB"):
        if size_from is None:
            v = 16  # TEXT, CLOB, BLOB: about 20 bytes
        else:  # VARCHAR(k), BLOB(k): k bytes
            digits = re.match(rb"\D*(\d*)", name[size_from:]).group(1)
            v = int(digits) if digits and int(digits) < 2**31 else 0
    return min(255, v // 4 + 1)


def log_estimate(x: int) -> int:
    """sqlite3LogEst: about 10 * log2(x)."""
    if x < 2:
        return 0
    y = 40
    if x < 8:
        while x < 8:
            y -= 10
            x <<= 1
    else:
        shift = x.bit_length() - 4
        y += shift * 10
        x >>= shift
    return (0, 2, 3, 5, 6, 7, 8, 9)[x & 7] + y - 10


def covering_index_scan(scope: Scope, index: int, catalog: Catalog, scan: FullScan) -> IndexScan | None:
    """Like SQLite, read the whole table through an index holding every
    column the query uses when its rows are narrower than the table's
    (whereLoopAddBtree's "full scan via index"): the rows then come in index
    order.  The cheapest index wins, the newest of equals."""
    table = scope.entries[index].table
    width = sum(size_estimate(c.type) for c in table.columns) + (table.rowid_column is None)
    table_size = log_estimate(4 * width)
    best = best_cost = None
    for info in scope.entries[index].indexes():
        if not info.ordered or info.table_pk:
            continue  # (a WITHOUT ROWID table's PRIMARY KEY: its full scan is the table's)
        index_size = log_estimate(4 * (sum(size_estimate(table.columns[p].type) for p in info.entry_positions) + 1))
        cost = 15 * index_size // table_size
        if index_size >= table_size or (best is not None and cost >= best_cost):
            continue
        candidate = IndexScan(info, catalog.index_tree(info), scan.tree, [], None, None, scan.rows)
        candidate.cover_if_possible(scope, index)
        if candidate.covering:
            best, best_cost = candidate, cost
    return best


def grouping_index_scan(scope: Scope, index: int, catalog: Catalog, scan: FullScan,
                        group: list[tuple[int, str]]) -> IndexScan | None:
    """A full scan through an index that orders the groups, as SQLite takes
    one (without statistics) to save sorting them: its first columns are all
    the GROUP BY columns in any order (``grouping`` "all": the groups then
    come out in index order), or the first GROUP BY terms in their written
    order (a partial order).  So each group's rows come in index order, which
    decides its bare columns.  Ordering more terms wins, then a covering
    index, then the narrower one."""
    table = scope.entries[index].table
    wanted = set(group)
    best = best_rank = None
    for info in scope.entries[index].indexes():
        if not info.ordered or info.table_pk:
            continue
        columns = list(zip(info.positions, info.collations))
        if len(columns) >= len(group) and set(columns[:len(group)]) == wanted:
            satisfied = len(group)
        else:
            satisfied = 0
            while satisfied < min(len(group), len(columns)) and columns[satisfied] == group[satisfied]:
                satisfied += 1
        if not satisfied:
            continue
        candidate = IndexScan(info, catalog.index_tree(info), scan.tree, [], None, None, scan.rows)
        candidate.cover_if_possible(scope, index)
        size = sum(size_estimate(table.columns[p].type) for p in info.positions)
        rank = (-satisfied, not candidate.covering, size)
        if best is None or rank < best_rank:
            best, best_rank = candidate, rank
            candidate.grouping = "all" if satisfied == len(group) else "partial"
    return best


def plan_access(scope: Scope, index: int, catalog: Catalog, conjuncts: list[Expr], compiler: Compiler, order_hint: int | None = None, bound: set[int] | frozenset[int] | None = None) -> AccessPath:
    """Choose the cheapest way to read table ``index`` of ``scope``.

    ``bound`` is the set of tables joined before it (default: those before it
    in ``scope``); conditions may use their columns as lookup keys.  An OR
    whose every term can use a row id or index lookup becomes a union of
    those lookups."""
    table = scope.entries[index].table
    if bound is None:
        bound = set(range(index))
    if isinstance(table, JsonEachSource):
        return JsonEachScan(table)
    if isinstance(table, DerivedSource):
        scan = DerivedScan(table)
        if type(table) in (DerivedSource, PragmaSource):  # (not a CTE's working table, which changes)
            return hash_lookup(scope, index, catalog, conjuncts, compiler, bound, scan) or scan
        return scan
    rows = table_rows(catalog, table)
    candidates = access_candidates(scope, index, catalog, conjuncts, compiler, bound, rows)
    for conjunct in conjuncts:
        terms = split_disjuncts(conjunct)
        if len(terms) < 2:
            continue
        parts = []
        for term in terms:
            options = access_candidates(
                scope, index, catalog, split_conjuncts(term), compiler, bound, rows
            )[1:]  # without the full scan
            if not options:
                break
            parts.append(min(options, key=lambda a: a.estimate()[1]))
        else:
            candidates.append(MultiScan(parts, catalog.table_tree(table), "OR"))
    best = min(candidates, key=lambda a: a.estimate()[1] * (FULL_SCAN_PENALTY if type(a) is FullScan else 1))
    hint = scope.entries[index].hint
    if hint is not None and isinstance(best, FullScan):
        # As SQLite: no automatic index, and INDEXED BY never scans the table
        # itself but the whole index.
        if hint is not NOT_INDEXED and hint.ordered:
            return IndexScan(hint, catalog.index_tree(hint), best.tree, [], None, None, rows)
        return best
    if isinstance(best, FullScan):
        lookup = hash_lookup(scope, index, catalog, conjuncts, compiler, bound, best)
        if lookup is not None:
            return lookup
    if isinstance(best, FullScan) and order_hint is not None and order_hint != ROWID:
        # Nothing narrows the scan, but ORDER BY ... LIMIT wants this column
        # first: walk an index on it in order and stop early.
        for info in scope.entries[index].indexes():
            if info.ordered and (info.positions[0], info.collations[0]) == order_hint:
                tree = catalog.table_tree(table)
                return IndexScan(info, catalog.index_tree(info), tree, [], None, None, rows)
    return best


# ---- statements ------------------------------------------------------------------


class Executor:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.last_insert_rowid = 0
        self.parameters = []  # values of ?-parameters; compiled plans read this list
        self.once_caches = []  # caches of uncorrelated subqueries of the plan being compiled
        self.expanding = []  # views and CTEs being compiled (to detect one that uses itself)
        self.cte_scopes = []  # the WITH clauses in effect: dicts of lower-case name -> CteInfo
        self.column_hook = None  # called with (Column, table) for each column reference compiled
        self.statement_journal = True  # see PreparedInsert.statement_journal
        self.ran = False  # see execute
        self.triggers = Triggers(self)
        self.outer_scope = None  # the NEW / OLD scope while a trigger's statements are compiled
        self.compiling_trigger = 0  # > 0: RAISE() is allowed
        # "main" while a view or trigger of the main database is compiled: its
        # names mean main's tables (SQLite's sqlite3FixSrcList); a temporary
        # object's search the temp database first.
        self.default_schema = None
        self.pragma_schema = None  # the schema of the PRAGMA being run ("main", "temp" or None)
        self.frame_depth = 0  # trigger programs and foreign key actions running (SQLite's nFrame)
        self.pinned = {}  # table name -> frame depth of an UPDATE whose REPLACE runs DELETE triggers (check_pinned)
        self.compile_depth = 0  # prepare() calls in progress
        self.program_log = []  # the programs the statement being compiled compiled, in order (log_program)
        self.program_seen = set()
        self.program_stack = []  # for each trigger program being compiled: the programs it asks for
        self.changes = 0  # changes() and total_changes(), kept up to date by Database
        self.total_changes = 0
        # PRAGMA settings of the connection (see minidb.pragmas).
        self.settings = {"foreign_keys": 0, "defer_foreign_keys": 0, "ignore_check_constraints": 0,
                         "recursive_triggers": 0, "cache_size": -2000, "synchronous": 2}
        self.data_version = 1  # PRAGMA data_version: bumped when another connection commits
        self.integrity_problems = None  # Database.integrity_check, for PRAGMA integrity_check
        self.in_transaction = lambda: False  # set by Database
        self.foreign_keys = ForeignKeys(self)

    def execute(self, stmt: Statement, parameters: Sequence[SQLValue] = ()) -> Result:
        """Execute a parsed statement with the given parameter values (a list
        indexed by parameter number - 1)."""
        self.parameters[:] = parameters
        self.statement_journal = True
        self.ran = False  # whether the statement got past compiling (Database: what an error ends)
        dates.statement_time[0] = None  # 'now' is fixed for the length of a statement
        keys = self.foreign_keys
        keys.immediate = 0
        keys.unchecked = None
        if isinstance(stmt, (Select, Compound, Values, Insert, Update, Delete)):
            plan = self.prepare(stmt)
            for cache in plan.once_caches:
                cache.clear()
            self.ran = True
            self.statement_journal = getattr(plan, "statement_journal", True)
            if keys.enabled and isinstance(stmt, (Insert, Update, Delete)) and getattr(plan, "view", None) is None \
                    and keys.involved(plan.table):
                self.statement_journal = self.statement_journal or self.foreign_keys_may_abort(stmt, plan)
            result = plan.run()
            self.check_foreign_keys()
            return result
        if isinstance(stmt, CreateTable):
            if stmt.query is not None:
                return self.create_table_as(stmt)
            self.catalog.create_table(stmt, self.check_new_table)
            return Result()
        if isinstance(stmt, DropTable):
            self.drop_table(stmt)
            return Result()
        if isinstance(stmt, CreateIndex):
            return self.create_index(stmt)
        if isinstance(stmt, DropIndex):
            self.catalog.drop_index(stmt.name, stmt.if_exists, stmt.schema)
            return Result()
        if isinstance(stmt, CreateView):
            self.catalog.create_view(stmt)
            return Result()
        if isinstance(stmt, Reindex):
            return self.reindex(stmt.name)
        if isinstance(stmt, Vacuum):
            return self.vacuum(stmt)
        if isinstance(stmt, AlterTable):
            return self.alter_table(stmt)
        if isinstance(stmt, DropView):
            self.catalog.drop_view(stmt.name, stmt.if_exists, stmt.schema)
            return Result()
        if isinstance(stmt, Explain):
            return self.explain(stmt.statement)
        if isinstance(stmt, Analyze):
            self.catalog.analyze(stmt.name)
            return Result()
        if isinstance(stmt, CreateTrigger):
            self.catalog.create_trigger(stmt)
            return Result()
        if isinstance(stmt, DropTrigger):
            self.catalog.drop_trigger(stmt.name, stmt.if_exists, stmt.schema)
            return Result()
        if isinstance(stmt, Pragma):
            self.ran = True
            rows, columns = pragmas.run(self, stmt.name, stmt.value, stmt.schema)
            return Result(rows, columns)
        raise OperationalError(f"unsupported statement: {type(stmt).__name__}")

    # ---- what SQLite compiles with a statement ------------------------------------

    def log_program(self, key: tuple, entry: tuple) -> bool:
        """Note that the statement being compiled compiles a trigger program
        or a foreign key action (SQLite's list pParse->pTriggerPrg, newest
        last); False if it already did (SQLite compiles each once)."""
        for requested in self.program_stack:
            requested.append((key, entry))
        if key in self.program_seen:
            return False
        self.program_seen.add(key)
        self.program_log.append(entry)
        return True

    def set_null_link(self, table: TableInfo) -> Link | None:
        """A SQLite quirk (isSetNullAction): where it codes the check of a new
        row's foreign keys, it leaves out foreign key F if the program it
        compiled last is F's ON DELETE / ON UPDATE SET NULL action."""
        if self.program_log:
            entry = self.program_log[-1]
            if entry[0] == "action" and entry[1].child is table and entry[3] == "SET NULL":
                return entry[1]
        return None

    def compile_delete(self, table: TableInfo, orconf: str | None, triggers: bool = True) -> None:
        """What SQLite compiles to delete a row of ``table`` (sqlite3GenerateRowDelete):
        the triggers (all of them first, for their column masks), the foreign
        keys of the row, their actions."""
        if triggers:
            self.triggers.prepare_listed(table.name, "DELETE", None, orconf)
        keys = self.foreign_keys
        if keys.involved(table):
            keys.prepare(table, "delete")

    def compile_update(self, table: TableInfo, changed: set[int], orconf: str | None,
                       replace: bool = False, rowid_changed: bool = False) -> Link | None:
        """What SQLite compiles to update rows of ``table`` setting ``changed``
        (sqlite3Update): the triggers (all of them first, for their column
        masks); the constraint checks (REPLACE's DELETE); the foreign keys of
        the old and the new row; their actions.  Returns the foreign key whose
        new-row check it leaves out."""
        names = trigger_names(table, changed)
        triggers, keys = self.triggers, self.foreign_keys
        triggers.prepare_listed(table.name, "UPDATE", names, orconf)
        checked = None if keys.every_index(table, changed) else changed  # (update.c's hasFK>1)
        if replace and replace_possible(table, orconf, rowid_changed, checked):
            self.compile_delete(table, "REPLACE", bool(self.settings["recursive_triggers"]))
        unchecked = None
        if keys.enabled:
            keys.prepare(table, "update", changed, actions=False)
            unchecked = self.set_null_link(table)
            if keys.required(table, changed):
                keys.prepare_actions(table, "update", changed)
        return unchecked

    def foreign_keys_may_abort(self, stmt: Insert | Update | Delete, plan: Any) -> bool:
        """Whether SQLite gives a statement on a table with foreign keys a
        statement journal for them: when it writes several rows (any UPDATE
        or DELETE with foreign key work does) and their code may abort."""
        keys = self.foreign_keys
        if isinstance(stmt, Insert):
            # (a REPLACE deletes rows; an upsert updates them)
            return plan.multi_write and plan.foreign_keys_abort()
        if isinstance(stmt, Update):
            return keys.required(plan.table, plan.changed) and keys.may_abort(plan.table, "update", plan.changed)
        return keys.may_abort(plan.table, "delete")

    def check_foreign_keys(self) -> None:
        """At the end of a statement: immediate foreign key violations fail
        it; deferred ones too when it is not inside a transaction (it
        commits now)."""
        keys = self.foreign_keys
        if keys.statement_failed() or (not self.in_transaction() and keys.transaction_failed()):
            raise self.constraint_error("FOREIGN KEY constraint failed", "ABORT")

    def drop_table(self, stmt: DropTable) -> None:
        """DROP TABLE; with foreign keys on, a parent table is emptied first
        (its children's actions run, violations count), as SQLite does."""
        keys = self.foreign_keys
        lowered = ascii_lower(stmt.name)
        owner = next((c for c in self.catalog.search(stmt.schema) if lowered in c.tables or lowered in c.views), None)
        table = None if owner is None else owner.tables.get(lowered)  # (None: a view, DROP TABLE refuses it)
        if keys.enabled and table is not None and ascii_lower(table.name) != "sqlite_sequence":
            self.catalog.check_writable(table, "dropped")
            deferred_child = any(link.deferred or keys.defer_all() for link in keys.children_of(table))
            if keys.parents_of(table) or (deferred_child and keys.transaction_failed()):
                tree = self.catalog.table_tree(table)
                for rowid in list(tree.keys()):
                    if rowid in tree:
                        self.delete_row(table, tree, rowid, fire=False)  # (SQLite disables its triggers)
                        keys.extra_changes += 1  # (total_changes() counts them, as in SQLite)
                if not keys.defer_all() and keys.statement_failed():
                    raise self.constraint_error("FOREIGN KEY constraint failed", "ABORT")
        self.catalog.drop_table(stmt.name, stmt.if_exists, stmt.schema)

    def constant(self, expr: Expr) -> SQLValue:
        """The value of a constant expression (a DEFAULT)."""
        return Compiler(Scope(), executor=self).compile(expr)([])

    def foreign_key_violations(self, table_name: object = None) -> list[tuple]:
        """PRAGMA foreign_key_check: (table, rowid, parent, foreign key number)
        for each child row whose parent is missing, as SQLite reports them."""
        keys = self.foreign_keys
        catalog = self.catalog
        if table_name is not None:
            table = next((c.tables[ascii_lower(str(table_name))] for c in catalog.search()
                          if ascii_lower(str(table_name)) in c.tables), None)
            if table is None:
                raise OperationalError(f"no such table: {table_name}")
            tables = [table]
        else:  # (the main database's, as SQLite's pragma without a schema)
            tables = sorted(catalog.tables.values(), key=lambda t: t.schema_key or 0, reverse=True)
        found = []
        for table in tables:
            links = keys.children_of(table)
            if not links:
                continue
            parents = catalog.owner(table).tables  # (a parent is in its child's database)
            for link in links:
                if ascii_lower(link.key.parent) in parents:
                    try:
                        link.locate(parents)
                    except Exception as exc:  # (a mismatch)
                        raise OperationalError(str(exc)) from None
            for rowid, record in catalog.table_tree(table).scan():
                row = self.load_row(table, rowid, record)
                for link in links:
                    if ascii_lower(link.key.parent) not in parents:
                        missing = all(row[table.column_index(n)] is not None for n in link.key.columns)
                    else:
                        missing = keys.parent_exists(link, row) is False
                    if missing:
                        found.append((table.name, rowid if table.has_rowid else None,
                                      link.key.parent, link.number))
        return found

    def prepare(self, stmt: Select | Compound | Insert | Update | Delete) -> PreparedStatement:
        """The compiled plan of a SELECT/INSERT/UPDATE/DELETE.  Plans are kept
        on the (cached) syntax tree and reused until the schema changes."""
        cached = getattr(stmt, "_plan", None)
        if cached is not None and cached[0] == self.catalog.version:
            return cached[1]
        if self.compile_depth == 0:
            self.program_log, self.program_seen = [], set()
        self.compile_depth += 1
        try:
            return self._prepare(stmt)
        finally:
            self.compile_depth -= 1

    def _prepare(self, stmt: Select | Compound | Insert | Update | Delete) -> PreparedStatement:
        self.once_caches = []
        if isinstance(stmt, (Select, Compound, Values)):
            plan = PreparedSelect(self.compile_query(stmt))
            plan.aborts = bool(self.compiling_trigger) and calls_function(stmt)  # (see Program.may_abort)
        else:
            with self.cte_scope(stmt.ctes or []):
                view = self.catalog.find_view(stmt.table, stmt.schema or self.default_schema)
                event = "INSERT" if isinstance(stmt, Insert) else "UPDATE" if isinstance(stmt, Update) else "DELETE"
                names = [name for name, _ in stmt.assignments] if isinstance(stmt, Update) else None
                # (With RETURNING any trigger on the view will do: SQLite's own
                # RETURNING trigger makes the list it checks non-empty.)
                if view is not None and (self.triggers.matching(view.name, "INSTEAD OF", event, names) or (
                        stmt.returning is not None and self.catalog.triggers_on(view.name))):
                    plan = (PreparedViewInsert(self, stmt, view) if isinstance(stmt, Insert)
                            else PreparedViewChange(self, stmt, view))
                elif isinstance(stmt, Insert):
                    plan = PreparedInsert(self, stmt)
                elif isinstance(stmt, Update):
                    plan = PreparedUpdate(self, stmt)
                else:
                    plan = PreparedDelete(self, stmt)
        plan.once_caches = self.once_caches
        stmt._plan = (self.catalog.version, plan)
        return plan

    # ---- reading rows ------------------------------------------------------

    @staticmethod
    def load_row(table: TableInfo, rowid: int, record: bytes | list) -> Row:
        row = record if type(record) is list else decode_row(record)  # (SQLite files: a row)
        if table.virtual:
            return expand_virtual(table, row, rowid)
        if len(row) < len(table.columns):  # written before ALTER TABLE ADD COLUMN
            row.extend(table.padding[len(row):])
        if table.rowid_column is not None:
            row[table.rowid_column] = rowid
        row.append(rowid)
        return row

    def compile_query(self, stmt: Select | Compound | Values, parent: Scope | None = None) -> CompiledQuery:
        """Compile a SELECT, VALUES or compound SELECT (``parent``: the
        enclosing query's scope when this is a subquery), with its WITH clause."""
        if parent is None:
            parent = self.outer_scope  # (a trigger's NEW / OLD)
        if stmt.ctes:
            with self.cte_scope(stmt.ctes):
                return self._compile_query(stmt, parent)
        return self._compile_query(stmt, parent)

    def _compile_query(self, stmt: Select | Compound | Values, parent: Scope | None) -> CompiledQuery:
        if isinstance(stmt, Compound):
            return CompiledCompound(self, stmt, parent)
        if isinstance(stmt, Values):
            return CompiledValues(self, stmt, parent)
        return CompiledSelect(self, stmt, parent)

    # ---- common table expressions (WITH) -------------------------------------

    @contextlib.contextmanager
    def cte_scope(self, ctes: list[Cte]) -> Iterator[None]:
        """Make the CTEs of a WITH clause visible (to each other too)."""
        names = {}
        for cte in ctes:
            lowered = ascii_lower(cte.name)
            if lowered in names:
                raise OperationalError(f"duplicate WITH table name: {cte.name}")
            names[lowered] = CteInfo(cte, len(self.cte_scopes))
        self.cte_scopes.append(names)
        try:
            yield
        finally:
            self.cte_scopes.pop()

    def find_cte(self, name: str) -> CteInfo | WorkingSource | None:
        lowered = ascii_lower(name)
        for names in reversed(self.cte_scopes):
            if lowered in names:
                return names[lowered]
        return None

    def cte_source(self, found: CteInfo | WorkingSource, scope: Scope) -> DerivedSource:
        """A CTE used in FROM, compiled in the scope of its WITH clause."""
        if isinstance(found, WorkingSource):  # a recursive CTE inside its recursive part
            if scope.parent is not found.parent_scope:
                raise OperationalError(f"circular reference: {found.name}")
            if found.used:
                raise OperationalError(f"multiple references to recursive table: {found.name}")
            found.used = True
            return found
        if found in self.expanding:
            raise OperationalError(f"circular reference: {found.cte.name}")
        saved = self.cte_scopes
        self.expanding.append(found)
        self.cte_scopes = saved[:found.level + 1]
        try:
            return self.compile_cte(found.cte, scope.parent)
        finally:
            self.cte_scopes = saved
            self.expanding.pop()

    def compile_cte(self, cte: Cte, parent: Scope | None) -> DerivedSource:
        body = cte.query
        parts = body.selects if isinstance(body, Compound) else [body]
        operators = body.operators if isinstance(body, Compound) else []
        recursive = [self_reference_count(part, cte.name) for part in parts]
        if not any(recursive):
            compiled = self.compile_query(body, parent)
            check_cte_columns(cte, compiled.names)
            return DerivedSource(cte.name, compiled, cte.columns, body)
        k = next(i for i, count in enumerate(recursive) if count)
        if k == 0 or operators[k - 1] not in ("UNION", "UNION ALL"):
            raise OperationalError(f"circular reference: {cte.name}")
        initial_stmt = parts[0] if k == 1 else Compound(parts[:k], operators[:k - 1])
        initial = self.compile_query(initial_stmt, parent)
        check_cte_columns(cte, initial.names)
        names = cte.columns or unique_names(initial.names)
        working = WorkingSource(cte.name, names, initial.affinities, parent, initial.collations)
        compiled_parts = []
        for part in parts[k:]:
            if isinstance(part, Select) and (part.group_by or any(
                contains_aggregate(item.expr) for item in part.items if not isinstance(item.expr, Star)
            )):
                raise OperationalError("recursive aggregate queries not supported")
            working.used = False
            self.cte_scopes.append({ascii_lower(cte.name): working})
            try:
                compiled = self.compile_query(part, parent)
            finally:
                self.cte_scopes.pop()
            if len(compiled.names) != len(names):
                raise OperationalError(
                    f"SELECTs to the left and right of {operators[k - 1]} "
                    "do not have the same number of result columns"
                )
            compiled_parts.append(compiled)
        order_terms, limit = [], None
        if isinstance(body, Compound):
            if body.order_by:
                order_terms = self.compound_order_terms(body, [initial] + compiled_parts)
            limit = self.compile_limit(body)
        source = RecursiveSource(cte.name, initial, names, working, compiled_parts,
                                 operators[k - 1] == "UNION", order_terms, limit)
        source.strip = tuple(range(len(names))) if carries_json(body) else ()  # (never flattened)
        return source

    def view_source(self, view: ViewInfo) -> DerivedSource:
        """A view used in FROM: its SELECT, compiled as a subquery that sees
        no enclosing query."""
        if view in self.expanding:
            raise OperationalError(f"view {view.name} is circularly defined")
        self.expanding.append(view)
        saved, self.cte_scopes = self.cte_scopes, []  # a view sees no CTE of the query using it
        outer, self.outer_scope = self.outer_scope, None  # (nor a trigger's NEW / OLD)
        schema, self.default_schema = self.default_schema, None if view.temp else "main"
        try:
            compiled = self.compile_query(view.query)
        finally:
            self.default_schema = schema
            self.outer_scope = outer
            self.expanding.pop()
            self.cte_scopes = saved
        return DerivedSource(view.name, compiled, view.columns, view.query)

    def build_from(self, joins: list[Join], scope: Scope) -> tuple[list[Join], list[DerivedSource]]:
        """Add the FROM clause's tables to ``scope``.

        Returns the joins with USING / NATURAL turned into ON conditions, and
        the derived tables (which must be materialized before each run)."""
        derived = []
        normalized = []
        scope.last_right = max((i for i, j in enumerate(joins) if j.kind in ("RIGHT", "FULL")), default=-1)
        for index, join in enumerate(joins):
            ref = join.table
            if isinstance(ref, DerivedTable):
                # A subquery in FROM cannot see its sibling tables, only
                # the queries enclosing this one.
                compiled = self.compile_query(ref.query, parent=scope.parent)
                if compiled.correlated:
                    scope.uses_outer = True
                source = DerivedSource(ref.alias or "", compiled, query=ref.query)
                derived.append(source)
                scope.add(source, ref.alias or "")
            elif isinstance(ref, TableRef) and ref.schema is not None:
                derived += self.add_table(ref, scope, ref.schema)
            elif ascii_lower(ref.name) in ("json_each", "json_tree", "jsonb_each", "jsonb_tree") and (
                    isinstance(ref, TableFunction) or self.find_cte(ref.name) is None
                    and not self.catalog.has_table(ref.name) and self.catalog.find_view(ref.name) is None):
                self.json_each_source(ref, scope)
            elif isinstance(ref, TableFunction) or (
                    pragmas.function_spec(ref.name) is not None and self.find_cte(ref.name) is None
                    and not self.catalog.has_table(ref.name) and self.catalog.find_view(ref.name) is None):
                if not isinstance(ref, TableFunction):
                    ref = TableFunction(ascii_lower(ref.name), [], ref.alias, ref.pos)
                source, condition = self.table_function_source(ref, scope)
                derived.append(source)
                if source.correlated:
                    scope.uses_outer = True
                scope.add(source, ref.alias or ref.name)
                if condition is not None:  # its argument comes from a table before it in FROM
                    on = Binary("=", Column("arg", ref.alias or ref.name), condition)
                    join = dataclasses.replace(join, on=on if join.on is None else Binary("AND", join.on, on))
            elif self.find_cte(ref.name) is not None:
                source = self.cte_source(self.find_cte(ref.name), scope)
                if not isinstance(source, WorkingSource):
                    derived.append(source)
                    if source.correlated:
                        scope.uses_outer = True
                scope.add(source, ref.alias)
            else:
                derived += self.add_table(ref, scope, self.default_schema)
            if join.natural or join.using is not None:
                join = self.using_condition(scope, index, join)
            normalized.append(join)
        return normalized, derived

    def add_table(self, ref: TableRef, scope: Scope, schema: str | None) -> list[DerivedSource]:
        """Add the table or view ``ref`` names (in ``schema``; None: temp
        first, then main) to ``scope``; returns the view's source, if a view."""
        view = self.catalog.find_view(ref.name, schema)
        if view is not None:
            source = self.view_source(view)
            scope.add(source, ref.alias)
            return [source]
        table = self.catalog.get_table(ref.name, schema)
        self.catalog.check_index_hint(table, ref.indexed_by)
        scope.add(table, ref.alias)
        if ref.not_indexed:
            scope.entries[-1].hint = NOT_INDEXED
        elif ref.indexed_by is not None:
            scope.entries[-1].hint = self.catalog.owner(table).indexes[ascii_lower(ref.indexed_by)]
        return []

    def json_each_source(self, ref: TableFunction | TableRef, scope: Scope) -> None:
        """Add json_each() / json_tree() to ``scope``.  Its arguments see it
        too, as in SQLite: one that uses its own columns (or row id) leaves
        SQLite's virtual table without its argument, and it has no rows."""
        name = ascii_lower(ref.name)
        args = ref.args if isinstance(ref, TableFunction) else []
        if len(args) > 2:
            raise OperationalError(f"too many arguments on {name}() - max 2")
        source = JsonEachSource(name)
        scope.add(source, ref.alias or name)
        me = len(scope.entries) - 1
        compiler = Compiler(scope, executor=self)
        saved, scope.watch = scope.watch, set()  # (the tables the references resolve to, subqueries' too)
        try:
            compiled = [compiler.compile(arg) for arg in args]
        finally:
            depends, scope.watch = scope.watch, saved
            if saved is not None:
                saved |= depends
        if me not in depends:
            source.args, source.depends = compiled, depends

    def table_function_source(self, ref: TableFunction, scope: Scope) -> tuple[PragmaSource, Expr | None]:
        """The source of ``pragma_<name>(arg, schema)`` in FROM, and an
        expression the hidden column ``arg`` must equal, when the argument
        uses a table before it in the FROM clause."""
        spec = pragmas.function_spec(ref.name)
        if spec is None:
            raise OperationalError(f"no such table: {ref.name}")
        takes_arg = spec.arg is not None
        if len(ref.args) > 1 + takes_arg:
            raise OperationalError(f"too many arguments on {ref.name}() - max {1 + takes_arg}")
        arg = ref.args[0] if takes_arg and ref.args else None
        schema = ref.args[-1] if len(ref.args) > takes_arg else None
        compiler = Compiler(scope, executor=self)
        if schema is not None:
            if tables_referenced(schema, scope):
                raise OperationalError(f"MiniDB needs a constant schema argument for {ref.name}()")
            schema = compiler.compile(schema)
        lateral = arg is not None and bool(tables_referenced(arg, scope))
        source = PragmaSource(self, ref.name, spec, None if lateral or arg is None else compiler.compile(arg),
                              schema, lateral, scope)
        return source, (Collate(arg, "NOCASE") if lateral else None)

    @staticmethod
    def using_condition(scope: Scope, index: int, join: Join) -> Join:
        """``JOIN t USING (c, ...)`` / ``NATURAL JOIN t`` as an ON condition.

        As in SQLite, an unqualified ``c`` afterwards means the left table's
        column after an inner or LEFT JOIN (the right table's copy becomes
        reachable only by qualified name), the right table's after a RIGHT
        JOIN, and the first non-NULL of them (a Merge) after a FULL JOIN.
        The left side of the condition is the left-most table with the
        column; in a FROM clause with a RIGHT or FULL JOIN, the first
        non-NULL of all the left tables with it (all but the first must have
        joined on it with USING)."""
        right = scope.entries[index]
        left_entries = scope.entries[:index]

        def having(name):
            return [e for e in left_entries if e.table.column_index(name) is not None]

        if join.natural:
            names = [c.name for c in right.table.columns if having(c.name)]
        else:
            names = join.using
        condition = None
        for name in names:
            key = ascii_lower(name)
            lefts = having(name)
            if not lefts or right.table.column_index(name) is None:
                raise OperationalError(
                    f"cannot join using column {name} - column not present in both tables"
                )
            if scope.last_right < 0 or len(lefts) == 1:
                left = Column(name, lefts[0].name)
            else:
                if any(key not in e.using for e in lefts[1:]):
                    raise OperationalError(f"ambiguous reference to {name} in USING()")
                left = Call("COALESCE", tuple(Column(name, e.name) for e in lefts), defer_affinity=True)
            equal = Binary("=", left, Column(name, right.name))
            condition = equal if condition is None else Binary("AND", condition, equal)
            right.using.add(key)
            if join.kind not in ("RIGHT", "FULL"):
                right.hidden.add(key)
                continue
            # What the unqualified name meant so far, for FULL JOIN's Merge.
            position = right.table.column_index(name)
            affinity = right.table.affinities[position]
            parts = []
            if key in scope.merged:
                merge = scope.merged.pop(key)
                parts, affinity = merge.parts, merge.affinity
            else:
                visible = [e for e in lefts if key not in e.hidden]
                if visible:
                    first = visible[0].table.column_index(name)
                    parts, affinity = [visible[0].offset + first], visible[0].table.affinities[first]
            for entry in lefts:
                entry.hidden.add(key)
            if join.kind == "FULL":
                right.hidden.add(key)
                scope.add_merge(name, index, parts + [right.offset + position], affinity)
        return dataclasses.replace(join, on=condition, using=None, natural=False)

    def where_compiler(self, scope: Scope, aggregate: bool) -> Compiler:
        """The compiler for WHERE and ON.  As in SQLite, an aggregate there is
        an error either way, reported differently in an aggregate query."""
        if aggregate:
            return Compiler(scope, misuse="misuse of aggregate: {name}()", executor=self, allow_aggregates=True)
        return Compiler(scope, executor=self)

    def plan_joins(self, scope: Scope, joins: list[Join], where: Expr | None, order_hint: int | None = None, covering: bool = False, aggregate: bool = False, group_hint: list[tuple[int, str]] | None = None) -> tuple[list[JoinLevel], list[RowFunction]]:
        """Plan a nested loop over ``joins``; returns (levels, constants).

        WHERE conjuncts and the ON conditions of inner joins form one pool of
        filters; each is checked at the first level where all the tables it
        uses are bound.  Conjuncts that use none of the tables (and no
        subquery) are the ``constants``: like SQLite, callers test them once
        before the loop starts and skip it entirely when one is false.  An
        outer (LEFT, RIGHT, FULL) JOIN's ON condition decides which rows
        match at its own level (and is the only thing its access path may
        use).  A RIGHT or FULL JOIN adds its unmatched rows after the loop
        (see join_rows), so a condition on the joined rows is never tested
        before its level.  Without outer joins the tables are joined in the
        cheapest order.
        """
        compiler = self.where_compiler(scope, aggregate)
        rights = [i for i, join in enumerate(joins) if join.kind in ("RIGHT", "FULL")]
        for j, join in enumerate(joins):
            # As in SQLite, an outer join's ON may not use a table to its right
            # (nor any ON, with a RIGHT or FULL JOIN in the FROM clause).
            if join.on is not None and (join.kind != "INNER" or rights):
                scope.watch = set()
                try:
                    Compiler(scope, executor=self, allow_aggregates=True).compile(join.on)
                except OperationalError:
                    pass  # reported below, when compiled for real
                finally:
                    used, scope.watch = scope.watch, None
                if any(index > j for index in used):
                    raise OperationalError("ON clause references tables to its right")

        def floor(j):
            """The lowest level for a condition of join j (len(joins): WHERE)."""
            return max((i for i in rights if i < j), default=0)

        # (conjunct, lowest level, join whose ON it comes from or None)
        pool = [(c, floor(len(joins)), None) for c in split_conjuncts(fold_and(where))]
        for j, join in enumerate(joins):
            if join.kind == "INNER":
                pool += [(c, floor(j), j) for c in split_conjuncts(fold_and(join.on))]
        referenced = []
        constants = []
        for conjunct, lowest, home in pool:
            tables = tables_referenced(conjunct, scope)
            if tables or (home is not None and any(i > home for i in rights)):
                # (A constant ON condition before a RIGHT JOIN only filters
                # the rows it joins to.)
                referenced.append((conjunct, tables, lowest if tables else home, home))
            else:
                constants.append(compiler.compile(conjunct))
        order = list(range(len(joins)))
        if len(joins) > 1 and all(join.kind == "INNER" for join in joins):
            order = self.join_order(scope, [(c, tables) for c, tables, _, _ in referenced], compiler)
            if group_hint and order[0] != 0 and all(len(tables) < 2 for _, tables, _, _ in referenced) and not any(
                    0 in tables for _, tables, _, _ in referenced):
                # A cross join whose first table has an index ordering the
                # groups: SQLite keeps that table outermost to save the sort.
                entry = scope.entries[0]
                if not isinstance(entry.table, DerivedSource):
                    scan = full_scan(self.catalog.table_tree(entry.table), entry.table,
                                     table_rows(self.catalog, entry.table))
                    found = grouping_index_scan(scope, 0, self.catalog, scan, group_hint)
                    if found is not None and found.grouping == "all":
                        order = [0] + [i for i in order if i != 0]
        position = {table: i for i, table in enumerate(order)}
        placed = {}
        for conjunct, tables, lowest, home in referenced:
            level = max([position[t] for t in tables] + [lowest])
            if home is not None and any(i > home for i in rights):
                # An ON condition before a RIGHT JOIN uses no table after its
                # join (checked above); one with a subquery counts as using
                # every table, yet must be tested at its join.
                level = min(level, home)
            placed.setdefault(level, []).append(conjunct)
        # Compile all conditions first: the access paths may then check which
        # columns the query uses (covering indexes).
        compiled = []
        for level, index in enumerate(order):
            join = joins[index]
            match = None
            if join.kind != "INNER" and join.on is not None:
                match = compiler.compile(join.on)
            compiled.append((match, [compiler.compile(f) for f in placed.get(level, [])]))
        levels = []
        for level, (index, (match, filters)) in enumerate(zip(order, compiled)):
            join = joins[index]
            entry = scope.entries[index]
            if join.kind == "INNER":
                usable = [c for c, lowest, _ in pool if lowest <= level]
            else:
                usable = split_conjuncts(join.on)
            hint = order_hint if level == 0 and index == 0 and not rights else None
            access = plan_access(scope, index, self.catalog, usable, compiler, hint,
                                 bound=set(order[:level]))
            if group_hint and type(access) is FullScan and level == 0 and index == 0 and not rights:
                access = grouping_index_scan(scope, index, self.catalog, access, group_hint) or access
            if isinstance(access, IndexScan) and not covering:
                access.index_values = False  # (UPDATE / DELETE read the table)
            if covering and isinstance(access, IndexScan):
                access.cover_if_possible(scope, index)
            elif covering and type(access) is FullScan:
                access = covering_index_scan(scope, index, self.catalog, access) or access
            levels.append(JoinLevel(entry.table, entry.offset, access,
                                    join.kind in ("LEFT", "FULL"), match, filters))
            if join.kind in ("RIGHT", "FULL"):
                scan = plan_access(scope, index, self.catalog, [], compiler)
                levels[-1].unmatched = JoinLevel(entry.table, entry.offset, scan, False, None, [])
            if covering and isinstance(entry.table, TableInfo) and entry.table.virtual:
                # (A SELECT computes only the VIRTUAL columns it uses, as SQLite.)
                unused = unused_virtual(entry.table, {p for i, p in scope.used if i == index})
                if unused:
                    for level in (levels[-1], levels[-1].unmatched):
                        if level is not None:
                            level.skip_virtual(unused)
            levels[-1].merges = [(m.slot, m.parts) for m in scope.merged.values() if m.index == index]
        return levels, constants

    def join_order(self, scope: Scope, referenced: list[tuple[Expr, set[int]]], compiler: Compiler) -> list[int]:
        """The table order with the lowest estimated nested loop cost: every
        permutation for up to 6 tables, greedy beyond.  A condition that only
        filters (no lookup uses it) is guessed to keep a quarter of the rows."""
        count = len(scope.entries)
        pool = [conjunct for conjunct, _ in referenced]
        accesses = {}

        def access(table, bound):
            key = (table, bound)
            if not getattr(scope.entries[table].table, "depends", set()) <= bound:
                return math.inf, math.inf  # (json_each() after the tables its arguments use)
            if key not in accesses:
                plan = plan_access(scope, table, self.catalog, pool, compiler, bound=set(bound))
                rows, cost = plan.estimate()
                if isinstance(plan, (FullScan, DerivedScan)):
                    newly = sum(1 for _, tables in referenced
                                if table in tables and tables <= bound | {table})
                    rows = max(1, rows / RANGE_FACTOR ** newly)
                accesses[key] = rows, cost
            return accesses[key]

        def total(order):
            cost, outer, bound = 0, 1, frozenset()
            for table in order:
                rows, probe = access(table, bound)
                cost += outer * probe
                outer *= rows
                bound |= {table}
            return cost

        if count <= 6:
            best = min(itertools.permutations(range(count)), key=total)  # first of equals
            return list(best)
        order, bound, outer = [], frozenset(), 1
        while len(order) < count:
            table = min((t for t in range(count) if t not in bound),
                        key=lambda t: outer * access(t, bound)[1])
            outer *= access(table, bound)[0]
            order.append(table)
            bound |= {table}
        return order

    @staticmethod
    def join_rows(scope: Scope, levels: list[JoinLevel]) -> Iterator[Row]:
        """Yield every row of the nested loop join.  The same list object is
        yielded each time; callers must copy it to keep it."""
        row = [None] * scope.width
        truth = values.truth
        depth = len(levels)
        for level in levels:
            if isinstance(level.access, HashLookup):
                level.access.reset()
        # (Python allows at most 20 nested blocks: many tables use visit.)
        if levels and len(levels) <= MAX_GENERATED_LEVELS and all(level.plain for level in levels):
            loop = levels[0].loop
            if loop is None:
                loop = levels[0].loop = inner_join_loop(levels)
            return loop(row)

        def passes(conditions):
            for condition in conditions:
                if not truth(condition(row)):
                    return False
            return True

        # Row ids a RIGHT / FULL JOIN level matched, by level.
        matched_ids = {i: set() for i, level in enumerate(levels) if level.unmatched is not None}

        def merge(level):
            for slot, parts in level.merges:
                row[slot] = next((row[p] for p in parts if row[p] is not None), None)

        def visit(i):
            if i == depth:
                yield row
                return
            level = levels[i]
            start, stop = level.offset, level.offset + len(level.table.columns) + 1
            load = level.load
            seen = matched_ids.get(i)
            matched = False
            for rowid, record in level.access.candidates(row):
                row[start:stop] = load(rowid, record)
                if level.merges:
                    merge(level)
                if level.match is not None and not truth(level.match(row)):
                    continue
                matched = True
                if seen is not None:
                    seen.add(row[stop - 1])
                if passes(level.filters):
                    yield from visit(i + 1)
            if level.outer and not matched:
                row[start:stop] = [None] * (stop - start)
                merge(level)
                if passes(level.filters):
                    yield from visit(i + 1)

        def unmatched(i):
            """The rows of a RIGHT / FULL JOIN's table that nothing before it
            matched, with NULL for the tables before, joined to the rest."""
            level = levels[i]
            for before in levels[:i]:
                row[before.offset:before.offset + len(before.table.columns) + 1] = \
                    [None] * (len(before.table.columns) + 1)
                for slot, _ in before.merges:
                    row[slot] = None
            start, stop = level.offset, level.offset + len(level.table.columns) + 1
            scan, seen = level.unmatched, matched_ids[i]
            for rowid, record in scan.access.candidates(row):
                row[start:stop] = scan.load(rowid, record)
                merge(level)
                if row[stop - 1] not in seen and passes(level.filters):
                    yield from visit(i + 1)

        def run():
            yield from visit(0)
            for i in matched_ids:
                yield from unmatched(i)

        return run() if matched_ids else visit(0)

    def explain(self, stmt: Select | Compound | Update | Delete) -> Result:
        """One row (table, access path) per table the statement reads, in join order."""
        if isinstance(stmt, (Select, Compound)):
            compiled = self.compile_query(stmt)
            parts = compiled.parts if isinstance(compiled, CompiledCompound) else [compiled]
            levels = [level for part in parts for level in (part.levels or [])]
        else:
            scope = Scope()
            joins, _ = self.build_from([Join(TableRef(stmt.table))], scope)
            levels, _ = self.plan_joins(scope, joins, stmt.where)
        return Result(
            [(level.table.name, level.access.describe()) for level in levels], ["table", "plan"]
        )

    # ---- SELECT -------------------------------------------------------------

    def expand_items(self, stmt: Select, scope: Scope) -> tuple[list[Expr], list[str]]:
        """Select-list expressions with ``*`` expanded, and the column names."""
        exprs, names = [], []
        for item in stmt.items:
            if isinstance(item.expr, Star):
                for table_name, column_name in scope.star_columns(item.expr.table):
                    exprs.append(Column(column_name, table_name))
                    names.append(column_name)
                continue
            exprs.append(item.expr)
            if item.alias is not None:
                names.append(item.alias)
            elif isinstance(item.expr, Column):
                names.append(item.expr.name)
            else:
                names.append(item.text)
        return exprs, names

    @staticmethod
    def result_column_reference(expr: Expr, names: list[str], clause: str, position: int, scope: Scope) -> int | None:
        """Resolve ORDER BY / GROUP BY shorthands: a column number or an alias.

        Returns the 0-based result column index, or None for a plain expression.
        """
        number = constant_integer(expr)
        if number is not None:
            if not 1 <= number <= len(names):
                raise OperationalError(
                    f"{ordinal(position)} {clause} term out of range - "
                    f"should be between 1 and {len(names)}"
                )
            return number - 1
        if isinstance(expr, Column) and expr.table is None:
            lowered = [ascii_lower(name) for name in names]
            if ascii_lower(expr.name) in lowered:
                if clause == "GROUP BY":
                    try:
                        scope.resolve(expr)
                        return None  # an input column wins over an alias in GROUP BY
                    except OperationalError:
                        pass
                return lowered.index(ascii_lower(expr.name))
        return None

    def order_terms(self, stmt: Select, exprs: list[Expr], names: list[str], compiler: Compiler) -> tuple[list[OrderTerm], list[RowFunction]]:
        """Returns ([(source, index, descending, nulls first, collation)], [functions]).

        ``source`` is "output" (index into the result row) or "key" (index into
        the extra sort values computed by ``functions``).  A COLLATE on a
        column number or alias applies to that result column."""
        terms, functions = [], []
        for position, item in enumerate(stmt.order_by, 1):
            nulls_first = item.nulls_first if item.nulls_first is not None else not item.descending
            inner = strip_collate(item.expr)
            index = self.result_column_reference(inner, names, "ORDER BY", position, compiler.scope)
            if index is not None:
                collation = compiler.collation(item.expr if inner is not item.expr else exprs[index])
                terms.append(("output", index, item.descending, nulls_first, collation))
            else:
                terms.append(("key", len(functions), item.descending, nulls_first, compiler.collation(item.expr)))
                functions.append(compiler.compile(item.expr))
        return terms, functions

    @staticmethod
    def compound_order_terms(stmt: Compound, parts: list[CompiledSelect],
                             collations: list[str | None] | None = None) -> list[OrderTerm]:
        """ORDER BY of a compound SELECT: every term must name a result column
        (by number, by name or alias, or as the same expression).  A term
        sorts by its COLLATE, else by the column's ``collations``."""
        terms = []
        count = len(parts[0].names)
        for position, item in enumerate(stmt.order_by, 1):
            nulls_first = item.nulls_first if item.nulls_first is not None else not item.descending
            explicit = item.expr
            item = dataclasses.replace(item, expr=strip_collate(item.expr))
            index = constant_integer(item.expr)
            if index is not None:
                if not 1 <= index <= count:
                    raise OperationalError(
                        f"{ordinal(position)} ORDER BY term out of range - "
                        f"should be between 1 and {count}"
                    )
                index -= 1
            else:
                for part in reversed(parts):
                    if isinstance(item.expr, Column) and item.expr.table is None:
                        lowered = [ascii_lower(name) for name in part.names]
                        if ascii_lower(item.expr.name) in lowered:
                            index = lowered.index(ascii_lower(item.expr.name))
                            break
                    if item.expr in part.exprs:
                        index = part.exprs.index(item.expr)
                        break
                if index is None:
                    raise OperationalError(
                        f"{ordinal(position)} ORDER BY term does not match any column in the result set"
                    )
            if explicit is not item.expr:
                collation = values.collation_name(explicit.collation)
            else:
                collation = collations[index] if collations else None
            terms.append(("output", index, item.descending, nulls_first, collation))
        return terms

    def group_functions(self, stmt: Select, exprs: list[Expr], names: list[str],
                        scope: Scope) -> tuple[list[RowFunction], list[Callable[[SQLValue], tuple]]]:
        """The GROUP BY terms' functions and sort key functions (by their
        collations; a COLLATE on a column number or alias applies to it)."""
        compiler = Compiler(
            scope, misuse="aggregate functions are not allowed in the GROUP BY clause", executor=self
        )
        functions, keys = [], []
        for position, expr in enumerate(stmt.group_by, 1):
            inner = strip_collate(expr)
            index = self.result_column_reference(inner, names, "GROUP BY", position, scope)
            collation = compiler.collation(expr)
            if index is not None:
                if inner is expr:
                    collation = compiler.collation(exprs[index])
                expr = exprs[index]
            functions.append(compiler.compile(expr))
            keys.append(values.collation_sort_key(collation))
        return functions, keys

    @staticmethod
    def group_rows(rows: Iterable[Row], scope: Scope, group_functions: list[RowFunction], aggregates: AggregateCollector,
                   group_keys: list[Callable[[SQLValue], tuple]] | None = None, in_order: bool = False) -> Iterator[Row]:
        """Aggregate ``rows`` into groups; yield each group's representative row
        followed by its aggregate results, ordered by group key (``in_order``:
        as the groups came, when an index scan delivered them in order)."""
        groups = {}
        keys = group_keys if group_keys is not None else [values.sort_key] * len(group_functions)
        aggregates.grouping_loop(group_functions, keys)(rows, groups, aggregates.new_state)
        if not groups and not group_functions:
            groups[()] = [[None] * scope.width, aggregates.new_state()]
        for key in (groups if in_order else sorted(groups)):
            representative, state = groups[key]
            yield representative + aggregates.results(state)

    def compile_limit(self, stmt: Select | Compound) -> Callable[[], tuple[int, int | None]] | None:
        """Functions () -> (offset, end) for LIMIT/OFFSET, or None."""
        if stmt.limit is None:
            return None
        compiler = Compiler(Scope(), executor=self)
        limit = compiler.compile(stmt.limit)
        offset = compiler.compile(stmt.offset) if stmt.offset is not None else None

        def integer(function):
            value = values.numeric_affinity(function([]))
            if not isinstance(value, int):
                raise IntegrityError("datatype mismatch")
            return value

        def bounds():
            count = integer(limit)
            if count == 0:
                return 0, 0  # (SQLite stops here: the OFFSET is not evaluated)
            start = max(integer(offset), 0) if offset is not None else 0
            return start, None if count < 0 else start + count
        return bounds


    # ---- INSERT --------------------------------------------------------------

    def prepare_row(self, table: TableInfo, row: Row) -> int | None:
        """Apply column affinities; returns the requested row id.  The row
        is SQLite's registers: JSON values keep their subtype (triggers,
        RETURNING and generated columns see it) until stored_row."""
        for i, affinity in enumerate(table.affinities):
            row[i] = values.apply_affinity(row[i], affinity)
        if table.rowid_column is None:
            return None
        rowid = row[table.rowid_column]
        if rowid is not None and not isinstance(rowid, int):
            raise IntegrityError("datatype mismatch")
        return rowid

    @staticmethod
    def stored_row(table: TableInfo, row: Row) -> Row:
        """A new row's values as its record and index entries hold them
        (values.record_value): no JSON subtype, no IntReal."""
        if RECORD_CONVERTED.isdisjoint(map(type, row)):
            return row
        return [values.record_value(v, a) for v, a in zip(row, table.affinities)]

    @staticmethod
    def generator(table: TableInfo) -> Callable[[Row], None]:
        """The function computing the generated columns of a new row (an
        error at once when they make a loop, as SQLite reports it when it
        compiles the statement)."""
        fill = table.fill_generated
        if fill is None:
            fill = table.fill_generated = compile_generated(table, generated_order(table, table.generated))
        return fill

    def not_null_violation(self, table: TableInfo, row: Row, conflict: str | None = None,
                           raw: Row | None = None) -> tuple[str, str] | None:
        """The NOT NULL constraint ``row`` violates, if any, and how to
        resolve it (IGNORE, ABORT, FAIL or ROLLBACK).  Under REPLACE a NULL
        becomes the column's default first, if that is not NULL (also in
        ``raw``, the values before affinities, which upserts may see)."""
        # As SQLite, in two passes: in column order, a REPLACE column with a
        # default gets it, the others are checked; then the REPLACE columns
        # that are still NULL fail as ABORT.
        # Generated columns are checked in the second pass, computed again
        # first if a REPLACE column could have taken its default.
        replaced = []
        for i, column in enumerate(table.columns):
            if not column.not_null or i == table.rowid_column or column.generated is not None:
                continue  # (a NULL row id alias: a new row id)
            how = conflict or column.not_null_conflict or "ABORT"
            if how == "REPLACE":
                if column.default is not None:
                    if row[i] is None:
                        value = Compiler(Scope(), executor=self).compile(column.default)([])
                        row[i] = values.apply_affinity(value, table.affinities[i])
                        if raw is not None:
                            real = table.affinities[i] == values.REAL and type(value) is int
                            raw[i] = float(value) if real else value
                    replaced.append(i)
                    continue
                how = "ABORT"
            if row[i] is None:
                return f"NOT NULL constraint failed: {table.name}.{column.name}", how
        if replaced and table.generated:
            self.generator(table)(row)
            if raw is not None:
                for i in table.generated:
                    raw[i] = row[i]
        for i, column in enumerate(table.columns):
            if row[i] is not None or not (i in replaced or (column.not_null and column.generated is not None)):
                continue
            how = "ABORT" if i in replaced else conflict or column.not_null_conflict or "ABORT"
            return f"NOT NULL constraint failed: {table.name}.{column.name}", "ABORT" if how == "REPLACE" else how
        return None

    def check_violation(self, table: TableInfo, row: Row, conflict: str | None,
                        changed: set[int] | None = None) -> tuple[str, str] | None:
        """The CHECK constraint ``row`` (values and row id) violates, if
        any, and how to resolve it.  An UPDATE (``changed``: the positions
        it assigns, the row id as ``len(columns)``) checks only the
        constraints that use a changed column, as SQLite does."""
        checks = table.compiled_checks
        if checks is None:
            checks = table.compiled_checks = self.compile_checks(table)
        for message, failed, positions in checks:
            if changed is not None and not changed & positions:
                continue
            if failed(row):
                how = conflict or "ABORT"
                return message, "ABORT" if how == "REPLACE" else how
        return None

    def check_new_table(self, table: TableInfo) -> None:
        """The errors CREATE TABLE reports for its expressions."""
        self.check_constraints_compile(table)
        check_generated(table)

    def check_constraints_compile(self, table: TableInfo) -> None:
        """The errors CREATE TABLE reports for its CHECK constraints."""
        for check in table.checks:
            for node in walk(check.expr):
                if isinstance(node, (Subquery, InSelect, Exists)):
                    raise OperationalError("subqueries prohibited in CHECK constraints")
                if isinstance(node, Parameter):
                    raise OperationalError("parameters prohibited in CHECK constraints")
        self.compile_checks(table)

    def compile_checks(self, table: TableInfo) -> list[tuple[str, RowFunction, set[int]]]:
        """(message, failed(row), positions used) for each CHECK constraint."""
        scope = Scope()
        scope.add(table)
        compiler = Compiler(scope, executor=self)
        width = len(table.columns)
        truth = values.truth
        checks = []
        for check in table.checks:
            test = compiler.compile(check.expr)
            positions = check_positions(table, check)

            def failed(row, test=test):
                value = test(row)
                return value is not None and not truth(value)
            checks.append((f"CHECK constraint failed: {check.name or sqlite_dequote(check.text)}", failed, positions))
        return checks

    @staticmethod
    def constraint_error(message: str, conflict: str) -> IntegrityError:
        """A constraint violation under the statement's conflict resolution:
        Database keeps the statement's earlier changes for FAIL and rolls
        back the whole transaction for ROLLBACK (ABORT: just the statement)."""
        error = IntegrityError(message)
        error.resolution = conflict
        return error

    def find_conflict(self, index: IndexInfo, row: Row, own_rowid: int | None) -> int | None:
        """The row id of a row that ``row`` collides with in UNIQUE ``index``
        (not counting row ``own_rowid``: the row being updated)."""
        key_values = [row[p] for p in index.positions]
        if any(v is None for v in key_values):
            return None  # NULLs never conflict
        prefix = index.prefix(key_values)
        row_id = index.row_id
        for key, _ in self.catalog.index_tree(index).scan(prefix, prefix + (HIGH,)):
            other = row_id(key)
            if other != own_rowid:
                return other
        return None

    @staticmethod
    def unique_error(table: TableInfo, index: IndexInfo) -> str:
        return "UNIQUE constraint failed: " + ", ".join(f"{table.name}.{c}" for c in index.column_names)

    def replace_fires(self, table: TableInfo) -> bool:
        """Whether a REPLACE's delete of a row of ``table`` runs triggers."""
        return bool(self.settings["recursive_triggers"]) and self.triggers.exist(table.name, "DELETE")

    def replace_rechecks(self, table: TableInfo) -> bool:
        """Whether SQLite checks the uniqueness constraints again after a
        REPLACE deleted a row of ``table`` (its regTrigCnt): DELETE triggers
        may run, or foreign key work (the table is in a foreign key)."""
        return self.replace_fires(table) or self.foreign_keys.involved(table)

    def recheck_unique(self, table: TableInfo, tree: BTree, row: Row, rowid: int, own: int | None,
                       indexes: list[IndexInfo], last_found: int | None = None) -> None:
        """After a REPLACE that may have run triggers or foreign key actions,
        SQLite checks the row id and the REPLACE ``indexes`` again, as ABORT
        (the triggers may have kept a row - RAISE(IGNORE) - or added one).
        ``own``: the row id of the row being updated.  The recheck copies the
        first pass's code without reading the found entry's row id: it
        compares the row id the first pass found last (``last_found``) with
        ``own``, so an UPDATE trips over its own entry of an index whose key
        it did not change when the first pass last found another row.  (A
        WITHOUT ROWID table's code reads the found entry's PRIMARY KEY, so
        there the comparison is right; its PRIMARY KEY is among ``indexes``.)"""
        if not table.has_rowid:
            for index in indexes:
                if self.find_conflict(index, row, None) not in (None, own):
                    raise self.constraint_error(self.unique_error(table, index), "ABORT")
            return
        if rowid != own and rowid in tree:
            raise self.constraint_error(self.rowid_conflict(table).args[0], "ABORT")
        for index in indexes:
            if self.find_conflict(index, row, None) is not None and (own is None or last_found != own):
                raise self.constraint_error(self.unique_error(table, index), "ABORT")

    def check_pinned(self, table: TableInfo) -> None:
        """SQLite pins the cursor of the row an UPDATE is changing while a
        REPLACE of a UNIQUE conflict deletes another row and runs its DELETE
        triggers (OP_CursorLock); a write to that table from them fails with
        SQLITE_CONSTRAINT_PINNED."""
        depth = self.pinned.get(table.name)
        if depth is not None and self.frame_depth > depth:
            raise self.constraint_error("constraint failed", "ABORT")

    def delete_row(self, table: TableInfo, tree: BTree, rowid: int, replace: bool = False,
                   orconf: str | None = None, fire: bool = True) -> Row | None:
        """Delete a row and its index entries; returns it (with its row id),
        or None if a BEFORE trigger's RAISE(IGNORE) kept it or the trigger
        deleted it.  With foreign keys on: their checks before, their
        actions after.  ``replace``: deleted by REPLACE, which fires DELETE
        triggers only with PRAGMA recursive_triggers (as in SQLite);
        ``fire``: False for DROP TABLE's implicit DELETE."""
        row = self.load_row(table, rowid, tree.get(rowid))
        triggers = self.triggers
        fire = fire and (not replace or self.settings["recursive_triggers"])
        if replace:
            orconf = "REPLACE"
        current = row
        if fire and triggers.matching(table.name, "BEFORE", "DELETE"):
            try:
                triggers.fire(table.name, "BEFORE", "DELETE", row, None, None, orconf)
            except TriggerIgnore:
                return None
            if rowid not in tree:
                return None
            current = self.load_row(table, rowid, tree.get(rowid))
        keys = self.foreign_keys
        involved = keys.involved(table)
        if involved:
            keys.row_removing(table, row)
        if self.pinned:
            self.check_pinned(table)
        self.remove_index_entries(table, current, rowid)
        tree.delete(rowid)
        if involved:
            keys.convert_old(table, row)
            keys.actions(table, row)
        if fire and triggers.matching(table.name, "AFTER", "DELETE"):
            try:
                triggers.fire(table.name, "AFTER", "DELETE", row, None, None, orconf)
            except TriggerIgnore:
                pass
            except Error as exc:
                exc.row_done = 1  # (SQLite counted the row before its AFTER triggers)
                raise
        return row

    def check_unique(self, table: TableInfo, row: Row, rowid: int) -> None:
        """Raise if another row has the same values in a UNIQUE index.

        NULLs never conflict.  Indexes are checked newest first, like SQLite.
        """
        for index in table.indexes:
            if not index.unique:
                continue
            key_values = [row[p] for p in index.positions]
            if any(v is None for v in key_values):
                continue
            prefix = index.prefix(key_values)
            for key, _ in self.catalog.index_tree(index).scan(prefix, prefix + (HIGH,)):
                if index.row_id(key) != rowid:
                    columns = ", ".join(f"{table.name}.{c}" for c in index.column_names)
                    raise IntegrityError(f"UNIQUE constraint failed: {columns}")

    def add_index_entries(self, table: TableInfo, row: Row, rowid: int) -> None:
        for index in table.indexes:
            if not index.table_pk:  # (a WITHOUT ROWID table's PRIMARY KEY tree is the table's)
                self.catalog.index_tree(index).insert(index.key(row, rowid), b"")

    def remove_index_entries(self, table: TableInfo, row: Row, rowid: int) -> None:
        for index in table.indexes:
            if not index.table_pk:
                self.catalog.index_tree(index).delete(index.key(row, rowid))

    @staticmethod
    def row_key(table: TableInfo, row: Row) -> tuple:
        """A WITHOUT ROWID table's key for a row: its PRIMARY KEY's sort keys."""
        pk = table.pk_index
        return pk.prefix([row[p] for p in pk.positions])

    @staticmethod
    def encode(table: TableInfo, row: Row) -> bytes:
        stored = list(row)
        if table.rowid_column is not None:
            stored[table.rowid_column] = None  # kept in the key, not the record
        if table.storage is not None:
            stored = [stored[p] for p in table.storage]  # (not the VIRTUAL columns)
        return encode_record(stored)

    def insert_row(self, table: TableInfo, tree: BTree, row: Row, conflict: str | None = None,
                   upserts: Sequence[PreparedUpsert] = (), rowid: SQLValue = None,
                   defaults: DefaultRegisters | None = None, sequence: list[int] | None = None,
                   single: bool = False) -> tuple[str, Row] | None:
        """Insert ``row`` under a conflict resolution (INSERT OR ...; None:
        each constraint's ON CONFLICT, else ABORT) and the statement's ON
        CONFLICT clauses.  Returns ("insert", row + [rowid]), ("update",
        row + [rowid]) when an upsert updated an existing row, or None when
        nothing changed (IGNORE, DO NOTHING, DO UPDATE ... WHERE false).

        As in SQLite: NOT NULL is checked first, then CHECK, then the upsert
        targets in clause order, then the row id and the other UNIQUE
        indexes (newest first, those to REPLACE last).  REPLACE deletes each
        conflicting row and goes on.  ``rowid`` is a row id given by name
        for a table without an INTEGER PRIMARY KEY; ``defaults``: the
        statement's columns filled with their defaults; ``sequence``: the
        AUTOINCREMENT counter of the statement (a one-item list)."""
        # The values before column affinities (see below); SQLite has already
        # made integers in REAL columns REALs (OP_RealAffinity).
        raw = [float(v) if a == values.REAL and type(v) is int else v for v, a in zip(row, table.affinities)]
        given, rowid = rowid, self.prepare_row(table, row)
        if defaults is not None and defaults.converted:
            for position in defaults.positions:
                raw[position] = row[position]
        if given is not None:
            rowid = values.apply_affinity(given, values.INTEGER)
            if not isinstance(rowid, int):
                raise IntegrityError("datatype mismatch")
        triggers = self.triggers
        if triggers.matching(table.name, "BEFORE", "INSERT"):
            # NEW has the values with their affinities, and row id -1 when it is not known yet.
            new = row + [-1 if rowid is None else rowid]
            if table.rowid_column is not None:
                new[table.rowid_column] = new[-1]
            if table.generated:
                self.generator(table)(new)
            try:
                triggers.fire(table.name, "BEFORE", "INSERT", None, new, None, conflict)
            except TriggerIgnore:
                return None
        if sequence is not None and rowid is not None:
            sequence[0] = max(sequence[0], rowid)
        fresh = rowid is None  # (a new row id is never taken)
        keyed = not table.has_rowid  # (WITHOUT ROWID: the PRIMARY KEY is the key)
        if table.generated:
            # SQLite computes them once the row id is known, before the constraints.
            if fresh and not keyed:
                rowid = self.new_rowid(tree, sequence)
            if table.rowid_column is not None:
                row[table.rowid_column] = raw[table.rowid_column] = rowid
            self.generator(table)(row)
            for position in table.generated:
                raw[position] = row[position]
        violation = self.not_null_violation(table, row, conflict, raw)
        if violation is not None:
            if violation[1] == "IGNORE":
                return None
            raise self.constraint_error(*violation)
        if keyed:
            rowid = self.row_key(table, row)
        elif fresh and not table.generated:
            rowid = self.new_rowid(tree, sequence)
        if table.rowid_column is not None:
            row[table.rowid_column] = raw[table.rowid_column] = rowid
        if table.checks and not self.settings["ignore_check_constraints"]:
            # (SQLite applies the column affinities in place before it tests CHECK constraints.)
            if defaults is not None:
                defaults.converted = True
            violation = self.check_violation(table, row + [rowid], conflict)
            if violation is not None:
                if violation[1] == "IGNORE":
                    return None
                raise self.constraint_error(*violation)
        constraints = [u.constraint for u in upserts if u.constraint is not None]
        rowid_how = conflict or table.rowid_conflict() or "ABORT"
        unique = [i for i in table.indexes if i.unique]
        if keyed:
            pass  # (the PRIMARY KEY's uniqueness is that of its index)
        elif rowid_how == "REPLACE" and conflict is None and unique and "rowid" not in constraints:
            unique.append("rowid")  # SQLite defers a REPLACE of the row id until after the others
        else:
            unique.insert(0, "rowid")
        constraints += [c for c in unique if c not in constraints]
        # SQLite applies the column affinities to the new values in place when
        # it checks the first index (or a CHECK constraint); an upsert's
        # "excluded" row shows them converted only if the conflict was found
        # after that.
        converted = bool(table.checks) and not self.settings["ignore_check_constraints"]
        replaced = False  # a REPLACE deleted a row and its DELETE triggers ran
        for constraint in constraints:
            if constraint == "rowid":
                other = rowid if not fresh and rowid in tree else None
            else:
                converted = True
                if defaults is not None:
                    defaults.converted = True
                other = self.find_conflict(constraint, row, None)
            if other is None:
                continue
            upsert = next((u for u in upserts if u.constraint in (constraint, None)), None)
            if upsert is not None:
                return upsert.apply(tree, other, (row if converted else raw) + [rowid])
            how = rowid_how if constraint == "rowid" else conflict or constraint.conflict or "ABORT"
            if how == "IGNORE":
                return None
            if how == "REPLACE":
                self.delete_row(table, tree, other, replace=True)
                replaced = replaced or self.replace_rechecks(table)
                continue
            message = self.rowid_conflict(table).args[0] if constraint == "rowid" else self.unique_error(table, constraint)
            raise self.constraint_error(message, how)
        if replaced:
            self.recheck_unique(table, tree, row, rowid, None,
                                [c for c in constraints if c != "rowid" and (conflict or c.conflict) == "REPLACE"])
        if defaults is not None:
            defaults.converted = True  # (OP_MakeRecord converts in place too)
        if self.foreign_keys.involved(table):
            self.foreign_keys.row_inserted(table, row + [rowid], single)
        if self.pinned:
            self.check_pinned(table)
        stored = self.stored_row(table, row)
        tree.insert(rowid, self.encode(table, stored))
        self.add_index_entries(table, stored, rowid)
        if triggers.matching(table.name, "AFTER", "INSERT"):
            if not keyed:
                self.last_insert_rowid = rowid  # (the trigger sees it)
            try:
                triggers.fire(table.name, "AFTER", "INSERT", None, row + [rowid], None, conflict)
            except TriggerIgnore:
                pass
            except Error as exc:
                exc.row_done = 1
                raise
        return "insert", row + [rowid]

    def update_row(self, table: TableInfo, tree: BTree, rowid: int, old: Row, new: Row, conflict: str | None = None,
                   changed: set[int] | None = None) -> Row | None:
        """Replace row ``rowid`` (``old``: its values and row id) with ``new``
        (values and row id).  Returns the stored row with its row id, or
        None if IGNORE skipped it.  ``changed``: the positions assigned (for
        the CHECK constraints)."""
        width = len(table.columns)
        row = new[:width]
        keyed = not table.has_rowid  # (WITHOUT ROWID: the PRIMARY KEY is the key)
        if keyed:
            self.prepare_row(table, row)
            new_rowid = self.row_key(table, row)
        elif table.rowid_column is None:
            new_rowid = values.numeric_affinity(new[width])
            if not isinstance(new_rowid, int):
                raise IntegrityError("datatype mismatch")
            self.prepare_row(table, row)
        else:
            new_rowid = self.prepare_row(table, row)
            if new_rowid is None:
                raise IntegrityError("datatype mismatch")
        if table.generated:
            self.generator(table)(row)
        triggers = self.triggers
        names = None if changed is None else trigger_names(table, changed)
        current = old
        before = triggers.matching(table.name, "BEFORE", "UPDATE", names)
        if before:
            new = row
            if table.generated and changed is not None:
                # SQLite loads only the columns the UPDATE sets or the BEFORE
                # triggers name as new.x (sqlite3TriggerColmask): NEW's
                # generated columns see NULL for the others.
                used = new_columns_used(table, before)
                new = [None if (c.generated is None and i not in changed and i != table.rowid_column and i < 32
                                and used is not None and i not in used) else v
                       for i, (c, v) in enumerate(zip(table.columns, row))]
                self.generator(table)(new)
            try:
                triggers.fire(table.name, "BEFORE", "UPDATE", old, new + [new_rowid], names, conflict)
            except TriggerIgnore:
                return None
            if rowid not in tree:
                return None  # (the trigger deleted it)
            # The trigger may have changed the row: the columns the UPDATE does
            # not set take their values from it now (SQLite's trigger1-18.0).
            current = self.load_row(table, rowid, tree.get(rowid))
            for i in range(width):
                if (changed is None or i not in changed) and i != table.rowid_column:
                    row[i] = current[i]
            if table.generated:
                self.generator(table)(row)
            if keyed:
                new_rowid = self.row_key(table, row)
        violation = self.not_null_violation(table, row, conflict)
        if violation is not None:
            if violation[1] == "IGNORE":
                return None
            raise self.constraint_error(*violation)
        if keyed:
            new_rowid = self.row_key(table, row)  # (REPLACE may have given a PRIMARY KEY column its default)
        if table.checks and not self.settings["ignore_check_constraints"]:
            if changed is not None and table.rowid_column in changed:
                changed = changed | {width}
            violation = self.check_violation(table, row + [new_rowid], conflict, changed)
            if violation is not None:
                if violation[1] == "IGNORE":
                    return None
                raise self.constraint_error(*violation)
        # SQLite checks the indexes with a column the UPDATE sets, or all of
        # them when it sets the row id or a foreign key needs it (update.c's aRegIdx).
        width = len(table.columns)
        every = (changed is None or table.rowid_column in changed or width in changed
                 or self.foreign_keys.every_index(table, changed))
        if keyed and not every:  # (a new PRIMARY KEY changes every index's entry: update.c's chngPk)
            every = any(p in changed for p in table.pk_index.positions)
        constraints = [i for i in table.indexes if i.unique and (every or any(p in changed for p in i.positions))]
        rowid_how = conflict or table.rowid_conflict() or "ABORT"
        if keyed:
            pass  # (the PRIMARY KEY's uniqueness is that of its index)
        elif rowid_how == "REPLACE" and conflict is None and constraints:
            constraints.append("rowid")
        else:
            constraints.insert(0, "rowid")
        replaced = False  # a REPLACE deleted a row and SQLite will check again (replace_rechecks)
        last_found = None  # the row id the last index lookup found (recheck_unique)
        for constraint in constraints:
            if constraint == "rowid":
                if new_rowid == rowid or new_rowid not in tree:
                    continue
                other, how = new_rowid, rowid_how
            else:
                other = self.find_conflict(constraint, row, None)
                if other is None:
                    continue
                last_found = other
                if other == rowid:
                    continue
                how = conflict or constraint.conflict or "ABORT"
            if how == "IGNORE":
                return None
            if how != "REPLACE":
                message = (self.rowid_conflict(table).args[0] if constraint == "rowid"
                           else self.unique_error(table, constraint))
                raise self.constraint_error(message, how)
            fires = self.replace_fires(table)
            pin = fires and constraint != "rowid"
            if pin:
                saved_pin = self.pinned.get(table.name)
                self.pinned[table.name] = self.frame_depth
            try:
                self.delete_row(table, tree, other, replace=True)
            finally:
                if pin:
                    if saved_pin is None:
                        del self.pinned[table.name]
                    else:
                        self.pinned[table.name] = saved_pin
            replaced = replaced or fires or self.foreign_keys.involved(table)
        if replaced:
            self.recheck_unique(table, tree, row, new_rowid, rowid,
                                [c for c in constraints if c != "rowid" and (conflict or c.conflict) == "REPLACE"],
                                last_found)
        keys = self.foreign_keys
        involved = keys.involved(table) and keys.required(table, changed)
        if involved:
            keys.row_removing(table, old, changed)
        if self.pinned:
            self.check_pinned(table)
        self.remove_index_entries(table, current, rowid)
        if involved:
            keys.convert_old(table, old, changed)
        if new_rowid != rowid or (keyed and involved and (
                changed is None or keys.every_index(table, changed) or changed & set(table.pk_index.positions))):
            # (SQLite deletes the old row first when the SET assigns the
            # PRIMARY KEY - chngPk, whatever the value - or a foreign key
            # needs it - hasFK>1: a WITHOUT ROWID table's row is then gone
            # while its new foreign keys are looked up)
            tree.delete(rowid)
        if involved:
            keys.row_adding(table, row + [new_rowid], changed)
        stored = self.stored_row(table, row)
        tree.insert(new_rowid, self.encode(table, stored), replace=True)
        self.add_index_entries(table, stored, new_rowid)
        if involved:
            keys.actions(table, old, row + [new_rowid], changed)
        if triggers.matching(table.name, "AFTER", "UPDATE", names):
            try:
                triggers.fire(table.name, "AFTER", "UPDATE", old, row + [new_rowid], names, conflict)
            except TriggerIgnore:
                pass
            except Error as exc:
                exc.row_done = 1
                raise
        return row + [new_rowid]

    def compile_returning(self, items: list[SelectItem] | None, scope: Scope) -> tuple[list[RowFunction], list[str]] | None:
        """RETURNING: functions of a changed row (its values and row id) and the column names."""
        if items is None:
            return None
        exprs, names = self.expand_items(Select(items), scope)
        compiler = Compiler(scope, executor=self)
        return [compiler.compile(e) for e in exprs], names

    @staticmethod
    def new_rowid(tree: BTree, sequence: list[int] | None = None) -> int:
        """One more than the largest row id; if that is taken by the maximum
        integer, try random ones like SQLite does.  With AUTOINCREMENT
        (``sequence``: the largest row id the table ever had) one more than
        the larger of the two, or the database is "full"."""
        last = tree.last_key()
        if sequence is not None:
            rowid = max(sequence[0], last or 0) + 1
            if rowid > values.INT_MAX:
                raise OperationalError("database or disk is full")
            sequence[0] = rowid
            return rowid
        if last is None:
            return 1
        if last < values.INT_MAX:
            return last + 1
        for _ in range(100):
            candidate = random.randint(1, 2**62)
            if candidate not in tree:
                return candidate
        raise OperationalError("database or disk is full")

    def sequence_value(self, table: TableInfo) -> int | None:
        """The AUTOINCREMENT counter of ``table`` in sqlite_sequence, if any."""
        sequence = self.catalog.owner(table).tables.get("sqlite_sequence")
        if sequence is None:
            return None
        for rowid, record in self.catalog.table_tree(sequence).scan():
            row = self.load_row(sequence, rowid, record)
            if row[0] == table.name:
                return values.to_int64(row[1]) if row[1] is not None else 0
        return None

    def set_sequence_value(self, table: TableInfo, value: int) -> None:
        sequence = self.catalog.owner(table).tables.get("sqlite_sequence")
        if sequence is None:
            return
        tree = self.catalog.table_tree(sequence)
        for rowid, record in tree.scan():
            if self.load_row(sequence, rowid, record)[0] == table.name:
                tree.insert(rowid, encode_record([table.name, value]), replace=True)
                return
        tree.insert(self.new_rowid(tree), encode_record([table.name, value]))

    @staticmethod
    def rowid_conflict(table: TableInfo) -> IntegrityError:
        if table.rowid_column is None:
            return IntegrityError(f"UNIQUE constraint failed: {table.name}.rowid")
        name = table.columns[table.rowid_column].name
        return IntegrityError(f"UNIQUE constraint failed: {table.name}.{name}")

    # ---- indexes -------------------------------------------------------------

    # ---- ALTER TABLE ------------------------------------------------------------

    def alter_table(self, stmt: AlterTable) -> Result:
        """ALTER TABLE edits the stored SQL text the way SQLite does (so
        whatever MiniDB does not model in it survives)."""
        catalog = self.catalog
        table = catalog.get_table(stmt.table, stmt.schema)
        catalog.check_writable(table, "altered")
        if stmt.action != "add":
            self.check_schema_resolves(table)
        if stmt.action == "rename":
            self.rename_table(table, stmt.new_name)
        elif stmt.action == "rename column":
            self.rename_column(table, stmt.column, stmt.new_name, stmt.new_quoted)
        elif stmt.action == "add":
            self.add_column(table, stmt.definition, stmt.definition_text)
        else:
            self.drop_column(table, stmt.column)
        catalog.load()
        return Result()

    def check_schema_resolves(self, table: TableInfo) -> None:
        """SQLite's check before RENAME and DROP COLUMN (renameTestSchema,
        sqlite_rename_table): every view and trigger of the table's database
        (in schema order) and, for a main table, of the temp database too
        must still compile."""
        catalogs = [self.catalog.temp] if table.temp else list(reversed(self.catalog.search()))
        for catalog in catalogs:
            items = sorted([*catalog.views.values(), *catalog.triggers.values()], key=lambda o: o.schema_key or 0)
            for item in items:
                saved = self.cte_scopes
                self.cte_scopes = []
                self.triggers.disabled += 1
                try:
                    if isinstance(item, ViewInfo):
                        self.view_source(item)
                    else:
                        owner = self.catalog.temp if item.on_temp else self.catalog
                        source = owner.tables.get(ascii_lower(item.table_name))
                        if source is None:
                            source = self.view_source(owner.views[ascii_lower(item.table_name)])
                        Program(self, item, source, None)
                except OperationalError as exc:
                    kind = "view" if isinstance(item, ViewInfo) else "trigger"
                    raise OperationalError(f"error in {kind} {item.name}: {exc.args[0]}") from None
                finally:
                    self.triggers.disabled -= 1
                    self.cte_scopes = saved

    def _views(self) -> list[ViewInfo]:
        return self.catalog.all_views()

    def _view_references(self, view: ViewInfo, table: TableInfo) -> list[Column] | None:
        """The column references of a view that resolve to ``table`` (None if
        the view does not compile)."""
        found = []
        saved = self.cte_scopes
        self.cte_scopes = []
        self.column_hook = lambda expr, source: found.append(expr) if source is table else None
        try:
            self.compile_query(view.query)
        except Error:
            return None
        finally:
            self.column_hook = None
            self.cte_scopes = saved
        return found

    def _trigger_references(self, trigger: TriggerInfo, table: TableInfo) -> list[Column] | None:
        """The column references of a trigger's program that resolve to
        ``table`` (None if the program does not compile)."""
        found = []
        catalog = self.catalog.temp if trigger.on_temp else self.catalog
        source = catalog.tables.get(ascii_lower(trigger.table_name))
        if source is None:
            view = catalog.views.get(ascii_lower(trigger.table_name))
            if view is None:
                return None
        self.column_hook = lambda expr, owner: found.append(expr) if owner is table else None
        self.triggers.disabled += 1  # (not the programs of the triggers its statements fire)
        try:
            Program(self, trigger, source if source is not None else self.view_source(view), None)
        except Error:
            return None
        finally:
            self.column_hook = None
            self.triggers.disabled -= 1
        return found

    @staticmethod
    def _trigger_nodes(trigger: TriggerInfo) -> Iterator[object]:
        yield from walk_nodes(trigger.stmt.body)
        yield from walk_nodes(trigger.stmt.when)

    def _referencing_tables(self, table: TableInfo) -> list[tuple[TableInfo, CreateTable]]:
        """The tables (``table`` too) with a foreign key to ``table``, and their parsed SQL."""
        found = []
        for other in self.catalog.owner(table).tables.values():  # (a parent is in its child's database)
            if any(ascii_lower(key.parent) == ascii_lower(table.name) for key in other.foreign_keys):
                found.append((other, parse(other.sql)))
        return found

    def rename_table(self, table: TableInfo, new: str) -> None:
        catalog = self.catalog
        lowered = ascii_lower(new)
        own = catalog.owner(table)
        if lowered in own.tables or lowered in own.views or lowered in own.indexes:
            raise OperationalError(f"there is already another table or index with this name: {new}")
        if lowered.startswith(catalog.reserved_prefixes):
            raise OperationalError(f"object name reserved for internal use: {new}")
        old = table.name
        for view in self._views():
            stmt = parse(view.sql)
            edits = [(node.pos, quote(new)) for node in walk_nodes(stmt.query)
                     if isinstance(node, TableRef) and ascii_lower(node.name) == ascii_lower(old)]
            edits += [(node.table_pos, quote(new)) for node in walk_nodes(stmt.query)
                      if isinstance(node, Column) and node.table is not None
                      and ascii_lower(node.table) == ascii_lower(old) and node.table_pos >= 0]
            if edits:
                catalog.rewrite_view(view, apply_edits(view.sql, edits))
        # Triggers: their table, and the table in their statements (as alter.c does).
        for trigger in catalog.all_triggers():
            changes, target = [], trigger.table_name
            if ascii_lower(trigger.table_name) == ascii_lower(old):
                changes.append((trigger.stmt.table_pos, quote(new)))
                target = new
            for node in self._trigger_nodes(trigger):
                if isinstance(node, TableRef) and ascii_lower(node.name) == ascii_lower(old) and node.pos >= 0:
                    changes.append((node.pos, quote(new)))
                elif (isinstance(node, (Insert, Update, Delete)) and ascii_lower(node.table) == ascii_lower(old)
                      and node.table_pos >= 0):
                    changes.append((node.table_pos, quote(new)))
                elif (isinstance(node, Column) and node.table is not None
                      and ascii_lower(node.table) == ascii_lower(old) and node.table_pos >= 0):
                    changes.append((node.table_pos, quote(new)))
            if changes:
                catalog.rewrite_trigger(trigger, apply_edits(trigger.sql, changes), target)
        # The foreign keys naming it (its own too), and its own name.
        edits = {table: [(parse(table.sql).name_pos, quote(new))]}
        for other, stmt in self._referencing_tables(table):
            edits.setdefault(other, []).extend((key.parent_pos, quote(new)) for key in all_foreign_keys(stmt)
                                               if ascii_lower(key.parent) == ascii_lower(old))
        for other, changes in edits.items():
            other.sql = apply_edits(other.sql, changes)
            if other is not table:
                catalog.rewrite_table_entries(other)
        table.name = new
        for index in table.indexes:
            if index.is_auto:
                prefix = catalog.auto_prefix
                index.name = prefix + new + index.name[len(prefix) + len(old):]
                index.sql = None
            else:
                index.sql = apply_edits(index.sql, [(parse(index.sql).table_pos, quote(new))])
        sequence = own.tables.get("sqlite_sequence")
        if sequence is not None:
            tree = catalog.table_tree(sequence)
            for rowid, record in list(tree.scan()):
                row = self.load_row(sequence, rowid, record)
                if row[0] == old:
                    tree.insert(rowid, encode_record([new, row[1]]), replace=True)
        catalog.rewrite_table_entries(table)

    def rename_column(self, table: TableInfo, old: str, new: str, new_quoted: bool = False) -> None:
        catalog = self.catalog
        position = table.column_index(old)
        if position is None:
            raise OperationalError(f'no such column: "{old}"')
        if any(ascii_lower(c.name) == ascii_lower(new) for i, c in enumerate(table.columns) if i != position):
            raise OperationalError(f"error in table {table.name} after rename: duplicate column name: {new}")
        old = ascii_lower(table.columns[position].name)

        def renamed(text, pos):
            """The new name for the token at ``pos``, quoted as SQLite does."""
            bare = text[pos:pos + 1].isalnum() or text[pos:pos + 1] in "_$" or ord(text[pos]) > 127
            return new if bare and not new_quoted else quote(new)

        for view in self._views():
            references = self._view_references(view, table)
            edits = [(node.pos, plain_identifier(new)) for node in references or ()
                     if node.pos >= 0 and ascii_lower(node.name) == old]
            if edits:
                catalog.rewrite_view(view, apply_edits(view.sql, edits))
        for trigger in catalog.all_triggers():
            on_table = ascii_lower(trigger.table_name) == ascii_lower(table.name)
            stmt, positions = trigger.stmt, []
            if on_table and stmt.columns:
                positions += [p for name, p in zip(stmt.columns, stmt.column_pos) if ascii_lower(name) == old]
            for node in self._trigger_nodes(trigger):
                if (on_table and isinstance(node, Column) and node.table is not None
                        and ascii_lower(node.table) in ("new", "old") and ascii_lower(node.name) == old):
                    positions.append(node.pos)
                elif isinstance(node, Insert) and ascii_lower(node.table) == ascii_lower(table.name) and node.columns:
                    positions += [p for name, p in zip(node.columns, node.column_pos) if ascii_lower(name) == old]
                elif isinstance(node, Update) and ascii_lower(node.table) == ascii_lower(table.name):
                    positions += [p for (name, _), p in zip(node.assignments, node.assignment_pos)
                                  if ascii_lower(name) == old]
            positions += [node.pos for node in self._trigger_references(trigger, table) or ()
                          if ascii_lower(node.name) == old]
            positions = sorted({p for p in positions if p >= 0})
            if positions:
                catalog.rewrite_trigger(trigger, apply_edits(trigger.sql, [(p, renamed(trigger.sql, p))
                                                                           for p in positions]), trigger.table_name)
        own = parse(table.sql)
        edits = {table: [c.pos for c in own.columns if ascii_lower(c.name) == old]}
        expressions = [c.expr for c in all_constraints(own) if isinstance(c, CheckConstraint)]
        expressions += [c.generated for c in own.columns if c.generated is not None]
        for expr in expressions:
            edits[table] += [node.pos for node in walk_nodes(expr)
                             if isinstance(node, Column) and ascii_lower(node.name) == old and node.pos >= 0
                             and (node.table is None or ascii_lower(node.table) == ascii_lower(table.name))]
        for key in [c for c in all_constraints(own) if isinstance(c, KeyConstraint)]:
            edits[table] += [c.pos for c in key.columns if ascii_lower(c.name) == old and not key.column_level]
        for key in all_foreign_keys(own):
            edits[table] += [p for name, p in zip(key.columns, key.column_pos)
                             if ascii_lower(name) == old and p not in edits[table]]
        for other, stmt in self._referencing_tables(table):
            for key in all_foreign_keys(stmt):
                if ascii_lower(key.parent) == ascii_lower(table.name):
                    edits.setdefault(other, []).extend(
                        p for name, p in zip(key.parent_columns, key.parent_column_pos) if ascii_lower(name) == old)
        for other, positions in edits.items():
            other.sql = apply_edits(other.sql, [(p, renamed(other.sql, p)) for p in positions])
            if other is not table:
                catalog.rewrite_table_entries(other)
        for index in table.indexes:
            if index.sql is not None and not index.is_auto:
                stmt = parse(index.sql)
                positions = [p for name, p in zip(stmt.columns, stmt.column_pos) if ascii_lower(name) == old]
                index.sql = apply_edits(index.sql, [(p, renamed(index.sql, p)) for p in positions])
        catalog.rewrite_table_entries(table)

    def add_column(self, table: TableInfo, column: ColumnDef, text: str) -> None:
        if table.column_index(column.name) is not None:
            raise OperationalError(f"duplicate column name: {column.name}")
        if column.primary_key:
            raise OperationalError("Cannot add a PRIMARY KEY column")
        if column.unique:
            raise OperationalError("Cannot add a UNIQUE column")
        tree = self.catalog.table_tree(table)
        if column.generated is None:
            if not is_constant_default(column.default):
                raise OperationalError("Cannot add a column with non-constant default")
            if column.not_null and constant_default(column.default) is None:
                raise OperationalError("Cannot add a NOT NULL column with default value NULL")
        elif column.stored and next(iter(tree.scan()), None) is not None:
            raise OperationalError("cannot add a STORED column")  # (to a table with rows)
        if column.collation is not None:
            values.collation_name(column.collation)
        stmt = parse(table.sql)
        sql = table.sql[:stmt.columns_end] + ", " + text.rstrip("; \t\n\r\f\v") + table.sql[stmt.columns_end:]
        added = TableInfo(table.name, table.columns + [column], table.root, table.schema_key,
                          table.table_constraints, sql, table.without_rowid)
        added.validate()
        self.check_constraints_compile(added)
        try:
            check_generated(added, loops=False)
        except OperationalError as exc:
            raise OperationalError(f"error in table {table.name} after add column: {exc.args[0]}") from None
        if added.checks or (column.not_null and column.generated is not None):
            # As SQLite: the rows must pass the table's CHECK and NOT NULL
            # constraints now (the first problem PRAGMA quick_check finds).
            checks = self.compile_checks(added)
            not_null = [i for i, c in enumerate(added.columns) if c.not_null and i != added.rowid_column]
            # (Only the VIRTUAL columns those constraints use are computed.)
            unused = unused_virtual(added, set(not_null).union(*(check_positions(added, c) for c in added.checks)))
            for rowid, record in tree.scan():
                stored = record if type(record) is list else decode_row(record)
                row = expand_virtual(added, stored, rowid, unused) if added.virtual else self.load_row(added, rowid, record)
                if any(row[i] is None for i in not_null):
                    raise OperationalError("NOT NULL constraint failed")  # (SQLite's raise() in a nested statement)
                if any(failed(row) for _, failed, _ in checks):
                    raise OperationalError("CHECK constraint failed")
        table.sql = sql
        self.catalog.rewrite_table_entries(table)

    def drop_column(self, table: TableInfo, name: str) -> None:
        position = table.column_index(name)
        if position is None:
            raise OperationalError(f'no such column: "{name}"')
        column = table.columns[position]
        primary = table.primary_key
        if primary is not None and any(ascii_lower(c.name) == ascii_lower(column.name) for c in primary.columns):
            raise OperationalError(f'cannot drop PRIMARY KEY column: "{column.name}"')
        if column.unique:
            raise OperationalError(f'cannot drop UNIQUE column: "{column.name}"')
        if len(table.columns) == 1:
            raise OperationalError(f'cannot drop column "{column.name}": no other columns exist')
        stmt = parse(table.sql)
        start = stmt.columns[position].pos
        if position < len(table.columns) - 1:
            sql = table.sql[:start] + table.sql[stmt.columns[position + 1].pos:]
        else:
            start = table.sql.rindex(",", 0, start)
            sql = table.sql[:start] + table.sql[stmt.columns_end:]
        try:
            changed = parse(sql)
            dropped = TableInfo(table.name, changed.columns, table.root, None, changed.constraints, sql,
                                changed.without_rowid)
            dropped.validate()
            self.check_new_table(dropped)
        except Error as exc:
            raise OperationalError(f"error in table {table.name} after drop column: {exc.args[0]}") from None
        for index in table.indexes:
            if position in index.positions and not index.is_auto:
                raise OperationalError(
                    f"error in index {index.name} after drop column: no such column: {column.name}")
        for view in self._views():
            references = self._view_references(view, table)
            if any(ascii_lower(node.name) == ascii_lower(column.name) for node in references or ()):
                raise OperationalError(
                    f"error in view {view.name} after drop column: no such column: {column.name}")
        for trigger in self.catalog.all_triggers():
            dropped_name = ascii_lower(column.name)
            used = [node for node in self._trigger_references(trigger, table) or ()
                    if ascii_lower(node.name) == dropped_name]
            if ascii_lower(trigger.table_name) == ascii_lower(table.name):
                used += [node for node in self._trigger_nodes(trigger) if isinstance(node, Column)
                         and node.table is not None and ascii_lower(node.table) in ("new", "old")
                         and ascii_lower(node.name) == dropped_name]
            if used:
                first = min(used, key=lambda node: node.pos)  # (SQLite reports the first, as written)
                name = first.name if first.table is None else f"{first.table}.{first.name}"
                raise OperationalError(f"error in trigger {trigger.name} after drop column: no such column: {name}")
        if position not in table.virtual:  # (a VIRTUAL column is in no record)
            tree = self.catalog.table_tree(table)
            rows = [(rowid, self.load_row(table, rowid, record)) for rowid, record in tree.scan()]
            for rowid, row in rows:
                tree.insert(rowid, self.encode(dropped, row[:position] + row[position + 1:-1]), replace=True)
        table.sql = sql
        self.catalog.rewrite_table_entries(table)

    def reindex(self, name: str | None) -> Result:
        """Rebuild the indexes of ``name`` (an index, a table, or the default
        collation BINARY), or all of them."""
        catalog = self.catalog
        lowered = None if name is None else ascii_lower(name)
        named = next((c for c in catalog.search() if lowered in c.indexes or lowered in c.tables), None)
        if lowered is None or lowered == "binary":
            indexes = catalog.all_indexes()
        elif named is not None and lowered in named.indexes:
            indexes = [named.indexes[lowered]]
        elif named is not None:
            indexes = list(named.tables[lowered].indexes)
        elif lowered in ("nocase", "rtrim"):
            indexes = []  # no index uses these collations
        else:
            raise OperationalError("unable to identify the object to be reindexed")
        for index in indexes:
            if index.table_pk:
                continue  # (the table itself)
            catalog.index_tree(index).clear()
            self.build_index(index)
        return Result()

    def vacuum(self, stmt: Vacuum) -> Result:
        """Rebuild the database compactly: every table and index is copied, in
        schema order, into a new in-memory database with bulk_load, whose
        pages then replace the database's own (the page count shrinks and the
        free list is gone; a checkpoint shrinks the file).  ``VACUUM INTO``
        writes the copy to a new file instead."""
        if stmt.schema == "temp":
            return Result()  # temporary views live in memory only
        if stmt.into is not None:
            path = Compiler(Scope(), executor=self).compile(stmt.into)([])
            if not isinstance(path, str):
                raise OperationalError("non-text filename")
            if os.path.exists(path) and os.path.getsize(path) > 0:
                raise OperationalError("output file already exists")
            target = SqlitePager(path, page_size=self.vacuum_page_size()) if self.catalog.sqlite else Pager(path)
            try:
                if self.catalog.sqlite:
                    self.vacuum_auto_vacuum(target)
                self.copy_database(target, keep_rowids=True)
                target.commit()
                target.end_transaction()
                target.checkpoint()
            finally:
                target.close_files()
            return Result()
        pager = self.catalog.pager
        copy = SqlitePager(page_size=self.vacuum_page_size()) if self.catalog.sqlite else Pager()
        if self.catalog.sqlite:
            self.vacuum_auto_vacuum(copy)
        self.copy_database(copy, keep_rowids=False)
        if self.catalog.sqlite and copy.geometry.page_size != pager.geometry.page_size:
            pager.resize(copy.geometry.page_size)
        count = copy.page_count
        for pgno in range(1, count + self.catalog.sqlite):  # (SQLite's pages count from 1)
            if pgno in copy.cache:
                pager.write(copy.cache[pgno])
        pager.write(pager.header)
        pager.header.page_count = count
        last = count  # the last page kept
        if self.catalog.sqlite:
            pager.header.freelist_trunk = pager.header.freelist_count = 0
            pager.header.autovacuum_root = copy.header.autovacuum_root
            pager.header.incremental_vacuum = copy.header.incremental_vacuum
            pager.note_schema_change()
        else:
            pager.header.freelist_head = 0
            last = count - 1
        for pgno in [p for p in pager.cache if p > last]:
            del pager.cache[pgno]
            pager.dirty.discard(pgno)
        self.catalog.load()
        return Result()

    def vacuum_auto_vacuum(self, target: SqlitePager) -> None:
        """The new database VACUUM writes has the auto_vacuum mode PRAGMA
        auto_vacuum asked for, or the current one (as SQLite)."""
        pager = self.catalog.pager
        mode = pager.next_auto_vacuum if pager.next_auto_vacuum is not None else pager.auto_vacuum
        target.write(target.header)
        target.header.autovacuum_root = 1 if mode else 0
        target.header.incremental_vacuum = int(mode == 2)

    def vacuum_page_size(self) -> int:
        """The page size VACUUM writes: what PRAGMA page_size asked for, except
        for an in-memory database (as SQLite)."""
        pager = self.catalog.pager
        if pager.next_page_size is not None and pager.path is not None:
            return pager.next_page_size
        return pager.geometry.page_size

    def copy_database(self, target: Pager | SqlitePager, keep_rowids: bool) -> None:
        """Copy the schema, tables and indexes into the empty database ``target``
        (the schema table keeps its keys, objects get new root pages).

        Without ``keep_rowids`` (VACUUM, but not VACUUM INTO) a table with
        neither an INTEGER PRIMARY KEY nor an index gets new rowids 1, 2, 3...
        in rowid order, as SQLite's VACUUM gives them (its transfer
        optimization keeps rowids only where they may be referenced)."""
        if self.catalog.sqlite:
            self._copy_sqlite_database(target, keep_rowids)
            return
        catalog = Catalog(target)
        source = self.catalog
        for key, value in list(source.schema.scan()):
            kind, name, table_name, root, sql = decode_record(value)[0]
            if kind in ("table", "index"):
                index = source.indexes.get(ascii_lower(name)) if kind == "index" else None
                table = source.tables[ascii_lower(name)] if kind == "table" else None
                # (an index's own codec: NOCASE / RTRIM keys must stay collation keys in the page cache;
                # a WITHOUT ROWID table's is its PRIMARY KEY's)
                codec = (IntKey if table.has_rowid else table.pk_index.codec) if kind == "table" \
                    else index.codec if index is not None else IndexKeyCodec
                tree = BTree.create(target, codec)
                entries = BTree(source.pager, root, codec).scan()
                if kind == "table" and not keep_rowids and table.has_rowid:
                    if table.rowid_column is None and not table.indexes:
                        entries = ((rowid, record) for rowid, (_, record) in enumerate(entries, 1))
                tree.bulk_load(entries)
                root = tree.root
            catalog.schema.insert(key, encode_record([kind, name, table_name, root, sql]))

    def _copy_sqlite_database(self, target: SqlitePager, keep_rowids: bool) -> None:
        """copy_database for SQLite-format files: every tree is copied cell
        by cell in its order, so even objects MiniDB cannot parse survive."""
        from minidb.sqlite_btree import IndexTree, SqliteTable, TableTree

        source = self.catalog
        pager = source.pager
        rows = [(key, decode_record(value)[0]) for key, value in list(source.schema.scan())]
        roots = {}  # schema key -> root page made beforehand (auto_vacuum)
        if target.auto_vacuum:
            # Root pages first, at the front of the file, in the order SQLite's
            # VACUUM creates them: the tables, then the indexes (sqlite_sequence
            # comes with the first AUTOINCREMENT table).
            tables = [(k, r) for k, r in rows if r[0] == "table" and r[3]]
            sequence = next((k for k, r in tables if ascii_lower(r[1]) == "sqlite_sequence"), None)
            for key, row in tables:
                if key == sequence:
                    continue
                roots[key] = (IndexTree if index_rooted(pager, row[3]) else TableTree).create(target)
                info = source.tables.get(ascii_lower(row[1]))
                if sequence is not None and sequence not in roots and info is not None and info.autoincrement:
                    roots[sequence] = TableTree.create(target)
            if sequence is not None and sequence not in roots:
                roots[sequence] = TableTree.create(target)
            for key, row in rows:
                if row[0] == "index" and row[3]:
                    roots[key] = IndexTree.create(target)
        for key, row in rows:
            kind, name, root = row[0], row[1], row[3]
            if kind in ("table", "index") and root:
                if kind == "table" and index_rooted(pager, root):
                    kind = "index"  # (a WITHOUT ROWID table: an index tree, copied as one)
                if kind == "table":
                    tree = TableTree(pager, root)
                    table = source.tables.get(ascii_lower(name))
                    renumber = (not keep_rowids and table is not None and table.rowid_column is None
                                and not table.indexes)
                    entries = ((n if renumber else rowid, tree.payload(cell))
                               for n, (rowid, cell) in enumerate(tree.scan(), 1))
                    row[3] = TableTree.build(target, entries)
                else:
                    tree = IndexTree(pager, root)
                    row[3] = IndexTree.build(target, ((0, tree.payload(cell)) for cell in tree.cells()))
                if key in roots:  # (into the root made for it)
                    (TableTree if kind == "table" else IndexTree)(target, roots[key]).adopt(row[3])
                    row[3] = roots[key]
        # The schema last, built rather than inserted into (no balancing that frees pages).
        TableTree.build_into(target, 1, ((key, SqliteTable._payload(row)) for key, row in rows))
        if target.auto_vacuum and target.header.freelist_count:
            target.vacuum_pages()  # (the pages adopt() freed: the copy has none)
        target.header.user_version = pager.header.user_version
        target.header.application_id = pager.header.application_id

    def create_table_as(self, stmt: CreateTable) -> Result:
        """CREATE TABLE ... AS SELECT, as SQLite's sqlite3EndTable: the
        table's columns are the query's (unique names, the type names of
        their affinities), its SQL is made up (createTableStmt), and the rows
        go in with row ids 1, 2, ... (changing neither changes() nor
        last_insert_rowid())."""
        catalog = self.catalog
        database = catalog.temp_catalog() if stmt.temp else catalog
        database._check_reserved(stmt.name)
        if database._exists(stmt.name, stmt.if_not_exists):
            return Result()
        database._check_new_name(stmt.name)
        compiled = self.compile_query(stmt.query)
        names = [f"column{i}" if ascii_lower(n) in ("true", "false") else n
                 for i, n in enumerate(compiled.names, 1)]
        source = DerivedSource(stmt.name, compiled, unique_names(names), stmt.query)
        sql = create_table_sql(stmt.name, [c.name for c in source.columns], source.affinities)
        created = parse(sql)
        created.temp = stmt.temp
        table = catalog.create_table(created)
        tree = catalog.table_tree(table)
        for rowid, row in enumerate(list(compiled.run()), 1):
            row = list(row)
            self.prepare_row(table, row)
            tree.insert(rowid, self.encode(table, self.stored_row(table, row)))
        return Result()

    def create_index(self, stmt: CreateIndex) -> Result:
        index = self.catalog.create_index(stmt)
        if index is None:
            return Result()
        self.ran = True  # (a duplicate found while filling it is a run-time error)
        self.build_index(index)
        return Result()

    def build_index(self, index: IndexInfo) -> None:
        """Fill the (empty) tree of ``index`` from its table: the keys are
        sorted, checked for duplicates and loaded bottom up."""
        table = index.table
        load_row, key = self.load_row, (index.build_key if index.raw_reals else index.key)
        keys = sorted(key(load_row(table, rowid, record), rowid)
                      for rowid, record in self.catalog.table_tree(table).scan())
        if index.unique:
            width = len(index.positions)
            for a, b in zip(keys, keys[1:]):
                # NULLs never conflict (their sort keys start with 0).
                if a[:width] == b[:width] and all(part[0] != 0 for part in a[:width]):
                    raise IntegrityError(self.unique_error(table, index))
        self.catalog.index_tree(index).bulk_load((k, b"") for k in keys)


MAX_GENERATED_LEVELS = 16


def inner_join_loop(levels: list[JoinLevel]) -> Callable[[Row], Iterator[Row]]:
    """A generated generator function for a nested loop over inner joins:
    one ``for`` statement per table, its conditions tested inline."""
    env = {"_truth": values.truth}
    lines = ["def loop(row):"]
    indent = "    "
    for i, level in enumerate(levels):
        env[f"_candidates{i}"] = level.access.candidates
        env[f"_load{i}"] = level.load
        start, stop = level.offset, level.offset + len(level.table.columns) + 1
        lines.append(f"{indent}for _rowid, _record in _candidates{i}(row):")
        indent += "    "
        lines.append(f"{indent}row[{start}:{stop}] = _load{i}(_rowid, _record)")
        for j, condition in enumerate(level.filters):
            env[f"_filter{i}_{j}"] = condition
            lines.append(f"{indent}_v = _filter{i}_{j}(row)")
            lines.append(f"{indent}if (not _v) if type(_v) is int else not _truth(_v):")
            lines.append(f"{indent}    continue")
    lines.append(f"{indent}yield row")
    text = "\n".join(lines)
    code = _CODE_CACHE.get(text)
    if code is None:
        code = _CODE_CACHE[text] = compile(text, "<join loop>", "exec")
    exec(code, env)
    return env["loop"]


class JoinLevel:
    """One table of a nested loop join."""

    @property
    def plain(self) -> bool:
        """An inner join level (inner_join_loop handles it)."""
        return not self.outer and self.unmatched is None and not self.merges and self.match is None

    def __init__(self, table: Source, offset: int, access: AccessPath, outer: bool, match: RowFunction | None, filters: list[RowFunction]) -> None:
        self.table = table
        self.offset = offset  # position of the table's first slot in a row
        self.access = access
        self.outer = outer  # LEFT / FULL JOIN: emit a NULL row when nothing matches
        self.match = match  # an outer join's ON condition
        self.unmatched = None  # RIGHT / FULL JOIN: a JoinLevel scanning the whole table
        self.merges = []  # (slot, part slots) of the Merges to set once this table is bound
        self.filters = filters  # conditions checked once this table is bound
        self.loop = None  # of the first level: the generated loop (inner_join_loop)
        if isinstance(table, DerivedSource) or getattr(access, "yields_rows", False):
            self.load = lambda rowid, row: row  # already a row
        else:
            load_row = Executor.load_row
            self.load = lambda rowid, record: load_row(table, rowid, record)

    def skip_virtual(self, unused: dict[int, None]) -> None:
        """Leave the VIRTUAL columns ``unused`` NULL instead of computing them."""
        table, access = self.table, self.access
        if isinstance(access, IndexScan):
            access.unused = unused
        if not getattr(access, "yields_rows", False):
            self.load = lambda rowid, record: expand_virtual(
                table, record if type(record) is list else decode_row(record), rowid, unused)


class CompiledSelect:
    """A SELECT compiled once; ``run()`` evaluates it (again) and returns its rows."""

    def __init__(self, executor: Executor, stmt: Select, parent: Scope | None = None) -> None:
        self.executor = executor
        try:
            self.compile(stmt, parent)
        except NeedsAggregate as signal:
            if signal.scope is not self.scope:
                raise
            self.compile(stmt, parent, aggregate=True)

    def compile(self, stmt: Select, parent: Scope | None, aggregate: bool = False) -> None:
        """``aggregate``: an aggregate query even without aggregates of its
        own outside subqueries (see NeedsAggregate)."""
        executor = self.executor
        self.scope = scope = Scope(parent)
        joins, self.derived = executor.build_from(stmt.source, scope)
        self.exprs, self.names = executor.expand_items(stmt, scope)
        # Compile the result columns first: only the other clauses see aliases.
        # (Their subqueries find them through scope.aliases.)
        aliases = {}
        for item in stmt.items:
            if item.alias is not None and not isinstance(item.expr, Star):
                aliases.setdefault(ascii_lower(item.alias), item)
        if aliases:
            stmt, joins = self.substitute_aliases(stmt, joins, aliases)
        # As in SQLite, GROUP BY or an aggregate in the result columns makes
        # an aggregate query (not one in HAVING or ORDER BY).
        self.is_aggregate = aggregate or bool(stmt.group_by) or any(owns_aggregate(e, scope) for e in self.exprs)
        if stmt.having is not None and not self.is_aggregate:
            raise OperationalError("HAVING clause on a non-aggregate query")
        self.aggregates = scope.aggregates = AggregateCollector(scope.width) if self.is_aggregate else None
        self.windows = WindowCollector(stmt.windows)
        exprs = [*self.exprs, *(item.expr for item in stmt.order_by)]
        scope.has_windows = lambda: any(isinstance(node, Call) and node.over is not None  # (asked for rarely)
                                        for e in exprs for node in walk(e))
        compiler = Compiler(scope, self.aggregates, misuse="misuse of aggregate: {name}()",
                            executor=executor, allow_aggregates=True, windows=self.windows)
        scope.phase = "outputs"
        self.output_row, self.affinities = compiler.compile_tuple(self.exprs)
        self.collation_compiler = Compiler(scope, executor=executor)
        self._collations = None
        scope.aliases = aliases
        scope.phase = "order"
        self.order_terms, order_functions = executor.order_terms(
            stmt, self.exprs, self.names, compiler
        )
        self.order_key = tuple_function(order_functions)
        scope.phase = "having"
        compiler.windows = None
        self.having = compiler.compile(stmt.having) if stmt.having is not None else None
        scope.phase = "group"
        self.group_functions, self.group_keys = executor.group_functions(stmt, self.exprs, self.names, scope)
        scope.phase = "where"
        self.levels = None
        self.constants = []  # conditions tested once, before the loop
        order_columns = self.order_columns(stmt)
        if stmt.source:
            hint = order_columns[0] if order_columns and stmt.limit is not None else None
            self.levels, self.constants = executor.plan_joins(
                scope, joins, stmt.where, hint, covering=True, aggregate=self.is_aggregate,
                group_hint=self.group_columns(stmt)
            )
        elif stmt.where is not None:
            self.constants = [executor.where_compiler(scope, self.is_aggregate).compile(stmt.where)]
        scope.phase = None
        # Window results follow the row's values (and aggregate results).
        self.windows.base = scope.width + (len(self.aggregates.calls) if self.aggregates is not None else 0)
        if not self.windows.groups:
            self.windows = None
        self.distinct = stmt.distinct
        self.limit = executor.compile_limit(stmt)
        self.first_row_only = self.is_aggregate and self.min_max_on_equal_column(stmt)
        # An index scan that orders every GROUP BY column delivers the groups in its order.
        self.groups_in_order = bool(
            self.levels and self.levels[0].offset == scope.entries[0].offset
            and getattr(self.levels[0].access, "grouping", None) == "all"
        )
        # Else SQLite's sorter carries the rows to the groups as records: JSON
        # subtypes go and IntReals become integers.  (Only sources other than
        # a table's stored columns can have them.)
        special = any(not isinstance(entry.table, TableInfo) or entry.table.virtual for entry in scope.entries)
        self.group_sorter = special and bool(self.group_functions) and not self.groups_in_order \
            and not self.scan_groups(stmt)
        # (Window functions read their rows back from an ephemeral table.)
        self.window_sorter = self.windows is not None and (special or self.is_aggregate)
        # True when the first table's access path already yields ORDER BY order.
        self.presorted = bool(
            self.levels and order_columns and not self.is_aggregate and self.windows is None
            and all(level.unmatched is None for level in self.levels)
            and self.levels[0].offset == scope.entries[0].offset  # (the join order may put another table first)
            and follows_order(order_columns, self.levels[0].access.order())
        )

    def min_max_on_equal_column(self, stmt: Select) -> bool:
        """Whether SQLite reads only the first row of this aggregate query.
        For a lone min(x) / max(x) without GROUP BY, SQLite asks the WHERE
        loop for rows in x order and stops after the first; a WHERE term
        "x = <expression of other tables>" (or IS) under x's collation makes
        that order hold for any loop - although under a numeric comparison
        '1' and '1.0' both equal 1.0.  So the result is the first matching
        row's x, as SQLite gives it."""
        scope, calls = self.scope, self.aggregates.calls
        if stmt.group_by or stmt.having is not None or len(calls) != 1 or len(scope.entries) != 1:
            return False
        name, args, _, filter_ = calls[0]
        if name not in ("MIN", "MAX") or len(args) != 1 or filter_ is not None:
            return False
        call = next((node for e in self.exprs for node in walk(e) if isinstance(node, Call)
                     and node.name.upper() == name and len(node.args) == 1 and node.over is None), None)
        if call is None or not isinstance(strip_collate(call.args[0]), Column):
            return False
        compiler = Compiler(scope, executor=self.executor)

        def local_slot(expr):
            try:
                slot, _, _, depth = scope.resolve(expr)
            except (AliasReference, OperationalError):
                return None
            return slot if depth == 0 else None

        slot = local_slot(strip_collate(call.args[0]))
        if slot is None:
            return False
        wanted = compiler.collation(call.args[0]) or "BINARY"
        for term in split_conjuncts(stmt.where):
            if not isinstance(term, Binary) or term.op not in ("=", "==", "IS"):
                continue
            for column, other in ((term.left, term.right), (term.right, term.left)):
                column = strip_collate(column)
                if not isinstance(column, Column) or local_slot(column) != slot:
                    continue
                if any(isinstance(node, (Column, Select, Compound, Exists, InSelect, Subquery))
                       and (not isinstance(node, Column) or local_slot(node) is not None)
                       for node in walk(other)):
                    continue
                if ascii_lower(compiler.comparison_collation(term.left, term.right)) == ascii_lower(wanted):
                    return True
        return False

    @property
    def collations(self) -> list[str | None]:
        """The result columns' collations (None: none), as a subquery's
        columns have them; computed when first needed (an unknown collation
        is an error only then, as in SQLite)."""
        if self._collations is None:
            self._collations = [self.collation_compiler.collation(e) for e in self.exprs]
        return self._collations

    def result_collation(self, i: int) -> tuple[str | None, bool]:
        """(collation, whether an explicit COLLATE gives it) of result column ``i``."""
        expr = self.exprs[i]
        return self.collation_compiler.collation(expr), has_collate(expr)

    def substitute_aliases(self, stmt: Select, joins: list[Join], aliases: dict[str, SelectItem]) -> tuple[Select, list[Join]]:
        """Replace references to result column aliases in WHERE, ON, GROUP BY,
        HAVING and ORDER BY with the aliased expressions, as SQLite does for
        a name that is no column of the FROM clause.  (A whole GROUP BY or
        ORDER BY term that is an alias is left to group_term/order_terms.)"""
        scope = self.scope

        def replacer(windows_allowed):
            def replace(column):
                key = ascii_lower(column.name)
                if column.table is not None or key not in aliases or scope._matches(column):
                    return None
                item = aliases[key]
                if not windows_allowed and contains_window(item.expr):
                    raise OperationalError(f"misuse of aliased window function {item.alias}")
                return item.expr
            return replace

        restricted, ordering = replacer(False), replacer(True)

        def term(expr, replace):
            return expr if isinstance(expr, Column) else substitute_columns(expr, replace)

        # (SQLite's parser has already folded "X AND 0": an alias there is never resolved.)
        stmt = dataclasses.replace(
            stmt,
            where=substitute_columns(fold_and(stmt.where), restricted),
            having=substitute_columns(fold_and(stmt.having), restricted),
            group_by=[term(e, restricted) for e in stmt.group_by],
            order_by=[dataclasses.replace(item, expr=term(item.expr, ordering)) for item in stmt.order_by],
        )
        joins = [dataclasses.replace(join, on=substitute_columns(fold_and(join.on), restricted)) for join in joins]
        return stmt, joins

    def group_columns(self, stmt: Select) -> list[tuple[int, str]] | None:
        """GROUP BY as a list of (position, collation) of the first table's
        columns, or None unless every term is a plain column of it (not the
        row id, not a result column number)."""
        if not stmt.group_by or not self.scope.entries or isinstance(self.scope.entries[0].table, DerivedSource):
            return None
        entry = self.scope.entries[0]
        compiler = Compiler(self.scope, executor=self.executor)
        columns = []
        for term in stmt.group_by:
            expr = strip_collate(term)
            if not isinstance(expr, Column):
                return None  # (an alias was substituted already)
            try:
                slot, _, table_index, depth = self.scope.resolve(expr)
            except (AliasReference, OperationalError):
                return None
            position = slot - entry.offset
            if depth or table_index != 0 or slot == self.scope.rowid_slot(0) or position == entry.table.rowid_column:
                return None
            column = (position, compiler.collation(term) or "BINARY")
            if column not in columns:
                columns.append(column)
        return columns

    def scan_groups(self, stmt: Select) -> bool:
        """Whether the first table's scan delivers the rows in GROUP BY order
        (then SQLite needs no sorter): e.g. GROUP BY the row id of a full scan."""
        if not stmt.group_by or not self.levels or self.levels[0].offset != self.scope.entries[0].offset:
            return False
        entry = self.scope.entries[0]
        if not isinstance(entry.table, TableInfo):
            return False
        compiler = Compiler(self.scope, executor=self.executor)
        rowid_slot = self.scope.rowid_slot(0)
        wanted = []
        for term in stmt.group_by:
            expr = strip_collate(term)
            if not isinstance(expr, Column):
                return False
            try:
                slot, _, table_index, depth = self.scope.resolve(expr)
            except (AliasReference, OperationalError):
                return False
            if depth or table_index != 0:
                return False
            position = slot - entry.offset
            if slot == rowid_slot or position == entry.table.rowid_column:
                wanted.append(ROWID)
            else:
                wanted.append((position, compiler.collation(term) or "BINARY"))
        return follows_order(wanted, self.levels[0].access.order())

    def order_columns(self, stmt: Select) -> list[int] | None:
        """ORDER BY as positions of the first table's columns (ROWID for the row
        id), or None unless every term is an ascending, NULLS FIRST plain
        column of that table."""
        if not self.order_terms or not self.scope.entries:
            return None
        entry = self.scope.entries[0]
        table = entry.table
        rowid_slot = self.scope.rowid_slot(0)
        columns = []
        for (source, index, descending, nulls_first, collation), item in zip(self.order_terms, stmt.order_by):
            expr = strip_collate(self.exprs[index] if source == "output" else item.expr)
            if descending or not nulls_first or not isinstance(expr, Column):
                return None
            try:
                slot, _, table_index, depth = self.scope.resolve(expr)
            except AliasReference:
                return None
            except OperationalError:
                if is_true_false(expr):
                    return None
                raise
            if depth or table_index != 0:
                return None
            position = slot - entry.offset
            if slot == rowid_slot or position == table.rowid_column:
                position = ROWID
            else:
                position = (position, collation or "BINARY")  # (an index must sort by the same collation)
            columns.append(position)
        return columns

    @property
    def correlated(self) -> bool:
        return self.scope.uses_outer

    def run(self, max_rows: int | None = None) -> list[tuple]:
        """The result rows (tuples).  ``max_rows`` lets a caller that needs
        only the first rows (EXISTS, scalar subqueries) stop early."""
        # (SQLite computes LIMIT first; with LIMIT 0 nothing else runs.)
        start, end = self.limit() if self.limit is not None else (0, None)
        if end is not None and end <= start:
            return []
        if not passes_constants(self.constants, self.scope):
            rows = []
        elif self.levels is not None:
            for source in self.derived:
                source.materialize()
            rows = self.executor.join_rows(self.scope, self.levels)
        else:
            rows = [[]]
        output_row, order_key = self.output_row, self.order_key
        if max_rows is not None and not self.order_terms and not self.distinct:
            end = max_rows if end is None else min(end, start + max_rows)
        if self.is_aggregate:
            if self.first_row_only:
                rows = itertools.islice(rows, 1)
            truth, having = values.truth, self.having
            if self.group_sorter:
                rows = (values.through_sorter(row) for row in rows)
            rows = (
                group_row
                for group_row in self.executor.group_rows(
                    rows, self.scope, self.group_functions, self.aggregates, self.group_keys, self.groups_in_order
                )
                if having is None or truth(having(group_row))
            )
        if self.windows is not None:
            if self.window_sorter:
                rows = (values.through_sorter(row) for row in rows)
            rows = self.windows.apply(rows)
        records = ((output_row(row), order_key(row)) for row in rows)
        if self.distinct:
            records = distinct_records(records, self.collations)
        if self.presorted or not self.order_terms:
            # Rows already come in ORDER BY order: stop as soon as LIMIT is met.
            records = itertools.islice(records, start, end)
        else:
            records = order_records(records, self.order_terms, start, end)
            if values.int_reals_made[0]:
                return [values.through_record(output) for output, _ in records]  # (SQLite's sorter)
        return [output for output, _ in records]


def passes_constants(constants: list[RowFunction], scope: Scope) -> bool:
    """Whether every condition that uses no table of ``scope`` is true."""
    if not constants:
        return True
    row = [None] * scope.width
    return all(values.truth(condition(row)) for condition in constants)


def follows_order(wanted: list[int], provided: tuple[list[int], set[int]] | None) -> bool:
    """Whether rows ordered by ``provided`` = (columns, constant columns) are
    also ordered by the ``wanted`` columns."""
    if provided is None:
        return False
    order, constant = provided
    j = 0
    for position in wanted:
        if position in constant:
            continue
        if j < len(order) and order[j] == position:
            j += 1
            if position == ROWID:
                return True  # the row id is unique: later terms cannot matter
            continue
        return False
    return True


class CompiledCompound:
    """``SELECT ... UNION [ALL] | INTERSECT | EXCEPT SELECT ...`` compiled once."""

    def __init__(self, executor: Executor, stmt: Compound, parent: Scope | None = None) -> None:
        self.parts = [executor.compile_query(select, parent) for select in stmt.selects]
        count = len(self.parts[0].names)
        for operator, part in zip(stmt.operators, self.parts[1:]):
            if len(part.names) != count:
                raise OperationalError(
                    f"SELECTs to the left and right of {operator} "
                    "do not have the same number of result columns"
                )
        self.operators = stmt.operators
        self.names = self.parts[0].names
        self.exprs = self.parts[0].exprs
        # SQLite takes a compound's affinity from its last SELECT (as a
        # scalar subquery or IN's right side); as a table (in FROM, a view, a
        # CTE) its columns get the affinity sqlite3SubqueryColType gives them.
        self.affinities = self.parts[-1].affinities
        self.table_affinities = compound_table_affinities(self.parts)
        # Rows compare by each column's collation in the leftmost SELECT that
        # has one (SQLite's multiSelectCollSeq) - where they compare at all.
        self.key_collations = None
        if stmt.order_by or any(operator != "UNION ALL" for operator in self.operators):
            self.key_collations = [next((c for c in (part.collations[i] for part in self.parts) if c), None)
                                   for i in range(count)]
        self.order_terms = executor.compound_order_terms(stmt, self.parts, self.key_collations)
        self.limit = executor.compile_limit(stmt)

    @property
    def collations(self) -> list[str | None]:
        """As a subquery's columns: the leftmost SELECT's."""
        return self.parts[0].collations

    def result_collation(self, i: int) -> tuple[str | None, bool]:
        """(For IN (SELECT ...): the last SELECT's, as in SQLite.)"""
        return self.parts[-1].result_collation(i)

    @property
    def correlated(self) -> bool:
        return any(part.correlated for part in self.parts)

    def run(self, max_rows: int | None = None) -> list[tuple]:
        start, end = self.limit() if self.limit is not None else (0, None)
        if end is not None and end <= start:
            return []
        rows = self.parts[0].run()
        for operator, part in zip(self.operators, self.parts[1:]):
            rows = combine(operator, rows, part.run(), self.key_collations)
        records = order_records([(row, ()) for row in rows], self.order_terms, start, end)
        if values.int_reals_made[0] and (self.order_terms or self.operators != ["UNION ALL"] * len(self.operators)):
            return [values.through_record(row) for row, _ in records]  # (a sorter or a temporary table)
        return [row for row, _ in records]


class CompiledValues:
    """``VALUES (...), (...)``: rows of expressions; columns column1, column2, ..."""

    def __init__(self, executor: Executor, stmt: Values, parent: Scope | None = None) -> None:
        self.scope = Scope(parent)
        compiler = Compiler(self.scope, executor=executor)
        self.rows = [[compiler.compile(e) for e in row] for row in stmt.rows]
        width = len(stmt.rows[0])
        self.names = [f"column{i}" for i in range(1, width + 1)]
        self.affinities = [None] * width
        self.exprs = list(stmt.rows[0])
        self.collation_compiler = compiler

    @property
    def collations(self) -> list[str | None]:
        return [self.collation_compiler.collation(e) for e in self.exprs]

    def result_collation(self, i: int) -> tuple[str | None, bool]:
        return self.collation_compiler.collation(self.exprs[i]), has_collate(self.exprs[i])

    @property
    def correlated(self) -> bool:
        return self.scope.uses_outer

    def run(self, max_rows: int | None = None) -> list[tuple]:
        rows = self.rows if max_rows is None else self.rows[:max_rows]
        return [tuple(f([]) for f in row) for row in rows]


class PreparedSelect:
    def __init__(self, compiled: CompiledQuery) -> None:
        self.compiled = compiled

    def run(self) -> Result:
        return Result(self.compiled.run(), self.compiled.names)


class PreparedInsert:
    def __init__(self, executor: Executor, stmt: Insert) -> None:
        self.executor = executor
        table = self.table = executor.catalog.table_to_modify(stmt.table, stmt.schema or executor.default_schema)
        width = len(table.columns)
        if stmt.columns is None:
            self.positions = [p for p in range(width) if table.columns[p].generated is None] if table.generated \
                else list(range(width))
        else:
            self.positions = []
            for name in stmt.columns:
                position = table.column_index(name)
                if position is None:
                    if ascii_lower(name) not in ROWID_NAMES or not table.has_rowid:
                        raise OperationalError(f"table {table.name} has no column named {name}")
                    # The row id by name; "width" when it is not a column.
                    position = width if table.rowid_column is None else table.rowid_column
                elif table.columns[position].generated is not None:
                    raise OperationalError(f'cannot INSERT into generated column "{table.columns[position].name}"')
                self.positions.append(position)
        rowid_position = width if table.rowid_column is None else table.rowid_column
        self.rowid_given = rowid_position in self.positions
        # A column named twice takes its first value; the row id its last (sqlite3Insert).
        self.assign = None
        if len(set(self.positions)) < len(self.positions):
            self.assign, seen = [], set()
            for i, position in enumerate(self.positions):
                if position == rowid_position or position not in seen:
                    self.assign.append((i, position))
                    seen.add(position)
        compiler = Compiler(Scope(executor.outer_scope), executor=executor)
        self.defaults = [(p, compiler.compile(c.default)) for p, c in enumerate(table.columns)
                         if p not in self.positions and c.default is not None]
        self.conflict = stmt.conflict
        self.upserts = [PreparedUpsert(executor, table, clause) for clause in stmt.upsert]
        # SQLite checks the row id for a conflict only when the INSERT gives it.
        checks = ["rowid"] if self.rowid_given else []
        checks += [index for index in table.indexes if index.unique]
        for upsert in self.upserts:
            if any(next((u for u in self.upserts if u.constraint in (check, None)), None) is upsert
                   for check in checks):
                upsert.resolve()
        scope = Scope()
        scope.add(table)
        self.returning = executor.compile_returning(stmt.returning, scope)
        self.rows = []
        self.query = None
        if stmt.query is not None:
            self.query = executor.compile_query(stmt.query)
            self.check_count(stmt, len(self.query.names))
        for exprs in stmt.rows:
            self.check_count(stmt, len(exprs))
            self.rows.append([compiler.compile(e) for e in exprs])
        self.tree = executor.catalog.table_tree(table)
        if table.generated:
            executor.generator(table)
        multi_write = self.multi_write = self.query is not None or len(self.rows) > 1
        # SQLite puts the rows of a SELECT or of several VALUES rows in a
        # temporary table first (records: the values lose the JSON subtype)
        # when the table has INSERT triggers - RETURNING is one - or the rows
        # read the table (sqlite3Insert's useTempTable); else its co-routine's
        # registers keep the subtype (for the generated columns too).
        self.temp_table = multi_write and (
            self.returning is not None or bool(executor.catalog.any_triggers and executor.triggers.exist(
                table.name, "INSERT")) or reads_table(
                [stmt.query] + [e for exprs in stmt.rows for e in exprs if type(e) not in (Literal, Parameter)],
                table))
        self.prepare_programs()
        # SQLite keeps a statement journal for a multi-row write (a SELECT,
        # several rows, triggers) that may abort: a constraint checked as
        # ABORT, a function call, a trigger program that may abort.
        triggers = executor.triggers
        # (A REPLACE that may delete a row with foreign keys or DELETE triggers
        # to run is a multi-row write too: sqlite3MultiWrite in
        # sqlite3GenerateConstraintChecks.)
        replace_deletes = replace_possible(table, self.conflict, self.rowid_given,
                                           handled=[u.constraint for u in self.upserts]) and (
            (executor.foreign_keys.enabled and executor.foreign_keys.involved(table))
            or (bool(executor.settings["recursive_triggers"]) and triggers.exist(table.name, "DELETE")))
        multi = multi_write or replace_deletes or bool(
            executor.catalog.any_triggers and triggers.exist(table.name, "INSERT"))
        # (Only a multi-row write needs it, or a statement of a trigger program: Program.may_abort.)
        self.aborts = (multi or bool(executor.compiling_trigger)) and (self.may_abort() or calls_function(stmt) or (
            table.generated and calls_function([c.generated for c in table.columns])) or triggers.may_abort(
            table.name, "INSERT", None, self.conflict) or any(
            # (an upsert's UPDATE runs its triggers' programs as OR ABORT)
            upsert.assignments and triggers.may_abort(table.name, "UPDATE", [
                table.columns[p].name if p < width else "rowid" for p, _ in upsert.assignments], "ABORT")
            for upsert in self.upserts) or (
            bool(executor.settings["recursive_triggers"]) and replace_possible(
                table, self.conflict, self.rowid_given, handled=[u.constraint for u in self.upserts])
            and triggers.may_abort(table.name, "DELETE", None, "REPLACE")) or self.foreign_keys_abort())
        self.statement_journal = multi and self.aborts

    def prepare_programs(self) -> None:
        """What SQLite compiles with the statement, in its order (so the
        first error is the one SQLite reports): the BEFORE triggers; the
        constraint checks' work - an upsert's UPDATE, REPLACE's DELETE (with
        their triggers and foreign keys); the foreign keys of the new row;
        the AFTER triggers."""
        executor, table = self.executor, self.table
        triggers, keys = executor.triggers, executor.foreign_keys
        self.unchecked = None
        if not executor.catalog.any_triggers and not keys.enabled:
            self.fk_multi = True  # (nothing to compile; used only with foreign keys)
            return
        triggers.prepare(table.name, "INSERT", None, self.conflict, ("BEFORE",))
        # A statement is a multi-row write (sqlite3MultiWrite) with a SELECT,
        # RETURNING or INSERT triggers, in a trigger program, and when an
        # upsert's UPDATE or a REPLACE's DELETE (coded within it) has triggers
        # or foreign key work.
        multi = (self.multi_write or self.returning is not None or bool(executor.compiling_trigger)
                 or triggers.exist(table.name, "INSERT"))
        width = len(table.columns)
        for upsert in self.upserts:
            if upsert.assignments:
                positions = {position for position, _ in upsert.assignments}
                upsert.unchecked = executor.compile_update(table, positions, "ABORT")
                names = trigger_names(table, positions)
                multi = multi or triggers.exist(table.name, "UPDATE", names) or (
                    keys.enabled and keys.required(table, positions))
        replaces = replace_possible(table, self.conflict, self.rowid_given, handled=[u.constraint for u in self.upserts])
        if replaces:
            recursive = bool(executor.settings["recursive_triggers"])
            executor.compile_delete(table, "REPLACE", recursive)
            multi = multi or keys.involved(table) or (recursive and triggers.exist(table.name, "DELETE"))
        self.fk_multi = multi
        if keys.enabled:
            keys.prepare(table, "insert", single_insert=not self.fk_multi)
            self.unchecked = executor.set_null_link(table)
        triggers.prepare(table.name, "INSERT", None, self.conflict, ("AFTER",))

    def foreign_keys_abort(self) -> bool:
        """Whether its foreign key code may abort (fkey.c's sqlite3MayAbort,
        whatever the statement's OR clause): a child row checked, a REPLACE's
        delete, an upsert's update."""
        keys, table = self.executor.foreign_keys, self.table
        return keys.enabled and (keys.may_abort(table, "insert") or (
            replace_possible(table, self.conflict, True) and keys.may_abort(table, "delete")) or any(
            u.do_update and keys.may_abort(table, "update", u.changed_positions()) for u in self.upserts))

    def may_abort(self) -> bool:
        """Whether a constraint check could abort the statement.

        SQLite decides when compiling whether a statement might abort: a
        constraint checked under ABORT, or a call of a (not inlined)
        function, which may raise an error.  Only then, and when it writes
        several rows, does it keep a statement journal; so only then does an
        error inside a transaction that is not a constraint violation (such
        as a datatype mismatch) undo the rows the statement already wrote.
        Otherwise they stay (see Database.execute_statement)."""
        table, conflict = self.table, self.conflict
        if self.executor.replace_rechecks(table) and replace_possible(
                table, conflict, self.rowid_given, handled=[u.constraint for u in self.upserts]):
            return True  # (the recheck after a REPLACE halts as ABORT: Executor.recheck_unique)
        if any(c.not_null and (conflict or c.not_null_conflict or "ABORT") in ("ABORT", "REPLACE")
               for c in table.columns):
            return True  # REPLACE fixes NOT NULL with a default value; MiniDB has none
        if table.checks and (conflict in (None, "ABORT", "REPLACE")
                             or any(calls_function(check.expr) for check in table.checks)):
            return True  # (a function call in a CHECK may raise an error, like one in the statement)

        def handled(constraint, own):
            return (conflict or own or "ABORT") != "ABORT" or any(
                u.constraint in (constraint, None) for u in self.upserts)

        if self.rowid_given and not handled("rowid", table.rowid_conflict()):
            return True
        if any(index.unique and not handled(index, index.conflict) for index in table.indexes):
            return True
        checked = {i for i, c in enumerate(table.columns) if c.not_null}
        checked.update(p for index in table.indexes if index.unique for p in index.positions)
        checked.update(p for check in table.checks for p in check_positions(table, check))
        checked.update((table.rowid_column, len(table.columns)))

        def changes(upsert):  # (with the generated columns that follow them)
            positions = {p for p, _ in upsert.assignments}
            return positions | generated_dependents(table, positions)

        return any(u.assignments and changes(u) & checked for u in self.upserts)

    def check_count(self, stmt: Insert, count: int) -> None:
        if count != len(self.positions):
            if stmt.columns is None:
                raise OperationalError(
                    f"table {self.table.name} has {len(self.positions)} columns "
                    f"but {count} values were supplied"
                )
            raise OperationalError(f"{count} values for {len(self.positions)} columns")

    def run(self) -> Result:
        executor, table, width = self.executor, self.table, len(self.table.columns)
        # All rows are computed first: SQLite evaluates the (constant)
        # subqueries of VALUES once, and a SELECT from the table being
        # inserted into does not see the new rows.
        rows = []
        sources = self.query.run() if self.query is not None else (
            [function([]) for function in functions] for functions in self.rows
        )
        if self.temp_table:
            sources = ([values.record_value(v) if type(v) in SUBTYPED else v for v in source] for source in sources)
        assign, positions = self.assign, self.positions
        for source in sources:
            row = [None] * (width + 1)  # the last: a row id given by name
            if assign is None:
                for position, value in zip(positions, source):
                    row[position] = value
            else:
                for i, position in assign:
                    row[position] = source[i]
            for position, default in self.defaults:
                row[position] = default([])
            rows.append(row)
        keys = executor.foreign_keys
        saved_unchecked, keys.unchecked = keys.unchecked, self.unchecked  # (prepare_programs did the compiling)
        changed = []  # rows inserted or updated by an upsert, with their row ids
        defaults = DefaultRegisters([position for position, _ in self.defaults])
        sequence = None
        if table.autoincrement:
            start = executor.sequence_value(table)
            sequence = [start or 0]
        try:
            for row in rows:
                rowid = row.pop()
                outcome = executor.insert_row(table, self.tree, row, self.conflict, self.upserts, rowid, defaults,
                                              sequence, not self.fk_multi)
                if outcome is not None:
                    kind, stored = outcome
                    if kind == "insert" and table.has_rowid:
                        executor.last_insert_rowid = stored[-1]
                    changed.append(stored)
        except Error as exc:
            exc.changes = len(changed) + exc.__dict__.pop("row_done", 0)  # the rows that FAIL (or no statement journal) keeps
            raise
        finally:
            keys.unchecked = saved_unchecked
        if sequence is not None and sequence[0] != start:
            executor.set_sequence_value(table, sequence[0])
        return returning_result(self.returning, changed)


class DefaultRegisters:
    """SQLite computes a column's (constant) DEFAULT once per INSERT, into
    the register the rows are built in, and applies the column affinities
    to those registers in place - at the first index check, or when the
    record is made.  So once a row got that far, the "excluded" row of a
    later upsert shows the default converted (a SQLite quirk kept here)."""

    def __init__(self, positions: list[int]) -> None:
        self.positions = positions
        self.converted = False


class PreparedSingleTable:
    """The part of UPDATE / DELETE that finds the rows matching WHERE."""

    def __init__(self, executor: Executor, table_name: str, where: Expr | None, indexed_by: str | None = None,
                 not_indexed: bool = False, schema: str | None = None) -> None:
        self.executor = executor
        self.table = executor.catalog.table_to_modify(table_name, schema or executor.default_schema)
        executor.catalog.check_index_hint(self.table, indexed_by)
        self.tree = executor.catalog.table_tree(self.table)
        self.scope = Scope(executor.outer_scope)
        ref = TableRef(self.table.name, indexed_by=indexed_by, not_indexed=not_indexed,
                       schema="temp" if self.table.temp else "main")
        joins, _ = executor.build_from([Join(ref)], self.scope)
        self.levels, self.constants = executor.plan_joins(self.scope, joins, where)
        self.rowid_slot = self.scope.rowid_slot(0)

    def matching_rows(self, two_pass: bool = False) -> list[tuple[int, Row]]:
        """(rowid, row copy) of every matching row, all found before any
        change.  When SQLite can't change the rows during its scan (foreign
        keys, RETURNING, REPLACE, a new rowid), it collects the rowids in a
        RowSet or a temporary table first, and so works through them in
        rowid order: an order foreign key actions and REPLACE can show."""
        slot = self.rowid_slot
        if not passes_constants(self.constants, self.scope):
            return []
        rows = [(row[slot], list(row)) for row in self.executor.join_rows(self.scope, self.levels)]
        if two_pass:
            rows.sort(key=itemgetter(0))
        return rows


class PreparedUpdate(PreparedSingleTable):
    def __init__(self, executor: Executor, stmt: Update) -> None:
        super().__init__(executor, stmt.table, stmt.where, stmt.indexed_by, stmt.not_indexed, stmt.schema)
        table, width = self.table, len(self.table.columns)
        compiler = Compiler(self.scope, executor=executor)
        self.assignments = []
        for name, expr in stmt.assignments:
            position = table.column_index(name)
            if position is None:
                if ascii_lower(name) not in ROWID_NAMES or not table.has_rowid:
                    raise OperationalError(f"no such column: {name}")
                position = width if table.rowid_column is None else table.rowid_column
            elif table.columns[position].generated is not None:
                raise OperationalError(f'cannot UPDATE generated column "{table.columns[position].name}"')
            self.assignments.append((position, compiler.compile(expr)))
        self.conflict = stmt.conflict
        self.returning = executor.compile_returning(stmt.returning, self.scope)
        # Like PreparedInsert.may_abort: the constraints the changed columns
        # take part in, under ABORT (REPLACE, for NOT NULL); with the
        # generated columns that use them.
        changed = self.changed = {p for p, _ in self.assignments}
        if table.generated:
            executor.generator(table)
            changed |= generated_dependents(table, changed)
        width = len(table.columns)
        rowid_changed = self.rowid_changed = bool(changed & {width, table.rowid_column})
        conflict = stmt.conflict
        # (A function in a generated column counts too: it is computed with the statement.)
        self.statement_journal = calls_function(stmt) or bool(
            table.generated and calls_function([c.generated for c in table.columns])) or any(
            (conflict or table.columns[p].not_null_conflict or "ABORT") in ("ABORT", "REPLACE")
            for p in changed if p < width and table.columns[p].not_null
        ) or any(conflict in (None, "ABORT", "REPLACE") or calls_function(check.expr)
                 for check in table.checks
                 if (changed | ({width} if rowid_changed else set())) & check_positions(table, check)) or (
            rowid_changed and (conflict or table.rowid_conflict() or "ABORT") == "ABORT"
        ) or any(index.unique and changed & set(index.positions) and (conflict or index.conflict or "ABORT") == "ABORT"
                 for index in table.indexes)
        # Whether a REPLACE may delete a row the statement has yet to update.
        self.may_replace = "REPLACE" in (conflict, table.rowid_conflict(), *(i.conflict for i in table.indexes))
        self.names = trigger_names(table, changed)
        # What SQLite compiles with the statement, in its order (Executor.compile_update).
        self.unchecked = executor.compile_update(table, self.changed, conflict, True, rowid_changed)
        self.aborts = self.statement_journal or executor.triggers.may_abort(table.name, "UPDATE", self.names, conflict)
        if executor.triggers.exist(table.name, "UPDATE", self.names):
            self.statement_journal = self.aborts  # (triggers make it a multi-row write)

    def run(self) -> Result:
        executor, table, tree = self.executor, self.table, self.tree
        keys = executor.foreign_keys
        saved_unchecked, keys.unchecked = keys.unchecked, self.unchecked
        changed = []
        try:
            triggered = executor.triggers.exist(table.name, "UPDATE", self.names)
            two_pass = self.may_replace or self.rowid_changed or self.returning is not None or triggered or (
                keys.enabled and keys.required(table, self.changed))
            for rowid, old in self.matching_rows(two_pass):
                if self.may_replace or keys.enabled or triggered:
                    if rowid not in tree:
                        continue  # an earlier row's REPLACE (or a foreign key action, a trigger) deleted it
                    if triggered or keys.enabled:
                        old = executor.load_row(table, rowid, tree.get(rowid))  # (as it is now)
                new = list(old)
                for position, function in self.assignments:
                    new[position] = function(old)
                stored = executor.update_row(table, tree, rowid, old, new, self.conflict, self.changed)
                if stored is not None:
                    changed.append(stored)
        except Error as exc:
            exc.changes = len(changed) + exc.__dict__.pop("row_done", 0)
            raise
        finally:
            keys.unchecked = saved_unchecked
        return returning_result(self.returning, changed)


class PreparedDelete(PreparedSingleTable):
    def __init__(self, executor: Executor, stmt: Delete) -> None:
        super().__init__(executor, stmt.table, stmt.where, stmt.indexed_by, stmt.not_indexed, stmt.schema)
        self.returning = executor.compile_returning(stmt.returning, self.scope)
        self.delete_all = stmt.where is None and self.returning is None
        executor.compile_delete(self.table, None)  # (what SQLite compiles with it, in its order)
        keys = executor.foreign_keys
        # (SQLite: a statement journal for a multi-row write - triggers, foreign
        # keys, RETURNING - that may abort; a plain DELETE cannot fail half way.)
        multi = executor.triggers.exist(self.table.name, "DELETE") or keys.involved(self.table) or (
            self.returning is not None)
        self.aborts = (multi or bool(executor.compiling_trigger)) and (
            calls_function(stmt) or executor.triggers.may_abort(self.table.name, "DELETE", None, None) or (
                keys.enabled and keys.involved(self.table) and keys.may_abort(self.table, "delete")))
        self.statement_journal = multi and self.aborts

    def run(self) -> Result:
        executor, table, tree = self.executor, self.table, self.tree
        keys = executor.foreign_keys
        involved = keys.involved(table) or executor.triggers.exist(table.name, "DELETE")
        if self.delete_all and not involved:
            count = len(tree)
            tree.clear()
            for index in table.indexes:
                executor.catalog.index_tree(index).clear()
            return Result(rowcount=count)
        matches = self.matching_rows(involved or self.returning is not None)
        if not involved:
            for rowid, row in matches:
                executor.remove_index_entries(table, row, rowid)
                tree.delete(rowid)
            return returning_result(self.returning, [row for _, row in matches])
        deleted = []
        try:
            for rowid, row in matches:
                if rowid not in tree:
                    continue  # (a foreign key action or a trigger deleted it)
                row = executor.delete_row(table, tree, rowid)
                if row is not None:
                    deleted.append(row)
        except Error as exc:
            exc.changes = len(deleted) + exc.__dict__.pop("row_done", 0)
            raise
        return returning_result(self.returning, deleted)


class PreparedViewInsert:
    """INSERT into a view with INSTEAD OF INSERT triggers: each row, as NEW
    (the values as given, without affinities), fires them instead."""

    def __init__(self, executor: Executor, stmt: Insert, view: ViewInfo) -> None:
        self.executor = executor
        self.view = view
        if stmt.upsert:
            raise OperationalError("cannot UPSERT a view")
        source = self.source = executor.view_source(view)
        width = len(source.columns)
        if stmt.columns is None:
            self.positions = list(range(width))
        else:
            self.positions = []
            for name in stmt.columns:
                position = source.column_index(name)
                if position is None:
                    if ascii_lower(name) not in ROWID_NAMES:
                        raise OperationalError(f"table {view.name} has no column named {name}")
                    position = width  # (SQLite accepts a row id for a view, and ignores it)
                self.positions.append(position)
        compiler = Compiler(Scope(executor.outer_scope), executor=executor)
        self.query = None
        self.rows = []
        if stmt.query is not None:
            self.query = executor.compile_query(stmt.query)
            self.check_count(stmt, len(self.query.names))
        for exprs in stmt.rows:
            self.check_count(stmt, len(exprs))
            self.rows.append([compiler.compile(e) for e in exprs])
        scope = Scope(executor.outer_scope)
        scope.add(source)
        self.returning = executor.compile_returning(stmt.returning, scope)
        self.conflict = stmt.conflict
        executor.triggers.prepare(view.name, "INSERT", None, self.conflict)
        self.instead = bool(executor.triggers.matching(view.name, "INSTEAD OF", "INSERT"))
        self.aborts = calls_function(stmt) or executor.triggers.may_abort(view.name, "INSERT", None, self.conflict)
        self.statement_journal = self.aborts

    def check_count(self, stmt: Insert, count: int) -> None:
        if count != len(self.positions):
            if stmt.columns is None:
                raise OperationalError(f"table {self.view.name} has {len(self.source.columns)} columns "
                                       f"but {count} values were supplied")
            raise OperationalError(f"{count} values for {len(self.positions)} columns")

    def run(self) -> Result:
        executor, width = self.executor, len(self.source.columns)
        if self.query is not None:
            sources = self.query.run()
        else:
            sources = [[f([]) for f in functions] for functions in self.rows]
        done = []
        for values_ in sources:
            new = [None] * (width + 1)
            for position, value in zip(self.positions, values_):
                new[position] = value
            if self.instead and new[width] is not None and not isinstance(
                    values.apply_affinity(new[width], values.INTEGER), int):
                raise IntegrityError("datatype mismatch")  # (building NEW, SQLite checks the row id it ignores)
            new[width] = None
            try:
                executor.triggers.fire(self.view.name, "INSTEAD OF", "INSERT", None, new, None, self.conflict)
            except TriggerIgnore:
                continue
            except Error as exc:
                exc.changes = 0  # (a view's changes() is 0, also after RAISE(FAIL))
                raise
            done.append(new)
        if self.returning is not None:
            # (SQLite reads a REAL column of the new rows with OP_RealAffinity - not in typeof())
            real = [i for i, a in enumerate(self.source.affinities) if a == values.REAL]
            done = [[float(v) if i in real and type(v) is int else v for i, v in enumerate(row)] for row in done]
        return view_result(self.returning, done)


class PreparedViewChange:
    """UPDATE or DELETE of a view with INSTEAD OF triggers: the view's rows
    that match WHERE are found first; each fires the triggers, as OLD (and
    NEW: OLD with the SET values, converted by the columns' affinities when
    an INSTEAD OF trigger fires)."""

    def __init__(self, executor: Executor, stmt: Update | Delete, view: ViewInfo) -> None:
        self.executor = executor
        self.view = view
        self.scope = scope = Scope(executor.outer_scope)
        joins, self.derived = executor.build_from([Join(TableRef(view.name))], scope)
        self.source = source = scope.entries[0].table
        self.levels, self.constants = executor.plan_joins(scope, joins, stmt.where)
        compiler = Compiler(scope, executor=executor)
        self.assignments = []
        self.names = None
        self.event = "DELETE"
        self.conflict = None
        if isinstance(stmt, Update):
            self.event = "UPDATE"
            self.conflict = stmt.conflict
            self.names = []
            for name, expr in stmt.assignments:
                position = source.column_index(name)
                if position is None:
                    raise OperationalError(f"no such column: {name}")
                self.assignments.append((position, compiler.compile(expr)))
                self.names.append(name)
        self.returning = executor.compile_returning(stmt.returning, scope)
        executor.triggers.prepare(view.name, self.event, self.names, self.conflict)
        # (SQLite converts NEW only for INSTEAD OF triggers - its BEFORE ones - not for RETURNING alone.)
        self.convert = bool(executor.triggers.matching(view.name, "INSTEAD OF", self.event, self.names))
        self.aborts = calls_function(stmt) or executor.triggers.may_abort(view.name, self.event, self.names,
                                                                          self.conflict)
        self.statement_journal = self.aborts

    def run(self) -> Result:
        executor, source = self.executor, self.source
        width = len(source.columns)
        if not passes_constants(self.constants, self.scope):
            rows = []
        else:
            for derived in self.derived:
                derived.materialize()
            rows = [list(row[:width]) + [None] for row in executor.join_rows(self.scope, self.levels)]
        done = []
        for old in rows:
            new = None
            if self.event == "UPDATE":
                new = list(old)
                for position, function in self.assignments:
                    value = function(old)
                    new[position] = values.apply_affinity(value, source.affinities[position]) if self.convert else value
            try:
                executor.triggers.fire(self.view.name, "INSTEAD OF", self.event, old, new, self.names, self.conflict)
            except TriggerIgnore:
                continue
            except Error as exc:
                exc.changes = 0
                raise
            done.append(new if new is not None else old)
        return view_result(self.returning, done)


def view_result(returning: tuple[list[RowFunction], list[str]] | None, rows: list[Row]) -> Result:
    """The result of a change of a view: no rows changed (changes() is 0), its RETURNING rows."""
    result = returning_result(returning, rows)
    result.rowcount = 0
    return result


class PreparedUpsert:
    """One ON CONFLICT clause of an INSERT."""

    def __init__(self, executor: Executor, table: TableInfo, clause: Upsert) -> None:
        self.executor = executor
        self.table = table
        self.clause = clause
        self.constraint = self.find_constraint(table, clause)  # None: any uniqueness constraint
        self.do_update = clause.assignments is not None
        self.assignments = []
        self.where = None
        self.unchecked = None  # the foreign key its UPDATE leaves unchecked (Executor.set_null_link)

    def resolve(self) -> None:
        """Compile DO UPDATE's SET and WHERE.  Like SQLite, PreparedInsert
        does this only for a clause that some conflict check can reach: a
        name error in any other clause goes unreported."""
        if not self.do_update or self.assignments:
            return
        table, clause = self.table, self.clause
        # SET and WHERE see the existing row (by the table's name) and "excluded".
        self.scope = Scope(self.executor.outer_scope)
        self.scope.add(table)
        self.scope.add(ExcludedSource(table))
        # An unqualified name is the existing row's column; excluded.x must be qualified.
        self.scope.entries[1].hidden.update(ascii_lower(c.name) for c in table.columns)
        compiler = Compiler(self.scope, executor=self.executor)
        width = len(table.columns)
        assignments = []
        for name, expr in clause.assignments:
            position = table.column_index(name)
            if position is None:
                if ascii_lower(name) not in ROWID_NAMES or not table.has_rowid:
                    raise OperationalError(f"no such column: {name}")
                position = width if table.rowid_column is None else table.rowid_column
            elif table.columns[position].generated is not None:
                raise OperationalError(f'cannot UPDATE generated column "{table.columns[position].name}"')
            assignments.append((position, compiler.compile(expr)))
        self.where = compiler.compile(clause.where) if clause.where is not None else None
        self.assignments = assignments

    def changed_positions(self) -> set[int]:
        """The columns DO UPDATE sets (the row id as len(columns))."""
        table, width = self.table, len(self.table.columns)
        positions = set()
        for name, _ in self.clause.assignments:
            position = table.column_index(name)
            positions.add(width if position is None or position == table.rowid_column else position)
        return positions

    @staticmethod
    def find_constraint(table: TableInfo, clause: Upsert) -> IndexInfo | str | None:
        if clause.columns is None:
            return None
        if clause.target_where is None:
            # As in SQLite, the INTEGER PRIMARY KEY (or rowid) matches only
            # on its own: a UNIQUE index that includes it never matches.
            alias = None if table.rowid_column is None else ascii_lower(table.columns[table.rowid_column].name)
            wanted = [ascii_lower(c) for c in clause.columns]
            for name in clause.columns:
                if table.column_index(name) is None and (ascii_lower(name) not in ROWID_NAMES or not table.has_rowid):
                    raise OperationalError(f"no such column: {name}")
            if len(wanted) == 1 and wanted[0] in (alias, *ROWID_NAMES):
                return "rowid"
            # A target column with a COLLATE matches only an index column of that collation.
            given = {ascii_lower(c): values.collation_name(k) for c, k in zip(clause.columns, clause.collations or ())
                     if k is not None}
            for index in table.indexes:
                names = [ascii_lower(c) for c in index.column_names]
                if (index.unique and alias not in names and len(names) == len(wanted)
                        and set(names) == set(wanted)
                        and all(given.get(n, k) == k for n, k in zip(names, index.collations))):
                    return index
        raise OperationalError("ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint")

    def apply(self, tree: BTree, rowid: int, excluded: Row) -> tuple[str, Row] | None:
        """Handle a conflict with existing row ``rowid``; ``excluded`` is the
        row that could not be inserted (with its row id)."""
        if not self.do_update:
            return None
        executor, table = self.executor, self.table
        old = executor.load_row(table, rowid, tree.get(rowid))
        if values.int_reals_made[0]:
            # (SQLite reads excluded.x of REAL affinity with OP_RealAffinity:
            # a generated column's IntReal is a REAL there.)
            excluded = [float(v) if type(v) is values.IntReal else v for v in excluded]
        context = old + excluded
        if self.where is not None and not values.truth(self.where(context)):
            return None
        new = list(old)
        for position, function in self.assignments:
            new[position] = function(context)
        # (SQLite runs DO UPDATE as an UPDATE OR ABORT: the constraints' own ON CONFLICT does not apply.)
        keys = executor.foreign_keys
        saved, keys.unchecked = keys.unchecked, self.unchecked  # (its UPDATE's own, see compile_update)
        changed = {position for position, _ in self.assignments}
        if table.generated:
            changed |= generated_dependents(table, changed)
        try:
            stored = executor.update_row(table, tree, rowid, old, new, "ABORT", changed)
        finally:
            keys.unchecked = saved
        return None if stored is None else ("update", stored)  # (None: a trigger deleted or kept the row)


# Functions SQLite compiles inline (no function call that could raise an error).
INLINE_FUNCTIONS = frozenset(("COALESCE", "IFNULL", "IIF", "IF", "LIKELY", "UNLIKELY", "LIKELIHOOD"))


def calls_function(node: object) -> bool:
    """Whether a statement calls a scalar function (LIKE included) anywhere,
    subqueries too: SQLite then assumes it may abort (PreparedInsert.may_abort)."""
    if isinstance(node, Like):
        return True
    if isinstance(node, Call):
        if node.name not in INLINE_FUNCTIONS and not values.is_aggregate_call(node.name, len(node.args)):
            return True
    if isinstance(node, (list, tuple)):
        return any(calls_function(item) for item in node)
    if dataclasses.is_dataclass(node):
        return any(calls_function(getattr(node, f.name)) for f in dataclasses.fields(node))
    return False


def returning_result(returning: tuple[list[RowFunction], list[str]] | None, rows: list[Row]) -> Result:
    """The result of an INSERT, UPDATE or DELETE: its RETURNING rows, if any."""
    if returning is None:
        return Result(rowcount=len(rows))
    functions, names = returning
    return Result([tuple(f(row) for f in functions) for row in rows], names, rowcount=len(rows))


def combine(operator: str, left: list[tuple], right: list[tuple], collations: list[str | None] | None = None) -> list[tuple]:
    """Apply a compound operator.  Like SQLite (which merges the sorted
    sides), the distinct forms return rows in sorted order, comparing values
    by the columns' ``collations``; of equal rows UNION keeps the right
    side's first, the others the left side's first."""
    if operator == "UNION ALL":
        return left + right
    key = row_key(collations or [None] * len(left[0] if left else right[0] if right else ()))
    kept = {}
    for row in left:
        kept.setdefault(key(row), row)
    if operator == "UNION":
        first = {}
        for row in right:
            first.setdefault(key(row), row)
        kept.update(first)
    else:
        right_keys = {key(row) for row in right}
        want = operator == "INTERSECT"
        kept = {k: row for k, row in kept.items() if (k in right_keys) == want}
    return [kept[k] for k in sorted(kept)]


def row_key(collations: list[str | None]) -> Callable[[tuple], tuple]:
    """A function row -> the tuple of its values' sort keys under ``collations``."""
    functions = [values.collation_sort_key(c) for c in collations]
    if all(f is values.sort_key for f in functions):
        sort_key = values.sort_key
        return lambda row: tuple(sort_key(v) for v in row)
    return lambda row: tuple(f(v) for f, v in zip(functions, row))


class ExcludedSource:
    """The ``excluded`` row of an upsert: the table's columns without
    affinities (as in SQLite; its values may not be converted either, see
    Executor.insert_row) and without collations (the other operand's
    decides a comparison)."""

    has_rowid = True
    indexes = ()
    uncollated = True

    def __init__(self, table: TableInfo) -> None:
        self.name = "excluded"
        self.columns = table.columns
        self.rowid_column = table.rowid_column
        self.affinities = [None] * len(table.columns)
        self.collations = [None] * len(table.columns)
        self.column_index = table.column_index


class ColumnName:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class DerivedSource:
    """A subquery in FROM, seen as a table whose rows are recomputed per run."""

    has_rowid = False
    rowid_column = None
    indexes = ()

    strip = ()  # the columns whose JSON subtype the rows lose (lost_subtypes)
    bare = frozenset()  # the columns SQLite's flattening replaces by a column of its own (bare_columns)

    def __init__(self, name: str, compiled: CompiledQuery, names: list[str] | None = None,
                 query: Any = None) -> None:
        """``names``: the column names a view declares, if any.  ``query``:
        the subquery, to see whether it may return JSON (whose subtype the
        rows lose, as in SQLite)."""
        self.name = name or "subquery"
        self.compiled = compiled
        if query is not None:
            self.bare = bare_columns(query, len(compiled.names))
            self.strip = lost_subtypes(query, len(compiled.names), self.bare)
        if names is None:
            names = unique_names(compiled.names)
        elif len(names) != len(compiled.names):
            raise OperationalError(
                f"expected {len(names)} columns for '{name}' but got {len(compiled.names)}"
            )
        self.columns = [ColumnName(n) for n in names]
        self.affinities = list(getattr(compiled, "table_affinities", compiled.affinities))
        self.collations = list(compiled.collations)
        self.positions = {}
        for i, n in enumerate(names):
            self.positions.setdefault(ascii_lower(n), i)
        self.rows = []

    def column_index(self, name: str) -> int | None:
        return self.positions.get(ascii_lower(name))

    @property
    def correlated(self) -> bool:
        return self.compiled.correlated

    def materialize(self) -> None:
        if self.strip:
            self.rows = [plain_text(row, self.strip) + [i] for i, row in enumerate(self.compiled.run(), 1)]
        else:
            self.rows = [list(row) + [i] for i, row in enumerate(self.compiled.run(), 1)]


_EACH_FUNCTIONS = frozenset({"json_each", "json_tree", "jsonb_each", "jsonb_tree"})
SUBTYPED = (JSONText, JSONBlob)  # the types of values with the JSON subtype
RECORD_CONVERTED = frozenset((JSONText, JSONBlob, IntReal))  # (see values.record_value)


def carries_json(query: object) -> bool:
    """Whether a subquery may return values with the JSON subtype: it calls
    a JSON function or reads json_each() / json_tree()."""
    return any(isinstance(node, Call) and node.name in jsonfuncs.SUBTYPE_FUNCTIONS
               or isinstance(node, (TableRef, TableFunction)) and ascii_lower(node.name) in _EACH_FUNCTIONS
               for node in walk_nodes(query))


def bare_columns(query: object, width: int) -> frozenset[int]:
    """The result columns of a subquery in FROM (or a view or CTE) that are
    bare column references of a subquery SQLite flattens into the query
    using it (roughly: a plain SELECT; the outer query is not looked at).
    SQLite's substExpr puts such a column itself in place of the subquery's,
    so it keeps its JSON subtype and, for a row id, has no collation; any
    other expression gets an implicit COLLATE (BINARY if none) and loses the
    subtype."""
    flattened = (isinstance(query, Select) and not (query.distinct or query.group_by or query.order_by or query.windows)
                 and query.having is None and query.limit is None
                 and not any(contains_aggregate(item.expr) or contains_window(item.expr)
                             for item in query.items if not isinstance(item.expr, Star)))
    if not flattened:
        return frozenset()
    if any(isinstance(item.expr, Star) for item in query.items):
        if all(isinstance(item.expr, (Star, Column)) for item in query.items):
            return frozenset(range(width))
        return frozenset()  # (not worked out column by column)
    return frozenset(i for i, item in enumerate(query.items) if isinstance(item.expr, Column))


def reads_table(node: object, table: TableInfo) -> bool:
    """Whether a statement part names ``table`` in a FROM clause (SQLite's readsTable)."""
    name = ascii_lower(table.name)
    return any(isinstance(n, TableRef) and ascii_lower(n.name) == name for n in walk_nodes(node))


def lost_subtypes(query: object, width: int, bare: frozenset[int]) -> tuple[int, ...]:
    """The columns of a subquery in FROM whose values lose the JSON subtype
    on the way out: all but its bare_columns, if it may return JSON at all."""
    if not carries_json(query):
        return ()
    return tuple(i for i in range(width) if i not in bare)


def plain_text(row: Sequence, positions: tuple[int, ...]) -> list:
    """The values of a row, without the JSON subtype in the columns at ``positions``."""
    row = list(row)
    for i in positions:
        value = row[i]
        if type(value) is JSONText:
            row[i] = str.__str__(value)
        elif type(value) is JSONBlob:
            row[i] = bytes(value)
    return row


class PragmaSource(DerivedSource):
    """``pragma_<name>(arg, schema)`` in FROM: the pragma's rows, with the
    hidden columns ``arg`` and ``schema`` (left out of ``*``).  With an
    argument from an earlier table of the FROM clause (``lateral``), the rows
    for every possible argument, which the join then matches on ``arg``."""

    def __init__(self, executor: Executor, name: str, spec: pragmas.Spec, arg: RowFunction | None,
                 schema: RowFunction | None, lateral: bool, scope: Scope) -> None:
        self.name = name
        self.executor = executor
        self.spec = spec
        self.arg = arg
        self.schema = schema
        self.lateral = lateral
        self.scope = scope
        names = list(spec.columns) + (["arg"] if spec.arg is not None else []) + ["schema"]
        self.columns = [ColumnName(n) for n in names]
        self.hidden_columns = {"arg", "schema"}
        self.affinities = [None] * len(names)
        self.collations = [None] * len(names)
        self.positions = {ascii_lower(n): i for i, n in enumerate(names)}
        self.rows = []

    @property
    def correlated(self) -> bool:
        return False  # (its arguments read the enclosing query's row through scope.cell)

    def materialize(self) -> None:
        row = [None] * self.scope.width
        schema = self.schema(row) if self.schema is not None else None
        if schema is not None:
            pragmas.check_schema(str(schema), quoted=True)
        spec, executor = self.spec, self.executor
        if self.lateral:
            arguments = pragmas.argument_domain(executor, spec)
        elif spec.arg is not None:
            arguments = [self.arg(row) if self.arg is not None else None]
        else:
            arguments = [None]
        rows = []
        saved, executor.pragma_schema = executor.pragma_schema, None if schema is None else ascii_lower(str(schema))
        try:
            for argument in arguments:
                for result in spec.rows(executor, argument):
                    rows.append(list(result) + ([argument] if spec.arg is not None else []) + [schema])
        finally:
            executor.pragma_schema = saved
        self.rows = [row + [i] for i, row in enumerate(rows, 1)]


class JsonEachSource(DerivedSource):
    """``json_each(json[, root])`` / ``json_tree(...)`` in FROM: SQLite's
    JSON virtual tables, with the hidden columns ``json`` and ``root``.  The
    arguments may use tables before it in the FROM clause: its rows are
    computed for each row of those (JsonEachScan)."""

    has_rowid = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.args = []  # (none: no rows)
        self.depends = set()  # the FROM clause's tables the arguments use
        self.recursive = name.endswith("tree")
        self.binary = name.startswith("jsonb")  # (jsonb_each: a container value is JSONB)
        names = jsonfuncs.EACH_COLUMNS
        self.columns = [ColumnName(n) for n in names]
        self.hidden_columns = {"json", "root"}
        self.affinities = [values.BLOB] * len(names)  # (columns declared without a type)
        self.collations = [None] * len(names)
        self.positions = {n: i for i, n in enumerate(names)}
        self.rows = []

    @property
    def correlated(self) -> bool:
        return False  # (its arguments read the enclosing query's row through scope.cell)

    def materialize(self) -> None:
        pass  # (JsonEachScan computes the rows)

    def rows_for(self, row: Row) -> Iterator[tuple[int, list]]:
        if not self.args:
            return  # (SQLite's xFilter without the json argument: no rows)
        args = [arg(row) for arg in self.args]
        rows = jsonfuncs.each_rows(args[0], args[1] if len(args) > 1 else None, self.recursive, len(args) > 1,
                                   self.binary)
        for rowid, values_ in enumerate(rows):
            values_.append(rowid)
            yield rowid, values_


class JsonEachScan:
    """The rows of json_each() / json_tree() for the current row of the join."""

    def __init__(self, source: JsonEachSource) -> None:
        self.source = source

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        return self.source.rows_for(row)

    def order(self) -> tuple[list[int], set[int]] | None:
        return None

    def estimate(self) -> tuple[float, float]:
        return 25, 25

    def describe(self) -> str:
        return f"SCAN {self.source.name} VIRTUAL TABLE INDEX 0:"


class CteInfo:
    """A CTE and the depth of its WITH clause in Executor.cte_scopes."""

    __slots__ = ("cte", "level")

    def __init__(self, cte: Cte, level: int) -> None:
        self.cte = cte
        self.level = level


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


def sqlite_dequote(text: str) -> str:
    """SQLite's sqlite3Dequote, which names an unnamed CHECK constraint after
    its text: text starting with a quote keeps only what is quoted there
    (so "CHECK ([UnitPrice] >= 0)" fails as "UnitPrice")."""
    if not text or text[0] not in "\"'`[":
        return text
    quote = "]" if text[0] == "[" else text[0]
    result = []
    i = 1
    while i < len(text):
        if text[i] == quote:
            if text[i + 1:i + 2] != quote:
                break
            i += 1
        result.append(text[i])
        i += 1
    return "".join(result)


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


def all_constraints(stmt: CreateTable) -> list[object]:
    """The constraints of a CREATE TABLE: the columns', then the table's."""
    return [c for column in stmt.columns for c in column.constraints] + stmt.constraints


def all_foreign_keys(stmt: CreateTable) -> list[ForeignKey]:
    return [c for c in all_constraints(stmt) if isinstance(c, ForeignKey)]


def apply_edits(text: str, edits: list[tuple[int, str]]) -> str:
    """Replace the SQL token starting at each position with new text."""
    lengths = {token.pos: len(token.text) for token in tokenize(text)}
    for pos, replacement in sorted(set(edits), reverse=True):
        text = text[:pos] + replacement + text[pos + lengths[pos]:]
    return text


def plain_identifier(name: str) -> str:
    """A name as written in SQL: bare if that reads back the same, else quoted."""
    tokens = tokenize(name)
    if len(tokens) == 2 and tokens[0].kind == "IDENT" and tokens[0].text == name:
        return name
    return quote(name)


def check_cte_columns(cte: Cte, names: list[str]) -> None:
    if cte.columns is not None and len(cte.columns) != len(names):
        raise OperationalError(f"table {cte.name} has {len(names)} values for {len(cte.columns)} columns")


def self_reference_count(query: object, name: str) -> int:
    """How often a SELECT's own FROM clause names table ``name``."""
    if not isinstance(query, Select):
        return 0
    lowered = ascii_lower(name)
    return sum(1 for join in query.source
               if isinstance(join.table, TableRef) and ascii_lower(join.table.name) == lowered)


class WorkingSource(DerivedSource):
    """A recursive CTE as its recursive part sees it: the one row being
    processed (set by RecursiveSource before each run)."""

    def __init__(self, name: str, names: list[str], affinities: list, parent_scope: Scope | None,
                 collations: list[str | None] | None = None) -> None:
        self.name = name
        self.columns = [ColumnName(n) for n in names]
        self.affinities = list(affinities)
        self.collations = list(collations or [None] * len(names))
        self.positions = {}
        for i, n in enumerate(names):
            self.positions.setdefault(ascii_lower(n), i)
        self.rows = []
        self.parent_scope = parent_scope  # only the recursive part's own FROM may use it
        self.used = False

    @property
    def correlated(self) -> bool:
        return False

    def materialize(self) -> None:
        pass


class RecursiveSource(DerivedSource):
    """A recursive CTE: the initial rows go into a queue; each row taken out
    is part of the result and is fed (as the working table) to the recursive
    parts, whose rows join the queue.  UNION drops rows seen before; ORDER BY
    makes the queue a priority queue (ties in arrival order); LIMIT and
    OFFSET apply to the rows taken out, as in SQLite."""

    def __init__(self, name: str, initial: CompiledQuery, names: list[str], working: WorkingSource,
                 parts: list[CompiledQuery], distinct: bool, order_terms: list[OrderTerm],
                 limit: Callable[[], tuple[int, int | None]] | None) -> None:
        super().__init__(name, initial, names)
        self.working = working
        self.parts = parts
        self.distinct = distinct
        self.order_key = order_key(order_terms) if order_terms else None
        self.limit = limit

    @property
    def correlated(self) -> bool:
        return self.compiled.correlated or any(part.correlated for part in self.parts)

    def materialize(self) -> None:
        start, end = self.limit() if self.limit is not None else (0, None)
        seen = set()
        queue = []  # a heap of (key, arrival, row) with ORDER BY, else a FIFO
        arrivals = itertools.count()
        head = 0
        identify, key = row_key(self.collations), self.order_key

        def push(row: tuple) -> None:
            if self.distinct:
                identity = identify(row)
                if identity in seen:
                    return
                seen.add(identity)
            if key is not None:
                heapq.heappush(queue, (key((row, ())), next(arrivals), row))
            else:
                queue.append(row)

        for row in self.compiled.run():
            push(tuple(plain_text(row, self.strip) if self.strip else row))
        out, taken = [], 0
        while (len(queue) > head) if key is None else queue:
            if end is not None and taken >= end:
                break
            if key is not None:
                row = heapq.heappop(queue)[2]
            else:
                row = queue[head]
                head += 1
            if taken >= start:
                out.append(row)
            taken += 1
            if end is not None and taken >= end:
                break
            self.working.rows = [list(row) + [1]]
            for part in self.parts:
                for new in part.run():
                    push(tuple(plain_text(new, self.strip) if self.strip else new))
        self.rows = [list(row) + [i] for i, row in enumerate(out, 1)]


def compound_table_affinities(parts: list[CompiledQuery]) -> list[str | None]:
    """The affinities of a compound SELECT's columns as a table (SQLite's
    sqlite3SubqueryColType): the first SELECT's, or the first one after it
    that has one; then none (BLOB) if another SELECT's column may hold text
    where it is numeric, or a number where it is TEXT
    (sqlite3ExprDataType)."""
    result = []
    for i in range(len(parts[0].names)):
        mask, k = 0, 0
        affinity = parts[0].affinities[i]
        while affinity is None and k + 1 < len(parts):
            mask |= expr_data_type(parts[k], i)
            k += 1
            affinity = parts[k].affinities[i]
        if affinity is not None and affinity != values.BLOB and (k + 1 < len(parts) or k > 0):
            for part in parts[k + 1:]:
                mask |= expr_data_type(part, i)
            if affinity == values.TEXT and mask & 0x01:
                affinity = values.BLOB
            elif affinity in values.NUMERIC_AFFINITIES and mask & 0x02:
                affinity = values.BLOB
            elif affinity in values.NUMERIC_AFFINITIES and isinstance(parts[0].exprs[i], Cast):
                affinity = values.NUMERIC  # (SQLite's SQLITE_AFF_FLEXNUM)
        result.append(affinity)
    return result


def expr_data_type(part: CompiledQuery, i: int) -> int:
    """SQLite's sqlite3ExprDataType of result column ``i`` of a compound's
    SELECT: what it may hold - 1 a number, 2 text, 4 a blob (NULL aside)."""
    expr = part.exprs[i]
    affinity = part.affinities[i]
    while True:
        if isinstance(expr, Collate) or (isinstance(expr, Unary) and expr.op == "+"):
            expr = expr.expr if isinstance(expr, Collate) else expr.operand
            affinity = part.collation_compiler.compile_with_affinity(expr)[1]
            continue
        if isinstance(expr, Literal):
            value = expr.value
            return 0 if value is None else 0x02 if isinstance(value, str) else 0x04 if isinstance(
                value, bytes) else 0x01
        if isinstance(expr, Binary) and expr.op == "||":
            return 0x06
        if isinstance(expr, (Parameter, Call)):
            return 0x07
        if isinstance(expr, Column) and is_true_false_name(expr):
            return 0x01
        if isinstance(expr, (Column, Subquery, Cast)):
            return 0x05 if affinity in values.NUMERIC_AFFINITIES else 0x06 if affinity == values.TEXT else 0x07
        if isinstance(expr, Case):
            compiler = part.collation_compiler
            results = [result for _, result in expr.whens] + ([expr.else_] if expr.else_ is not None else [])
            mask = 0
            for result in results:
                fake = _DataTypePart([result], [compiler.compile_with_affinity(result)[1]], compiler)
                mask |= expr_data_type(fake, 0)
            return mask
        return 0x01


class _DataTypePart:
    """One expression with its affinity, to ask expr_data_type about."""

    def __init__(self, exprs: list, affinities: list, compiler: Compiler) -> None:
        self.exprs, self.affinities, self.collation_compiler = exprs, affinities, compiler


def unique_names(names: list[str]) -> list[str]:
    """Column names of a subquery or view as SQLite makes them unique
    (sqlite3ColumnsFromExprList): a repeated name gets ":1", ":2", ... in
    place of a ":<digits>" ending it has (ignoring case)."""
    seen = set()
    result = []
    for name in names:
        count = 0
        while ascii_lower(name) in seen:
            base = name
            end = len(base) - 1
            while end > 0 and base[end].isdigit() and base[end].isascii():
                end -= 1
            if end > 0 and base[end] == ":":
                base = base[:end]
            count += 1
            name = f"{base}:{count}"
        seen.add(ascii_lower(name))
        result.append(name)
    return result


# Every word SQLite's tokenizer takes as a keyword (sqlite3KeywordCode): a
# name it writes into SQL it makes up is quoted if it is one of them.
SQLITE_KEYWORDS = frozenset("""
    ABORT ACTION ADD AFTER ALL ALTER ALWAYS ANALYZE AND AS ASC ATTACH AUTOINCREMENT BEFORE BEGIN BETWEEN BY
    CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE
    CURRENT_TIME CURRENT_TIMESTAMP DATABASE DEFAULT DEFERRABLE DEFERRED DELETE DESC DETACH DISTINCT DO DROP
    EACH ELSE END ESCAPE EXCEPT EXCLUDE EXCLUSIVE EXISTS EXPLAIN FAIL FILTER FIRST FOLLOWING FOR FOREIGN FROM
    FULL GENERATED GLOB GROUP GROUPS HAVING IF IGNORE IMMEDIATE IN INDEX INDEXED INITIALLY INNER INSERT
    INSTEAD INTERSECT INTO IS ISNULL JOIN KEY LAST LEFT LIKE LIMIT MATCH MATERIALIZED NATURAL NO NOT NOTHING
    NOTNULL NULL NULLS OF OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION PLAN PRAGMA PRECEDING PRIMARY QUERY
    RAISE RANGE RECURSIVE REFERENCES REGEXP REINDEX RELEASE RENAME REPLACE RESTRICT RETURNING RIGHT ROLLBACK
    ROW ROWS SAVEPOINT SELECT SET TABLE TEMP TEMPORARY THEN TIES TO TRANSACTION TRIGGER UNBOUNDED UNION UNIQUE
    UPDATE USING VACUUM VALUES VIEW VIRTUAL WHEN WHERE WINDOW WITH WITHOUT
""".split())
# The type each affinity gets in the SQL SQLite makes up for CREATE TABLE ... AS.
AFFINITY_TYPE_NAMES = {values.TEXT: " TEXT", values.NUMERIC: " NUM", values.INTEGER: " INT", values.REAL: " REAL"}


def ident_put(name: str) -> str:
    """A name as SQLite's identPut writes it: bare when it is letters,
    digits and _ (not starting with a digit, not a keyword), else quoted."""
    plain = all((c.isascii() and c.isalnum()) or c == "_" for c in name)
    if not plain or not name or name[0].isdigit() or ascii_upper(name) in SQLITE_KEYWORDS:
        return quote(name)
    return name


def create_table_sql(name: str, columns: list[str], affinities: list[str | None]) -> str:
    """SQLite's createTableStmt: the CREATE TABLE of CREATE TABLE ... AS,
    on one line when short, else one column per line."""
    size = sum(len(c) + c.count('"') + 2 + 5 for c in columns) + len(name) + name.count('"') + 2
    separator, between, end = ("", ",", ")") if size < 50 else ("\n  ", ",\n  ", "\n)")
    parts = [ident_put(c) + AFFINITY_TYPE_NAMES.get(a, "") for c, a in zip(columns, affinities)]
    return f"CREATE TABLE {ident_put(name)}({separator}{between.join(parts)}{end}"


def folded_literal(expr: Expr) -> Literal | None:
    """The literal SQLite's parser reduces ``expr`` to, or None.

    The parser folds ``X AND 0`` / ``0 AND X`` to 0 unless a side calls a
    function (LIKE counts), and ``<non-NULL literal> IS [NOT] NULL`` to 0 or 1
    (looking through unary + and -).  Folded values are only observable
    through ORDER BY / GROUP BY column numbers.
    """
    if isinstance(expr, Literal):
        return expr
    if isinstance(expr, Binary) and expr.op == "AND":
        def is_zero(literal):  # an INTEGER 0; Literal(0.0) == Literal(0) in Python
            return literal is not None and type(literal.value) is int and literal.value == 0

        zero = Literal(0)
        if (is_zero(folded_literal(expr.left)) or is_zero(folded_literal(expr.right))) and not any(
            isinstance(e, (Call, Like)) for side in (expr.left, expr.right) for e in walk(side)
        ):
            return zero
        return None
    if isinstance(expr, Binary) and expr.op in ("IS", "IS NOT") and expr.right == Literal(None):
        operand = expr.left
        while isinstance(operand, Unary) and operand.op in ("+", "-"):
            operand = operand.operand
        literal = folded_literal(operand)
        if literal is not None and literal.value is not None:
            return Literal(int(expr.op == "IS NOT"))
    return None


def fold_and(expr: Expr | None) -> Expr | None:
    """``expr`` with every AND that SQLite's parser folds to 0 replaced by 0."""
    if isinstance(expr, Binary) and expr.op == "AND":
        folded = Binary("AND", fold_and(expr.left), fold_and(expr.right))
        return Literal(0) if folded_literal(folded) == Literal(0) else folded
    return expr


def constant_integer(expr: Expr) -> int | None:
    """The value of ``expr`` if SQLite treats it as a column number in
    ORDER BY / GROUP BY: an integer (after parser folding) that fits in 32
    bits, under any number of unary + and -.  Otherwise None."""
    if isinstance(expr, Unary) and expr.op in ("+", "-"):
        value = constant_integer(expr.operand)
        if value is None:
            return None
        return -value if expr.op == "-" else value
    literal = folded_literal(expr)
    if literal is not None:
        value = literal.value
        if isinstance(value, int) and 0 <= value <= 2**31 - 1:
            return value
    return None


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def distinct_records(records: Iterable[Record], collations: list[str | None] | None = None) -> Iterator[Record]:
    """Drop records with duplicate output rows, keeping the first;
    1 and 1.0 count as equal, and texts equal under the columns' collations."""
    seen = set()
    key_of = None
    for record in records:
        if key_of is None:
            key_of = row_key(collations or [None] * len(record[0]))
        key = key_of(record[0])
        if key not in seen:
            seen.add(key)
            yield record


class Descending:
    """Wraps a sort key so that it orders in reverse (for DESC terms)."""

    __slots__ = ("key",)

    def __init__(self, key: tuple) -> None:
        self.key = key

    def __lt__(self, other: Descending) -> bool:
        return other.key < self.key

    def __eq__(self, other: object) -> bool:
        return self.key == other.key


def order_key(terms: list[OrderTerm]) -> Callable[[Record], list]:
    """A key function for (output, keys) records ordering by ``terms``."""
    parts = []
    for source, index, descending, nulls_first, collation in terms:
        parts.append((0 if source == "output" else 1, index, descending,
                      (0,) if nulls_first else (2,), values.collation_sort_key(collation)))

    def key(record):
        result = []
        for column, index, descending, null_key, sort_key in parts:
            value = record[column][index]
            if value is None:
                result.append(null_key)
            elif descending:
                result.append((1, Descending(sort_key(value))))
            else:
                result.append((1, sort_key(value)))
        return result

    return key


def order_records(records: Iterable[Record], terms: list[OrderTerm], start: int = 0, end: int | None = None) -> list[Record]:
    """Sort records by ORDER BY ``terms`` (stably) and keep [start:end].
    With a LIMIT only the first ``end`` records are kept while sorting."""
    key = order_key(terms)
    if end is not None:
        return heapq.nsmallest(end, records, key=key)[start:]
    return sorted(records, key=key)[start:end]
