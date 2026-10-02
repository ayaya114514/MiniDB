"""SQLite's core scalar functions beyond the basic ones in ``values``, and
its math functions (the reference SQLite is built with them).

Each follows SQLite's func.c: argument conversions, NULL handling and the
treatment of BLOBs, of text with a NUL character and of out-of-range
numbers.  ``printf()`` / ``format()`` are in ``printf``; the date and time
functions in ``dates``.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Callable
from functools import lru_cache

from minidb import jsonfuncs, values
from minidb.errors import OperationalError
from minidb.dates import DATE_FUNCTIONS
from minidb.fp import atof
from minidb.values import INT_MAX, INT_MIN, SQLValue, numeric_type_value, to_int64, to_text

LENGTH_LIMIT = 1_000_000_000  # SQLITE_LIMIT_LENGTH's default


def _text(value: SQLValue) -> str:
    """sqlite3_value_text(): the value as text, up to a NUL character where
    SQLite's C string loops stop."""
    return values._c_string(to_text(value))


def _as_real(value: SQLValue) -> float:
    """sqlite3_value_double()."""
    number = values.numeric_prefix(value)
    return float(number)


# ---- text ----------------------------------------------------------------------


def substr(value: SQLValue, start: SQLValue, length: SQLValue = LENGTH_LIMIT) -> SQLValue:
    """substr(X, Y[, Z]): Z characters (bytes of a BLOB) from position Y,
    counting from 1, or from the end if Y is negative; a negative Z takes
    the characters before Y."""
    if value is None or start is None or length is None:
        return None
    p1, p2 = to_int64(start), to_int64(length)
    if value == b"":
        return None  # SQLite gets no pointer for an empty BLOB
    if isinstance(value, bytes):
        units = value
    else:
        text = _text(value)
        # Characters as SQLite steps through them; Python's differ only for
        # bytes that are not UTF-8 (lone surrogates, see values.to_text).
        units = _characters(text) if any(0xDC80 <= ord(c) <= 0xDCFF for c in text) else text
    negative = p2 < 0
    if negative:
        p2 = -p2
    if p1 < 0:
        p1 += len(units)
        if p1 < 0:
            p2 = 0 if p2 < 0 else p2 + p1
            p1 = 0
    elif p1 > 0:
        p1 -= 1
    elif p2 > 0:
        p2 -= 1
    if negative:
        p1 -= p2
        if p1 < 0:
            p2 += p1
            p1 = 0
    piece = units[p1:p1 + max(p2, 0)]
    return piece if isinstance(piece, (str, bytes)) else "".join(piece)


def replace(value: SQLValue, pattern: SQLValue, replacement: SQLValue) -> SQLValue:
    """In SQLite's order: an empty pattern returns X (as text) even when the
    replacement is NULL."""
    if value is None or pattern is None:
        return None
    pattern_text = to_text(pattern)
    if pattern_text == "" or pattern_text[0] == "\x00":  # SQLite tests zPattern[0]==0
        return to_text(value)
    if replacement is None:
        return None
    return to_text(value).replace(pattern_text, to_text(replacement))


def _utf8_chunks(data: bytes) -> list[bytes]:
    """The characters of a C string as SQLite steps over them
    (SQLITE_SKIP_UTF8): a lead byte and its continuation bytes, up to a NUL."""
    chunks, i = [], 0
    while i < len(data) and data[i]:
        j = i + 1
        if data[i] >= 0xC0:
            while j < len(data) and data[j] & 0xC0 == 0x80:
                j += 1
        chunks.append(data[i:j])
        i = j
    return chunks


def _trim(value: SQLValue, characters: SQLValue, left: bool, right: bool) -> SQLValue:
    """SQLite's trimFunc: the set of characters is a C string (it ends at a
    NUL); they are removed by comparing bytes."""
    if value is None or characters is None:
        return None
    text = to_text(value)
    chars = to_text(characters)
    if text.isascii() and chars.isascii():
        chars = values._c_string(chars)
        if not chars:
            return text
        if left:
            text = text.lstrip(chars)
        if right:
            text = text.rstrip(chars)
        return text
    data = text.encode("utf-8", "surrogateescape")
    chunks = _utf8_chunks(chars.encode("utf-8", "surrogateescape"))
    start, end = 0, len(data)
    if left:
        while start < end:
            chunk = next((c for c in chunks if data.startswith(c, start, end)), None)
            if chunk is None:
                break
            start += len(chunk)
    if right:
        while start < end:
            chunk = next((c for c in chunks if data.endswith(c, start, end)), None)
            if chunk is None:
                break
            end -= len(chunk)
    return data[start:end].decode("utf-8", "surrogateescape")


