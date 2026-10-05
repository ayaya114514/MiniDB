"""Access paths and join planning.

For each table the planner looks at the WHERE conjuncts and picks an access
path: a lookup or range scan on the row id when a conjunct constrains the
INTEGER PRIMARY KEY (or ``rowid``), an index search, a hash lookup for an
unindexed equality join, otherwise a full scan.  Access paths narrow the
candidate rows; the conditions an index lookup does not decide exactly are
still applied to every candidate.  ``JoinLevel`` and ``inner_join_loop``
turn a join order into a nested loop (generated Python source).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

from minidb import values
from minidb.btree import BTree
from minidb.catalog import HIGH, Catalog, IndexInfo, TableInfo
from minidb.errors import OperationalError
from minidb.parser import Between, Binary, Column, InList
from minidb.parser import Expr
from minidb.values import SQLValue
from minidb.record import decode_row
from minidb.sqlite_pager import SqlitePager
from minidb.expressions import (
    AccessPath, AliasReference, Bound, Compiler, NOT_INDEXED, Row, RowFunction, Scope, Source, _AFFINITY_FUNCTIONS,
    _CODE_CACHE, _FLIPPED, has_collate, is_parse_constant, is_true_false, split_conjuncts, split_disjuncts,
    strip_collate, tables_referenced,
)
from minidb.generated import expand_virtual, load_row
from minidb.sources import DerivedSource, JsonEachScan, JsonEachSource, PragmaSource


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
        # The equality conjuncts the lookup decides exactly (SQLite's
        # disableTerm): the join loop need not test them again.
        self.consumed = []
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
        bounds = self.bounds(row)
        if bounds is None:
            return iter(())
        return (key for key, _ in self.index_tree.scan(*bounds))

    def count(self, row: Row) -> int:
        """How many index keys are in range."""
        bounds = self.bounds(row)
        if bounds is None:
            return 0
        count_range = getattr(self.index_tree, "count_range", None)
        if count_range is None:
            return sum(1 for _ in self.index_tree.scan(*bounds))
        return count_range(*bounds)

    def bounds(self, row: Row) -> tuple[tuple, tuple, bool, bool] | None:
        """(start, end, start inclusive, end inclusive) of the index keys in
        range, or None when nothing can be."""
        key_functions = self.index.key_functions
        prefix = []
        for key_function, sort_key in zip(self.equal, key_functions):
            value = key_function(row)
            if value is None:
                return None  # col = NULL is never true
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
                return None
            if self.lower[1]:
                start, start_inclusive = prefix + (sort_key(value),), True
            else:
                start, start_inclusive = prefix + (sort_key(value), HIGH), False
        if self.upper:
            value = self.upper[0](row)
            if value is None:
                return None
            if self.upper[1]:
                end = prefix + (sort_key(value), HIGH)
            else:
                end, end_inclusive = prefix + (sort_key(value),), False
        return start, end, start_inclusive, end_inclusive

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
            table = self.table
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

    def __init__(self, position: int, op: str, key: RowFunction | list[RowFunction], convert: Callable[[SQLValue], SQLValue] | None = None, joined: bool = False, collation: str = "BINARY", source: Expr | None = None) -> None:
        self.position = position  # column position in the table, or ROWID
        self.op = op  # "=", "<", "<=", ">", ">=" or "IN"
        self.key = key  # key function(s) evaluated on the outer row
        self.convert = convert  # the affinity conversion the key gets
        self.joined = joined  # the key uses a table joined before this one
        self.collation = collation  # of the comparison: an index must have the same
        self.source = source  # the conjunct itself (None: a term derived from one)


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

    def column_position(expr: Expr) -> int | None:
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

    def key_function(position: int, expr: Expr, key_affinity: str | None = None) -> RowFunction | None:
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
            constraints.append(Constraint(position, op, key, conversions.get(key), joined, collation, conjunct))
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
        equal, consumed = [], []
        for position in info.positions:
            found = next((c for c in usable if c.position == position and c.op == "="), None)
            if found is None:
                break
            equal.append(found.key)
            if found.source is not None and position not in info.table.virtual:
                consumed.append(found.source)
        lower = upper = None
        if len(equal) < len(info.positions):
            lower, upper = _bounds(usable, info.positions[len(equal)])
        if equal or lower or upper:
            candidates.append(IndexScan(info, catalog.index_tree(info), tree, equal, lower, upper, rows))
            candidates[-1].consumed = consumed
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
            self.load = lambda rowid, record: load_row(table, rowid, record)

    def skip_virtual(self, unused: dict[int, None]) -> None:
        """Leave the VIRTUAL columns ``unused`` NULL instead of computing them."""
        table, access = self.table, self.access
        if isinstance(access, IndexScan):
            access.unused = unused
        if not getattr(access, "yields_rows", False):
            self.load = lambda rowid, record: expand_virtual(
                table, record if type(record) is list else decode_row(record), rowid, unused)
