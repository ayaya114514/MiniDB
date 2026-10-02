"""Window functions, ported from SQLite's window.c.

SQLite sorts the rows of a query by each window's PARTITION BY and ORDER BY
terms and then streams them through three cursors on a buffer of the
current partition: ``end`` (the next row whose values enter the frame:
xStep), ``start`` (the next row to leave it: xInverse) and ``current`` (the
next row to return).  Which of the three moves when depends on the frame
(sqlite3WindowCodeStep).  WindowGroup.run follows that code step by step,
so that aggregates see exactly the same sequence of xStep and xInverse
calls: a sliding SUM() of REALs, for example, then has the same rounding.

The built-in window functions are aggregates with fixed frames
(sqlite3WindowUpdate), except for first_value(), nth_value(), lead() and
lag(), which read rows of the buffer directly.  An EXCLUDE clause makes
SQLite recompute each frame from scratch (windowFullScan).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from minidb import jsonfuncs
from minidb.errors import OperationalError
from minidb.values import (
    INT_MAX, INT_MIN, SQLValue, SumAccumulator, add, collation_compare, collation_sort_key, compare,
    numeric_affinity, numeric_type_value, sort_key,
    subtract, to_int64, to_number, to_text, truth,
)

# name -> (minimum, maximum) number of arguments of the built-in window functions
WINDOW_FUNCTIONS = {
    "ROW_NUMBER": (0, 0), "RANK": (0, 0), "DENSE_RANK": (0, 0), "PERCENT_RANK": (0, 0),
    "CUME_DIST": (0, 0), "NTILE": (1, 1), "LAST_VALUE": (1, 1), "NTH_VALUE": (2, 2),
    "FIRST_VALUE": (1, 1), "LEAD": (1, 3), "LAG": (1, 3),
}

# The frames SQLite gives some built-in window functions, whatever the OVER
# clause says: (unit, start, start offset, end).
BUILTIN_FRAMES = {
    "ROW_NUMBER": ("ROWS", "UNBOUNDED", None, "CURRENT"),
    "DENSE_RANK": ("RANGE", "UNBOUNDED", None, "CURRENT"),
    "RANK": ("RANGE", "UNBOUNDED", None, "CURRENT"),
    "PERCENT_RANK": ("GROUPS", "CURRENT", None, "UNBOUNDED"),
    "CUME_DIST": ("GROUPS", "FOLLOWING", 1, "UNBOUNDED"),
    "NTILE": ("ROWS", "CURRENT", None, "UNBOUNDED"),
    "LEAD": ("ROWS", "UNBOUNDED", None, "UNBOUNDED"),
    "LAG": ("ROWS", "UNBOUNDED", None, "CURRENT"),
}

# Functions that read rows of the partition buffer instead of aggregating.
DIRECT_FUNCTIONS = ("FIRST_VALUE", "NTH_VALUE", "LEAD", "LAG")

NTH_VALUE_ERROR = "second argument to nth_value must be a positive integer"


# ---- accumulators ----------------------------------------------------------------
#
# Each has step(args), inverse(args), value() (xValue: the result so far)
# and finalize() (xFinalize).  ``args`` is the tuple of argument values.


class WindowSum(SumAccumulator):
    """sum(), total() and avg() with SQLite's sumInverse()."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def step(self, args: tuple) -> None:
        super().step(args[0])

    def inverse(self, args: tuple) -> None:
        value = numeric_type_value(args[0])
        if value is None:
            return
        self.count -= 1
        if not self.approx:
            difference = self.int_sum - to_int64(value)  # (only integers were added so far)
            if INT_MIN <= difference <= INT_MAX:
                self.int_sum = difference
                return
            self.overflow = True
            self.approx = True
            self._kbn_init(self.int_sum)
        if isinstance(value, int):
            if value != INT_MIN:
                self._kbn_step_int(-value)
            else:
                self._kbn_step_int(INT_MAX)
                self._kbn_step_int(1)
        else:
            self._kbn_step(-float(to_number(value)))

    def value(self) -> SQLValue:
        if self.name == "TOTAL":
            total = self._real_total()
            return None if total != total else total
        if self.count <= 0:
            return None
        if self.name == "AVG":
            average = self._real_total() / self.count
            return None if average != average else average
        if not self.approx:
            return self.int_sum
        if self.overflow:
            raise OperationalError("integer overflow")
        total = self._real_total()
        return None if total != total else total

    finalize = value


