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

import dataclasses
import heapq
import itertools
import random
from collections.abc import Callable, Iterable, Iterator, Sequence
from operator import itemgetter
from typing import Any, Protocol, Union

from minidb import values
from minidb.btree import BTree
from minidb.catalog import HIGH, Catalog, IndexInfo, TableInfo, ViewInfo
from minidb.errors import Error, IntegrityError, NotSupportedError, OperationalError
from minidb.parser import (
    Analyze, Between, Binary, Call, Case, Cast, Column, Compound, CreateIndex, CreateTable,
    CreateView, Delete, DerivedTable, DropIndex, DropTable, DropView, Exists, Explain, InList,
    InSelect, Insert, Join, Like, Literal, Parameter, Reindex, Select, SelectItem, Star, Subquery,
    TableRef, Unary, Update, Upsert,
)
from minidb.parser import Expr, Statement
from minidb.values import SQLValue, ascii_lower
from minidb.record import decode_record, encode_record

ROWID_NAMES = ("rowid", "oid", "_rowid_")

Row = list  # the values of every table of a query, each followed by its row id
RowFunction = Callable[[Row], Any]  # a compiled expression
Record = tuple[tuple, tuple]  # (result row, extra ORDER BY values)
OrderTerm = tuple[str, int, bool, bool]  # (source, index, descending, NULLs first)
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


class ScopeEntry:
    """One table (or derived table) of a FROM clause."""

    __slots__ = ("name", "table", "offset", "hidden")

    def __init__(self, name: str, table: Source, offset: int) -> None:
        self.name = name  # alias or table name, lower case
        self.table = table
        self.offset = offset  # position of its first column in a row
        self.hidden = set()  # USING / NATURAL columns only reachable when qualified


class Scope:
    """The tables visible to expressions and where their values sit in a row."""

    def __init__(self, parent: Scope | None = None) -> None:
        self.entries = []
        self.width = 0
        self.parent = parent  # scope of the enclosing query, for correlated subqueries
        self.cell = [None]  # the row of this scope while one of its subqueries runs
        self.uses_outer = False  # some expression here refers to an enclosing query
        self.used = set()  # (table index, column position) pairs referenced so far

    def add(self, table: Source, alias: str | None = None) -> None:
        name = ascii_lower(alias if alias is not None else table.name)
        self.entries.append(ScopeEntry(name, table, self.width))
        self.width += len(table.columns) + 1

    def rowid_slot(self, index: int) -> int:
        entry = self.entries[index]
        return entry.offset + len(entry.table.columns)

    def _matches(self, column: Column) -> list[tuple[int, str | None, int]]:
        matches = []
        lowered = ascii_lower(column.name)
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
            if matches:
                for inner in passed:
                    inner.uses_outer = True
                slot, _, index = matches[0]
                scope.used.add((index, slot - scope.entries[index].offset))
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

    def star_columns(self, table_name: str | None = None) -> list[tuple[str, str]]:
        """(table name, column name) pairs that ``*`` or ``table.*`` expands to."""
        if not self.entries:
            raise OperationalError("no tables specified")
        result = []
        found = False
        for entry in self.entries:
            if table_name is not None and ascii_lower(table_name) != entry.name:
                continue
            found = True
            for column in entry.table.columns:
                if table_name is None and ascii_lower(column.name) in entry.hidden:
                    continue
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
    elif isinstance(expr, Cast):
        yield from walk(expr.expr)
    elif isinstance(expr, Case):
        if expr.base is not None:
            yield from walk(expr.base)
        for condition, result in expr.whens:
            yield from walk(condition)
            yield from walk(result)
        if expr.else_ is not None:
            yield from walk(expr.else_)


def tables_referenced(expr: Expr, scope: Scope) -> set[int]:
    """Indexes of the tables of ``scope`` that ``expr`` uses.  An expression
    with a subquery counts as using all of them (it may be correlated)."""
    tables = set()
    for e in walk(expr):
        if isinstance(e, Column):
            _, _, index, depth = scope.resolve(e)
            if depth == 0:
                tables.add(index)
        elif isinstance(e, (Subquery, InSelect, Exists)):
            return set(range(len(scope.entries)))
    return tables


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


def value_comparator(op: str, left_affinity: str | None, right_affinity: str | None) -> Callable[[SQLValue, SQLValue], int | None]:
    """A function (a, b) -> 1, 0 or None comparing two values with SQLite's rules."""
    convert_left, convert_right = (
        _AFFINITY_FUNCTIONS.get(a)
        for a in values.comparison_affinities(left_affinity, right_affinity)
    )
    compare = values.compare
    if op in ("IS", "IS NOT"):
        want = op == "IS"

        def is_test(a, b):
            if a is None or b is None:
                return int((a is None and b is None) == want)
            if convert_left:
                a = convert_left(a)
            if convert_right:
                b = convert_right(b)
            return int((compare(a, b) == 0) == want)

        return is_test
    test = _TESTS[op]

    def comparator(a, b):
        if a is None or b is None:
            return None
        if convert_left:
            a = convert_left(a)
        if convert_right:
            b = convert_right(b)
        return int(test(compare(a, b)))

    return comparator


