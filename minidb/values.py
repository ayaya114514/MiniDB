"""SQL value semantics, following SQLite.

Values are Python ``None`` (NULL), ``int`` (INTEGER, 64-bit), ``float``
(REAL) and ``str`` (TEXT).  This module implements type affinity, the
three-valued comparison and logic rules, arithmetic with SQLite's overflow
and division rules, conversions to text, and the scalar functions.
"""

import math
import re
from functools import lru_cache

from minidb.errors import OperationalError

INT_MIN = -(2**63)
INT_MAX = 2**63 - 1

# Column affinities.  Expressions that are not column references have none.
INTEGER = "INTEGER"
TEXT = "TEXT"

_SPACE = " \t\n\v\f\r"
_NUMBER = r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_WHOLE_NUMBER = re.compile(rf"[{_SPACE}]*({_NUMBER})[{_SPACE}]*\Z")
_NUMBER_PREFIX = re.compile(rf"[{_SPACE}]*({_NUMBER})")
_INTEGER_LITERAL = re.compile(r"[+-]?[0-9]+\Z")


def _parse_number(literal):
    """Convert a numeric literal (already validated) to int or float."""
    if _INTEGER_LITERAL.match(literal):
        value = int(literal)
        if INT_MIN <= value <= INT_MAX:
            return value
    return float(literal)


def _real(value):
    """Normalize a float result: NaN becomes NULL, as in SQLite."""
    return None if math.isnan(value) else value


def _real_to_int_if_exact(value):
    if INT_MIN < value < INT_MAX and value == int(value):
        return int(value)
    return value


# ---- affinity ------------------------------------------------------------


def numeric_affinity(value):
    """Apply INTEGER (numeric) affinity: well-formed numeric text becomes a number."""
    if isinstance(value, str):
        match = _WHOLE_NUMBER.match(value)
        if not match:
            return value
        number = _parse_number(match.group(1))
        return _real_to_int_if_exact(number) if isinstance(number, float) else number
    if isinstance(value, float):
        return _real_to_int_if_exact(value)
    return value


def text_affinity(value):
    """Apply TEXT affinity: numbers are converted to their text form."""
    if isinstance(value, (int, float)):
        return to_text(value)
    return value


def apply_affinity(value, affinity):
    if affinity == INTEGER:
        return numeric_affinity(value)
    if affinity == TEXT:
        return text_affinity(value)
    return value


def comparison_affinities(left, right):
    """Which affinity to apply to each operand of a comparison (SQLite rules)."""
    if left == INTEGER and right != INTEGER:
        return None, INTEGER
    if right == INTEGER and left != INTEGER:
        return INTEGER, None
    if left == TEXT and right is None:
        return None, TEXT
    if right == TEXT and left is None:
        return TEXT, None
    return None, None


# ---- conversions -----------------------------------------------------------


def format_real(value):
    """Render a REAL as text like SQLite: 15 significant digits if they
    round-trip, otherwise 17; always with a '.'; exponent form below 1e-4 and
    from 1e17.  (SQLite's own digit generation is approximate, so a few
    values still differ in the last digits.)"""
    if math.isinf(value):
        return "Inf" if value > 0 else "-Inf"
    if value == 0:
        return "0.0"
    text = f"{value:.14e}"
    if float(text) != value:
        text = f"{value:.16e}"
    mantissa, exponent = text.split("e")
    exponent = int(exponent)
    sign = "-" if mantissa.startswith("-") else ""
    digits = mantissa.lstrip("-").replace(".", "").rstrip("0")
    if exponent < -4 or exponent >= 17:
        sign_char = "+" if exponent >= 0 else "-"
        return f"{sign}{digits[0]}.{digits[1:] or '0'}e{sign_char}{abs(exponent):02d}"
    if exponent >= 0:
        whole = digits[:exponent + 1].ljust(exponent + 1, "0")
        return f"{sign}{whole}.{digits[exponent + 1:] or '0'}"
    return f"{sign}0.{'0' * (-exponent - 1)}{digits}"


def to_text(value):
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        return format_real(value)
    return str(value)


def to_number(value):
    """Lenient numeric conversion used by arithmetic: text uses its numeric prefix."""
    if not isinstance(value, str):
        return value
    match = _NUMBER_PREFIX.match(value)
    return _parse_number(match.group(1)) if match else 0


_INTEGER_PREFIX = re.compile(rf"[{_SPACE}]*([+-]?[0-9]+)")


def to_int64(value):
    """Convert to a 64-bit integer as SQLite does: REALs truncate and saturate,
    TEXT uses its leading integer digits ('1e2' -> 1)."""
    if isinstance(value, str):
        match = _INTEGER_PREFIX.match(value)
        if not match:
            return 0
        return max(INT_MIN, min(INT_MAX, int(match.group(1))))
    if isinstance(value, float):
        if math.isnan(value):
            return 0
        if value <= INT_MIN:
            return INT_MIN
        if value >= INT_MAX:
            return INT_MAX
        return int(value)
    return value


def truth(value):
    """SQL truth value: None for NULL, otherwise whether the number is non-zero."""
    if value is None:
        return None
    return to_number(value) != 0