class WindowCount:
    def __init__(self) -> None:
        self.count = 0

    def step(self, args: tuple) -> None:
        if not args or args[0] is not None:
            self.count += 1

    def inverse(self, args: tuple) -> None:
        if not args or args[0] is not None:
            self.count -= 1

    def value(self) -> SQLValue:
        return self.count

    finalize = value


class WindowMinMax:
    """min() / max() while the frame only grows (SQLite's minmaxStep)."""

    def __init__(self, want: int, compare: Callable = compare) -> None:
        self.want = want  # 1 for MAX, -1 for MIN
        self.compare = compare  # (under the argument's collation)
        self.best = None

    def step(self, args: tuple) -> None:
        value = args[0]
        if value is not None and (self.best is None or self.compare(value, self.best) == self.want):
            self.best = value

    def inverse(self, args: tuple) -> None:  # pragma: no cover - SQLite has no xInverse
        raise AssertionError("min()/max() have no inverse")

    def value(self) -> SQLValue:
        return self.best

    finalize = value


class WindowMinMaxIndex:
    """min() / max() over a sliding frame: SQLite keeps the values in an
    index ordered by (value, sequence number), MIN descending, and returns
    its last entry; xInverse deletes the first entry equal to the value."""

    def __init__(self, want: int, compare: Callable = compare) -> None:
        self.want = want
        self.compare = compare
        self.entries = []  # (value, sequence number), unordered
        self.sequence = 0

    def step(self, args: tuple) -> None:
        if args[0] is not None:
            self.sequence += 1
            self.entries.append((args[0], self.sequence))

    def inverse(self, args: tuple) -> None:
        value = args[0]
        if value is None:
            return
        equal = [entry for entry in self.entries if self.compare(entry[0], value) == 0]
        if equal:
            self.entries.remove(min(equal, key=lambda entry: entry[1]))

    def value(self) -> SQLValue:
        best = None
        for entry in self.entries:
            if best is None:
                best = entry
                continue
            order = self.compare(entry[0], best[0])
            if order == self.want or (order == 0 and entry[1] > best[1]):
                best = entry
        return None if best is None else best[0]

    finalize = value


def _utf8(value: SQLValue) -> bytes:
    return to_text(value).encode("utf-8", "surrogateescape")


class WindowGroupConcat:
    """group_concat() / string_agg() as SQLite's GroupConcatCtx: the text is
    kept as bytes and xInverse cuts the first value and the separator after
    it off the front (with SQLite's quirks, such as xValue returning a NUL
    character when values remain but the text is empty)."""

    def __init__(self) -> None:
        self.text = b""
        self.active = False  # str.mxAlloc != 0: the next value is not the first term
        self.accumulated = 0
        self.first_separator_length = 0
        self.separator_lengths = None
        self.stepped = False  # the aggregate context exists

    def step(self, args: tuple) -> None:
        value = args[0]
        if value is None:
            return
        self.stepped = True
        first = not self.active
        self.active = True
        if len(args) == 1:
            if not first:
                self.text += b","
            else:
                self.first_separator_length = 1
        elif not first:
            separator = args[1]
            length = 0
            if separator is not None:
                data = _utf8(separator)
                self.text += data
                length = len(data)
            if length != self.first_separator_length or self.separator_lengths is not None:
                if self.separator_lengths is None:
                    self.separator_lengths = [self.first_separator_length] * (self.accumulated - 1)
                del self.separator_lengths[self.accumulated - 1:]
                self.separator_lengths.append(length)
        else:
            self.first_separator_length = 0 if args[1] is None else len(_utf8(args[1]))
        self.accumulated += 1
        self.text += _utf8(value)

    def inverse(self, args: tuple) -> None:
        value = args[0]
        if value is None:
            return
        remove = len(_utf8(value))
        self.accumulated -= 1
        if self.separator_lengths is not None:
            if self.accumulated > 0:
                remove += self.separator_lengths.pop(0)
        else:
            remove += self.first_separator_length
        self.text = b"" if remove >= len(self.text) else self.text[remove:]
        if not self.text:
            self.active = False
            self.separator_lengths = None

    def value(self) -> SQLValue:
        if not self.stepped:
            return None
        if self.accumulated > 0 and not self.text:
            return "\x00"  # sqlite3_result_text(context, "", 1, ...)
        return self.text.decode("utf-8", "surrogateescape") if self.text else None

    def finalize(self) -> SQLValue:
        if not self.stepped:
            return None
        return self.text.decode("utf-8", "surrogateescape")


