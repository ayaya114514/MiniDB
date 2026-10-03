"""What can stand in FROM besides a table: subqueries, views and CTEs
(``DerivedSource``, recursive ones included), ``pragma_xxx()`` and
``json_each()`` / ``json_tree()``, with the affinities and JSON subtypes
their columns carry."""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable, Iterator, Sequence
from typing import Any, TYPE_CHECKING

from minidb import jsonfuncs, pragmas, values
from minidb.catalog import TableInfo
from minidb.errors import OperationalError
from minidb.parser import (
    Binary, Call, Case, Cast, Collate, Column, Cte, Literal, Parameter, Select, Star, Subquery, TableFunction,
    TableRef, Unary,
)
from minidb.parser import is_true_false_name
from minidb.jsonb import JSONBlob, JSONText
from minidb.values import ascii_lower
from minidb.expressions import (
    CompiledQuery, Compiler, OrderTerm, Row, RowFunction, Scope, contains_aggregate, contains_window,
)
from minidb.generated import walk_nodes
from minidb.ordering import order_key, row_key, unique_names

if TYPE_CHECKING:
    from minidb.executor import Executor
else:
    Executor = Any  # (only in annotations: their modules import this one)


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
