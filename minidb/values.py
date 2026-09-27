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
    """Render a REAL as text like SQLite: always with a '.', exponent form
    below 1e-4 and from 1e17.  Uses the shortest digits that round-trip."""
    if math.isinf(value):
        return "Inf" if value > 0 else "-Inf"
    if value == 0:
        return "0.0"
    sign = "-" if value < 0 else ""
    mantissa, _, exp = repr(abs(value)).partition("e")
    integer_part, _, fraction = mantissa.partition(".")
    digits = integer_part + fraction
    significant = digits.lstrip("0")
    exponent = len(integer_part) + int(exp or 0) - (len(digits) - len(significant)) - 1
    digits = significant.rstrip("0")
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


def to_int64(value):
    """Convert to a 64-bit integer as SQLite does (truncate, saturate)."""
    value = to_number(value)
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
    """Three-way comparison of two non-NULL values (-1, 0 or 1)."""
    ka, kb = sort_key(a), sort_key(b)
    return (ka > kb) - (ka < kb)


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
    a, b = to_number(a), to_number(b)
    if a is None or b is None:
        return None
    both_int = isinstance(a, int) and isinstance(b, int)
    a, b = to_int64(a), to_int64(b)
    if b == 0:
        return None
    result = abs(a) % abs(b)
    if a < 0:
        result = -result
    return result if both_int else float(result)


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
    value = to_number(value)
    if value is None:
        return None
    if value == INT_MIN and isinstance(value, int):
        raise OperationalError("integer overflow")
    return abs(value)


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
    if any(v is None for v in values):
        return None
    return min(values, key=sort_key)


def _fn_max(*values):
    if any(v is None for v in values):
        return None
    return max(values, key=sort_key)


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