class RowNumber:
    def __init__(self) -> None:
        self.count = 0

    def step(self, args: tuple) -> None:
        self.count += 1

    def inverse(self, args: tuple) -> None:
        pass

    def value(self) -> SQLValue:
        return self.count

    finalize = value


class DenseRank:
    def __init__(self) -> None:
        self.stepped = 0
        self.rank = 0

    def step(self, args: tuple) -> None:
        self.stepped = 1

    def inverse(self, args: tuple) -> None:
        pass

    def value(self) -> SQLValue:
        if self.stepped:
            self.rank += 1
            self.stepped = 0
        return self.rank

    finalize = value


class Rank:
    def __init__(self) -> None:
        self.steps = 0
        self.rank = 0

    def step(self, args: tuple) -> None:
        self.steps += 1
        if self.rank == 0:
            self.rank = self.steps

    def inverse(self, args: tuple) -> None:
        pass

    def value(self) -> SQLValue:
        rank, self.rank = self.rank, 0
        return rank

    finalize = value


class PercentRank:
    def __init__(self) -> None:
        self.total = 0
        self.steps = 0

    def step(self, args: tuple) -> None:
        self.total += 1

    def inverse(self, args: tuple) -> None:
        self.steps += 1

    def value(self) -> SQLValue:
        return self.steps / (self.total - 1) if self.total > 1 else 0.0

    finalize = value


class CumeDist:
    def __init__(self) -> None:
        self.total = 0
        self.steps = 0
        self.stepped = False

    def step(self, args: tuple) -> None:
        self.stepped = True
        self.total += 1

    def inverse(self, args: tuple) -> None:
        self.steps += 1

    def value(self) -> SQLValue:
        return self.steps / self.total if self.stepped else None

    finalize = value


class Ntile:
    def __init__(self) -> None:
        self.total = 0
        self.buckets = 0
        self.row = 0

    def step(self, args: tuple) -> None:
        if self.total == 0:
            self.buckets = to_int64(args[0]) if args[0] is not None else 0
            if self.buckets <= 0:
                raise OperationalError("argument of ntile must be a positive integer")
        self.total += 1

    def inverse(self, args: tuple) -> None:
        self.row += 1

    def value(self) -> SQLValue:
        if self.buckets <= 0:
            return None
        size = self.total // self.buckets
        if size == 0:
            return self.row + 1
        large = self.total - self.buckets * size
        small = large * (size + 1)
        if self.row < small:
            return 1 + self.row // (size + 1)
        return 1 + large + (self.row - small) // size

    finalize = value


class LastValue:
    def __init__(self) -> None:
        self.last = None
        self.count = 0

    def step(self, args: tuple) -> None:
        self.last = args[0]
        self.count += 1

    def inverse(self, args: tuple) -> None:
        self.count -= 1
        if self.count == 0:
            self.last = None

    def value(self) -> SQLValue:
        return self.last

    finalize = value


def _positive_integer(value: SQLValue, message: str) -> int:
    """The value of an nth_value() argument or a ROWS/GROUPS offset, which
    must be an integer (OP_MustBeInt)."""
    number = numeric_type_value(value)
    if isinstance(number, float) and number.is_integer() and INT_MIN <= number <= INT_MAX:
        number = int(number)
    if not isinstance(number, int):
        raise OperationalError(message)
    return number