def trim(value: SQLValue, characters: SQLValue = " ") -> SQLValue:
    return _trim(value, characters, True, True)


def ltrim(value: SQLValue, characters: SQLValue = " ") -> SQLValue:
    return _trim(value, characters, True, False)


def rtrim(value: SQLValue, characters: SQLValue = " ") -> SQLValue:
    return _trim(value, characters, False, True)


def instr(haystack: SQLValue, needle: SQLValue) -> int | None:
    """Position (from 1) of the first ``needle`` in ``haystack``, 0 if none:
    bytes when both are BLOBs, else characters."""
    if haystack is None or needle is None:
        return None
    if isinstance(haystack, bytes) and isinstance(needle, bytes):
        return haystack.find(needle) + 1
    text, part = to_text(haystack), to_text(needle)
    return text.find(part) + 1


def concat(*args: SQLValue) -> str:
    return "".join(to_text(a) for a in args if a is not None)


def concat_ws(separator: SQLValue, *args: SQLValue) -> str | None:
    if separator is None:
        return None
    return to_text(separator).join(to_text(a) for a in args if a is not None)


def _utf8(code: int) -> bytes:
    """SQLite's UTF-8 encoding of one code point (surrogates included)."""
    if code < 0x80:
        return bytes([code])
    if code < 0x800:
        return bytes([0xC0 + ((code >> 6) & 0x1F), 0x80 + (code & 0x3F)])
    if code < 0x10000:
        return bytes([0xE0 + ((code >> 12) & 0x0F), 0x80 + ((code >> 6) & 0x3F), 0x80 + (code & 0x3F)])
    return bytes([0xF0 + ((code >> 18) & 0x07), 0x80 + ((code >> 12) & 0x3F),
                  0x80 + ((code >> 6) & 0x3F), 0x80 + (code & 0x3F)])


