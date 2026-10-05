"""Compiled queries: a SELECT is compiled once into a ``CompiledSelect``
(or ``CompiledCompound`` / ``CompiledValues``) whose ``run()`` can be called
many times - a correlated subquery runs once per row of its outer query."""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from minidb import values
from minidb.catalog import TableInfo
from minidb.errors import OperationalError
from minidb.parser import (
    Binary, Call, Column, Compound, Delete, Exists, Expr, InSelect, Insert, Join, Select, SelectItem, Star,
    Subquery, Update, Values,
)
from minidb.values import ascii_lower
from minidb.expressions import (
    AggregateCollector, AliasReference, CompiledQuery, Compiler, NeedsAggregate, Result, Row, RowFunction, Scope,
    WindowCollector, contains_window, fold_and, has_collate, is_true_false, owns_aggregate, split_conjuncts,
    strip_collate, substitute_columns, tuple_function, walk,
)
from minidb.ordering import combine, distinct_records, order_key, order_records
from minidb.sources import DerivedSource, compound_table_affinities
from minidb.planner import IndexScan, ROWID

if TYPE_CHECKING:
    from minidb.executor import Executor
else:
    Executor = Any  # (only in annotations: their modules import this one)


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
        # Only count(*) over one index range with nothing else to test: count
        # the keys rather than visit the rows (see count_rows).
        self.count_level = None
        if self.is_aggregate and not self.group_functions and not self.first_row_only \
                and self.aggregates is not None and self.aggregates.calls \
                and all(name == "COUNT" and not args and filter_ is None
                        for name, args, _, filter_ in self.aggregates.calls) \
                and self.levels is not None and len(self.levels) == 1 and self.levels[0].plain \
                and not self.levels[0].filters and isinstance(self.levels[0].access, IndexScan):
            self.count_level = self.levels[0]
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

        def local_slot(expr: Column) -> int | None:
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

        def replacer(windows_allowed: bool) -> Callable[[Column], Expr | None]:
            def replace(column: Column) -> Expr | None:
                key = ascii_lower(column.name)
                if column.table is not None or key not in aliases or scope._matches(column):
                    return None
                item = aliases[key]
                if not windows_allowed and contains_window(item.expr):
                    raise OperationalError(f"misuse of aliased window function {item.alias}")
                return item.expr
            return replace

        restricted, ordering = replacer(False), replacer(True)

        def term(expr: Expr, replace: Callable[[Column], Expr | None]) -> Expr:
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

    def count_rows(self) -> Row:
        """The one group row of a query whose aggregates are all count(*) over
        ``count_level``: the counts are the index keys in range; the group's
        representative row (its bare columns) is the first row, as
        group_rows would keep it."""
        level, aggregates = self.count_level, self.aggregates
        row = [None] * self.scope.width
        state = aggregates.new_state()
        candidates = level.access.candidates(row)
        first = next(candidates, None)
        candidates.close()
        if first is not None:
            count = level.access.count(row)
            row[level.offset:level.offset + len(level.table.columns) + 1] = level.load(*first)
            for aggregate, _ in state:
                aggregate.count = count
        return row + aggregates.results(state)

    def run(self, max_rows: int | None = None) -> list[tuple]:
        """The result rows (tuples).  ``max_rows`` lets a caller that needs
        only the first rows (EXISTS, scalar subqueries) stop early."""
        # (SQLite computes LIMIT first; with LIMIT 0 nothing else runs.)
        start, end = self.limit() if self.limit is not None else (0, None)
        if end is not None and end <= start:
            return []
        counted = None
        if not passes_constants(self.constants, self.scope):
            rows = []
        elif self.levels is not None:
            for source in self.derived:
                source.materialize()
            if self.count_level is not None:
                counted = self.count_rows()
            else:
                rows = self.executor.join_rows(self.scope, self.levels)
        else:
            rows = [[]]
        output_row, order_key = self.output_row, self.order_key
        if max_rows is not None and not self.order_terms and not self.distinct:
            end = max_rows if end is None else min(end, start + max_rows)
        if counted is not None:
            having = self.having
            rows = [counted] if having is None or values.truth(having(counted)) else []
        elif self.is_aggregate:
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
            rows = self.windows.apply(rows, self.window_sorter)
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


_PLANNED = frozenset((Select, Compound, Values, Insert, Update, Delete))  # (statements with a prepared plan)


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