class NthValue:
    """nth_value() / first_value() when every frame is scanned (EXCLUDE)."""

    def __init__(self, first: bool) -> None:
        self.first = first
        self.steps = 0
        self.found = False
        self.result = None

    def step(self, args: tuple) -> None:
        if self.first:
            if not self.found:
                self.found, self.result = True, args[0]
            return
        wanted = numeric_type_value(args[1])
        if isinstance(wanted, float):
            if not (wanted.is_integer() and INT_MIN <= wanted <= INT_MAX):
                raise OperationalError(NTH_VALUE_ERROR)
            wanted = int(wanted)
        if not isinstance(wanted, int) or wanted <= 0:
            raise OperationalError(NTH_VALUE_ERROR)
        self.steps += 1
        if wanted == self.steps:
            self.found, self.result = True, args[0]

    def inverse(self, args: tuple) -> None:
        pass

    def value(self) -> SQLValue:
        return None

    def finalize(self) -> SQLValue:
        return self.result if self.found else None


# ---- the window functions of one window ---------------------------------------


class WindowFunction:
    """One window function call: its name, argument and FILTER functions and
    its number among the query's window functions (its result goes to that
    slot after the row's other values)."""

    def __init__(self, name: str, args: list[Callable], filter_: Callable | None, number: int,
                 collation: str | None = None) -> None:
        self.name = name
        self.args = args
        self.filter = filter_
        self.number = number
        self.compare = collation_compare(collation)  # (min() and max() compare by the argument's collation)

    def accumulator(self, sliding: bool, full_scan: bool) -> Any:
        name = self.name
        if name in ("SUM", "TOTAL", "AVG"):
            return WindowSum(name)
        if name == "COUNT":
            return WindowCount()
        if name in ("MIN", "MAX"):
            want = 1 if name == "MAX" else -1
            return (WindowMinMaxIndex(want, self.compare) if sliding and not full_scan
                    else WindowMinMax(want, self.compare))
        if name in ("GROUP_CONCAT", "STRING_AGG"):
            return WindowGroupConcat()
        if name in jsonfuncs.AGGREGATES:
            return jsonfuncs.WindowJsonGroup(jsonfuncs.AGGREGATES[name][0])
        if name == "ROW_NUMBER":
            return RowNumber()
        if name == "DENSE_RANK":
            return DenseRank()
        if name == "RANK":
            return Rank()
        if name == "PERCENT_RANK":
            return PercentRank()
        if name == "CUME_DIST":
            return CumeDist()
        if name == "NTILE":
            return Ntile()
        if name == "LAST_VALUE":
            return LastValue()
        if name in ("NTH_VALUE", "FIRST_VALUE"):
            return NthValue(name == "FIRST_VALUE")
        return None  # lead() and lag() read the buffer


class _Descending:
    __slots__ = ("key",)

    def __init__(self, key: tuple) -> None:
        self.key = key

    def __lt__(self, other: _Descending) -> bool:
        return other.key < self.key

    def __eq__(self, other: object) -> bool:
        return self.key == other.key


def _same(a: Sequence[SQLValue], b: Sequence[SQLValue], compares: Sequence[Callable]) -> bool:
    """OP_Compare equality: NULLs are equal to each other; each term
    compares by its own collation (``compares``)."""
    for x, y, compare in zip(a, b, compares):
        if x is None or y is None:
            if x is not y:
                return False
        elif compare(x, y) != 0:
            return False
    return True


RETURN, INVERSE, STEP = "return", "inverse", "step"