def _from_utf8(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def char(*args: SQLValue) -> str:
    result = bytearray()
    for arg in args:
        code = to_int64(arg) if arg is not None else 0
        if code < 0 or code > 0x10FFFF:
            code = 0xFFFD
        result += _utf8(code)
    return _from_utf8(bytes(result))


def unistr(value: SQLValue) -> str | None:
    """unistr(X): X with \\XXXX, \\uXXXX, \\+XXXXXX, \\UXXXXXXXX escapes for
    code points and \\\\ for a backslash."""
    if value is None:
        return None
    data = to_text(value).encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(data):
        j = data.find(b"\\", i)
        if j == -1:
            out += data[i:]
            break
        out += data[i:j]
        i = j
        following = data[i + 1:i + 2]
        if following == b"\\":
            out += b"\\"
            i += 2
            continue
        for marker, skip, count in ((b"", 1, 4), (b"+", 2, 6), (b"u", 2, 4), (b"U", 2, 8)):
            if marker == b"" and following and following in b"0123456789abcdefABCDEF" or \
                    marker and following == marker:
                digits = data[i + skip:i + skip + count]
                if len(digits) != count or any(b not in b"0123456789abcdefABCDEF" for b in digits):
                    raise OperationalError("invalid Unicode escape")
                out += _utf8(int(digits, 16) & 0xFFFFFFFF)
                i += skip + count
                break
        else:
            raise OperationalError("invalid Unicode escape")
    return _from_utf8(bytes(out))


# SQLite's sqlite3Utf8Trans1: the value bits of a UTF-8 lead byte from 0xC0 up.
_UTF8_TRANS1 = bytes(list(range(32)) + list(range(16)) + list(range(8)) + [0, 1, 2, 3, 0, 1, 0, 0])


def _utf8_read(data: bytes, i: int) -> tuple[int, int]:
    """SQLite's sqlite3Utf8Read: (code point, index after it).  Invalid
    sequences give U+FFFD; a stray continuation byte gives its own value."""
    c = data[i]
    i += 1
    if c >= 0xC0:
        c = _UTF8_TRANS1[c - 0xC0]
        while i < len(data) and data[i] & 0xC0 == 0x80:
            c = ((c << 6) + (data[i] & 0x3F)) & 0xFFFFFFFF
            i += 1
        if c < 0x80 or (c & 0xFFFFF800) == 0xD800 or (c & 0xFFFFFFFE) == 0xFFFE:
            c = 0xFFFD
    return c, i


def _characters(text: str) -> list[str]:
    """The text split into characters as SQLite's SQLITE_SKIP_UTF8 steps
    through it (differs from Python only for bytes that are not UTF-8)."""
    data = text.encode("utf-8", "surrogateescape")
    out, i = [], 0
    while i < len(data):
        start = i
        i += 1
        if data[start] >= 0xC0:
            while i < len(data) and data[i] & 0xC0 == 0x80:
                i += 1
        out.append(data[start:i].decode("utf-8", "surrogateescape"))
    return out


def unicode(value: SQLValue) -> int | None:
    if value is None:
        return None
    data = _text(value).encode("utf-8", "surrogateescape")
    if not data:
        return None
    return _utf8_read(data, 0)[0]


def octet_length(value: SQLValue) -> int | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return len(value)
    return len(to_text(value).encode("utf-8", "surrogateescape"))


# ---- BLOBs and literals ------------------------------------------------------------


def hex_(value: SQLValue) -> str:
    if value is None:
        return ""
    return values.to_blob(value).hex().upper()


def unhex(value: SQLValue, ignore: SQLValue = "") -> bytes | None:
    """The BLOB that hex text spells; characters in ``ignore`` may separate
    the pairs of digits.  NULL if the text is not hex."""
    if value is None or ignore is None:
        return None
    text, skip = _text(value), _text(ignore)
    digits = []
    for ch in text:
        if ch in "0123456789abcdefABCDEF":
            digits.append(ch)
        elif ch in skip and len(digits) % 2 == 0:
            continue
        else:
            return None
    if len(digits) % 2:
        return None
    return bytes.fromhex("".join(digits))


def quote(value: SQLValue, escape: bool = False) -> str:
    """The value as an SQL literal (SQLite's sqlite3QuoteValue).  With
    ``escape`` (unistr_quote) control characters in text use unistr()."""
    from minidb.printf import sql_printf
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return sql_printf("%!0.17g", [value])  # infinity shows as 9.0e+999
    if isinstance(value, bytes):
        return "X'" + value.hex().upper() + "'"
    return sql_printf("%#Q" if escape else "%Q", [value])


def unistr_quote(value: SQLValue) -> str:
    return quote(value, escape=True)


def zeroblob(size: SQLValue) -> bytes:
    n = to_int64(size) if size is not None else 0
    if n > LENGTH_LIMIT:
        raise OperationalError("string or blob too big")
    return bytes(max(n, 0))


def randomblob(size: SQLValue) -> bytes:
    n = to_int64(size) if size is not None else 1
    return random.randbytes(max(n, 1))


def random_() -> int:
    return random.randint(INT_MIN, INT_MAX)


# ---- numbers ----------------------------------------------------------------------


def round_(value: SQLValue, digits: SQLValue = 0) -> float | None:
    """round(X[, Y]): X rounded to Y decimal places (at most 30), as a REAL.
    SQLite rounds the decimal digits of X (its "%!.*f"), halves away from zero."""
    if value is None or digits is None:
        return None
    n = min(max(to_int64(digits), 0), 30)
    r = _as_real(value)
    if abs(r) >= 4503599627370496.0 or math.isnan(r):
        return r  # no fractional part to round
    if n == 0:
        return float(int(r + (-0.5 if r < 0 else 0.5)))
    from minidb.printf import sql_printf
    return atof(sql_printf("%!.*f", [n, r]))


def sign(value: SQLValue) -> int | None:
    """-1, 0 or 1 for a number (or numeric text); NULL otherwise."""
    value = numeric_type_value(value)
    if value is None or isinstance(value, (str, bytes)):
        return None
    return (value > 0) - (value < 0)


def iif(*args: SQLValue) -> SQLValue:
    """iif(B1, V1 [, B2, V2 ...] [, ELSE]) / if(): the first V whose B is true."""
    for i in range(0, len(args) - 1, 2):
        if values.truth(args[i]):
            return args[i + 1]
    return args[-1] if len(args) % 2 else None


# ---- pattern matching ------------------------------------------------------------


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern | None:
    """GLOB's pattern as a regular expression, following SQLite's
    patternCompare: * ? and sets [...] / [^...] in which a leading ] is a
    member, and "a-z" is a range (a reversed range matches nothing).  A set
    that is never closed matches nothing (None)."""
    parts = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        i += 1
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        elif ch == "[":
            invert = pattern[i:i + 1] == "^"
            if invert:
                i += 1
            members = []
            if pattern[i:i + 1] == "]":
                members.append(re.escape("]"))
                i += 1
            prior = None
            while i < n and pattern[i] != "]":
                c2 = pattern[i]
                i += 1
                if c2 == "-" and i < n and pattern[i] != "]" and prior is not None:
                    high = pattern[i]
                    i += 1
                    if prior <= high:
                        members.append(re.escape(prior) + "-" + re.escape(high))
                    prior = None
                else:
                    members.append(re.escape(c2))
                    prior = c2
            if i >= n:
                return None
            i += 1
            if members:
                parts.append(("[^" if invert else "[") + "".join(members) + "]")
            else:
                parts.append("." if invert else "(?!)")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts), re.DOTALL)