def type_name(value):
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    return "text"


# ---- comparison ------------------------------------------------------------


def sort_key(value):
    """Key ordering values like SQLite: NULL < numbers < text."""
    if value is None:
        return (0, 0)
    if isinstance(value, str):
        return (2, value)
    return (1, value)


def compare(a, b):
    """Three-way comparison of two non-NULL values (-1, 0 or 1): numbers < text."""
    a_text, b_text = type(a) is str, type(b) is str
    if a_text == b_text:
        return (a > b) - (a < b)
    return 1 if a_text else -1


# ---- operators ---------------------------------------------------------------


def _int_result(value, a, b, float_op):
    if INT_MIN <= value <= INT_MAX:
        return value
    return _real(float_op(float(a), float(b)))


def add(a, b):
    a, b = to_number(a), to_number(b)
    if a is None or b is None:
        return None
    if isinstance(a, int) and isinstance(b, int):
        return _int_result(a + b, a, b, lambda x, y: x + y)
    return _real(float(a) + float(b))


def subtract(a, b):
    a, b = to_number(a), to_number(b)
    if a is None or b is None:
        return None
    if isinstance(a, int) and isinstance(b, int):
        return _int_result(a - b, a, b, lambda x, y: x - y)
    return _real(float(a) - float(b))


def multiply(a, b):
    a, b = to_number(a), to_number(b)
    if a is None or b is None:
        return None
    if isinstance(a, int) and isinstance(b, int):
        return _int_result(a * b, a, b, lambda x, y: x * y)
    return _real(float(a) * float(b))


def divide(a, b):
    a, b = to_number(a), to_number(b)
    if a is None or b is None or b == 0:
        return None
    if isinstance(a, int) and isinstance(b, int):
        if a == INT_MIN and b == -1:
            return float(a) / b
        quotient = abs(a) // abs(b)
        return quotient if (a < 0) == (b < 0) else -quotient
    return _real(float(a) / float(b))


def remainder(a, b):
    na, nb = to_number(a), to_number(b)
    if na is None or nb is None:
        return None
    if isinstance(na, int) and isinstance(nb, int):
        a, b, as_real = na, nb, False
    else:
        # SQLite converts the original operands (not their numeric values) to integers.
        a, b, as_real = to_int64(a), to_int64(b), True
    if b == 0:
        return None
    result = abs(a) % abs(b)
    if a < 0:
        result = -result
    return float(result) if as_real else result


def negate(a):
    a = to_number(a)
    if a is None:
        return None
    if a == INT_MIN and isinstance(a, int):
        return -float(a)
    return -a


def concat(a, b):
    if a is None or b is None:
        return None
    return to_text(a) + to_text(b)


def logical_not(a):
    t = truth(a)
    return None if t is None else int(not t)


def logical_and(a, b):
    ta, tb = truth(a), truth(b)
    if ta is False or tb is False:
        return 0
    if ta is None or tb is None:
        return None
    return 1


def logical_or(a, b):
    ta, tb = truth(a), truth(b)
    if ta or tb:
        return 1
    if ta is None or tb is None:
        return None
    return 0


@lru_cache(maxsize=256)
def _like_regex(pattern):
    parts = []
    for ch in pattern:
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts), re.DOTALL | re.IGNORECASE)


def like(value, pattern):
    """``value LIKE pattern``: case-insensitive, ``%`` and ``_`` wildcards."""
    if value is None or pattern is None:
        return None
    return int(_like_regex(to_text(pattern)).fullmatch(to_text(value)) is not None)


# ---- scalar functions ----------------------------------------------------------


def _fn_abs(value):
    if value is None:
        return None
    if isinstance(value, int):
        if value == INT_MIN:
            raise OperationalError("integer overflow")
        return abs(value)
    return abs(float(to_number(value)))  # TEXT always gives a REAL, as in SQLite


def _fn_length(value):
    return None if value is None else len(to_text(value))


def _fn_lower(value):
    return None if value is None else to_text(value).lower()


def _fn_upper(value):
    return None if value is None else to_text(value).upper()