class WindowGroup:
    """Window functions that share one window definition (PARTITION BY,
    ORDER BY and frame), computed in one pass over the sorted rows."""

    def __init__(self, partition: list[Callable], order: list[tuple[Callable, bool, bool | None]],
                 unit: str, start: str, start_offset: Callable | None, end: str,
                 end_offset: Callable | None, exclude: str | None,
                 partition_collations: list[str | None] | None = None,
                 order_collations: list[str | None] | None = None) -> None:
        self.partition = partition
        self.order = order
        partition_collations = partition_collations or [None] * len(partition)
        order_collations = order_collations or [None] * len(order)
        self.partition_keys = [collation_sort_key(c) for c in partition_collations]
        self.order_keys = [collation_sort_key(c) for c in order_collations]
        self.partition_compares = [collation_compare(c) for c in partition_collations]
        self.peer_compares = [collation_compare(c) for c in order_collations]
        self.unit = unit
        self.start = start
        self.start_offset = start_offset
        self.end = end
        self.end_offset = end_offset
        self.exclude = exclude
        self.functions = []

    # ---- sorting ----

    def sort_key(self, keys: tuple) -> list:
        """The key of a row: its PARTITION BY values ascending, then its ORDER BY
        values (NULLs first unless NULLS LAST, reversed for DESC)."""
        part, peer = keys
        result = [(0,) if v is None else (1, key(v)) for v, key in zip(part, self.partition_keys)]
        for value, (_, descending, nulls_first), key in zip(peer, self.order, self.order_keys):
            if nulls_first is None:
                nulls_first = not descending
            if value is None:
                result.append((0,) if nulls_first else (2,))
            elif descending:
                result.append((1, _Descending(key(value))))
            else:
                result.append((1, key(value)))
        return result

    def run(self, rows: list[list], base: int) -> list[list]:
        """Set the window functions' results in ``rows`` (at ``base`` + their
        numbers); returns the rows in the order SQLite produces them (sorted
        by the window)."""
        self.base = base
        if not rows:
            return rows
        keys = [(tuple(f(row) for f in self.partition), tuple(f(row) for f, _, _ in self.order))
                for row in rows]
        order = sorted(range(len(rows)), key=lambda i: self.sort_key(keys[i]))
        ordered = [rows[i] for i in order]
        part_keys = [keys[i][0] for i in order]
        peers = [keys[i][1] for i in order]
        args = [[tuple(f(row) for f in function.args) for row in ordered] for function in self.functions]
        filters = [
            None if function.filter is None else [bool(truth(function.filter(row))) for row in ordered]
            for function in self.functions
        ]
        begin = 0
        for i in range(1, len(ordered) + 1):
            if i == len(ordered) or not _same(part_keys[i], part_keys[begin], self.partition_compares):
                _Partition(self, ordered[begin:i], peers[begin:i],
                           [a[begin:i] for a in args],
                           [None if f is None else f[begin:i] for f in filters]).run()
                begin = i
        return ordered


