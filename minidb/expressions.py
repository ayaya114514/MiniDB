"""Name resolution and expression compilation.

Expressions are compiled into Python closures (or generated Python source)
that take a *row*: a list holding, for every table in the query, its column
values followed by its row id.  A ``Scope`` maps column names to positions in
that list.  Scopes of subqueries have their enclosing query's scope as
parent; a reference to an outer column reads the outer row that the
subquery was invoked with.  ``AggregateCollector`` and ``WindowCollector``
gather a query's aggregate and window function calls.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Iterator
from operator import itemgetter
from typing import Any, Protocol, TYPE_CHECKING, Union

from minidb import functions, values, window
from minidb.triggers import TriggerIgnore, raise_error
from minidb.catalog import IndexInfo, TableInfo
from minidb.errors import OperationalError
from minidb.parser import (
    Between, Binary, Call, Case, Cast, Collate, Column, Compound, Exists, InList, Raise, InSelect, Like, Literal,
    Parameter, Select, SelectItem, Star, Subquery, Unary, Frame, WindowDef,
)
from minidb.parser import Expr
from minidb.jsonb import JSONBlob, JSONText
from minidb.values import IntReal
from minidb.values import SQLValue, ascii_lower

if TYPE_CHECKING:
    from minidb.sources import DerivedSource
    from minidb.queries import CompiledCompound, CompiledSelect, PreparedSelect
    from minidb.dml import PreparedDelete, PreparedInsert, PreparedUpdate
    from minidb.executor import Executor
else:
    CompiledCompound = CompiledSelect = DerivedSource = Executor = PreparedDelete = PreparedInsert = PreparedSelect = PreparedUpdate = Any  # (only in annotations: their modules import this one)

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
Source = Union[TableInfo, DerivedSource]  # a table or a subquery in FROM
CompiledQuery = Union[CompiledSelect, CompiledCompound]
PreparedStatement = Union[PreparedSelect, PreparedInsert, PreparedUpdate, PreparedDelete]


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

        def order(a: SQLValue, b: SQLValue) -> int:
            if isinstance(a, str):
                a = numeric(a)
            if isinstance(b, str):
                b = numeric(b)
            return compare(a, b)
    elif affinity == values.TEXT:
        text = values.text_affinity

        def order(a: SQLValue, b: SQLValue) -> int:
            if isinstance(a, str) or isinstance(b, str):
                return compare(text(a), text(b))
            return compare(a, b)
    else:
        order = compare
    if op in ("IS", "IS NOT"):
        want = op == "IS"

        def is_test(a: SQLValue, b: SQLValue) -> int:
            if a is None or b is None:
                return int((a is None and b is None) == want)
            return int((order(a, b) == 0) == want)

        return is_test
    test = _TESTS[op]

    def comparator(a: SQLValue, b: SQLValue) -> int | None:
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

        def test(row: Row) -> int:
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
            def and_(row: Row) -> int | None:
                a = truth(left(row))
                if a is False:
                    return 0
                b = truth(right(row))
                if b is False:
                    return 0
                return None if a is None or b is None else 1
            return and_
        if op == "OR":
            def or_(row: Row) -> int | None:
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

        def between(row: Row) -> int | None:
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

        def in_list(row: Row) -> int | None:
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
            def searched_case(row: Row) -> SQLValue:
                for condition, result in whens:
                    if truth(condition(row)):
                        return result(row)
                return otherwise(row)
            return searched_case

        def simple_case(row: Row) -> SQLValue:
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
            def run_correlated(row: Row) -> Any:
                cell[0] = row
                return transform(compiled.run(max_rows))
            return run_correlated
        cache = []
        self.executor.once_caches.append(cache)  # emptied before every execution

        def run_once(row: Row) -> Any:
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

        def summarize(rows: list[tuple]) -> tuple[set, bool, bool]:
            keys, has_null = set(), False
            for (candidate,) in rows:
                if candidate is None:
                    has_null = True
                    continue
                keys.add(sort_key(convert(candidate) if convert else candidate))
            return keys, has_null, bool(rows)

        members = self._runner(compiled, summarize)
        found, missing = (0, 1) if expr.negated else (1, 0)

        def in_select(row: Row) -> int | None:
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
            def ignore(row: Row) -> SQLValue:
                raise TriggerIgnore()
            return ignore
        message = self.compile(expr.message)

        def raise_(row: Row) -> SQLValue:
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
        if name not in SUBTYPE_WINDOW_FUNCTIONS:
            # SQLite computes the arguments in the subquery that fills the
            # window's ephemeral table and reads them back from its records
            # (an IntReal as an integer, no JSON subtype); only functions
            # that read subtypes compute them afterwards (bExprArgs), from
            # the columns as read back (CompiledSelect.window_sorter).
            args = [record_argument(arg) for arg in args]
        else:
            args = [table_argument(arg) for arg in args]
        filter_ = inner.compile(expr.filter) if expr.filter is not None else None
        collation = inner.collation(expr.args[0]) if name in ("MIN", "MAX") and args else None
        number = self.windows.add(definition, frame, name, args, filter_, inner, collation)
        windows = self.windows
        return lambda row: row[windows.base + number]


SUBTYPE_WINDOW_FUNCTIONS = {"JSON_GROUP_ARRAY", "JSON_GROUP_OBJECT", "JSONB_GROUP_ARRAY", "JSONB_GROUP_OBJECT"}


def record_argument(function: RowFunction) -> RowFunction:
    """``function`` with its result as a record gives it back (values.record_value)."""
    record_value, plain = values.record_value, values._PLAIN_TYPES
    return lambda row: v if type(v := function(row)) in plain else record_value(v)


def table_argument(function: RowFunction) -> RowFunction:
    """``function`` of the row's values as read back from a window's
    ephemeral table (values.through_sorter: a no-op on plain values)."""
    through_sorter = values.through_sorter
    return lambda row: function(through_sorter(row))


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

    def apply(self, rows: Iterable[Row], through_table: bool = False) -> list[Row]:
        """The rows with the window functions' results appended, in the order
        SQLite returns them: the window seen first is computed last (SQLite
        nests the others in subqueries).  ``through_table``: the query reads
        its values back from the window's ephemeral table (values.through_sorter)."""
        padding = [None] * self.count
        rows = [list(row) + padding for row in rows]
        base = self.base
        for k, (_, group) in enumerate(reversed(self.groups)):
            if k and through_table:  # (an outer window reads the values back from the inner one's table)
                rows = [values.through_sorter(row[:base]) + row[base:] for row in rows]
            rows = group.run(rows, base)
        if through_table:
            rows = [values.through_sorter(row[:base]) + row[base:] for row in rows]
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


def split_disjuncts(expr: Expr) -> list[Expr]:
    if isinstance(expr, Binary) and expr.op == "OR":
        return split_disjuncts(expr.left) + split_disjuncts(expr.right)
    return [expr]


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
SUBTYPED = (JSONText, JSONBlob)  # the types of values with the JSON subtype
RECORD_CONVERTED = frozenset((JSONText, JSONBlob, IntReal))  # (see values.record_value)


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
        def is_zero(literal: Literal | None) -> bool:  # an INTEGER 0; Literal(0.0) == Literal(0) in Python
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
