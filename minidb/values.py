"""SQL value semantics, following SQLite.

Values are Python ``None`` (NULL), ``int`` (INTEGER, 64-bit), ``float``
(REAL), ``str`` (TEXT) and ``bytes`` (BLOB).  This module implements type affinity, the
three-valued comparison and logic rules, arithmetic with SQLite's overflow
and division rules, conversions to text, and the scalar functions.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from functools import lru_cache

from minidb.errors import OperationalError
from minidb.fp import atof
from minidb.jsonb import JSONBlob, JSONText
from minidb.fp import format_real as fp_format_real

_TO_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_TO_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def ascii_lower(text: str) -> str:
    """Lower case as SQLite understands it: ASCII letters only (lower(),
    identifiers, keywords and type names alike; 'É' stays 'É')."""
    return text.lower() if text.isascii() else text.translate(_TO_LOWER)


def ascii_upper(text: str) -> str:
    return text.upper() if text.isascii() else text.translate(_TO_UPPER)

# A SQL value: NULL, INTEGER, REAL, TEXT or BLOB.
SQLValue = int | float | str | bytes | None

INT_MIN = -(2**63)
INT_MAX = 2**63 - 1

# Column affinities (from the declared type, see type_affinity).  Expressions
# that are not column references or CASTs have none (None).  In comparisons
# the three numeric affinities act alike; BLOB means "convert nothing".
INTEGER = "INTEGER"
REAL = "REAL"
NUMERIC = "NUMERIC"
TEXT = "TEXT"
BLOB = "BLOB"
NUMERIC_AFFINITIES = frozenset((INTEGER, REAL, NUMERIC))

_SPACE = " \t\n\v\f\r"
_NUMBER = r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_WHOLE_NUMBER = re.compile(rf"[{_SPACE}]*({_NUMBER})[{_SPACE}]*\Z")
_NUMBER_PREFIX = re.compile(rf"[{_SPACE}]*({_NUMBER})")
_INTEGER_LITERAL = re.compile(r"[+-]?[0-9]+\Z")


def _parse_number(literal: str) -> int | float:
    """Convert a numeric literal (already validated) to int or float."""
    if _INTEGER_LITERAL.match(literal):
        value = int(literal)
        if INT_MIN <= value <= INT_MAX:
            return value
    return atof(literal)


def _real(value: float) -> float | None:
    """Normalize a float result: NaN becomes NULL, as in SQLite."""
    return None if math.isnan(value) else value


def _real_to_int_if_exact(value: float) -> int | float:
    if INT_MIN < value < INT_MAX and value == int(value):
        return int(value)
    return value


# ---- affinity ------------------------------------------------------------


def numeric_affinity(value: SQLValue) -> SQLValue:
    """Apply INTEGER (numeric) affinity: well-formed numeric text becomes a
    number.  Of text with a NUL character SQLite reads only what comes
    before the NUL, and as a REAL (then made an INTEGER if exact)."""
    if isinstance(value, str):
        text = value
        if "\x00" in text:
            text = text.split("\x00", 1)[0]
        match = _WHOLE_NUMBER.match(text)
        if not match:
            return value
        number = _parse_number(match.group(1))
        if text is not value:
            number = float(number)
        return _real_to_int_if_exact(number) if isinstance(number, float) else number
    if isinstance(value, float):
        return _real_to_int_if_exact(value)
    return value


def text_affinity(value: SQLValue) -> SQLValue:
    """Apply TEXT affinity: numbers are converted to their text form."""
    if isinstance(value, (int, float)):
        return to_text(value)
    return value


def real_affinity(value: SQLValue) -> SQLValue:
    """Apply REAL affinity: like NUMERIC, but numbers end up as REALs."""
    value = numeric_affinity(value)
    return float(value) if isinstance(value, int) else value


def apply_affinity(value: SQLValue, affinity: str | None) -> SQLValue:
    """Convert a value stored in a column with ``affinity``."""
    if affinity == INTEGER or affinity == NUMERIC:
        return numeric_affinity(value)
    if affinity == TEXT:
        return text_affinity(value)
    if affinity == REAL:
        return real_affinity(value)
    return value


def comparison_affinity(left: str | None, right: str | None) -> str | None:
    """The affinity SQLite applies to both operands of a comparison: with
    two columns (any affinity), NUMERIC if either is numeric, else none;
    otherwise the column's.  (Applied to both, it changes a column's own
    value only where the value does not have the column's affinity, as a
    coalesce() of USING columns may not.)"""
    if left is not None and right is not None:
        return NUMERIC if left in NUMERIC_AFFINITIES or right in NUMERIC_AFFINITIES else None
    return left if left is not None else right


# ---- conversions -----------------------------------------------------------


class IntReal(float):
    """A whole REAL as SQLite keeps it after OP_Affinity applies REAL
    affinity (MEM_IntReal: a generated column's value): a real number,
    except in a record, which holds it as an integer (one without REAL
    affinity reads back an integer)."""

    __slots__ = ()


# Whether any IntReal was ever made (only then do sorts look for them).
int_reals_made = [False]


def int_real(value: SQLValue) -> SQLValue:
    """``value`` (with REAL affinity applied) as OP_Affinity leaves it: an
    IntReal when it is whole and fits in 48 bits."""
    if type(value) is float and value.is_integer() and -140737488355328.0 <= value <= 140737488355327.0:
        int_reals_made[0] = True
        return IntReal(int(value))
    return value


_PLAIN_TYPES = frozenset((type(None), int, float, str, bytes))


def record_value(value: SQLValue, affinity: str | None = None) -> SQLValue:
    """A value as a record holds it (in a column of ``affinity``): a JSON
    value without its subtype, an IntReal as an integer (as a REAL in a
    REAL column)."""
    kind = type(value)
    if kind in _PLAIN_TYPES:
        return value
    if kind is IntReal:
        return float(value) if affinity == REAL else int(value)
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, bytes):
        return bytes(value)
    return value


def through_record(row: tuple) -> tuple:
    """A result row as it comes out of SQLite's sorter or a temporary table
    (a record): IntReals become integers."""
    if any(type(v) is IntReal for v in row):
        return tuple(int(v) if type(v) is IntReal else v for v in row)
    return row


def format_real(value: float) -> str:
    """Render a REAL as text like SQLite: printf("%!.17g"), see fp.format_real."""
    return fp_format_real(value)


def to_text(value: SQLValue) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        return format_real(value)
    if isinstance(value, bytes):
        # SQLite reads the bytes as UTF-8 text; invalid bytes are kept (as
        # lone surrogates) so that converting back gives the same BLOB.
        return value.decode("utf-8", "surrogateescape")
    return str(value)


def to_blob(value: SQLValue) -> bytes | None:
    """``CAST(value AS BLOB)``: the bytes of its text."""
    if value is None or isinstance(value, bytes):
        return value
    return to_text(value).encode("utf-8", "surrogateescape")


def to_number(value: SQLValue) -> int | float | None:
    """Lenient numeric conversion used by arithmetic: text (and a BLOB, read
    as text) uses its numeric prefix."""
    if isinstance(value, bytes):
        value = to_text(value)
    if not isinstance(value, str):
        return value
    match = _NUMBER_PREFIX.match(value)
    if not match:
        return 0
    number = _parse_number(match.group(1))
    # sqlite3AtoF stops at a NUL but sqlite3Atoi64 reads past it, so text that
    # is a whole number up to a NUL counts as a REAL ('5\0' + 0 is 5.0, while
    # '5 x\0' + 0 is 5).
    if "\x00" in value and _WHOLE_NUMBER.match(value.split("\x00", 1)[0]):
        return float(number)
    return number


def numeric_prefix(value: SQLValue) -> int | float | None:
    """The number that text (or a BLOB, read as text) starts with; 0 if none."""
    if isinstance(value, bytes):
        value = to_text(value)
    if not isinstance(value, str):
        return value
    match = _NUMBER_PREFIX.match(value)
    return _parse_number(match.group(1)) if match else 0


_INTEGER_PREFIX = re.compile(rf"[{_SPACE}]*([+-]?[0-9]+)")


def to_int64(value: SQLValue) -> int:
    """Convert to a 64-bit integer as SQLite does: REALs truncate and saturate,
    TEXT uses its leading integer digits ('1e2' -> 1)."""
    if isinstance(value, bytes):
        value = to_text(value)
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


def truth(value: SQLValue) -> bool | None:
    """SQL truth value: None for NULL, otherwise whether the number is non-zero."""
    if value is None:
        return None
    return to_number(value) != 0


def type_name(value: SQLValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    if isinstance(value, bytes):
        return "blob"
    return "text"


# ---- comparison ------------------------------------------------------------

# The built-in collations: how text compares (other values compare alike under all).
COLLATIONS = ("BINARY", "NOCASE", "RTRIM")


def collation_name(name: str) -> str:
    """The canonical name of a collation (an error for unknown ones)."""
    upper = ascii_upper(name)
    if upper not in COLLATIONS:
        raise OperationalError(f"no such collation sequence: {name}")
    return upper



def _text_key(text: str) -> str:
    """Text as a string whose order is SQLite's: its UTF-8 bytes compared
    one by one.  For valid UTF-8 that is the order of code points, but text
    made from a BLOB may hold bytes that are not UTF-8 (lone surrogates, see
    to_text), and those must sort by their byte values; so non-ASCII text
    becomes its UTF-8 bytes, one character per byte."""
    if text.isascii():
        return text
    return text.encode("utf-8", "surrogateescape").decode("latin-1")


def sort_key(value: SQLValue) -> tuple:
    """Key ordering values like SQLite: NULL < numbers < text < BLOBs."""
    if value is None:
        return (0, 0)
    if isinstance(value, str):
        return (2, _text_key(value))
    if isinstance(value, bytes):
        return (3, value)
    return (1, value)


def plain_value(pair: tuple) -> SQLValue:
    """The value of a sort key (the inverse of sort_key and collation_sort_key)."""
    rank = pair[0]
    if rank == 0:
        return None
    if rank == 2:
        text = pair[1]
        if text.__class__ is Collated:
            return text.original
        if not text.isascii():
            return text.encode("latin-1").decode("utf-8", "surrogateescape")
    return pair[1]


class Collated(str):
    """The key of a text value under a collation (NOCASE, RTRIM): a string
    that compares and hashes as the key and remembers the value itself."""

    original: str


def _nocase_key(text: str) -> str:
    """NOCASE compares text with ASCII upper case letters made lower case
    (sqlite3StrNICmp), byte by byte up to the shorter length, and then by
    length - but it stops at a NUL in the first text: two texts with a NUL
    at the same place after equal bytes compare by their lengths alone."""
    key = ascii_lower(_text_key(text))
    nul = key.find("\x00")
    if nul < 0:
        return key
    return f"{key[:nul + 1]}{len(key):020d}"


def _rtrim_key(text: str) -> str:
    """RTRIM compares text without its trailing spaces."""
    return _text_key(text).rstrip(" ")


_COLLATION_KEYS = {"NOCASE": _nocase_key, "RTRIM": _rtrim_key}


@lru_cache(maxsize=None)
def collation_sort_key(collation: str | None) -> Callable[[SQLValue], tuple]:
    """sort_key under a collation (None or BINARY: sort_key itself)."""
    text_key = _COLLATION_KEYS.get(collation)
    if text_key is None:
        return sort_key

    def collated_sort_key(value: SQLValue) -> tuple:
        if isinstance(value, str):
            key = Collated(text_key(value))
            key.original = value
            return (2, key)
        return sort_key(value)
    return collated_sort_key


@lru_cache(maxsize=None)
def collation_compare(collation: str | None) -> Callable[[SQLValue, SQLValue], int]:
    """compare under a collation: it decides how two texts compare."""
    text_key = _COLLATION_KEYS.get(collation)
    if text_key is None:
        return compare

    def collated_compare(a: SQLValue, b: SQLValue) -> int:
        if isinstance(a, str) and isinstance(b, str):  # (text with the JSON subtype too)
            a, b = text_key(str.__str__(a)), text_key(str.__str__(b))
            return (a > b) - (a < b)
        return compare(a, b)
    return collated_compare


def compare(a: int | float | str | bytes, b: int | float | str | bytes) -> int:
    """Three-way comparison of two non-NULL values (-1, 0 or 1):
    numbers < text < BLOBs; text and BLOBs byte by byte."""
    a_text, b_text = type(a) is str, type(b) is str
    if not (a_text and b_text):
        # (text with the JSON subtype, minidb.jsonb.JSONText, is text)
        if not a_text and isinstance(a, str):
            a, a_text = str.__str__(a), True
        if not b_text and isinstance(b, str):
            b, b_text = str.__str__(b), True
    if a_text and b_text:
        if not (a.isascii() and b.isascii()):
            a, b = _text_key(a), _text_key(b)
        return (a > b) - (a < b)
    a_blob, b_blob = isinstance(a, bytes), isinstance(b, bytes)
    if a_text == b_text and a_blob == b_blob:
        return (a > b) - (a < b)  # two numbers or two BLOBs
    return 1 if (a_blob or a_text and not b_blob) else -1


# ---- operators ---------------------------------------------------------------


def _int_result(value: int, a: int, b: int, float_op: Callable[[float, float], float]) -> int | float | None:
    if INT_MIN <= value <= INT_MAX:
        return value
    return _real(float_op(float(a), float(b)))


def add(a: SQLValue, b: SQLValue) -> SQLValue:
    a, b = to_number(a), to_number(b)
    if a is None or b is None:
        return None
    if isinstance(a, int) and isinstance(b, int):
        return _int_result(a + b, a, b, lambda x, y: x + y)
    return _real(float(a) + float(b))


def subtract(a: SQLValue, b: SQLValue) -> SQLValue:
    a, b = to_number(a), to_number(b)
    if a is None or b is None:
        return None
    if isinstance(a, int) and isinstance(b, int):
        return _int_result(a - b, a, b, lambda x, y: x - y)
    return _real(float(a) - float(b))


def multiply(a: SQLValue, b: SQLValue) -> SQLValue:
    a, b = to_number(a), to_number(b)
    if a is None or b is None:
        return None
    if isinstance(a, int) and isinstance(b, int):
        return _int_result(a * b, a, b, lambda x, y: x * y)
    return _real(float(a) * float(b))


def divide(a: SQLValue, b: SQLValue) -> SQLValue:
    a, b = to_number(a), to_number(b)
    if a is None or b is None or b == 0:
        return None
    if isinstance(a, int) and isinstance(b, int):
        if a == INT_MIN and b == -1:
            return float(a) / b
        quotient = abs(a) // abs(b)
        return quotient if (a < 0) == (b < 0) else -quotient
    return _real(float(a) / float(b))


def remainder(a: SQLValue, b: SQLValue) -> SQLValue:
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


def negate(a: SQLValue) -> SQLValue:
    a = to_number(a)
    if a is None:
        return None
    if a == INT_MIN and isinstance(a, int):
        return -float(a)
    return -a


def _signed(value: int) -> int:
    """Wrap to a signed 64-bit integer."""
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value > INT_MAX else value


def bit_and(a: SQLValue, b: SQLValue) -> int | None:
    if a is None or b is None:
        return None
    return to_int64(a) & to_int64(b)


def bit_or(a: SQLValue, b: SQLValue) -> int | None:
    if a is None or b is None:
        return None
    return to_int64(a) | to_int64(b)


def bit_not(a: SQLValue) -> int | None:
    return None if a is None else ~to_int64(a)


def shift(a: SQLValue, b: SQLValue, left: bool) -> int | None:
    """``a << b`` (``left``) or ``a >> b`` as SQLite computes them: a negative
    amount shifts the other way, 64 or more gives 0 (or -1 shifting a
    negative number right), and left shifts wrap around."""
    if a is None or b is None:
        return None
    value, amount = to_int64(a), to_int64(b)
    if amount < 0:
        left, amount = not left, -amount
    if amount >= 64:
        return 0 if value >= 0 or left else -1
    return _signed(value << amount) if left else value >> amount


def shift_left(a: SQLValue, b: SQLValue) -> int | None:
    return shift(a, b, True)


def shift_right(a: SQLValue, b: SQLValue) -> int | None:
    return shift(a, b, False)


def concat(a: SQLValue, b: SQLValue) -> SQLValue:
    if a is None or b is None:
        return None
    return to_text(a) + to_text(b)


def logical_not(a: SQLValue) -> int | None:
    t = truth(a)
    return None if t is None else int(not t)


def logical_and(a: SQLValue, b: SQLValue) -> int | None:
    ta, tb = truth(a), truth(b)
    if ta is False or tb is False:
        return 0
    if ta is None or tb is None:
        return None
    return 1


@lru_cache(maxsize=256)
def _like_regex(pattern: str) -> re.Pattern:
    parts = []
    for ch in pattern:
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts), re.DOTALL | re.IGNORECASE | re.ASCII)  # ASCII-only case folding


def like(value: SQLValue, pattern: SQLValue) -> int | None:
    """``value LIKE pattern``: ``%`` and ``_`` wildcards, case-insensitive for
    ASCII letters only (like SQLite without the ICU extension)."""
    if value is None or pattern is None:
        return None
    return int(_like_regex(_c_string(to_text(pattern))).fullmatch(_c_string(to_text(value))) is not None)


def _c_string(text: str) -> str:
    """The text up to its first NUL character, where SQLite's C string
    functions (LIKE, length) stop."""
    return text.split("\x00", 1)[0] if "\x00" in text else text


# ---- scalar functions ----------------------------------------------------------


def _fn_abs(value: SQLValue) -> SQLValue:
    if value is None:
        return None
    if isinstance(value, int):
        if value == INT_MIN:
            raise OperationalError("integer overflow")
        return abs(value)
    return abs(float(to_number(value)))  # TEXT always gives a REAL, as in SQLite


def _fn_length(value: SQLValue) -> int | None:
    """Characters of text (up to a NUL character); bytes of a BLOB."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return len(value)
    text = _c_string(to_text(value))
    if text.isascii():
        return len(text)
    data = text.encode("utf-8", "surrogateescape")
    # SQLite counts every byte below 0xC0 as a character and lets a byte
    # from 0xC0 up take the continuation bytes (0x80-0xBF) after it: the
    # number of characters for valid UTF-8, also defined for invalid bytes.
    count, i = 0, 0
    while i < len(data):
        byte = data[i]
        i += 1
        if byte >= 0xC0:
            while i < len(data) and data[i] & 0xC0 == 0x80:
                i += 1
        count += 1
    return count


def _fn_lower(value: SQLValue) -> str | None:
    return None if value is None else ascii_lower(to_text(value))


def _fn_upper(value: SQLValue) -> str | None:
    return None if value is None else ascii_upper(to_text(value))


def _fn_coalesce(*values: SQLValue) -> SQLValue:
    for value in values:
        if value is not None:
            return value
    return None


def _nullif_function(compare: Callable[[SQLValue, SQLValue], int]) -> Callable[..., SQLValue]:
    def nullif(a: SQLValue, b: SQLValue) -> SQLValue:
        return None if a is not None and b is not None and compare(a, b) == 0 else a
    return nullif


def _min_function(compare: Callable[[SQLValue, SQLValue], int]) -> Callable[..., SQLValue]:
    def minimum(*values: SQLValue) -> SQLValue:
        """Scalar MIN: among equal values (1 and 1.0) SQLite returns the last one."""
        if any(v is None for v in values):
            return None
        best = values[0]
        for value in values[1:]:
            if compare(best, value) >= 0:
                best = value
        return best
    return minimum


def _max_function(compare: Callable[[SQLValue, SQLValue], int]) -> Callable[..., SQLValue]:
    def maximum(*values: SQLValue) -> SQLValue:
        """Scalar MAX: among equal values SQLite returns the first one."""
        if any(v is None for v in values):
            return None
        best = values[0]
        for value in values[1:]:
            if compare(best, value) < 0:
                best = value
        return best
    return maximum


_fn_nullif, _fn_min, _fn_max = _nullif_function(compare), _min_function(compare), _max_function(compare)
# The scalar functions that compare text by their arguments' collation
# (SQLite's SQLITE_FUNC_NEEDCOLL): name -> function of a compare function.
COLLATING_FUNCTIONS = {"MIN": _min_function, "MAX": _max_function, "NULLIF": _nullif_function}


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


def numeric_type_value(value: SQLValue) -> SQLValue:
    """SQLite's sqlite3_value_numeric_type() conversion: numeric-looking TEXT
    becomes INTEGER or REAL (without turning '5.0' into 5); other values stay."""
    if isinstance(value, str):
        if "\x00" in value:
            return _numeric_type_before_nul(value)
        match = _WHOLE_NUMBER.match(value)
        return _parse_number(match.group(1)) if match else value
    return value


def _numeric_type_before_nul(text: str) -> SQLValue:
    """sqlite3_value_numeric_type() of text with a NUL: SQLite reads the
    number before the NUL as a double; an integer literal becomes an INTEGER
    only if that double is one below 2**51 (sqlite3RealSameAsInt), since the
    whole text, NUL included, is no integer; otherwise a REAL."""
    match = _WHOLE_NUMBER.match(text.split("\x00", 1)[0])
    if not match:
        return text
    literal = match.group(1)
    real = atof(literal)
    if _INTEGER_LITERAL.match(literal) and real == int(real) and -(2**51) <= int(real) < 2**51:
        return int(real)
    return real


_KBN_LIMIT = 4503599627370496  # 2**52


def _c_remainder(a: int, b: int) -> int:
    result = abs(a) % abs(b)
    return -result if a < 0 else result


class SumAccumulator:
    """SUM/AVG/TOTAL state, ported from SQLite's func.c (Kahan-Babuska-Neumaier
    summation once any value is not an integer, integer overflow detection)."""

    def __init__(self) -> None:
        self.count = 0
        self.int_sum = 0
        self.approx = False
        self.overflow = False
        self.real_sum = 0.0
        self.error = 0.0

    def _kbn_init(self, value: int) -> None:
        if value <= -_KBN_LIMIT or value >= _KBN_LIMIT:
            small = _c_remainder(value, 16384)
            self.real_sum, self.error = float(value - small), float(small)
        else:
            self.real_sum, self.error = float(value), 0.0

    def _kbn_step(self, r: float) -> None:
        s = self.real_sum
        t = s + r
        if abs(s) > abs(r):
            self.error += (s - t) + r
        else:
            self.error += (r - t) + s
        self.real_sum = t

    def _kbn_step_int(self, value: int) -> None:
        if value <= -_KBN_LIMIT or value >= _KBN_LIMIT:
            small = _c_remainder(value, 16384)
            self._kbn_step(float(value - small))
            self._kbn_step(float(small))
        else:
            self._kbn_step(float(value))

    def step(self, value: SQLValue) -> None:
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

    def _real_total(self) -> float:
        if not self.approx:
            return float(self.int_sum)
        if math.isinf(self.error) or math.isnan(self.error):
            return self.real_sum
        return self.real_sum + self.error


class SumAggregate(SumAccumulator):
    def result(self) -> SQLValue:
        if self.count == 0:
            return None
        if not self.approx:
            return self.int_sum
        if self.overflow:
            raise OperationalError("integer overflow")
        return _real(self._real_total())


class AvgAggregate(SumAccumulator):
    def result(self) -> SQLValue:
        if self.count == 0:
            return None
        return _real(self._real_total() / self.count)


class TotalAggregate(SumAccumulator):
    def result(self) -> SQLValue:
        return _real(self._real_total())


class CountAggregate:
    def __init__(self) -> None:
        self.count = 0

    def step(self, value: SQLValue) -> None:
        if value is not None:
            self.count += 1

    def result(self) -> SQLValue:
        return self.count


class CountStarAggregate(CountAggregate):
    def step(self) -> None:
        self.count += 1


class MinMaxAggregate:
    """MIN or MAX; ``step`` reports whether the current extreme changed."""

    def __init__(self, want: int, compare: Callable[[SQLValue, SQLValue], int] = compare) -> None:
        self.want = want  # -1 for MIN, 1 for MAX
        self.compare = compare  # (under the argument's collation)
        self.value = None

    def step(self, value: SQLValue) -> bool:
        """Whether SQLite would load the bare columns from this row (it
        skips that for a NULL once there is a value, and for no new extreme)."""
        if value is None:
            return self.value is None
        if self.value is None or self.compare(value, self.value) == self.want:
            self.value = value
            return True
        return False

    def result(self) -> SQLValue:
        return self.value


class GroupConcatAggregate:
    def __init__(self) -> None:
        self.text = None

    def step(self, value: SQLValue, separator: SQLValue = ",") -> None:
        if value is None:
            return
        value = to_text(value)
        if self.text is None:
            self.text = value
        else:
            self.text += ("" if separator is None else to_text(separator)) + value

    def result(self) -> SQLValue:
        return self.text


# name -> (factory, minimum argument count, maximum argument count)
AGGREGATE_FUNCTIONS = {
    "AVG": (AvgAggregate, 1, 1),
    "COUNT": (CountAggregate, 0, 1),
    "GROUP_CONCAT": (GroupConcatAggregate, 1, 2),
    "MAX": (lambda: MinMaxAggregate(1), 1, 1),
    "MIN": (lambda: MinMaxAggregate(-1), 1, 1),
    "STRING_AGG": (GroupConcatAggregate, 2, 2),
    "SUM": (SumAggregate, 1, 1),
    "TOTAL": (TotalAggregate, 1, 1),
}


def is_aggregate_call(name: str, arg_count: int) -> bool:
    """MIN and MAX are aggregates with one argument and scalar with more."""
    if name in ("MIN", "MAX"):
        return arg_count == 1
    return name in AGGREGATE_FUNCTIONS


# ---- CAST ----------------------------------------------------------------------


def type_affinity(type_name: str) -> str:
    """SQLite's rules for the affinity of a declared type name."""
    name = ascii_upper(type_name)
    if "INT" in name:
        return "INTEGER"
    if "CHAR" in name or "CLOB" in name or "TEXT" in name:
        return "TEXT"
    if "BLOB" in name or not name:
        return "BLOB"
    if "REAL" in name or "FLOA" in name or "DOUB" in name:
        return "REAL"
    return "NUMERIC"


def cast(value: SQLValue, target: str) -> SQLValue:
    """``CAST(value AS <type with affinity target>)``."""
    if value is None:
        return None
    if target == "INTEGER":
        return to_int64(value)
    if target == "REAL":
        return float(to_number(value))
    if target == "TEXT":
        text = to_text(value)
        return JSONText(text) if type(value) is JSONBlob else text  # (SQLite keeps the JSON subtype)
    if target == "BLOB":
        return to_blob(value)
    # NUMERIC: text is read by its numeric prefix; integral REALs read from
    # text become INTEGERs, REAL values themselves stay REAL.
    if isinstance(value, (str, bytes)):
        number = numeric_prefix(value)
        return _real_to_int_if_exact(number) if isinstance(number, float) else number
    return value