def _fn_coalesce(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _fn_nullif(a, b):
    return None if a is not None and b is not None and compare(a, b) == 0 else a


def _fn_min(*values):
    """Scalar MIN: among equal values (1 and 1.0) SQLite returns the last one."""
    if any(v is None for v in values):
        return None
    best = values[0]
    for value in values[1:]:
        if compare(best, value) >= 0:
            best = value
    return best


def _fn_max(*values):
    """Scalar MAX: among equal values SQLite returns the first one."""
    if any(v is None for v in values):
        return None
    best = values[0]
    for value in values[1:]:
        if compare(best, value) < 0:
            best = value
    return best


# name -> (function, minimum argument count, maximum argument count or None)
SCALAR_FUNCTIONS = {
    "ABS": (_fn_abs, 1, 1),
    "COALESCE": (_fn_coalesce, 2, None),
    "IFNULL": (_fn_coalesce, 2, 2),
    "LENGTH": (_fn_length, 1, 1),
    "LOWER": (_fn_lower, 1, 1),
    "MAX": (_fn_max, 2, None),
    "MIN": (_fn_min, 2, None),
    "NULLIF": (_fn_nullif, 2, 2),
    "TYPEOF": (type_name, 1, 1),
    "UPPER": (_fn_upper, 1, 1),
}


# ---- aggregate functions -----------------------------------------------------


def numeric_type_value(value):
    """SQLite's sqlite3_value_numeric_type() conversion: numeric-looking TEXT
    becomes INTEGER or REAL (without turning '5.0' into 5); other values stay."""
    if isinstance(value, str):
        match = _WHOLE_NUMBER.match(value)
        return _parse_number(match.group(1)) if match else value
    return value


_KBN_LIMIT = 4503599627370496  # 2**52


def _c_remainder(a, b):
    result = abs(a) % abs(b)
    return -result if a < 0 else result


class SumAccumulator:
    """SUM/AVG/TOTAL state, ported from SQLite's func.c (Kahan-Babuska-Neumaier
    summation once any value is not an integer, integer overflow detection)."""

    def __init__(self):
        self.count = 0
        self.int_sum = 0
        self.approx = False
        self.overflow = False
        self.real_sum = 0.0
        self.error = 0.0

    def _kbn_init(self, value):
        if value <= -_KBN_LIMIT or value >= _KBN_LIMIT:
            small = _c_remainder(value, 16384)
            self.real_sum, self.error = float(value - small), float(small)
        else:
            self.real_sum, self.error = float(value), 0.0

    def _kbn_step(self, r):
        s = self.real_sum
        t = s + r
        if abs(s) > abs(r):
            self.error += (s - t) + r
        else:
            self.error += (r - t) + s
        self.real_sum = t

    def _kbn_step_int(self, value):
        if value <= -_KBN_LIMIT or value >= _KBN_LIMIT:
            small = _c_remainder(value, 16384)
            self._kbn_step(float(value - small))
            self._kbn_step(float(small))
        else:
            self._kbn_step(float(value))

    def step(self, value):
        value = numeric_type_value(value)
        if value is None:
            return
        self.count += 1
        is_int = isinstance(value, int)
        if not self.approx:
            if not is_int:
                self._kbn_init(self.int_sum)
                self.approx = True
                self._kbn_step(float(to_number(value)))
            elif INT_MIN <= self.int_sum + value <= INT_MAX:
                self.int_sum += value
            else:
                self.overflow = True
                self._kbn_init(self.int_sum)
                self.approx = True
                self._kbn_step_int(value)
        elif is_int:
            self._kbn_step_int(value)
        else:
            self.overflow = False
            self._kbn_step(float(to_number(value)))

    def _real_total(self):
        if not self.approx:
            return float(self.int_sum)
        if math.isinf(self.error) or math.isnan(self.error):
            return self.real_sum
        return self.real_sum + self.error


class SumAggregate(SumAccumulator):
    def result(self):
        if self.count == 0:
            return None
        if not self.approx:
            return self.int_sum
        if self.overflow:
            raise OperationalError("integer overflow")
        return _real(self._real_total())


class AvgAggregate(SumAccumulator):
    def result(self):
        if self.count == 0:
            return None
        return _real(self._real_total() / self.count)


class TotalAggregate(SumAccumulator):
    def result(self):
        return _real(self._real_total())


class CountAggregate:
    def __init__(self):
        self.count = 0

    def step(self, value):
        if value is not None:
            self.count += 1

    def result(self):
        return self.count


class CountStarAggregate(CountAggregate):
    def step(self):
        self.count += 1


class MinMaxAggregate:
    """MIN or MAX; ``step`` reports whether the current extreme changed."""

    def __init__(self, want):
        self.want = want  # -1 for MIN, 1 for MAX
        self.value = None

    def step(self, value):
        if value is None:
            return False
        if self.value is None or compare(value, self.value) == self.want:
            self.value = value
            return True
        return False

    def result(self):
        return self.value


class GroupConcatAggregate:
    def __init__(self):
        self.text = None

    def step(self, value, separator=","):
        if value is None:
            return
        value = to_text(value)
        if self.text is None:
            self.text = value
        else:
            self.text += ("" if separator is None else to_text(separator)) + value

    def result(self):
        return self.text


# name -> (factory, minimum argument count, maximum argument count)
AGGREGATE_FUNCTIONS = {
    "AVG": (AvgAggregate, 1, 1),
    "COUNT": (CountAggregate, 0, 1),
    "GROUP_CONCAT": (GroupConcatAggregate, 1, 2),
    "MAX": (lambda: MinMaxAggregate(1), 1, 1),
    "MIN": (lambda: MinMaxAggregate(-1), 1, 1),
    "SUM": (SumAggregate, 1, 1),
    "TOTAL": (TotalAggregate, 1, 1),
}


def is_aggregate_call(name, arg_count):
    """MIN and MAX are aggregates with one argument and scalar with more."""
    if name in ("MIN", "MAX"):
        return arg_count == 1
    return name in AGGREGATE_FUNCTIONS