def glob(pattern: SQLValue, value: SQLValue) -> int | None:
    """glob(P, X): X GLOB P."""
    if pattern is None or value is None:
        return None
    regex = _glob_regex(_text(pattern))
    return int(regex is not None and regex.fullmatch(_text(value)) is not None)


@lru_cache(maxsize=256)
def _like_escape_regex(pattern: str, escape: str) -> re.Pattern:
    parts = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        i += 1
        if ch == escape:
            if i < len(pattern):
                parts.append(re.escape(pattern[i]))
                i += 1
            else:
                return re.compile(r"(?!)")  # a trailing escape matches nothing
        elif ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts), re.DOTALL | re.IGNORECASE | re.ASCII)


def like(pattern: SQLValue, value: SQLValue, escape: SQLValue = None) -> int | None:
    """like(P, X[, E]): X LIKE P [ESCAPE E]."""
    if escape is None:
        return values.like(value, pattern)
    if pattern is None or value is None:
        return None
    escape_text = to_text(escape)
    if len(escape_text) != 1:
        raise OperationalError("ESCAPE expression must be a single character")
    regex = _like_escape_regex(_text(pattern), escape_text)
    return int(regex.fullmatch(_text(value)) is not None)


def like_escape(value: SQLValue, pattern: SQLValue, escape: SQLValue) -> int | None:
    """``value LIKE pattern ESCAPE escape`` (NULL escape: NULL)."""
    if escape is None:
        return None
    return like(pattern, value, escape)


# ---- math functions ------------------------------------------------------------------


def _math1(function: Callable[[float], float]) -> Callable[[SQLValue], float | None]:
    """A one-argument math function: NULL for NULL or non-numeric text, and
    for arguments outside its domain."""
    def apply(value: SQLValue) -> float | None:
        value = numeric_type_value(value)
        if value is None or isinstance(value, (str, bytes)):
            return None
        try:
            result = function(float(value))
        except (ValueError, OverflowError):
            return None
        return None if math.isnan(result) else result
    return apply


def _math2(function: Callable[[float, float], float]) -> Callable[[SQLValue, SQLValue], float | None]:
    def apply(a: SQLValue, b: SQLValue) -> float | None:
        a, b = numeric_type_value(a), numeric_type_value(b)
        if a is None or b is None or isinstance(a, (str, bytes)) or isinstance(b, (str, bytes)):
            return None
        try:
            result = function(float(a), float(b))
        except (ValueError, OverflowError, ZeroDivisionError):
            return None
        return None if math.isnan(result) else result
    return apply


def _rounding(function: Callable[[float], float]) -> Callable[[SQLValue], SQLValue]:
    """ceil / floor / trunc: an INTEGER stays as it is, other numbers give a REAL."""
    def apply(value: SQLValue) -> SQLValue:
        value = numeric_type_value(value)
        if value is None or isinstance(value, (str, bytes)):
            return None
        if isinstance(value, int):
            return value
        return float(function(value)) if math.isfinite(value) else value
    return apply


def _log(*args: SQLValue) -> float | None:
    """log(X) is the base-10 logarithm; log(B, X) the base-B one."""
    if len(args) == 1:
        return _math1(lambda x: math.log10(x) if x > 0 else math.nan)(args[0])
    return _math2(lambda b, x: math.log(x) / math.log(b) if x > 0 and b > 0 and b != 1 else math.nan)(*args)


def _power(x: float, y: float) -> float:
    """C's pow(): an overflow (or a zero to a negative power) is an infinity
    that keeps the sign of x when y is an odd integer."""
    try:
        return math.pow(x, y)
    except (OverflowError, ValueError):
        if x < 0 and y != int(y):
            return math.nan
        odd = abs(y) < 2 ** 53 and int(y) % 2 == 1
        return math.copysign(math.inf, x) if odd else math.inf


