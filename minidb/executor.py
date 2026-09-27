"""Query execution.

Expressions are compiled into Python closures that take a *row*: a list
holding, for every table in the query, its column values followed by its row
id.  A ``Scope`` maps column names to positions in that list.

For each table the planner looks at the WHERE conjuncts and picks an access
path: a lookup or range scan on the row id when a conjunct constrains the
INTEGER PRIMARY KEY (or ``rowid``), otherwise a full scan.  The access path
only narrows the candidate rows; the complete WHERE clause is still applied
to every candidate, so planning can never change a query's result.
"""

import random
from operator import itemgetter

from minidb import values
from minidb.btree import BTreeError, DuplicateKeyError
from minidb.catalog import HIGH
from minidb.errors import IntegrityError, OperationalError
from minidb.parser import (
    Between, Binary, Call, Column, CreateIndex, CreateTable, Delete, DropIndex, DropTable,
    Explain, InList, Insert, Join, Like, Literal, Select, Star, TableRef, Unary, Update,
)
from minidb.record import decode_record, encode_record

ROWID_NAMES = ("rowid", "oid", "_rowid_")


class Result(list):
    """Rows (a list of tuples) plus the result column names."""

    def __init__(self, rows=(), columns=()):
        super().__init__(rows)
        self.columns = list(columns)


# ---- name resolution --------------------------------------------------------


class Scope:
    """The tables visible to expressions and where their values sit in a row."""

    def __init__(self):
        self.entries = []  # (name, TableInfo, offset of its first column)
        self.width = 0

    def add(self, table, alias=None):
        name = (alias or table.name).lower()
        self.entries.append((name, table, self.width))
        self.width += len(table.columns) + 1

    def rowid_slot(self, index):
        _, table, offset = self.entries[index]
        return offset + len(table.columns)

    def resolve(self, column):
        """Return (slot, affinity, table index) for a column reference."""
        matches = []
        for index, (name, table, offset) in enumerate(self.entries):
            if column.table is not None and column.table.lower() != name:
                continue
            position = table.column_index(column.name)
            if position is not None:
                matches.append((offset + position, table.affinities[position], index))
            elif column.name.lower() in ROWID_NAMES:
                matches.append((offset + len(table.columns), values.INTEGER, index))
        if not matches:
            full_name = f"{column.table}.{column.name}" if column.table else column.name
            raise OperationalError(f"no such column: {full_name}")
        if len(matches) > 1:
            raise OperationalError(f"ambiguous column name: {column.name}")
        return matches[0]

    def star_columns(self, table_name=None):
        """(table name, column name) pairs that ``*`` or ``table.*`` expands to."""
        if not self.entries:
            raise OperationalError("no tables specified")
        result = []
        found = False
        for name, table, offset in self.entries:
            if table_name is not None and table_name.lower() != name:
                continue
            found = True
            result.extend((name, c.name) for c in table.columns)
        if not found:
            raise OperationalError(f"no such table: {table_name}")
        return result


def walk(expr):
    """Yield ``expr`` and all of its sub-expressions."""
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
    elif isinstance(expr, Like):
        yield from walk(expr.expr)
        yield from walk(expr.pattern)
    elif isinstance(expr, Call):
        for arg in expr.args:
            yield from walk(arg)


def tables_referenced(expr, scope):
    return {scope.resolve(e)[2] for e in walk(expr) if isinstance(e, Column)}


def split_conjuncts(expr):
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
}

_AFFINITY_FUNCTIONS = {values.INTEGER: values.numeric_affinity, values.TEXT: values.text_affinity}