class Compiler:
    """Compiles expression trees into closures ``fn(row) -> value``.

    With an ``AggregateCollector`` the compiler accepts aggregate function
    calls: each becomes a lookup of the aggregate's result, which the executor
    appends to the group's representative row.  Without one, an aggregate
    call is an error reported with ``misuse`` (formatted with the name).
    """

    def __init__(self, scope: Scope, aggregates: AggregateCollector | None = None, misuse: str = "misuse of aggregate function {name}()", executor: Executor | None = None) -> None:
        self.scope = scope
        self.aggregates = aggregates
        self.misuse = misuse
        self.executor = executor  # needed to compile subqueries

    def compile(self, expr: Expr) -> RowFunction:
        return self.compile_with_affinity(expr)[0]

    def compile_with_affinity(self, expr: Expr) -> tuple[RowFunction, str | None]:
        """Return (function, affinity); only column references have an affinity."""
        if isinstance(expr, Literal):
            value = expr.value
            return (lambda row: value), None
        if isinstance(expr, Parameter):
            # Read at run time, so a prepared plan works for any bound values.
            parameters, i = self.executor.parameters, expr.index - 1
            return (lambda row: parameters[i]), None
        if isinstance(expr, Column):
            slot, affinity, _, depth = self.scope.resolve(expr)
            if depth == 0:
                return itemgetter(slot), affinity
            cell = self.scope.ancestor(depth).cell  # the enclosing query's current row
            return (lambda row: cell[0][slot]), affinity
        if isinstance(expr, Cast):
            return self._cast(expr)
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
            return self.call(expr), None
        if isinstance(expr, Star):
            raise OperationalError("* is only allowed in a select list or COUNT(*)")
        raise OperationalError(f"cannot evaluate {expr!r}")

    def _unary(self, expr: Unary) -> RowFunction:
        operand = self.compile(expr.operand)
        if expr.op == "-":
            negate = values.negate
            return lambda row: negate(operand(row))
        if expr.op == "+":
            return operand
        if expr.op == "~":
            bit_not = values.bit_not
            return lambda row: bit_not(operand(row))
        logical_not = values.logical_not
        return lambda row: logical_not(operand(row))

    def _binary(self, expr: Binary) -> RowFunction:
        op = expr.op
        if op == "AND" and folded_literal(expr) == Literal(0):
            # SQLite's parser replaces this by 0: the operands are never
            # resolved, so e.g. a missing table in a subquery there is no error.
            return lambda row: 0
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
        comparator = value_comparator(op, left_affinity, right_affinity)
        return lambda row: comparator(left(row), right(row))

    def _between(self, expr: Between) -> RowFunction:
        value, affinity = self.compile_with_affinity(expr.expr)
        low, low_affinity = self.compile_with_affinity(expr.low)
        high, high_affinity = self.compile_with_affinity(expr.high)
        at_least = value_comparator(">=", affinity, low_affinity)
        at_most = value_comparator("<=", affinity, high_affinity)
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
        convert = _AFFINITY_FUNCTIONS.get(affinity)
        compare = values.compare
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
                if convert:
                    candidate = convert(candidate)
                if compare(v, candidate) == 0:
                    return found
            return None if saw_null else missing

        return in_list

    def _like(self, expr: Like) -> RowFunction:
        value = self.compile(expr.expr)
        pattern = self.compile(expr.pattern)
        like, logical_not = values.like, values.logical_not
        if expr.negated:
            return lambda row: logical_not(like(value(row), pattern(row)))
        return lambda row: like(value(row), pattern(row))

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
            for value, result in expr.whens:
                value, value_affinity = self.compile_with_affinity(value)
                whens.append(((value, value_comparator("=", base_affinity, value_affinity)),
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
        sort_key = values.sort_key

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

    def call(self, expr: Call) -> RowFunction:
        name = expr.name
        if values.is_aggregate_call(name, len(expr.args)):
            return self._aggregate(expr)
        if name not in values.SCALAR_FUNCTIONS:
            raise OperationalError(f"no such function: {ascii_lower(name)}")
        function, min_args, max_args = values.SCALAR_FUNCTIONS[name]
        # DISTINCT means nothing to a scalar function; SQLite ignores it.
        if len(expr.args) < min_args or (max_args is not None and len(expr.args) > max_args):
            raise OperationalError(f"wrong number of arguments to function {ascii_lower(name)}()")
        args = [self.compile(arg) for arg in expr.args]
        if len(args) == 1:
            (arg,) = args
            return lambda row: function(arg(row))
        return lambda row: function(*[arg(row) for arg in args])

    def _aggregate(self, expr: Call) -> RowFunction:
        name = expr.name
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
            args = [inner.compile(arg) for arg in expr.args]
        return itemgetter(self.aggregates.add(name, args, expr.distinct))


def in_select_affinity(left: str | None, right: str | None) -> str | None:
    """Affinity for ``x IN (SELECT y ...)`` (SQLite's sqlite3CompareAffinity):
    both columns: numeric if either is, else none; otherwise whichever exists.
    It is applied to both sides."""
    if left is not None and right is not None:
        numeric = left in values.NUMERIC_AFFINITIES or right in values.NUMERIC_AFFINITIES
        return values.NUMERIC if numeric else None
    return left if left is not None else right


def contains_aggregate(expr: Expr) -> bool:
    return any(
        isinstance(e, Call) and values.is_aggregate_call(e.name, len(e.args)) for e in walk(expr)
    )


class AggregateCollector:
    """The aggregate calls of a query and their per-group state."""

    def __init__(self, base_width: int) -> None:
        self.base_width = base_width  # aggregate results follow the row's slots
        self.calls = []  # (name, argument functions, distinct)

    def add(self, name: str, args: list[RowFunction], distinct: bool) -> int:
        self.calls.append((name, args, distinct))
        return self.base_width + len(self.calls) - 1

    @property
    def tracks_extreme(self) -> bool:
        """A lone MIN()/MAX() makes bare columns come from its row, as in SQLite."""
        return len(self.calls) == 1 and self.calls[0][0] in ("MIN", "MAX")

    def new_state(self) -> list[tuple[Any, set | None]]:
        state = []
        for name, args, distinct in self.calls:
            if name == "COUNT" and not args:
                aggregate = values.CountStarAggregate()
            else:
                aggregate = values.AGGREGATE_FUNCTIONS[name][0]()
            state.append((aggregate, set() if distinct else None))
        return state

    def step(self, state: list[tuple[Any, set | None]], row: Row) -> bool:
        """Feed one row to every aggregate; True if a lone MIN/MAX changed."""
        changed = False
        for (aggregate, seen), (_, args, _) in zip(state, self.calls):
            if not args:
                aggregate.step()
                continue
            arguments = [arg(row) for arg in args]
            if seen is not None:
                key = values.sort_key(arguments[0])
                if arguments[0] is None or key in seen:
                    continue
                seen.add(key)
            changed = aggregate.step(*arguments) or changed
        return changed

    @staticmethod
    def results(state: list[tuple[Any, set | None]]) -> list[SQLValue]:
        return [aggregate.result() for aggregate, _ in state]


# ---- access paths --------------------------------------------------------------
#
# Every access path yields (rowid, record or row) candidates for one table,
# reports the order it yields rows in, and estimates (rows, cost) so the
# planner can compare them.  Costs are in "row visits": reading a row from a
# table scan costs 1, finding one through an index costs a seek plus a table
# lookup.

SEEK_COST = 4  # descending a B+ tree
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
    def __init__(self, tree: BTree, rows: int) -> None:
        self.tree = tree
        self.rows = rows

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        return self.tree.scan()

    def order(self) -> tuple[list[int], set[int]] | None:
        """(columns the rows come ordered by, columns that are constant)."""
        return [ROWID], set()

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
        self.index_tree = index_tree
        self.table_tree = table_tree
        self.equal = equal  # key functions for the leading columns
        self.lower = lower  # (key function, inclusive) or None
        self.upper = upper
        self.table_rows = table_rows
        self.covering = False  # rows are built from index keys alone

    @property
    def yields_rows(self) -> bool:
        return self.covering

    def cover_if_possible(self, scope: Scope, table_index: int) -> None:
        """Use the index alone if it holds every column the query uses."""
        table = self.index.table
        available = set(self.index.positions) | {len(table.columns)}  # plus the row id
        if table.rowid_column is not None:
            available.add(table.rowid_column)
        used = {position for index, position in scope.used if index == table_index}
        self.covering = used <= available

    def keys(self, row: Row) -> Iterator[tuple]:
        """The index keys in range, in order."""
        sort_key = values.sort_key
        prefix = []
        for key_function in self.equal:
            value = key_function(row)
            if value is None:
                return iter(())  # col = NULL is never true
            prefix.append(sort_key(value))
        prefix = tuple(prefix)
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
        return [key[-1][1] for key in self.keys(row)]

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        keys = self.keys(row)
        if self.covering:
            table = self.index.table
            width, positions, alias = len(table.columns), self.index.positions, table.rowid_column
            for key in keys:
                rowid = key[-1][1]
                built = [None] * width
                for position, (rank, *value) in zip(positions, key):
                    if rank:
                        built[position] = value[0]
                if alias is not None:
                    built[alias] = rowid
                built.append(rowid)
                yield rowid, built
            return
        get = self.table_tree.get
        for key in keys:
            rowid = key[-1][1]
            yield rowid, get(rowid)

    def order(self) -> tuple[list[int], set[int]] | None:
        positions = self.index.positions
        return positions[len(self.equal):] + [ROWID], set(positions[:len(self.equal)])

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


class MultiScan:
    """The union of several row id / index lookups: ``col IN (...)`` on an
    index, or the terms of an OR.  Rows come in row id order."""

    def __init__(self, parts: list[AccessPath], table_tree: BTree, label: str) -> None:
        self.parts = parts
        self.table_tree = table_tree
        self.label = label

    def rowids(self, row: Row) -> list[int]:
        rowids = set()
        for part in self.parts:
            rowids.update(part.rowids(row))
        return sorted(rowids)

    def candidates(self, row: Row) -> Iterator[tuple[int, Any]]:
        get = self.table_tree.get
        for rowid in self.rowids(row):
            record = get(rowid)
            if record is not None:
                yield rowid, record

    def order(self) -> tuple[list[int], set[int]] | None:
        return [ROWID], set()

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

    def __init__(self, position: int, op: str, key: RowFunction | list[RowFunction]) -> None:
        self.position = position  # column position in the table, or ROWID
        self.op = op  # "=", "<", "<=", ">", ">=" or "IN"
        self.key = key  # key function(s) evaluated on the outer row


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
        if not isinstance(expr, Column):
            return None
        slot, _, table_index, depth = scope.resolve(expr)
        if depth or table_index != index:
            return None
        if slot == rowid_slot or slot - offset == table.rowid_column:
            return ROWID
        return slot - offset

    def key_function(position, expr, key_affinity=None):
        if tables_referenced(expr, scope) - bound:
            return None
        function, affinity = compiler.compile_with_affinity(expr)
        if position == ROWID:
            return function  # row id lookups apply numeric affinity themselves
        if key_affinity is not None:  # IN: the column's affinity applies to the items
            convert = _AFFINITY_FUNCTIONS.get(table.affinities[position])
        else:
            column_conversion, key_conversion = values.comparison_affinities(
                table.affinities[position], affinity
            )
            if column_conversion is not None:
                return None
            convert = _AFFINITY_FUNCTIONS.get(key_conversion)
        if convert is None:
            return function
        return lambda row: convert(function(row))

    for conjunct in conjuncts:
        if isinstance(conjunct, InList) and not conjunct.negated:
            position = column_position(conjunct.expr)
            if position is not None:
                keys = [key_function(position, item, "IN") for item in conjunct.items]
                if all(keys):
                    constraints.append(Constraint(position, "IN", keys))
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
            constraints.append(Constraint(position, op, key))
    return constraints


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


def access_candidates(scope: Scope, index: int, catalog: Catalog, conjuncts: list[Expr], compiler: Compiler, bound: set[int] | frozenset[int], rows: int) -> list[AccessPath]:
    """Every access path the conjuncts allow for table ``index``, full scan first."""
    table = scope.entries[index].table
    tree = catalog.table_tree(table)
    constraints = find_constraints(scope, index, conjuncts, compiler, bound)
    candidates = [FullScan(tree, rows)]
    for c in constraints:
        if c.position == ROWID and c.op in ("=", "IN"):
            candidates.append(RowidLookup(tree, [c.key] if c.op == "=" else c.key))
    lower, upper = _bounds(constraints, ROWID)
    if lower or upper:
        candidates.append(RowidRange(tree, lower, upper, rows))
    for info in table.indexes:
        equal = []
        for position in info.positions:
            key = next((c.key for c in constraints if c.position == position and c.op == "="), None)
            if key is None:
                break
            equal.append(key)
        lower = upper = None
        if len(equal) < len(info.positions):
            lower, upper = _bounds(constraints, info.positions[len(equal)])
        if equal or lower or upper:
            candidates.append(IndexScan(info, catalog.index_tree(info), tree, equal, lower, upper, rows))
        first = info.positions[0]
        for c in constraints:
            if c.position == first and c.op == "IN":
                index_tree = catalog.index_tree(info)
                parts = [IndexScan(info, index_tree, tree, [key], None, None, rows) for key in c.key]
                candidates.append(MultiScan(parts, tree, "IN"))
                break
    return candidates


def plan_access(scope: Scope, index: int, catalog: Catalog, conjuncts: list[Expr], compiler: Compiler, order_hint: int | None = None, bound: set[int] | frozenset[int] | None = None) -> AccessPath:
    """Choose the cheapest way to read table ``index`` of ``scope``.

    ``bound`` is the set of tables joined before it (default: those before it
    in ``scope``); conditions may use their columns as lookup keys.  An OR
    whose every term can use a row id or index lookup becomes a union of
    those lookups."""
    table = scope.entries[index].table
    if isinstance(table, DerivedSource):
        return DerivedScan(table)
    if bound is None:
        bound = set(range(index))
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
    best = min(candidates, key=lambda a: a.estimate()[1])  # the first of equals wins
    if isinstance(best, FullScan) and order_hint is not None and order_hint != ROWID:
        # Nothing narrows the scan, but ORDER BY ... LIMIT wants this column
        # first: walk an index on it in order and stop early.
        for info in table.indexes:
            if info.positions[0] == order_hint:
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
        self.expanding = []  # views being compiled (to detect a view that uses itself)
        self.statement_journal = True  # see PreparedInsert.statement_journal

    def execute(self, stmt: Statement, parameters: Sequence[SQLValue] = ()) -> Result:
        """Execute a parsed statement with the given parameter values (a list
        indexed by parameter number - 1)."""
        self.parameters[:] = parameters
        self.statement_journal = True
        if isinstance(stmt, (Select, Compound, Insert, Update, Delete)):
            plan = self.prepare(stmt)
            for cache in plan.once_caches:
                cache.clear()
            self.statement_journal = getattr(plan, "statement_journal", True)
            return plan.run()
        if isinstance(stmt, CreateTable):
            self.catalog.create_table(stmt)
            return Result()
        if isinstance(stmt, DropTable):
            self.catalog.drop_table(stmt.name, stmt.if_exists)
            return Result()
        if isinstance(stmt, CreateIndex):
            return self.create_index(stmt)
        if isinstance(stmt, DropIndex):
            self.catalog.drop_index(stmt.name, stmt.if_exists)
            return Result()
        if isinstance(stmt, CreateView):
            self.catalog.create_view(stmt)
            return Result()
        if isinstance(stmt, Reindex):
            return self.reindex(stmt.name)
        if isinstance(stmt, DropView):
            self.catalog.drop_view(stmt.name, stmt.if_exists)
            return Result()
        if isinstance(stmt, Explain):
            return self.explain(stmt.statement)
        if isinstance(stmt, Analyze):
            self.catalog.analyze(stmt.name)
            return Result()
        raise OperationalError(f"unsupported statement: {type(stmt).__name__}")

    def prepare(self, stmt: Select | Compound | Insert | Update | Delete) -> PreparedStatement:
        """The compiled plan of a SELECT/INSERT/UPDATE/DELETE.  Plans are kept
        on the (cached) syntax tree and reused until the schema changes."""
        cached = getattr(stmt, "_plan", None)
        if cached is not None and cached[0] == self.catalog.version:
            return cached[1]
        self.once_caches = []
        if isinstance(stmt, (Select, Compound)):
            plan = PreparedSelect(self.compile_query(stmt))
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
    def load_row(table: TableInfo, rowid: int, record: bytes) -> Row:
        row = decode_record(record)[0]
        if table.rowid_column is not None:
            row[table.rowid_column] = rowid
        row.append(rowid)
        return row

    def compile_query(self, stmt: Select | Compound, parent: Scope | None = None) -> CompiledQuery:
        """Compile a SELECT or compound SELECT (``parent``: the enclosing
        query's scope when this is a subquery)."""
        if isinstance(stmt, Compound):
            return CompiledCompound(self, stmt, parent)
        return CompiledSelect(self, stmt, parent)

    def view_source(self, view: ViewInfo) -> DerivedSource:
        """A view used in FROM: its SELECT, compiled as a subquery that sees
        no enclosing query."""
        if view in self.expanding:
            raise OperationalError(f"view {view.name} is circularly defined")
        self.expanding.append(view)
        try:
            compiled = self.compile_query(view.query)
        except OperationalError as exc:
            message = str(exc)
            if message.startswith("no such table: ") and "." not in message:
                # The view's tables are looked up in the main schema.
                raise OperationalError(message.replace(": ", ": main.", 1)) from None
            raise
        finally:
            self.expanding.pop()
        return DerivedSource(view.name, compiled, view.columns)

    def build_from(self, joins: list[Join], scope: Scope) -> tuple[list[Join], list[DerivedSource]]:
        """Add the FROM clause's tables to ``scope``.

        Returns the joins with USING / NATURAL turned into ON conditions, and
        the derived tables (which must be materialized before each run)."""
        derived = []
        normalized = []
        for index, join in enumerate(joins):
            ref = join.table
            if isinstance(ref, DerivedTable):
                # A subquery in FROM cannot see its sibling tables, only
                # the queries enclosing this one.
                compiled = self.compile_query(ref.query, parent=scope.parent)
                if compiled.correlated:
                    scope.uses_outer = True
                source = DerivedSource(ref.alias or "", compiled)
                derived.append(source)
                scope.add(source, ref.alias or "")
            elif self.catalog.find_view(ref.name) is not None:
                source = self.view_source(self.catalog.find_view(ref.name))
                derived.append(source)
                scope.add(source, ref.alias)
            else:
                table = self.catalog.get_table(ref.name)
                self.catalog.check_index_hint(table, ref.indexed_by)
                scope.add(table, ref.alias)
            if join.natural or join.using is not None:
                join = self.using_condition(scope, index, join)
            normalized.append(join)
        return normalized, derived

    @staticmethod
    def using_condition(scope: Scope, index: int, join: Join) -> Join:
        """``JOIN t USING (c, ...)`` / ``NATURAL JOIN t`` as an ON condition.
        The right table's copies of the columns become reachable only by
        qualified name, so ``c`` and ``*`` mean the left table's column."""
        right = scope.entries[index]
        left_entries = scope.entries[:index]

        def left_with(name):
            return next(
                (e for e in left_entries
                 if e.table.column_index(name) is not None and ascii_lower(name) not in e.hidden),
                None,
            )

        if join.natural:
            names = [c.name for c in right.table.columns if left_with(c.name) is not None]
        else:
            names = join.using
        condition = None
        for name in names:
            left = left_with(name)
            if left is None or right.table.column_index(name) is None:
                raise OperationalError(
                    f"cannot join using column {name} - column not present in both tables"
                )
            equal = Binary("=", Column(name, left.name), Column(name, right.name))
            condition = equal if condition is None else Binary("AND", condition, equal)
            right.hidden.add(ascii_lower(name))
        return dataclasses.replace(join, on=condition, using=None, natural=False)

    def plan_joins(self, scope: Scope, joins: list[Join], where: Expr | None, order_hint: int | None = None, covering: bool = False) -> tuple[list[JoinLevel], list[RowFunction]]:
        """Plan a nested loop over ``joins``; returns (levels, constants).

        WHERE conjuncts and the ON conditions of inner joins form one pool of
        filters; each is checked at the first level where all the tables it
        uses are bound.  Conjuncts that use none of the tables (and no
        subquery) are the ``constants``: like SQLite, callers test them once
        before the loop starts and skip it entirely when one is false.  A LEFT
        JOIN's ON condition decides which rows match at its own level (and is
        the only thing its access path may use).  Without LEFT JOINs the
        tables are joined in the cheapest order.
        """
        compiler = Compiler(scope, misuse="misuse of aggregate: {name}()", executor=self)
        on_compiler = Compiler(scope, executor=self)
        pool = split_conjuncts(fold_and(where))
        for join in joins:
            if join.kind != "LEFT":
                pool += split_conjuncts(fold_and(join.on))
        referenced = [(conjunct, tables_referenced(conjunct, scope)) for conjunct in pool]
        constants = [compiler.compile(conjunct) for conjunct, tables in referenced if not tables]
        referenced = [(conjunct, tables) for conjunct, tables in referenced if tables]
        order = list(range(len(joins)))
        if len(joins) > 1 and all(join.kind != "LEFT" for join in joins):
            order = self.join_order(scope, referenced, compiler)
        position = {table: i for i, table in enumerate(order)}
        placed = {}
        for conjunct, tables in referenced:
            level = max((position[t] for t in tables), default=0)
            placed.setdefault(level, []).append(conjunct)
        # Compile all conditions first: the access paths may then check which
        # columns the query uses (covering indexes).
        compiled = []
        for level, index in enumerate(order):
            join = joins[index]
            match = None
            if join.kind == "LEFT" and join.on is not None:
                match = on_compiler.compile(join.on)
            compiled.append((match, [compiler.compile(f) for f in placed.get(level, [])]))
        levels = []
        for level, (index, (match, filters)) in enumerate(zip(order, compiled)):
            join = joins[index]
            entry = scope.entries[index]
            usable = split_conjuncts(join.on) if join.kind == "LEFT" else pool
            hint = order_hint if level == 0 and index == 0 else None
            access = plan_access(scope, index, self.catalog, usable, compiler, hint,
                                 bound=set(order[:level]))
            if covering and isinstance(access, IndexScan):
                access.cover_if_possible(scope, index)
            levels.append(JoinLevel(entry.table, entry.offset, access,
                                    join.kind == "LEFT", match, filters))
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

        def passes(conditions):
            for condition in conditions:
                if not truth(condition(row)):
                    return False
            return True

        def visit(i):
            if i == depth:
                yield row
                return
            level = levels[i]
            start, stop = level.offset, level.offset + len(level.table.columns) + 1
            load = level.load
            matched = False
            for rowid, record in level.access.candidates(row):
                row[start:stop] = load(rowid, record)
                if level.match is not None and not truth(level.match(row)):
                    continue
                matched = True
                if passes(level.filters):
                    yield from visit(i + 1)
            if level.outer and not matched:
                row[start:stop] = [None] * (stop - start)
                if passes(level.filters):
                    yield from visit(i + 1)

        return visit(0)

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
            if item.alias:
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
        """Returns ([(source, index, descending, nulls first)], [functions]).

        ``source`` is "output" (index into the result row) or "key" (index into
        the extra sort values computed by ``functions``)."""
        terms, functions = [], []
        for position, item in enumerate(stmt.order_by, 1):
            nulls_first = item.nulls_first if item.nulls_first is not None else not item.descending
            index = self.result_column_reference(item.expr, names, "ORDER BY", position, compiler.scope)
            if index is not None:
                terms.append(("output", index, item.descending, nulls_first))
            else:
                terms.append(("key", len(functions), item.descending, nulls_first))
                functions.append(compiler.compile(item.expr))
        return terms, functions

    @staticmethod
    def compound_order_terms(stmt: Compound, parts: list[CompiledSelect]) -> list[OrderTerm]:
        """ORDER BY of a compound SELECT: every term must name a result column
        (by number, by name or alias, or as the same expression)."""
        terms = []
        count = len(parts[0].names)
        for position, item in enumerate(stmt.order_by, 1):
            nulls_first = item.nulls_first if item.nulls_first is not None else not item.descending
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
            terms.append(("output", index, item.descending, nulls_first))
        return terms

    def group_functions(self, stmt: Select, exprs: list[Expr], names: list[str], scope: Scope) -> list[RowFunction]:
        compiler = Compiler(
            scope, misuse="aggregate functions are not allowed in the GROUP BY clause", executor=self
        )
        functions = []
        for position, expr in enumerate(stmt.group_by, 1):
            index = self.result_column_reference(expr, names, "GROUP BY", position, scope)
            if index is not None:
                expr = exprs[index]
            functions.append(compiler.compile(expr))
        return functions

    @staticmethod
    def group_rows(rows: Iterable[Row], scope: Scope, group_functions: list[RowFunction], aggregates: AggregateCollector) -> Iterator[Row]:
        """Aggregate ``rows`` into groups; yield each group's representative row
        followed by its aggregate results, ordered by group key."""
        groups = {}
        sort_key = values.sort_key
        tracks_extreme = aggregates.tracks_extreme
        for row in rows:
            key = tuple(sort_key(f(row)) for f in group_functions)
            group = groups.get(key)
            if group is None:
                group = groups[key] = [list(row), aggregates.new_state()]
            if aggregates.step(group[1], row) and tracks_extreme:
                group[0] = list(row)
        if not groups and not group_functions:
            groups[()] = [[None] * scope.width, aggregates.new_state()]
        for key in sorted(groups):
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
            start = max(integer(offset), 0) if offset is not None else 0
            return start, None if count < 0 else start + count
        return bounds


    # ---- INSERT --------------------------------------------------------------

    def prepare_row(self, table: TableInfo, row: Row) -> int | None:
        """Apply column affinities; returns the requested row id."""
        for i, affinity in enumerate(table.affinities):
            row[i] = values.apply_affinity(row[i], affinity)
        if table.rowid_column is None:
            return None
        rowid = row[table.rowid_column]
        if rowid is not None and not isinstance(rowid, int):
            raise IntegrityError("datatype mismatch")
        return rowid

    @staticmethod
    def not_null_violation(table: TableInfo, row: Row) -> str | None:
        for i, column in enumerate(table.columns):
            if column.not_null and row[i] is None:
                return f"NOT NULL constraint failed: {table.name}.{column.name}"
        return None

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
        prefix = tuple(values.sort_key(v) for v in key_values)
        for key, _ in self.catalog.index_tree(index).scan(prefix, prefix + (HIGH,)):
            if key[-1][1] != own_rowid:
                return key[-1][1]
        return None

    @staticmethod
    def unique_error(table: TableInfo, index: IndexInfo) -> str:
        return "UNIQUE constraint failed: " + ", ".join(f"{table.name}.{c}" for c in index.column_names)

    def delete_row(self, table: TableInfo, tree: BTree, rowid: int) -> Row:
        """Delete a row and its index entries; returns it (with its row id)."""
        row = self.load_row(table, rowid, tree.get(rowid))
        self.remove_index_entries(table, row, rowid)
        tree.delete(rowid)
        return row

    def check_unique(self, table: TableInfo, row: Row, rowid: int) -> None:
        """Raise if another row has the same values in a UNIQUE index.

        NULLs never conflict.  Indexes are checked newest first, like SQLite.
        """
        sort_key = values.sort_key
        for index in table.indexes:
            if not index.unique:
                continue
            key_values = [row[p] for p in index.positions]
            if any(v is None for v in key_values):
                continue
            prefix = tuple(sort_key(v) for v in key_values)
            for key, _ in self.catalog.index_tree(index).scan(prefix, prefix + (HIGH,)):
                if key[-1][1] != rowid:
                    columns = ", ".join(f"{table.name}.{c}" for c in index.column_names)
                    raise IntegrityError(f"UNIQUE constraint failed: {columns}")

    def add_index_entries(self, table: TableInfo, row: Row, rowid: int) -> None:
        for index in table.indexes:
            self.catalog.index_tree(index).insert(index.key(row, rowid), b"")

    def remove_index_entries(self, table: TableInfo, row: Row, rowid: int) -> None:
        for index in table.indexes:
            self.catalog.index_tree(index).delete(index.key(row, rowid))

    @staticmethod
    def encode(table: TableInfo, row: Row) -> bytes:
        stored = list(row)
        if table.rowid_column is not None:
            stored[table.rowid_column] = None  # kept in the key, not the record
        return encode_record(stored)

    def insert_row(self, table: TableInfo, tree: BTree, row: Row, conflict: str = "ABORT", upserts: Sequence[PreparedUpsert] = ()) -> tuple[str, Row] | None:
        """Insert ``row`` under a conflict resolution (INSERT OR ...) and the
        statement's ON CONFLICT clauses.  Returns ("insert", row + [rowid]),
        ("update", row + [rowid]) when an upsert updated an existing row, or
        None when nothing changed (IGNORE, DO NOTHING, DO UPDATE ... WHERE false).

        As in SQLite: NOT NULL is checked first, then the upsert targets in
        clause order, then the row id and the other UNIQUE indexes (newest
        first).  REPLACE deletes each conflicting row and goes on."""
        raw = list(row)  # the values before column affinities (see below)
        rowid = self.prepare_row(table, row)
        violation = self.not_null_violation(table, row)
        if violation is not None:
            if conflict == "IGNORE":
                return None
            raise self.constraint_error(violation, conflict)
        if rowid is None:
            rowid = self.new_rowid(tree)
        if table.rowid_column is not None:
            row[table.rowid_column] = raw[table.rowid_column] = rowid
        constraints = [u.constraint for u in upserts if u.constraint is not None]
        constraints += [c for c in ["rowid"] + [i for i in table.indexes if i.unique] if c not in constraints]
        # SQLite applies the column affinities to the new values in place when
        # it checks the first index; an upsert's "excluded" row shows them
        # converted only if the conflict was found after that.
        converted = False
        for constraint in constraints:
            if constraint == "rowid":
                other = rowid if rowid in tree else None
            else:
                converted = True
                other = self.find_conflict(constraint, row, None)
            if other is None:
                continue
            upsert = next((u for u in upserts if u.constraint in (constraint, None)), None)
            if upsert is not None:
                return upsert.apply(tree, other, (row if converted else raw) + [rowid])
            if conflict == "IGNORE":
                return None
            if conflict == "REPLACE":
                self.delete_row(table, tree, other)
                continue
            message = self.rowid_conflict(table).args[0] if constraint == "rowid" else self.unique_error(table, constraint)
            raise self.constraint_error(message, conflict)
        tree.insert(rowid, self.encode(table, row))
        self.add_index_entries(table, row, rowid)
        return "insert", row + [rowid]

    def update_row(self, table: TableInfo, tree: BTree, rowid: int, old: Row, new: Row, conflict: str = "ABORT") -> Row | None:
        """Replace row ``rowid`` (``old``: its values and row id) with ``new``
        (values and row id).  Returns the stored row with its row id, or
        None if IGNORE skipped it."""
        width = len(table.columns)
        row = new[:width]
        if table.rowid_column is None:
            new_rowid = values.numeric_affinity(new[width])
            if not isinstance(new_rowid, int):
                raise IntegrityError("datatype mismatch")
            self.prepare_row(table, row)
        else:
            new_rowid = self.prepare_row(table, row)
            if new_rowid is None:
                raise IntegrityError("datatype mismatch")
        violation = self.not_null_violation(table, row)
        if violation is not None:
            if conflict == "IGNORE":
                return None
            raise self.constraint_error(violation, conflict)
        if new_rowid != rowid and new_rowid in tree:
            if conflict == "IGNORE":
                return None
            if conflict != "REPLACE":
                raise self.constraint_error(self.rowid_conflict(table).args[0], conflict)
            self.delete_row(table, tree, new_rowid)
        for index in table.indexes:
            if not index.unique:
                continue
            other = self.find_conflict(index, row, rowid)
            if other is None:
                continue
            if conflict == "IGNORE":
                return None
            if conflict != "REPLACE":
                raise self.constraint_error(self.unique_error(table, index), conflict)
            self.delete_row(table, tree, other)
        self.remove_index_entries(table, old, rowid)
        if new_rowid != rowid:
            tree.delete(rowid)
        tree.insert(new_rowid, self.encode(table, row), replace=True)
        self.add_index_entries(table, row, new_rowid)
        return row + [new_rowid]

    def compile_returning(self, items: list[SelectItem] | None, scope: Scope) -> tuple[list[RowFunction], list[str]] | None:
        """RETURNING: functions of a changed row (its values and row id) and the column names."""
        if items is None:
            return None
        exprs, names = self.expand_items(Select(items), scope)
        compiler = Compiler(scope, executor=self)
        return [compiler.compile(e) for e in exprs], names

    @staticmethod
    def new_rowid(tree: BTree) -> int:
        """One more than the largest row id; if that is taken by the maximum
        integer, try random ones like SQLite does."""
        last = tree.last_key()
        if last is None:
            return 1
        if last < values.INT_MAX:
            return last + 1
        for _ in range(100):
            candidate = random.randint(1, 2**62)
            if candidate not in tree:
                return candidate
        raise OperationalError("database or disk is full")

    @staticmethod
    def rowid_conflict(table: TableInfo) -> IntegrityError:
        if table.rowid_column is None:
            return IntegrityError(f"UNIQUE constraint failed: {table.name}.rowid")
        name = table.columns[table.rowid_column].name
        return IntegrityError(f"UNIQUE constraint failed: {table.name}.{name}")

    # ---- indexes -------------------------------------------------------------

    def reindex(self, name: str | None) -> Result:
        """Rebuild the indexes of ``name`` (an index, a table, or the default
        collation BINARY), or all of them."""
        catalog = self.catalog
        lowered = None if name is None else ascii_lower(name)
        if lowered is None or lowered == "binary":
            indexes = list(catalog.indexes.values())
        elif lowered in catalog.indexes:
            indexes = [catalog.indexes[lowered]]
        elif lowered in catalog.tables:
            indexes = list(catalog.tables[lowered].indexes)
        elif lowered in ("nocase", "rtrim"):
            indexes = []  # no index uses these collations
        else:
            raise OperationalError("unable to identify the object to be reindexed")
        for index in indexes:
            tree = catalog.index_tree(index)
            tree.clear()
            for rowid, record in catalog.table_tree(index.table).scan():
                tree.insert(index.key(self.load_row(index.table, rowid, record), rowid), b"")
        return Result()

    def create_index(self, stmt: CreateIndex) -> Result:
        index = self.catalog.create_index(stmt)
        if index is None:
            return Result()
        table = index.table
        index_tree = self.catalog.index_tree(index)
        for rowid, record in self.catalog.table_tree(table).scan():
            row = self.load_row(table, rowid, record)
            if index.unique:
                self.check_unique(table, row, rowid)
            index_tree.insert(index.key(row, rowid), b"")
        return Result()


class JoinLevel:
    """One table of a nested loop join."""

    def __init__(self, table: Source, offset: int, access: AccessPath, outer: bool, match: RowFunction | None, filters: list[RowFunction]) -> None:
        self.table = table
        self.offset = offset  # position of the table's first slot in a row
        self.access = access
        self.outer = outer  # LEFT JOIN: emit a NULL row when nothing matches
        self.match = match  # LEFT JOIN ON condition
        self.filters = filters  # conditions checked once this table is bound
        if isinstance(table, DerivedSource) or getattr(access, "yields_rows", False):
            self.load = lambda rowid, row: row  # already a row
        else:
            load_row = Executor.load_row
            self.load = lambda rowid, record: load_row(table, rowid, record)


class CompiledSelect:
    """A SELECT compiled once; ``run()`` evaluates it (again) and returns its rows."""

    def __init__(self, executor: Executor, stmt: Select, parent: Scope | None = None) -> None:
        self.executor = executor
        self.scope = scope = Scope(parent)
        joins, self.derived = executor.build_from(stmt.source, scope)
        self.exprs, self.names = executor.expand_items(stmt, scope)
        self.is_aggregate = bool(stmt.group_by) or any(
            contains_aggregate(e)
            for e in self.exprs + [stmt.having] + [item.expr for item in stmt.order_by]
            if e is not None
        )
        if stmt.having is not None and not self.is_aggregate:
            raise OperationalError("HAVING clause on a non-aggregate query")
        self.aggregates = AggregateCollector(scope.width) if self.is_aggregate else None
        compiler = Compiler(scope, self.aggregates, executor=executor)
        compiled = [compiler.compile_with_affinity(e) for e in self.exprs]
        self.outputs = [function for function, _ in compiled]
        self.affinities = [affinity for _, affinity in compiled]
        self.order_terms, self.order_functions = executor.order_terms(
            stmt, self.exprs, self.names, compiler
        )
        self.having = compiler.compile(stmt.having) if stmt.having is not None else None
        self.group_functions = executor.group_functions(stmt, self.exprs, self.names, scope)
        self.levels = None
        self.constants = []  # conditions tested once, before the loop
        order_columns = self.order_columns(stmt)
        if stmt.source:
            hint = order_columns[0] if order_columns and stmt.limit is not None else None
            self.levels, self.constants = executor.plan_joins(
                scope, joins, stmt.where, hint, covering=True
            )
        elif stmt.where is not None:
            where = Compiler(scope, misuse="misuse of aggregate: {name}()", executor=executor)
            self.constants = [where.compile(stmt.where)]
        self.distinct = stmt.distinct
        self.limit = executor.compile_limit(stmt)
        # True when the first table's access path already yields ORDER BY order.
        self.presorted = bool(
            self.levels and order_columns and not self.is_aggregate
            and follows_order(order_columns, self.levels[0].access.order())
        )

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
        for (source, index, descending, nulls_first), item in zip(self.order_terms, stmt.order_by):
            expr = self.exprs[index] if source == "output" else item.expr
            if descending or not nulls_first or not isinstance(expr, Column):
                return None
            slot, _, table_index, depth = self.scope.resolve(expr)
            if depth or table_index != 0:
                return None
            position = slot - entry.offset
            if slot == rowid_slot or position == table.rowid_column:
                position = ROWID
            columns.append(position)
        return columns

    @property
    def correlated(self) -> bool:
        return self.scope.uses_outer

    def run(self, max_rows: int | None = None) -> list[tuple]:
        """The result rows (tuples).  ``max_rows`` lets a caller that needs
        only the first rows (EXISTS, scalar subqueries) stop early."""
        if not passes_constants(self.constants, self.scope):
            rows = []
        elif self.levels is not None:
            for source in self.derived:
                source.materialize()
            rows = self.executor.join_rows(self.scope, self.levels)
        else:
            rows = [[]]
        outputs, order_functions = self.outputs, self.order_functions
        start, end = self.limit() if self.limit is not None else (0, None)
        if max_rows is not None and not self.order_terms and not self.distinct:
            end = max_rows if end is None else min(end, start + max_rows)
        if self.is_aggregate:
            truth, having = values.truth, self.having
            rows = (
                group_row
                for group_row in self.executor.group_rows(
                    rows, self.scope, self.group_functions, self.aggregates
                )
                if having is None or truth(having(group_row))
            )
        records = ((tuple(f(row) for f in outputs), tuple(f(row) for f in order_functions))
                   for row in rows)
        if self.distinct:
            records = distinct_records(records)
        if self.presorted or not self.order_terms:
            # Rows already come in ORDER BY order: stop as soon as LIMIT is met.
            records = itertools.islice(records, start, end)
        else:
            records = order_records(records, self.order_terms, start, end)
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
        self.parts = [CompiledSelect(executor, select, parent) for select in stmt.selects]
        count = len(self.parts[0].names)
        for operator, part in zip(stmt.operators, self.parts[1:]):
            if len(part.names) != count:
                raise OperationalError(
                    f"SELECTs to the left and right of {operator} "
                    "do not have the same number of result columns"
                )
        self.operators = stmt.operators
        self.names = self.parts[0].names
        # SQLite takes a compound's affinity from its last SELECT.
        self.affinities = self.parts[-1].affinities
        self.order_terms = executor.compound_order_terms(stmt, self.parts)
        self.limit = executor.compile_limit(stmt)

    @property
    def correlated(self) -> bool:
        return any(part.correlated for part in self.parts)

    def run(self, max_rows: int | None = None) -> list[tuple]:
        rows = self.parts[0].run()
        for operator, part in zip(self.operators, self.parts[1:]):
            rows = combine(operator, rows, part.run())
        start, end = self.limit() if self.limit is not None else (0, None)
        records = order_records([(row, ()) for row in rows], self.order_terms, start, end)
        return [row for row, _ in records]


class PreparedSelect:
    def __init__(self, compiled: CompiledQuery) -> None:
        self.compiled = compiled

    def run(self) -> Result:
        return Result(self.compiled.run(), self.compiled.names)


class PreparedInsert:
    def __init__(self, executor: Executor, stmt: Insert) -> None:
        self.executor = executor
        table = self.table = executor.catalog.table_to_modify(stmt.table)
        width = len(table.columns)
        if stmt.columns is None:
            self.positions = list(range(width))
        else:
            self.positions = []
            for name in stmt.columns:
                position = table.column_index(name)
                if position is None:
                    raise OperationalError(f"table {table.name} has no column named {name}")
                self.positions.append(position)
        compiler = Compiler(Scope(), executor=executor)
        self.conflict = stmt.conflict
        self.upserts = [PreparedUpsert(executor, table, clause) for clause in stmt.upsert]
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
        multi_write = self.query is not None or len(self.rows) > 1
        self.statement_journal = multi_write and (self.may_abort() or calls_function(stmt))

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
        if conflict in ("ABORT", "REPLACE") and any(c.not_null for c in table.columns):
            return True  # REPLACE fixes NOT NULL with a default value; MiniDB has none

        def handled(constraint):
            return conflict != "ABORT" or any(u.constraint in (constraint, None) for u in self.upserts)

        if table.rowid_column is not None and table.rowid_column in self.positions and not handled("rowid"):
            return True
        if any(index.unique and not handled(index) for index in table.indexes):
            return True
        checked = {i for i, c in enumerate(table.columns) if c.not_null}
        checked.update(p for index in table.indexes if index.unique for p in index.positions)
        checked.update((table.rowid_column, len(table.columns)))
        return any(u.assignments and any(p in checked for p, _ in u.assignments) for u in self.upserts)

    def check_count(self, stmt: Insert, count: int) -> None:
        if count != len(self.positions):
            if stmt.columns is None:
                raise OperationalError(
                    f"table {self.table.name} has {len(self.table.columns)} columns "
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
        for source in sources:
            row = [None] * width
            for position, value in zip(self.positions, source):
                row[position] = value
            rows.append(row)
        changed = []  # rows inserted or updated by an upsert, with their row ids
        try:
            for row in rows:
                outcome = executor.insert_row(table, self.tree, row, self.conflict, self.upserts)
                if outcome is not None:
                    kind, stored = outcome
                    if kind == "insert":
                        executor.last_insert_rowid = stored[-1]
                    changed.append(stored)
        except Error as exc:
            exc.changes = len(changed)  # the rows that FAIL (or no statement journal) keeps
            raise
        return returning_result(self.returning, changed)


class PreparedSingleTable:
    """The part of UPDATE / DELETE that finds the rows matching WHERE."""

    def __init__(self, executor: Executor, table_name: str, where: Expr | None, indexed_by: str | None = None) -> None:
        self.executor = executor
        self.table = executor.catalog.table_to_modify(table_name)
        executor.catalog.check_index_hint(self.table, indexed_by)
        self.tree = executor.catalog.table_tree(self.table)
        self.scope = Scope()
        joins, _ = executor.build_from([Join(TableRef(self.table.name))], self.scope)
        self.levels, self.constants = executor.plan_joins(self.scope, joins, where)
        self.rowid_slot = self.scope.rowid_slot(0)

    def matching_rows(self) -> list[tuple[int, Row]]:
        """(rowid, row copy) of every matching row, all found before any change."""
        slot = self.rowid_slot
        if not passes_constants(self.constants, self.scope):
            return []
        return [(row[slot], list(row)) for row in self.executor.join_rows(self.scope, self.levels)]


class PreparedUpdate(PreparedSingleTable):
    def __init__(self, executor: Executor, stmt: Update) -> None:
        super().__init__(executor, stmt.table, stmt.where, stmt.indexed_by)
        table, width = self.table, len(self.table.columns)
        compiler = Compiler(self.scope, executor=executor)
        self.assignments = []
        for name, expr in stmt.assignments:
            position = table.column_index(name)
            if position is None:
                if ascii_lower(name) not in ROWID_NAMES:
                    raise OperationalError(f"no such column: {name}")
                position = width if table.rowid_column is None else table.rowid_column
            self.assignments.append((position, compiler.compile(expr)))
        self.conflict = stmt.conflict
        self.returning = executor.compile_returning(stmt.returning, self.scope)
        # Like PreparedInsert.may_abort: the constraints the changed columns
        # take part in, under ABORT (REPLACE, for NOT NULL).
        changed = {p for p, _ in self.assignments}
        width = len(table.columns)
        rowid_changed = bool(changed & {width, table.rowid_column})
        self.statement_journal = calls_function(stmt) or (
            stmt.conflict in ("ABORT", "REPLACE") and any(table.columns[p].not_null for p in changed if p < width)
        ) or (stmt.conflict == "ABORT" and (rowid_changed or any(
            index.unique and changed & set(index.positions) for index in table.indexes
        )))

    def run(self) -> Result:
        executor, table, tree = self.executor, self.table, self.tree
        changed = []
        try:
            for rowid, old in self.matching_rows():
                if self.conflict == "REPLACE" and rowid not in tree:
                    continue  # an earlier row's REPLACE deleted it
                new = list(old)
                for position, function in self.assignments:
                    new[position] = function(old)
                stored = executor.update_row(table, tree, rowid, old, new, self.conflict)
                if stored is not None:
                    changed.append(stored)
        except Error as exc:
            exc.changes = len(changed)
            raise
        return returning_result(self.returning, changed)


class PreparedDelete(PreparedSingleTable):
    def __init__(self, executor: Executor, stmt: Delete) -> None:
        super().__init__(executor, stmt.table, stmt.where, stmt.indexed_by)
        self.returning = executor.compile_returning(stmt.returning, self.scope)
        self.delete_all = stmt.where is None and self.returning is None

    def run(self) -> Result:
        executor, table, tree = self.executor, self.table, self.tree
        if self.delete_all:
            count = len(tree)
            tree.clear()
            for index in table.indexes:
                executor.catalog.index_tree(index).clear()
            return Result(rowcount=count)
        matches = self.matching_rows()
        for rowid, row in matches:
            executor.remove_index_entries(table, row, rowid)
            tree.delete(rowid)
        return returning_result(self.returning, [row for _, row in matches])


class PreparedUpsert:
    """One ON CONFLICT clause of an INSERT."""

    def __init__(self, executor: Executor, table: TableInfo, clause: Upsert) -> None:
        self.executor = executor
        self.table = table
        self.constraint = self.find_constraint(table, clause)  # None: any uniqueness constraint
        self.assignments = None  # None: DO NOTHING
        if clause.assignments is not None:
            # SET and WHERE see the existing row (by the table's name) and "excluded".
            self.scope = Scope()
            self.scope.add(table)
            self.scope.add(ExcludedSource(table))
            # An unqualified name is the existing row's column; excluded.x must be qualified.
            self.scope.entries[1].hidden.update(ascii_lower(c.name) for c in table.columns)
            compiler = Compiler(self.scope, executor=executor)
            width = len(table.columns)
            self.assignments = []
            for name, expr in clause.assignments:
                position = table.column_index(name)
                if position is None:
                    if ascii_lower(name) not in ROWID_NAMES:
                        raise OperationalError(f"no such column: {name}")
                    position = width if table.rowid_column is None else table.rowid_column
                self.assignments.append((position, compiler.compile(expr)))
            self.where = compiler.compile(clause.where) if clause.where is not None else None

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
                if table.column_index(name) is None and ascii_lower(name) not in ROWID_NAMES:
                    raise OperationalError(f"no such column: {name}")
            if len(wanted) == 1 and wanted[0] in (alias, *ROWID_NAMES):
                return "rowid"
            for index in table.indexes:
                names = [ascii_lower(c) for c in index.column_names]
                if (index.unique and alias not in names and len(names) == len(wanted)
                        and set(names) == set(wanted)):
                    return index
        raise OperationalError("ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint")

    def apply(self, tree: BTree, rowid: int, excluded: Row) -> tuple[str, Row] | None:
        """Handle a conflict with existing row ``rowid``; ``excluded`` is the
        row that could not be inserted (with its row id)."""
        if self.assignments is None:
            return None
        executor, table = self.executor, self.table
        old = executor.load_row(table, rowid, tree.get(rowid))
        context = old + excluded
        if self.where is not None and not values.truth(self.where(context)):
            return None
        new = list(old)
        for position, function in self.assignments:
            new[position] = function(context)
        return "update", executor.update_row(table, tree, rowid, old, new)


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


def combine(operator: str, left: list[tuple], right: list[tuple]) -> list[tuple]:
    """Apply a compound operator.  Like SQLite, the distinct forms return rows
    in sorted order, and a later duplicate replaces an earlier one."""
    if operator == "UNION ALL":
        return left + right
    sort_key = values.sort_key

    def key(row):
        return tuple(sort_key(v) for v in row)

    kept = {}
    if operator == "UNION":
        for row in left + right:
            kept[key(row)] = row
    else:
        right_keys = {key(row) for row in right}
        want = operator == "INTERSECT"
        for row in left:
            k = key(row)
            if (k in right_keys) == want:
                kept[k] = row
    return [kept[k] for k in sorted(kept)]


class ExcludedSource:
    """The ``excluded`` row of an upsert: the table's columns without
    affinities (as in SQLite; its values may not be converted either, see
    Executor.insert_row)."""

    has_rowid = True
    indexes = ()

    def __init__(self, table: TableInfo) -> None:
        self.name = "excluded"
        self.columns = table.columns
        self.rowid_column = table.rowid_column
        self.affinities = [None] * len(table.columns)
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

    def __init__(self, name: str, compiled: CompiledQuery, names: list[str] | None = None) -> None:
        """``names``: the column names a view declares, if any."""
        self.name = name or "subquery"
        self.compiled = compiled
        if names is None:
            names = unique_names(compiled.names)
        elif len(names) != len(compiled.names):
            raise OperationalError(
                f"expected {len(names)} columns for '{name}' but got {len(compiled.names)}"
            )
        self.columns = [ColumnName(n) for n in names]
        self.affinities = list(compiled.affinities)
        self.positions = {}
        for i, n in enumerate(names):
            self.positions.setdefault(ascii_lower(n), i)
        self.rows = []

    def column_index(self, name: str) -> int | None:
        return self.positions.get(ascii_lower(name))

    def materialize(self) -> None:
        self.rows = [list(row) + [i] for i, row in enumerate(self.compiled.run(), 1)]


def unique_names(names: list[str]) -> list[str]:
    """Column names of a subquery or view as SQLite makes them unique: a
    repeated name gets ":1", ":2", ... (ignoring case)."""
    seen = set()
    result = []
    for name in names:
        base, count = name, 0
        while ascii_lower(name) in seen:
            count += 1
            name = f"{base}:{count}"
        seen.add(ascii_lower(name))
        result.append(name)
    return result


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


def distinct_records(records: Iterable[Record]) -> Iterator[Record]:
    """Drop records with duplicate output rows, keeping the first;
    1 and 1.0 count as equal."""
    seen = set()
    sort_key = values.sort_key
    for record in records:
        key = tuple(sort_key(v) for v in record[0])
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
    sort_key = values.sort_key
    parts = []
    for source, index, descending, nulls_first in terms:
        parts.append((0 if source == "output" else 1, index, descending,
                      (0,) if nulls_first else (2,)))

    def key(record):
        result = []
        for column, index, descending, null_key in parts:
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