class _Partition:
    """sqlite3WindowCodeStep for the rows of one partition."""

    def __init__(self, group: WindowGroup, rows: list[list], peers: list[tuple],
                 args: list[list[tuple]], filters: list[list[bool] | None]) -> None:
        self.group = group
        self.rows = rows
        self.peers = peers
        self.args = args
        self.filters = filters
        self.count = 0  # rows in the buffer so far
        self.full_scan = group.exclude is not None
        self.flushing = False

    # ---- helpers mirroring window.c ----

    def peer(self, index: int) -> tuple:
        if index >= self.count:
            return (None,) * len(self.group.order)  # a cursor at EOF reads NULLs
        return self.peers[index]

    def init_accumulators(self) -> None:
        group = self.group
        sliding = group.start != "UNBOUNDED"
        self.accumulators = [f.accumulator(sliding, self.full_scan) for f in group.functions]
        # first/nth_value(): for each function, the rows inversed and stepped
        self.frame_start = [0] * len(group.functions)
        self.frame_end = [0] * len(group.functions)
        self.start_rowid, self.end_rowid = 1, 0  # EXCLUDE: the frame's rows

    def offset(self, function: Callable, index: int, starting: bool) -> SQLValue:
        """Evaluate a frame offset and check it as windowCheckValue does."""
        value = function(self.rows[index])
        if self.group.unit == "RANGE":
            which = "starting" if starting else "ending"
            number = numeric_affinity(value)
            if number is None or isinstance(number, (str, bytes)) or number < 0:
                raise OperationalError(f"frame {which} offset must be a non-negative number")
            return number
        which = "starting" if starting else "ending"
        number = _positive_integer(value, f"frame {which} offset must be a non-negative integer")
        if number < 0:
            raise OperationalError(f"frame {which} offset must be a non-negative integer")
        return number

    def aggregate(self, index: int, inverse: bool) -> None:
        """windowAggStep for row ``index`` (xStep, or xInverse)."""
        group = self.group
        for k, function in enumerate(group.functions):
            filters = self.filters[k]
            if filters is not None and not filters[index]:
                continue
            if function.name in ("FIRST_VALUE", "NTH_VALUE") and not self.full_scan:
                if inverse:
                    self.frame_start[k] += 1
                else:
                    self.frame_end[k] += 1
                continue
            if function.name in ("LEAD", "LAG"):
                continue
            accumulator = self.accumulators[k]
            if inverse:
                accumulator.inverse(self.args[k][index])
            else:
                accumulator.step(self.args[k][index])

    def final_values(self) -> list[SQLValue]:
        """windowAggFinal(p, 0): each function's xValue."""
        return [None if f.name in DIRECT_FUNCTIONS else a.value()
                for f, a in zip(self.group.functions, self.accumulators)]

    def return_row(self, index: int, results: list[SQLValue] | None) -> None:
        """windowReturnOneRow: store the results for row ``index``."""
        group = self.group
        row = self.rows[index]
        if self.full_scan:
            results = self.full_scan_values(index)
        for k, function in enumerate(group.functions):
            value = results[k]
            name = function.name
            if name in ("FIRST_VALUE", "NTH_VALUE") and not self.full_scan:
                wanted = 1
                if name == "NTH_VALUE":
                    wanted = _positive_integer(self.args[k][index][1], NTH_VALUE_ERROR)
                    if wanted <= 0:
                        raise OperationalError(NTH_VALUE_ERROR)
                target = self.frame_start[k] + wanted
                value = None
                if target <= self.frame_end[k]:
                    value = self.args[k][target - 1][0]
            elif name in ("LEAD", "LAG"):
                # The row whose row id in the buffer is this one's plus (lead)
                # or minus (lag) the offset, if it is there yet.
                arguments = self.args[k][index]
                value = arguments[2] if len(arguments) > 2 else None
                if len(arguments) < 2:
                    target = index + 1 + (1 if name == "LEAD" else -1)
                else:
                    target = (add if name == "LEAD" else subtract)(index + 1, arguments[1])
                    if isinstance(target, float):  # OP_SeekRowid: only an integral REAL will do
                        target = int(target) if target.is_integer() and INT_MIN <= target <= INT_MAX else None
                if isinstance(target, int) and 1 <= target <= self.count:
                    value = self.args[k][target - 1][0]
            row[group.base + function.number] = value

    def full_scan_values(self, index: int) -> list[SQLValue]:
        """windowFullScan: aggregate the frame's rows afresh, leaving out the
        excluded ones, and finalize."""
        group = self.group
        accumulators = [f.accumulator(False, True) for f in group.functions]
        exclude = group.exclude
        current = self.peers[index]
        for i in range(self.start_rowid - 1, min(self.end_rowid, self.count)):
            if exclude == "CURRENT ROW" and i == index:
                continue
            if exclude in ("GROUP", "TIES") and not (exclude == "TIES" and i == index):
                if not group.order or _same(self.peers[i], current, group.peer_compares):
                    continue
            for k, function in enumerate(group.functions):
                filters = self.filters[k]
                if filters is not None and not filters[i]:
                    continue
                arguments = self.args[k][i]
                if function.name == "NTH_VALUE":
                    arguments = (arguments[0], self.args[k][index][1])
                accumulators[k].step(arguments)
        return [a.finalize() for a in accumulators]

    def range_test(self, op: str, first: int, amount: SQLValue, second: int) -> bool:
        """windowCodeRangeTest: whether peer(first) +/- amount <op> peer(second)."""
        _, descending, nulls_first = self.group.order[0]
        big_null = nulls_first is not None and nulls_first == descending
        a, b = self.peer(first)[0], self.peer(second)[0]
        arithmetic = add
        if descending:
            op = {">=": "<=", ">": "<", "<=": ">="}[op]
            arithmetic = subtract
        if big_null:
            if a is None:
                if op == ">=":
                    return True
                if op == ">":
                    return b is not None
                if op == "<=":
                    return b is None
                return False
            if b is None:
                return op in ("<=", "<")
        if not isinstance(a, (str, bytes)):
            if (op == ">=" and arithmetic is add) or (op == "<=" and arithmetic is subtract):
                if a is not None and b is not None and _holds(op, compare(a, b)):
                    return True
            a = arithmetic(a, amount)
        if a is None:
            order = 0 if b is None else -1
        elif b is None:
            order = 1
        else:
            order = compare(a, b)
        return _holds(op, order)

    # ---- windowCodeOp ----

    def code_op(self, op: str, countdown: str | None = None, jump_on_eof: bool = False) -> bool:
        """Perform one RETURN_ROW, AGGINVERSE or AGGSTEP operation; returns
        True when ``jump_on_eof`` and the cursor ran past the last row."""
        group = self.group
        peers = group.unit != "ROWS"
        if op == INVERSE and group.start == "UNBOUNDED":
            return False
        while True:  # addrNextRange
            if countdown is not None:
                amount = getattr(self, countdown)
                if group.unit == "RANGE":
                    if op == INVERSE:
                        if group.start == "FOLLOWING":
                            done = self.range_test("<=", self.current, amount, self.start_cursor)
                        else:
                            done = self.range_test(">=", self.start_cursor, amount, self.current)
                    else:
                        done = self.range_test(">", self.end_cursor, amount, self.current)
                    if done:
                        return False
                elif amount > 0:
                    setattr(self, countdown, amount - 1)
                    return False
            results = None
            if op == RETURN and not self.full_scan:
                results = self.final_values()
            while True:  # addrContinue
                if group.start == group.end and countdown is not None and group.unit == "RANGE":
                    if op == INVERSE:
                        start_rowid = self.start_cursor + 1 if self.start_cursor < self.count else None
                        end_rowid = self.end_cursor + 1 if self.end_cursor < self.count else None
                        if start_rowid is not None and end_rowid is not None and start_rowid >= end_rowid:
                            return False
                    elif not self.flushing:
                        end_rowid = self.end_cursor + 1 if self.end_cursor < self.count else None
                        if end_rowid is not None and end_rowid >= self.count:
                            return False
                if op == RETURN:
                    cursor = "current"
                    self.return_row(self.current, results)
                elif op == INVERSE:
                    cursor = "start_cursor"
                    if self.full_scan:
                        self.start_rowid += 1
                    else:
                        self.aggregate(self.start_cursor, True)
                else:
                    cursor = "end_cursor"
                    if self.full_scan:
                        self.end_rowid += 1
                    else:
                        self.aggregate(self.end_cursor, False)
                position = getattr(self, cursor) + 1
                setattr(self, cursor, position)
                if position >= self.count:
                    return jump_on_eof
                if peers:
                    saved = cursor + "_peer"
                    if _same(self.peer(position), getattr(self, saved), self.group.peer_compares):
                        continue
                    setattr(self, saved, self.peer(position))
                break
            if group.unit == "RANGE" and countdown is not None:
                continue
            return False

    # ---- sqlite3WindowCodeStep ----

    def run(self) -> None:
        group = self.group
        unit, start, end = group.unit, group.start, group.end
        order = group.order
        index = 0
        while index < len(self.rows):
            self.count += 1
            if self.count == 1:
                self.init_accumulators()
                self.reg_start = self.reg_end = None
                if start in ("PRECEDING", "FOLLOWING"):
                    self.reg_start = self.offset(group.start_offset, index, True)
                if end in ("PRECEDING", "FOLLOWING"):
                    self.reg_end = self.offset(group.end_offset, index, False)
                if unit != "RANGE" and start == end and self.reg_start is not None:
                    normal = (self.reg_end >= self.reg_start) if start == "FOLLOWING" else (
                        self.reg_end <= self.reg_start)
                    if not normal:
                        # An empty frame: return the row alone, start afresh.
                        self.current = 0
                        self.return_row(0, None if self.full_scan else self.final_values())
                        self.rows, self.peers = self.rows[1:], self.peers[1:]
                        self.args = [a[1:] for a in self.args]
                        self.filters = [None if f is None else f[1:] for f in self.filters]
                        self.count = 0
                        continue
                if start == "FOLLOWING" and unit != "RANGE" and self.reg_end is not None:
                    self.reg_start = self.reg_end - self.reg_start
                self.start_cursor = self.current = self.end_cursor = 0
                self.reg_peer = self.start_cursor_peer = self.current_peer = self.end_cursor_peer = \
                    self.peers[index]
                index += 1
                continue
            if unit != "ROWS":
                if not order or _same(self.peers[index], self.reg_peer, self.group.peer_compares):
                    index += 1
                    continue
                self.reg_peer = self.peers[index]
            if start == "FOLLOWING":
                self.code_op(STEP)
                if end != "UNBOUNDED":
                    if unit == "RANGE":
                        while not self.range_test(">=", self.current, self.reg_end, self.end_cursor):
                            self.code_op(INVERSE, "reg_start")
                            self.code_op(RETURN)
                    else:
                        self.code_op(RETURN, "reg_end")
                        self.code_op(INVERSE, "reg_start")
            elif end == "PRECEDING":
                range_preceding = start == "PRECEDING" and unit == "RANGE"
                self.code_op(STEP, "reg_end")
                if range_preceding:
                    self.code_op(INVERSE, "reg_start")
                self.code_op(RETURN)
                if not range_preceding:
                    self.code_op(INVERSE, "reg_start")
            else:
                self.code_op(STEP)
                if end != "UNBOUNDED":
                    if unit == "RANGE":
                        while True:
                            if self.reg_end is not None and self.range_test(
                                    ">=", self.current, self.reg_end, self.end_cursor):
                                break
                            self.code_op(RETURN)
                            self.code_op(INVERSE, "reg_start" if self.reg_start is not None else None)
                            if self.reg_end is None:
                                break
                    elif self.reg_end is not None and self.reg_end > 0:
                        self.reg_end -= 1
                    else:
                        self.code_op(RETURN)
                        self.code_op(INVERSE, "reg_start" if self.reg_start is not None else None)
            index += 1
        if self.count:
            self.flush()

    def flush(self) -> None:
        group = self.group
        unit, start, end = group.unit, group.start, group.end
        self.flushing = True
        reg_start = "reg_start" if self.reg_start is not None else None
        reg_end = "reg_end" if self.reg_end is not None else None
        if end == "PRECEDING":
            range_preceding = start == "PRECEDING" and unit == "RANGE"
            self.code_op(STEP, reg_end)
            if range_preceding:
                self.code_op(INVERSE, reg_start)
            self.code_op(RETURN)
        elif start == "FOLLOWING":
            self.code_op(STEP)
            finished = False
            if unit == "RANGE":
                while True:
                    if self.code_op(INVERSE, reg_start, True):
                        break  # addrBreak2: return the remaining rows
                    if self.code_op(RETURN, None, True):
                        finished = True
                        break
            elif end == "UNBOUNDED":
                while True:
                    if self.code_op(RETURN, reg_start, True):
                        finished = True
                        break
                    if self.code_op(INVERSE, None, True):
                        break
            else:
                self.reg_end -= self.reg_start
                self.reg_start = 0
                while True:
                    if self.code_op(RETURN, reg_end, True):
                        finished = True
                        break
                    if self.code_op(INVERSE, reg_start, True):
                        break
            while not finished:
                if self.code_op(RETURN, None, True):
                    finished = True
        else:
            self.code_op(STEP)
            while True:
                if self.code_op(RETURN, None, True):
                    break
                self.code_op(INVERSE, reg_start)


def _holds(op: str, order: int) -> bool:
    if op == ">=":
        return order >= 0
    if op == ">":
        return order > 0
    if op == "<=":
        return order <= 0
    return order < 0