_FLIPPED = {"=": "=", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


def value_comparator(op, left_affinity, right_affinity):
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

    def __init__(self, scope, aggregates=None, misuse="misuse of aggregate function {name}()"):
        self.scope = scope
        self.aggregates = aggregates
        self.misuse = misuse

    def compile(self, expr):
        return self.compile_with_affinity(expr)[0]

    def compile_with_affinity(self, expr):
        """Return (function, affinity); only column references have an affinity."""
        if isinstance(expr, Literal):
            value = expr.value
            return (lambda row: value), None
        if isinstance(expr, Column):
            slot, affinity, _ = self.scope.resolve(expr)
            return itemgetter(slot), affinity
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

    def _unary(self, expr):
        operand = self.compile(expr.operand)
        if expr.op == "-":
            negate = values.negate
            return lambda row: negate(operand(row))
        if expr.op == "+":
            return operand
        logical_not = values.logical_not
        return lambda row: logical_not(operand(row))

    def _binary(self, expr):
        op = expr.op
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

    def _between(self, expr):
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

    def _in_list(self, expr):
        value, affinity = self.compile_with_affinity(expr.expr)
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

    def _like(self, expr):
        value = self.compile(expr.expr)
        pattern = self.compile(expr.pattern)
        like, logical_not = values.like, values.logical_not
        if expr.negated:
            return lambda row: logical_not(like(value(row), pattern(row)))
        return lambda row: like(value(row), pattern(row))

    def call(self, expr):
        name = expr.name
        if values.is_aggregate_call(name, len(expr.args)):
            return self._aggregate(expr)
        if name not in values.SCALAR_FUNCTIONS:
            raise OperationalError(f"no such function: {name.lower()}")
        function, min_args, max_args = values.SCALAR_FUNCTIONS[name]
        if expr.distinct or len(expr.args) < min_args or (
            max_args is not None and len(expr.args) > max_args
        ):
            raise OperationalError(f"wrong number of arguments to function {name.lower()}()")
        args = [self.compile(arg) for arg in expr.args]
        if len(args) == 1:
            (arg,) = args
            return lambda row: function(arg(row))
        return lambda row: function(*[arg(row) for arg in args])

    def _aggregate(self, expr):
        name = expr.name
        if self.aggregates is None:
            raise OperationalError(self.misuse.format(name=name.lower()))
        _, min_args, max_args = values.AGGREGATE_FUNCTIONS[name]
        star = expr.args == (Star(),)
        if star and name != "COUNT" or not min_args <= len(expr.args) <= max_args:
            raise OperationalError(f"wrong number of arguments to function {name.lower()}()")
        if star or not expr.args:
            args = []  # COUNT(*) and COUNT()
        else:
            inner = Compiler(self.scope)  # aggregates may not be nested
            args = [inner.compile(arg) for arg in expr.args]
        return itemgetter(self.aggregates.add(name, args, expr.distinct))


def contains_aggregate(expr):
    return any(
        isinstance(e, Call) and values.is_aggregate_call(e.name, len(e.args)) for e in walk(expr)
    )


class AggregateCollector:
    """The aggregate calls of a query and their per-group state."""

    def __init__(self, base_width):
        self.base_width = base_width  # aggregate results follow the row's slots
        self.calls = []  # (name, argument functions, distinct)

    def add(self, name, args, distinct):
        self.calls.append((name, args, distinct))
        return self.base_width + len(self.calls) - 1

    @property
    def tracks_extreme(self):
        """A lone MIN()/MAX() makes bare columns come from its row, as in SQLite."""
        return len(self.calls) == 1 and self.calls[0][0] in ("MIN", "MAX")

    def new_state(self):
        state = []
        for name, args, distinct in self.calls:
            if name == "COUNT" and not args:
                aggregate = values.CountStarAggregate()
            else:
                aggregate = values.AGGREGATE_FUNCTIONS[name][0]()
            state.append((aggregate, set() if distinct else None))
        return state

    def step(self, state, row):
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
    def results(state):
        return [aggregate.result() for aggregate, _ in state]


# ---- access paths --------------------------------------------------------------


class FullScan:
    def __init__(self, tree):
        self.tree = tree

    def candidates(self, row):
        return self.tree.scan()

    def describe(self):
        return "SCAN"


class RowidLookup:
    """Rows whose row id equals one of the given expressions (``=`` or ``IN``)."""

    def __init__(self, tree, key_functions):
        self.tree = tree
        self.key_functions = key_functions

    def candidates(self, row):
        keys = set()
        for key_function in self.key_functions:
            key = values.numeric_affinity(key_function(row))
            if isinstance(key, int):
                keys.add(key)
        for key in sorted(keys):
            value = self.tree.get(key)
            if value is not None:
                yield key, value

    def describe(self):
        return "SEARCH USING ROWID (=)"


class RowidRange:
    """Rows whose row id lies between optional lower and upper bounds."""

    def __init__(self, tree, lower, upper):
        self.tree = tree
        self.lower = lower  # (key function, inclusive) or None
        self.upper = upper

    def candidates(self, row):
        start = end = None
        start_inclusive = end_inclusive = True
        if self.lower:
            start = values.numeric_affinity(self.lower[0](row))
            if start is None or isinstance(start, str):
                return iter(())  # rowid > NULL or rowid > 'text' is never true
            start_inclusive = self.lower[1]
        if self.upper:
            end = values.numeric_affinity(self.upper[0](row))
            if end is None:
                return iter(())
            if isinstance(end, str):
                end = None  # every number is below any text
            end_inclusive = self.upper[1]
        return self.tree.scan(start, end, start_inclusive, end_inclusive)

    def describe(self):
        return "SEARCH USING ROWID (range)"


class IndexScan:
    """Rows found through a secondary index: equality on a prefix of its
    columns, optionally followed by a range on the next column."""

    def __init__(self, index, index_tree, table_tree, equal, lower, upper):
        self.index = index
        self.index_tree = index_tree
        self.table_tree = table_tree
        self.equal = equal  # key functions for the leading columns
        self.lower = lower  # (key function, inclusive) or None
        self.upper = upper

    def candidates(self, row):
        sort_key = values.sort_key
        prefix = []
        for key_function in self.equal:
            value = key_function(row)
            if value is None:
                return  # col = NULL is never true
            prefix.append(sort_key(value))
        prefix = tuple(prefix)
        start, start_inclusive = prefix, True
        end, end_inclusive = prefix + (HIGH,), True
        if self.lower or self.upper:
            start, start_inclusive = prefix + ((0, 0), HIGH), False  # skip NULLs
        if self.lower:
            value = self.lower[0](row)
            if value is None:
                return
            if self.lower[1]:
                start, start_inclusive = prefix + (sort_key(value),), True
            else:
                start, start_inclusive = prefix + (sort_key(value), HIGH), False
        if self.upper:
            value = self.upper[0](row)
            if value is None:
                return
            if self.upper[1]:
                end = prefix + (sort_key(value), HIGH)
            else:
                end, end_inclusive = prefix + (sort_key(value),), False
        get = self.table_tree.get
        for key, _ in self.index_tree.scan(start, end, start_inclusive, end_inclusive):
            rowid = key[-1][1]
            yield rowid, get(rowid)

    def describe(self):
        names = self.index.column_names
        parts = [f"{name}=?" for name in names[:len(self.equal)]]
        if self.lower:
            parts.append(f"{names[len(self.equal)]}>{'=' if self.lower[1] else ''}?")
        if self.upper:
            parts.append(f"{names[len(self.equal)]}<{'=' if self.upper[1] else ''}?")
        return f"SEARCH USING INDEX {self.index.name} ({' AND '.join(parts)})"


ROWID = -1  # column position standing for the row id in constraints


class Constraint:
    """A WHERE/ON conjunct of the form ``column op key`` usable by an access path."""

    def __init__(self, position, op, key):
        self.position = position  # column position in the table, or ROWID
        self.op = op  # "=", "<", "<=", ">", ">=" or "IN"
        self.key = key  # key function(s) evaluated on the outer row


def find_constraints(scope, index, conjuncts, compiler):
    """Constraints on table ``index`` whose other side only uses earlier tables.

    Keys are converted with the comparison affinity SQLite would apply to
    them; comparisons that would convert the *column* side are unusable.
    """
    _, table, offset = scope.entries[index]
    rowid_slot = scope.rowid_slot(index)
    earlier = set(range(index))
    constraints = []

    def column_position(expr):
        if not isinstance(expr, Column):
            return None
        slot, _, table_index = scope.resolve(expr)
        if table_index != index:
            return None
        if slot == rowid_slot or slot - offset == table.rowid_column:
            return ROWID
        return slot - offset

    def key_function(position, expr):
        if tables_referenced(expr, scope) - earlier:
            return None
        function, affinity = compiler.compile_with_affinity(expr)
        if position == ROWID:
            return function  # row id lookups apply numeric affinity themselves
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
            if column_position(conjunct.expr) == ROWID:
                keys = [key_function(ROWID, item) for item in conjunct.items]
                if all(keys):
                    constraints.append(Constraint(ROWID, "IN", keys))
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


def _bounds(constraints, position):
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


def plan_access(scope, index, catalog, conjuncts, compiler):
    """Choose how to read table ``index`` of ``scope`` given the usable conjuncts.

    Preference: row id lookup, then the index matching the most leading
    columns by equality (a fully matched UNIQUE index first), then a row id
    range, then an index range, then a full scan.
    """
    table = scope.entries[index][1]
    tree = catalog.table_tree(table)
    constraints = find_constraints(scope, index, conjuncts, compiler)
    for c in constraints:
        if c.position == ROWID and c.op == "=":
            return RowidLookup(tree, [c.key])
        if c.position == ROWID and c.op == "IN":
            return RowidLookup(tree, c.key)
    best, best_score = FullScan(tree), 0
    lower, upper = _bounds(constraints, ROWID)
    if lower or upper:
        best, best_score = RowidRange(tree, lower, upper), 3 + bool(lower and upper)
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
        score = 10 * len(equal) + 2 * bool(lower) + 2 * bool(upper)
        if info.unique and len(equal) == len(info.positions):
            score += 500
        if score > best_score:
            index_tree = catalog.index_tree(info)
            best = IndexScan(info, index_tree, tree, equal, lower, upper)
            best_score = score
    return best


# ---- statements ------------------------------------------------------------------


class Executor:
    def __init__(self, catalog):
        self.catalog = catalog

    def execute(self, stmt):
        if isinstance(stmt, Select):
            return self.select(stmt)
        if isinstance(stmt, Insert):
            return self.insert(stmt)
        if isinstance(stmt, Update):
            return self.update(stmt)
        if isinstance(stmt, Delete):
            return self.delete(stmt)
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
        if isinstance(stmt, Explain):
            return self.explain(stmt.statement)
        raise OperationalError(f"unsupported statement: {type(stmt).__name__}")

    # ---- reading rows ------------------------------------------------------

    @staticmethod
    def load_row(table, rowid, record):
        row = decode_record(record)[0]
        if table.rowid_column is not None:
            row[table.rowid_column] = rowid
        row.append(rowid)
        return row

    def build_scope(self, joins):
        scope = Scope()
        for join in joins:
            scope.add(self.catalog.get_table(join.table.name), join.table.alias)
        return scope

    def plan_joins(self, scope, joins, where):
        """Plan a nested loop over ``joins``; returns a list of ``JoinLevel``.

        WHERE conjuncts and the ON conditions of inner joins form one pool of
        filters; each is checked at the first level where all the tables it
        uses are bound.  A LEFT JOIN's ON condition decides which rows match
        at its own level (and is the only thing its access path may use).
        """
        compiler = Compiler(scope, misuse="misuse of aggregate: {name}()")
        on_compiler = Compiler(scope)
        pool = split_conjuncts(where)
        for join in joins:
            if join.kind != "LEFT":
                pool += split_conjuncts(join.on)
        placed = {}
        constant = []
        for conjunct in pool:
            tables = tables_referenced(conjunct, scope)
            if tables:
                placed.setdefault(max(tables), []).append(conjunct)
            else:
                constant.append(conjunct)
        levels = []
        for index, join in enumerate(joins):
            table = scope.entries[index][1]
            if join.kind == "LEFT":
                usable = split_conjuncts(join.on)
                match = on_compiler.compile(join.on) if join.on is not None else None
            else:
                usable = pool
                match = None
            filters = placed.get(index, [])
            if index == 0:
                filters = constant + filters
            levels.append(JoinLevel(
                table,
                scope.entries[index][2],
                plan_access(scope, index, self.catalog, usable, compiler),
                join.kind == "LEFT",
                match,
                [compiler.compile(f) for f in filters],
            ))
        return levels

    @staticmethod
    def join_rows(scope, levels):
        """Yield every row of the nested loop join.  The same list object is
        yielded each time; callers must copy it to keep it."""
        row = [None] * scope.width
        truth = values.truth
        load_row = Executor.load_row
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
            matched = False
            for rowid, record in level.access.candidates(row):
                row[start:stop] = load_row(level.table, rowid, record)
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

    def matching_rows(self, table, where):
        """Yield (rowid, row copy) for rows of a single table satisfying ``where``."""
        joins = [Join(TableRef(table.name))]
        scope = self.build_scope(joins)
        levels = self.plan_joins(scope, joins, where)
        slot = scope.rowid_slot(0)
        for row in self.join_rows(scope, levels):
            yield row[slot], list(row)

    def explain(self, stmt):
        """One row (table, access path) per table the statement reads, in join order."""
        if isinstance(stmt, Select):
            joins, where = stmt.source, stmt.where
        else:
            joins, where = [Join(TableRef(stmt.table))], stmt.where
        scope = self.build_scope(joins)
        levels = self.plan_joins(scope, joins, where)
        return Result(
            [(level.table.name, level.access.describe()) for level in levels], ["table", "plan"]
        )

    # ---- SELECT -------------------------------------------------------------

    def select(self, stmt):
        scope = self.build_scope(stmt.source)
        exprs, names = self.expand_items(stmt, scope)
        is_aggregate = bool(stmt.group_by) or any(
            contains_aggregate(e)
            for e in exprs + [stmt.having] + [item.expr for item in stmt.order_by]
            if e is not None
        )
        if stmt.having is not None and not is_aggregate:
            raise OperationalError("HAVING clause on a non-aggregate query")
        aggregates = AggregateCollector(scope.width) if is_aggregate else None
        compiler = Compiler(scope, aggregates)
        outputs = [compiler.compile(e) for e in exprs]
        order_terms, order_functions = self.order_terms(stmt, exprs, names, compiler)
        having = compiler.compile(stmt.having) if stmt.having is not None else None
        group_functions = self.group_functions(stmt, exprs, names, scope)
        if stmt.source:
            levels = self.plan_joins(scope, stmt.source, stmt.where)
            rows = self.join_rows(scope, levels)
        else:
            where = Compiler(scope, misuse="misuse of aggregate: {name}()")
            condition = where.compile(stmt.where) if stmt.where is not None else None
            rows = [[]] if condition is None or values.truth(condition([])) else []

        def record(row):
            return (
                tuple(f(row) for f in outputs),
                tuple(f(row) for f in order_functions),
            )

        if not is_aggregate:
            records = [record(row) for row in rows]
        else:
            records = []
            truth = values.truth
            for group_row in self.group_rows(rows, scope, group_functions, aggregates):
                if having is None or truth(having(group_row)):
                    records.append(record(group_row))
        if stmt.distinct:
            records = distinct_records(records)
        records = sort_records(records, order_terms)
        records = self.apply_limit(stmt, records)
        return Result([out for out, _ in records], names)

    def expand_items(self, stmt, scope):
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
    def result_column_reference(expr, names, clause, position, scope):
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
            lowered = [name.lower() for name in names]
            if expr.name.lower() in lowered:
                if clause == "GROUP BY":
                    try:
                        scope.resolve(expr)
                        return None  # an input column wins over an alias in GROUP BY
                    except OperationalError:
                        pass
                return lowered.index(expr.name.lower())
        return None

    def order_terms(self, stmt, exprs, names, compiler):
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

    def group_functions(self, stmt, exprs, names, scope):
        compiler = Compiler(scope, misuse="aggregate functions are not allowed in the GROUP BY clause")
        functions = []
        for position, expr in enumerate(stmt.group_by, 1):
            index = self.result_column_reference(expr, names, "GROUP BY", position, scope)
            if index is not None:
                expr = exprs[index]
            functions.append(compiler.compile(expr))
        return functions

    @staticmethod
    def group_rows(rows, scope, group_functions, aggregates):
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

    @staticmethod
    def apply_limit(stmt, records):
        if stmt.limit is None:
            return records
        compiler = Compiler(Scope())

        def integer(expr):
            value = values.numeric_affinity(compiler.compile(expr)([]))
            if not isinstance(value, int):
                raise IntegrityError("datatype mismatch")
            return value

        limit = integer(stmt.limit)
        offset = max(integer(stmt.offset), 0) if stmt.offset is not None else 0
        end = None if limit < 0 else offset + limit
        return records[offset:end]


    # ---- INSERT --------------------------------------------------------------

    def insert(self, stmt):
        table = self.catalog.get_table(stmt.table)
        width = len(table.columns)
        if stmt.columns is None:
            positions = list(range(width))
        else:
            positions = []
            for name in stmt.columns:
                position = table.column_index(name)
                if position is None:
                    raise OperationalError(f"table {table.name} has no column named {name}")
                positions.append(position)
        compiler = Compiler(Scope())
        rows = []
        for exprs in stmt.rows:
            if len(exprs) != len(positions):
                if stmt.columns is None:
                    raise OperationalError(
                        f"table {table.name} has {width} columns "
                        f"but {len(exprs)} values were supplied"
                    )
                raise OperationalError(f"{len(exprs)} values for {len(positions)} columns")
            rows.append([compiler.compile(e) for e in exprs])
        tree = self.catalog.table_tree(table)
        for functions in rows:
            row = [None] * width
            for position, function in zip(positions, functions):
                row[position] = function([])
            self.insert_row(table, tree, row)
        return Result()

    def prepare_row(self, table, row):
        """Apply column affinities and NOT NULL checks; returns the requested row id."""
        for i, affinity in enumerate(table.affinities):
            row[i] = values.apply_affinity(row[i], affinity)
        for i, column in enumerate(table.columns):
            if column.not_null and row[i] is None:
                raise IntegrityError(f"NOT NULL constraint failed: {table.name}.{column.name}")
        if table.rowid_column is None:
            return None
        rowid = row[table.rowid_column]
        if rowid is not None and not isinstance(rowid, int):
            raise IntegrityError("datatype mismatch")
        return rowid

    def check_unique(self, table, row, rowid):
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

    def add_index_entries(self, table, row, rowid):
        for index in table.indexes:
            try:
                self.catalog.index_tree(index).insert(index.key(row, rowid), b"")
            except BTreeError as exc:
                raise OperationalError(f"index {index.name}: {exc}") from None

    def remove_index_entries(self, table, row, rowid):
        for index in table.indexes:
            self.catalog.index_tree(index).delete(index.key(row, rowid))

    @staticmethod
    def encode(table, row):
        stored = list(row)
        if table.rowid_column is not None:
            stored[table.rowid_column] = None  # kept in the key, not the record
        return encode_record(stored)

    def insert_row(self, table, tree, row):
        rowid = self.prepare_row(table, row)
        if rowid is None:
            rowid = self.new_rowid(tree)
            if table.rowid_column is not None:
                row[table.rowid_column] = rowid
        elif rowid in tree:
            raise self.rowid_conflict(table)
        self.check_unique(table, row, rowid)
        tree.insert(rowid, self.encode(table, row))
        self.add_index_entries(table, row, rowid)
        return rowid

    @staticmethod
    def new_rowid(tree):
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
    def rowid_conflict(table):
        if table.rowid_column is None:
            return IntegrityError(f"UNIQUE constraint failed: {table.name}.rowid")
        name = table.columns[table.rowid_column].name
        return IntegrityError(f"UNIQUE constraint failed: {table.name}.{name}")

    # ---- UPDATE --------------------------------------------------------------

    def update(self, stmt):
        table = self.catalog.get_table(stmt.table)
        width = len(table.columns)
        scope = Scope()
        scope.add(table)
        compiler = Compiler(scope)
        assignments = []
        for name, expr in stmt.assignments:
            position = table.column_index(name)
            if position is None:
                if name.lower() not in ROWID_NAMES:
                    raise OperationalError(f"no such column: {name}")
                position = width if table.rowid_column is None else table.rowid_column
            assignments.append((position, compiler.compile(expr)))
        tree = self.catalog.table_tree(table)
        for rowid, old in list(self.matching_rows(table, stmt.where)):
            new = list(old)
            for position, function in assignments:
                new[position] = function(old)
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
            if new_rowid != rowid and new_rowid in tree:
                raise self.rowid_conflict(table)
            self.check_unique(table, row, rowid)
            self.remove_index_entries(table, old, rowid)
            if new_rowid != rowid:
                tree.delete(rowid)
            tree.insert(new_rowid, self.encode(table, row), replace=True)
            self.add_index_entries(table, row, new_rowid)
        return Result()

    # ---- DELETE --------------------------------------------------------------

    def delete(self, stmt):
        table = self.catalog.get_table(stmt.table)
        tree = self.catalog.table_tree(table)
        if stmt.where is None:
            tree.clear()
            for index in table.indexes:
                self.catalog.index_tree(index).clear()
            return Result()
        for rowid, row in list(self.matching_rows(table, stmt.where)):
            self.remove_index_entries(table, row, rowid)
            tree.delete(rowid)
        return Result()

    # ---- indexes -------------------------------------------------------------

    def create_index(self, stmt):
        index = self.catalog.create_index(stmt)
        if index is None:
            return Result()
        table = index.table
        index_tree = self.catalog.index_tree(index)
        for rowid, record in self.catalog.table_tree(table).scan():
            row = self.load_row(table, rowid, record)
            if index.unique:
                self.check_unique(table, row, rowid)
            try:
                index_tree.insert(index.key(row, rowid), b"")
            except BTreeError as exc:
                raise OperationalError(f"index {index.name}: {exc}") from None
        return Result()


class JoinLevel:
    """One table of a nested loop join."""

    def __init__(self, table, offset, access, outer, match, filters):
        self.table = table
        self.offset = offset  # position of the table's first slot in a row
        self.access = access
        self.outer = outer  # LEFT JOIN: emit a NULL row when nothing matches
        self.match = match  # LEFT JOIN ON condition
        self.filters = filters  # conditions checked once this table is bound


def folded_literal(expr):
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


def constant_integer(expr):
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


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def distinct_records(records):
    """Remove records with duplicate output rows, keeping the first;
    1 and 1.0 count as equal."""
    seen = set()
    result = []
    for record in records:
        key = tuple(values.sort_key(v) for v in record[0])
        if key not in seen:
            seen.add(key)
            result.append(record)
    return result


def sort_records(records, terms):
    """Sort (output, keys) records by ORDER BY terms using stable sorts from the
    last term to the first."""
    sort_key = values.sort_key
    for source, index, descending, nulls_first in reversed(terms):
        column = 0 if source == "output" else 1
        # Where NULLs go in the ascending order that is (maybe) reversed afterwards.
        null_rank = 0 if nulls_first != descending else 2

        def key(record, column=column, index=index, null_rank=null_rank):
            value = record[column][index]
            return (null_rank, 0) if value is None else (1, sort_key(value))

        records.sort(key=key, reverse=descending)
    return records