MATH_FUNCTIONS = {
    "ACOS": (_math1(math.acos), 1, 1),
    "ACOSH": (_math1(math.acosh), 1, 1),
    "ASIN": (_math1(math.asin), 1, 1),
    "ASINH": (_math1(math.asinh), 1, 1),
    "ATAN": (_math1(math.atan), 1, 1),
    "ATAN2": (_math2(math.atan2), 2, 2),
    "ATANH": (_math1(lambda x: math.copysign(math.inf, x) if abs(x) == 1 else math.atanh(x)), 1, 1),
    "CEIL": (_rounding(math.ceil), 1, 1),
    "CEILING": (_rounding(math.ceil), 1, 1),
    "COS": (_math1(math.cos), 1, 1),
    "COSH": (_math1(lambda x: math.cosh(x) if abs(x) < 710 else math.inf), 1, 1),
    "DEGREES": (_math1(math.degrees), 1, 1),
    "EXP": (_math1(lambda x: math.exp(x) if x < 710 else math.inf), 1, 1),
    "FLOOR": (_rounding(math.floor), 1, 1),
    "LN": (_math1(lambda x: math.log(x) if x > 0 else math.nan), 1, 1),
    "LOG": (_log, 1, 2),
    "LOG10": (_math1(lambda x: math.log10(x) if x > 0 else math.nan), 1, 1),
    "LOG2": (_math1(lambda x: math.log2(x) if x > 0 else math.nan), 1, 1),
    "MOD": (_math2(math.fmod), 2, 2),
    "PI": (lambda: math.pi, 0, 0),
    "POW": (_math2(_power), 2, 2),
    "POWER": (_math2(_power), 2, 2),
    "RADIANS": (_math1(math.radians), 1, 1),
    "SIN": (_math1(math.sin), 1, 1),
    "SINH": (_math1(lambda x: math.sinh(x) if abs(x) < 710 else math.copysign(math.inf, x)), 1, 1),
    "SQRT": (_math1(lambda x: math.sqrt(x) if x >= 0 else math.nan), 1, 1),
    "TAN": (_math1(math.tan), 1, 1),
    "TANH": (_math1(math.tanh), 1, 1),
    "TRUNC": (_rounding(math.trunc), 1, 1),
}


def _printf(*args: SQLValue) -> str | None:
    from minidb.printf import sql_printf
    if not args or args[0] is None:
        return None
    return sql_printf(to_text(args[0]), list(args[1:]))


# name -> (function, minimum argument count, maximum argument count or None)
SCALAR_FUNCTIONS = {
    **values.SCALAR_FUNCTIONS,
    **jsonfuncs.SCALAR_FUNCTIONS,
    **MATH_FUNCTIONS,
    **DATE_FUNCTIONS,
    "CHAR": (char, 0, None),
    "CONCAT": (concat, 1, None),
    "CONCAT_WS": (concat_ws, 2, None),
    "FORMAT": (_printf, 0, None),
    "GLOB": (glob, 2, 2),
    "HEX": (hex_, 1, 1),
    "IF": (iif, 2, None),
    "IIF": (iif, 2, None),
    "INSTR": (instr, 2, 2),
    "LIKE": (like, 2, 3),
    "LIKELIHOOD": (lambda value, _: value, 2, 2),  # (hints for SQLite's planner: the value itself)
    "LIKELY": (lambda value: value, 1, 1),
    "LTRIM": (ltrim, 1, 2),
    "OCTET_LENGTH": (octet_length, 1, 1),
    "PRINTF": (_printf, 0, None),
    "QUOTE": (quote, 1, 1),
    "RANDOM": (random_, 0, 0),
    "RANDOMBLOB": (randomblob, 1, 1),
    "REPLACE": (replace, 3, 3),
    "ROUND": (round_, 1, 2),
    "RTRIM": (rtrim, 1, 2),
    "SIGN": (sign, 1, 1),
    "SUBSTR": (substr, 2, 3),
    "SUBSTRING": (substr, 2, 3),
    "TRIM": (trim, 1, 2),
    "UNHEX": (unhex, 1, 2),
    "UNICODE": (unicode, 1, 1),
    "UNLIKELY": (lambda value: value, 1, 1),
    "UNISTR": (unistr, 1, 1),
    "UNISTR_QUOTE": (unistr_quote, 1, 1),
    "ZEROBLOB": (zeroblob, 1, 1),
}

# Functions whose result changes from call to call: never folded or cached.
NONDETERMINISTIC = frozenset(("RANDOM", "RANDOMBLOB", "CHANGES", "TOTAL_CHANGES", "LAST_INSERT_ROWID"))
