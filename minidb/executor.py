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

from operator import itemgetter

from minidb import values
from minidb.btree import DuplicateKeyError
from minidb.errors import IntegrityError, OperationalError
from minidb.parser import (
    Between, Binary, Call, Column, CreateTable, Delete, DropTable, Explain, InList, Insert,
    Like, Literal, Select, Star, Unary, Update,
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
        """(slot, column name) pairs that ``*`` or ``table.*`` expands to."""
        if not self.entries:
            raise OperationalError("no tables specified")
        result = []
        found = False
        for name, table, offset in self.entries:
            if table_name is not None and table_name.lower() != name:
                continue
            found = True
            result.extend((offset + i, c.name) for i, c in enumerate(table.columns))
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
    """Compiles expression trees into closures ``fn(row) -> value``."""

    def __init__(self, scope):
        self.scope = scope

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


def plan_access(scope, index, tree, conjuncts, compiler):
    """Choose how to read table ``index`` of ``scope`` given the WHERE conjuncts.

    Only conjuncts comparing the table's row id with an expression over the
    *earlier* tables (or constants) are usable.
    """
    _, table, offset = scope.entries[index]
    rowid_slots = {scope.rowid_slot(index)}
    if table.rowid_column is not None:
        rowid_slots.add(offset + table.rowid_column)
    earlier = set(range(index))
    equal, lower, upper = [], None, None

    def is_rowid(expr):
        return isinstance(expr, Column) and scope.resolve(expr)[0] in rowid_slots

    def usable(expr):
        return tables_referenced(expr, scope) <= earlier

    for conjunct in conjuncts:
        if isinstance(conjunct, InList) and not conjunct.negated and is_rowid(conjunct.expr):
            if all(usable(item) for item in conjunct.items) and not equal:
                equal = [compiler.compile(item) for item in conjunct.items]
            continue
        if not isinstance(conjunct, Binary) or conjunct.op not in _FLIPPED or conjunct.op == "!=":
            continue
        op, left, right = conjunct.op, conjunct.left, conjunct.right
        if is_rowid(right) and not is_rowid(left):
            op, left, right = _FLIPPED[op], right, left
        if not is_rowid(left) or not usable(right):
            continue
        key = compiler.compile(right)
        if op == "=":
            if not equal:
                equal = [key]
        elif op in (">", ">=") and lower is None:
            lower = (key, op == ">=")
        elif op in ("<", "<=") and upper is None:
            upper = (key, op == "<=")
    if equal:
        return RowidLookup(tree, equal)
    if lower or upper:
        return RowidRange(tree, lower, upper)
    return FullScan(tree)


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

    def single_table_access(self, table, where):
        scope = Scope()
        scope.add(table)
        compiler = Compiler(scope)
        tree = self.catalog.table_tree(table)
        return scope, compiler, plan_access(scope, 0, tree, split_conjuncts(where), compiler)

    def matching_rows(self, table, where):
        """Yield (rowid, row) for rows of a single table satisfying ``where``."""
        _, compiler, access = self.single_table_access(table, where)
        condition = compiler.compile(where) if where is not None else None
        truth = values.truth
        for rowid, record in access.candidates(None):
            row = self.load_row(table, rowid, record)
            if condition is None or truth(condition(row)):
                yield rowid, row

    def explain(self, stmt):
        """One row (table, access path) per table the statement reads."""
        if isinstance(stmt, Select):
            if stmt.source is None:
                return Result([], ["table", "plan"])
            table = self.catalog.get_table(stmt.source.name)
        else:
            table = self.catalog.get_table(stmt.table)
        _, _, access = self.single_table_access(table, stmt.where)
        return Result([(table.name, access.describe())], ["table", "plan"])

    # ---- SELECT -------------------------------------------------------------

    def select(self, stmt):
        scope = Scope()
        table = None
        if stmt.source is not None:
            table = self.catalog.get_table(stmt.source.name)
            scope.add(table, stmt.source.alias)
        compiler = Compiler(scope)
        functions, names = [], []
        for item in stmt.items:
            if isinstance(item.expr, Star):
                for slot, name in scope.star_columns(item.expr.table):
                    functions.append(itemgetter(slot))
                    names.append(name)
                continue
            functions.append(compiler.compile(item.expr))
            if item.alias:
                names.append(item.alias)
            elif isinstance(item.expr, Column):
                names.append(item.expr.name)
            else:
                names.append(item.text)
        if table is None:
            rows = [[]]
            if stmt.where is not None and not values.truth(compiler.compile(stmt.where)([])):
                rows = []
        else:
            rows = (row for _, row in self.matching_rows(table, stmt.where))
        result = [tuple(f(row) for f in functions) for row in rows]
        if stmt.distinct:
            result = distinct_rows(result)
        return Result(result, names)

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

    def check_unique(self, table, tree, row, rowid):
        for i in table.unique_columns():
            value = row[i]
            if value is None:
                continue
            for other_rowid, record in tree.scan():
                if other_rowid == rowid:
                    continue
                other = self.load_row(table, other_rowid, record)[i]
                if other is not None and values.compare(value, other) == 0:
                    raise IntegrityError(
                        f"UNIQUE constraint failed: {table.name}.{table.columns[i].name}"
                    )

    @staticmethod
    def encode(table, row):
        stored = list(row)
        if table.rowid_column is not None:
            stored[table.rowid_column] = None  # kept in the key, not the record
        return encode_record(stored)

    def insert_row(self, table, tree, row):
        rowid = self.prepare_row(table, row)
        if rowid is None:
            last = tree.last_key()
            rowid = 1 if last is None else last + 1
            if rowid > values.INT_MAX:
                raise OperationalError("database or disk is full")
            if table.rowid_column is not None:
                row[table.rowid_column] = rowid
        self.check_unique(table, tree, row, rowid)
        try:
            tree.insert(rowid, self.encode(table, row))
        except DuplicateKeyError:
            raise self.rowid_conflict(table) from None
        return rowid

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
            self.check_unique(table, tree, row, rowid)
            if new_rowid != rowid:
                if new_rowid in tree:
                    raise self.rowid_conflict(table)
                tree.delete(rowid)
            tree.insert(new_rowid, self.encode(table, row), replace=True)
        return Result()

    # ---- DELETE --------------------------------------------------------------

    def delete(self, stmt):
        table = self.catalog.get_table(stmt.table)
        tree = self.catalog.table_tree(table)
        if stmt.where is None:
            tree.clear()
            return Result()
        for rowid in [rowid for rowid, _ in self.matching_rows(table, stmt.where)]:
            tree.delete(rowid)
        return Result()


def distinct_rows(rows):
    """Remove duplicate rows, keeping the first; 1 and 1.0 count as equal."""
    seen = set()
    result = []
    for row in rows:
        key = tuple(values.sort_key(v) for v in row)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result
