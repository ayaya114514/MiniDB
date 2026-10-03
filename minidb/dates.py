"""SQLite's date and time functions: a port of date.c (SQLite 3.53).

julianday(), unixepoch(), date(), time(), datetime(), strftime(),
timediff(), current_date / current_time / current_timestamp.

Times are julian day numbers times 86,400,000 (milliseconds); the
Gregorian calendar is used for all dates from -4713-11-24 to 9999-12-31.
The functions take a time value (an ISO-8601 date and/or time, "now", a
julian day number or, with a modifier, a unix time) and any number of
modifiers ("+3 days", "start of month", "weekday 0", "localtime", ...).

The arithmetic follows the C code exactly: C's integer division and
remainder truncate toward zero (``_div``, ``_rem``), and the limits of the
"NNN units" modifiers are C floats.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable

from minidb import values
from minidb.errors import OperationalError
from minidb.fp import sql_atof
from minidb.values import SQLValue

MAX_JD = 464269060799999  # 9999-12-31 23:59:59.999
UNIX_EPOCH_JD_MS = 210866760000000  # 1970-01-01 as julian day * 86400000

# The current time for "now": fixed for the length of a statement (Executor.execute resets it).
statement_time: list[int | None] = [None]
# Where a value being computed must not depend on the time ("a generated
# column", "a CHECK constraint"), or None: "now", "localtime" and "utc" are
# then an error (SQLite's sqlite3NotPureFunc).
pure_context: list[str | None] = [None]


class _NotPure(Exception):
    pass


def _check_pure() -> None:
    if pure_context[0] is not None:
        raise _NotPure


def _div(a: int, b: int) -> int:
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _rem(a: int, b: int) -> int:
    return a - _div(a, b) * b


def _float32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


# "NNN units" modifiers: name, limit (a C float), seconds per unit.
_UNITS = [
    ("second", _float32(4.6427e+14), 1.0),
    ("minute", _float32(7.7379e+12), 60.0),
    ("hour", _float32(1.2897e+11), 3600.0),
    ("day", _float32(5373485.0), 86400.0),
    ("month", _float32(176546.0), 2592000.0),
    ("year", _float32(14713.0), 31536000.0),
]


class DateTime:
    __slots__ = ("iJD", "Y", "M", "D", "h", "m", "tz", "s", "validJD", "validYMD", "validHMS",
                 "nFloor", "rawS", "isError", "useSubsec", "isUtc", "isLocal")

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.iJD = self.Y = self.M = self.D = self.h = self.m = self.tz = 0
        self.s = 0.0
        self.validJD = self.validYMD = self.validHMS = False
        self.nFloor = 0
        self.rawS = self.isError = self.useSubsec = self.isUtc = self.isLocal = False

    def copy(self) -> DateTime:
        other = DateTime()
        for name in self.__slots__:
            setattr(other, name, getattr(self, name))
        return other


def _error(p: DateTime) -> None:
    p.clear()
    p.isError = True


def _digits(text: str, i: int, spec: str) -> list[int] | None:
    """SQLite's getDigits: fields like "40f-21a-21d" (count, minimum, maximum
    code, separator).  Returns the values of the fields converted, in order
    (fewer than asked if one failed)."""
    maxima = {"a": 12, "b": 14, "c": 24, "d": 31, "e": 59, "f": 14712}
    result = []
    while True:
        count, minimum, maximum = int(spec[0]), int(spec[1]), maxima[spec[2]]
        separator = spec[3] if len(spec) > 3 else ""
        value = 0
        for _ in range(count):
            if i >= len(text) or not text[i].isdigit() or not text[i].isascii():
                return result
            value = value * 10 + ord(text[i]) - 48
            i += 1
        following = text[i] if i < len(text) else ""
        if value < minimum or value > maximum or (separator and separator != following):
            return result
        result.append(value)
        i += 1
        if not separator:
            return result
        spec = spec[4:]


def _is_space(ch: str) -> bool:
    return ch in " \t\n\v\f\r"


def _parse_timezone(text: str, i: int, p: DateTime) -> bool:
    """Parse [+-]HH:MM or Z after a time; True on error."""
    while i < len(text) and _is_space(text[i]):
        i += 1
    p.tz = 0
    c = text[i] if i < len(text) else ""
    if c in ("Z", "z"):
        i += 1
        p.isLocal, p.isUtc = False, True
    elif c in ("+", "-"):
        sign = -1 if c == "-" else 1
        found = _digits(text, i + 1, "20b:20e")
        if len(found) != 2:
            return True
        i += 6
        p.tz = sign * (found[1] + found[0] * 60)
        if p.tz == 0:
            p.isLocal, p.isUtc = False, True
    else:
        return c != ""
    while i < len(text) and _is_space(text[i]):
        i += 1
    return i < len(text)


def _parse_hms(text: str, i: int, p: DateTime) -> bool:
    """HH:MM, HH:MM:SS or HH:MM:SS.FFF (then a time zone); True on error."""
    found = _digits(text, i, "20c:20e")
    if len(found) != 2:
        return True
    h, m = found
    i += 5
    ms = 0.0
    if i < len(text) and text[i] == ":":
        i += 1
        found = _digits(text, i, "20e")
        if len(found) != 1:
            return True
        s = found[0]
        i += 2
        if i + 1 < len(text) and text[i] == "." and text[i + 1].isdigit() and text[i + 1].isascii():
            scale = 1.0
            i += 1
            while i < len(text) and text[i].isdigit() and text[i].isascii():
                ms = ms * 10.0 + ord(text[i]) - 48
                scale *= 10.0
                i += 1
            ms /= scale
            if ms > 0.999:
                ms = 0.999
    else:
        s = 0
    p.validJD = False
    p.rawS = False
    p.validHMS = True
    p.h, p.m = h, m
    p.s = s + ms
    return _parse_timezone(text, i, p)


def _compute_jd(p: DateTime) -> None:
    if p.validJD:
        return
    if p.validYMD:
        Y, M, D = p.Y, p.M, p.D
    else:
        Y, M, D = 2000, 1, 1
    if Y < -4713 or Y > 9999 or p.rawS:
        _error(p)
        return
    if M <= 2:
        Y -= 1
        M += 12
    A = _div(Y + 4800, 100)
    B = 38 - A + _div(A, 4)
    X1 = _div(36525 * (Y + 4716), 100)
    X2 = _div(306001 * (M + 1), 10000)
    p.iJD = int((X1 + X2 + D + B - 1524.5) * 86400000)
    p.validJD = True
    if p.validHMS:
        p.iJD += p.h * 3600000 + p.m * 60000 + int(p.s * 1000 + 0.5)
        if p.tz:
            p.iJD -= p.tz * 60000
            p.validYMD = p.validHMS = False
            p.tz = 0
            p.isUtc, p.isLocal = True, False


def _compute_floor(p: DateTime) -> None:
    if p.D <= 28:
        p.nFloor = 0
    elif (1 << p.M) & 0x15AA:
        p.nFloor = 0
    elif p.M != 2:
        p.nFloor = int(p.D == 31)
    elif p.Y % 4 != 0 or (p.Y % 100 == 0 and p.Y % 400 != 0):
        p.nFloor = p.D - 28
    else:
        p.nFloor = p.D - 29


def _parse_ymd(text: str, p: DateTime) -> bool:
    i = 0
    negative = text.startswith("-")
    if negative:
        i = 1
    found = _digits(text, i, "40f-21a-21d")
    if len(found) != 3:
        return True
    Y, M, D = found
    i += 10
    while i < len(text) and (_is_space(text[i]) or text[i] == "T"):
        i += 1
    if not _parse_hms(text, i, p):
        pass
    elif i >= len(text):
        p.validHMS = False
    else:
        return True
    p.validJD = False
    p.validYMD = True
    p.Y, p.M, p.D = (-Y if negative else Y), M, D
    _compute_floor(p)
    if p.tz:
        _compute_jd(p)
    return False


def current_time_ms() -> int:
    """'now' as julian day * 86400000, the same throughout a statement."""
    if statement_time[0] is None:
        statement_time[0] = int(time.time() * 1000) + UNIX_EPOCH_JD_MS
    return statement_time[0]


def _set_now(p: DateTime) -> bool:
    _check_pure()
    p.iJD = current_time_ms()
    p.validJD = True
    p.isUtc, p.isLocal = True, False
    _clear_ymd_hms_tz(p)
    return False


def _set_raw_number(p: DateTime, r: float) -> None:
    p.s = r
    p.rawS = True
    if 0.0 <= r < 5373484.5:
        p.iJD = int(r * 86400000.0 + 0.5)
        p.validJD = True


def _parse_date_or_time(text: str, p: DateTime) -> bool:
    if not _parse_ymd(text, p):
        return False
    if not _parse_hms(text, 0, p):
        return False
    if values.ascii_lower(text) == "now":
        return _set_now(p)
    r, rc = sql_atof(text)
    if rc > 0:
        _set_raw_number(p, r)
        return False
    if values.ascii_lower(text) in ("subsec", "subsecond"):
        p.useSubsec = True
        return _set_now(p)
    return True


def _valid_jd(iJD: int) -> bool:
    return 0 <= iJD <= MAX_JD


def _compute_ymd(p: DateTime) -> None:
    if p.validYMD:
        return
    if not p.validJD:
        p.Y, p.M, p.D = 2000, 1, 1
    elif not _valid_jd(p.iJD):
        _error(p)
        return
    else:
        Z = _div(p.iJD + 43200000, 86400000)
        alpha = int((Z + 32044.75) / 36524.25) - 52
        A = Z + 1 + alpha - _div(alpha + 100, 4) + 25
        B = A + 1524
        C = int((B - 122.1) / 365.25)
        D = _div(36525 * (C & 32767), 100)
        E = int((B - D) / 30.6001)
        X1 = int(30.6001 * E)
        p.D = B - D - X1
        p.M = E - 1 if E < 14 else E - 13
        p.Y = C - 4716 if p.M > 2 else C - 4715
    p.validYMD = True


def _compute_hms(p: DateTime) -> None:
    if p.validHMS:
        return
    _compute_jd(p)
    day_ms = _rem(p.iJD + 43200000, 86400000)
    p.s = _rem(day_ms, 60000) / 1000.0
    day_min = _div(day_ms, 60000)
    p.m = day_min % 60
    p.h = day_min // 60
    p.rawS = False
    p.validHMS = True


def _compute_ymd_hms(p: DateTime) -> None:
    _compute_ymd(p)
    _compute_hms(p)


def _clear_ymd_hms_tz(p: DateTime) -> None:
    p.validYMD = p.validHMS = False
    p.tz = 0


def _to_localtime(p: DateTime) -> None:
    """Move a UTC time to local time (C's localtime(), with years outside
    1970-2037 mapped into that range and back, as SQLite does)."""
    _compute_jd(p)
    if p.iJD < 2108667600 * 100000 or p.iJD > 2130141456 * 100000:
        x = p.copy()
        _compute_ymd_hms(x)
        year_diff = (2000 + _rem(x.Y, 4)) - x.Y
        x.Y += year_diff
        x.validJD = False
        _compute_jd(x)
        t = _div(x.iJD, 1000) - 21086676 * 10000
    else:
        year_diff = 0
        t = _div(p.iJD, 1000) - 21086676 * 10000
    try:
        local = time.localtime(t)
    except (OverflowError, OSError, ValueError):
        raise OperationalError("local time unavailable") from None
    p.Y = local.tm_year - year_diff
    p.M = local.tm_mon
    p.D = local.tm_mday
    p.h = local.tm_hour
    p.m = local.tm_min
    p.s = local.tm_sec + _rem(p.iJD, 1000) * 0.001
    p.validYMD = p.validHMS = True
    p.validJD = False
    p.rawS = False
    p.tz = 0
    p.isError = False


def _auto_adjust(p: DateTime) -> None:
    if not p.rawS or p.validJD:
        p.rawS = False
    elif -21086676 * 10000 <= p.s <= 25340230 * 10000 + 799:
        r = p.s * 1000.0 + 210866760000000.0
        _clear_ymd_hms_tz(p)
        p.iJD = int(r + 0.5)
        p.validJD = True
        p.rawS = False


def _parse_modifier(z: str, p: DateTime, idx: int) -> bool:
    """Apply one modifier; True if it is not a valid modifier here."""
    lowered = values.ascii_lower(z)
    first = lowered[:1]
    if first == "a":
        if lowered == "auto":
            if idx > 1:
                return True
            _auto_adjust(p)
            return False
        return True
    if first == "c":
        if lowered == "ceiling":
            _compute_jd(p)
            _clear_ymd_hms_tz(p)
            p.nFloor = 0
            return False
        return True
    if first == "f":
        if lowered == "floor":
            _compute_jd(p)
            p.iJD -= p.nFloor * 86400000
            _clear_ymd_hms_tz(p)
            return False
        return True
    if first == "j":
        if lowered == "julianday":
            if idx > 1:
                return True
            if p.validJD and p.rawS:
                p.rawS = False
                return False
        return True
    if first == "l":
        if lowered == "localtime":
            _check_pure()
            if not p.isLocal:
                _to_localtime(p)
            p.isUtc, p.isLocal = False, True
            return False
        return True
    if first == "u":
        if lowered == "unixepoch" and p.rawS:
            if idx > 1:
                return True
            r = p.s * 1000.0 + 210866760000000.0
            if 0.0 <= r < 464269060800000.0:
                _clear_ymd_hms_tz(p)
                p.iJD = int(r + 0.5)
                p.validJD = True
                p.rawS = False
                return False
            return True
        if lowered == "utc":
            _check_pure()
            if not p.isUtc:
                _compute_jd(p)
                guess = original = p.iJD
                err = 0
                count = 0
                while True:
                    guess -= err
                    new = DateTime()
                    new.iJD = guess
                    new.validJD = True
                    _to_localtime(new)
                    _compute_jd(new)
                    err = new.iJD - original
                    if not err or count >= 3:
                        break
                    count += 1
                p.clear()
                p.iJD = guess
                p.validJD = True
                p.isUtc, p.isLocal = True, False
            return False
        return True
    if first == "w":
        if lowered.startswith("weekday "):
            r, rc = sql_atof(z[8:])
            if rc > 0 and 0.0 <= r < 7.0 and int(r) == r:
                n = int(r)
                _compute_ymd_hms(p)
                p.tz = 0
                p.validJD = False
                _compute_jd(p)
                Z = _rem(_div(p.iJD + 129600000, 86400000), 7)
                if Z > n:
                    Z -= 7
                p.iJD += (n - Z) * 86400000
                _clear_ymd_hms_tz(p)
                return False
        return True
    if first == "s":
        if not lowered.startswith("start of "):
            if lowered in ("subsec", "subsecond"):
                p.useSubsec = True
                return False
            return True
        if not p.validJD and not p.validYMD and not p.validHMS:
            return True
        rest = lowered[9:]
        _compute_ymd(p)
        p.validHMS = True
        p.h = p.m = 0
        p.s = 0.0
        p.rawS = False
        p.tz = 0
        p.validJD = False
        if rest == "month":
            p.D = 1
            return False
        if rest == "year":
            p.M = 1
            p.D = 1
            return False
        return rest != "day"
    if first and first in "+-0123456789":
        return _numeric_modifier(z, p)
    return True


def _numeric_modifier(z: str, p: DateTime) -> bool:
    """"+NNN days", "+YYYY-MM-DD[ HH:MM[:SS.SSS]]" or "+HH:MM[:SS.SSS]"."""
    z0 = z[0]
    n = 1
    while n < len(z):
        c = z[n]
        if c == ":" or _is_space(c):
            break
        if c == "-":
            if n == 5 and len(_digits(z, 1, "40f")) == 1:
                break
            if n == 6 and len(_digits(z, 1, "50f")) == 1:
                break
        n += 1
    r, rc = sql_atof(z[:n])
    if rc <= 0:
        return True
    z2 = z
    at = n  # position in z2 of what follows the number
    if n < len(z) and z[n] == "-":
        # (+|-)YYYY-MM-DD adds or subtracts years, months (0-11) and days (0-30)
        if z0 not in "+-":
            return True
        if n == 5:
            found = _digits(z, 1, "40f-20a-20d")
        else:
            found = _digits(z, 1, "50f-20a-20d")
            z = z[1:]
        if len(found) != 3:
            return True
        Y, M, D = found
        if M >= 12 or D >= 31:
            return True
        _compute_ymd_hms(p)
        p.validJD = False
        if z0 == "-":
            p.Y -= Y
            p.M -= M
            D = -D
        else:
            p.Y += Y
            p.M += M
        x = _div(p.M - 1, 12) if p.M > 0 else _div(p.M - 12, 12)
        p.Y += x
        p.M -= x * 12
        _compute_floor(p)
        _compute_jd(p)
        p.validHMS = p.validYMD = False
        p.iJD += D * 86400000
        if len(z) <= 11:
            return False
        if _is_space(z[11]) and len(_digits(z, 12, "20c:20e")) == 2:
            z2 = z[12:]
            at = 2
        else:
            return True
    if at < len(z2) and z2[at] == ":":
        # (+|-)HH:MM:SS.FFF adds or subtracts a time
        start = 0 if z2[0].isdigit() else 1
        tx = DateTime()
        if _parse_hms(z2, start, tx):
            return True
        _compute_jd(tx)
        tx.iJD -= 43200000
        day = _div(tx.iJD, 86400000)
        tx.iJD -= day * 86400000
        if z0 == "-":
            tx.iJD = -tx.iJD
        _compute_jd(p)
        _clear_ymd_hms_tz(p)
        p.iJD += tx.iJD
        return False
    # "+NNN units"
    rest = z[n:].lstrip(" \t\n\v\f\r")
    length = len(rest.encode("utf-8", "surrogateescape"))
    if length < 3 or length > 10:
        return True
    unit = values.ascii_lower(rest)
    if unit.endswith("s"):
        unit = unit[:-1]
    _compute_jd(p)
    rounder = -0.5 if r < 0 else 0.5
    p.nFloor = 0
    failed = True
    for i, (name, limit, seconds) in enumerate(_UNITS):
        if unit == name and -limit < r < limit:
            if i == 4:  # months
                _compute_ymd_hms(p)
                p.M += int(r)
                x = _div(p.M - 1, 12) if p.M > 0 else _div(p.M - 12, 12)
                p.Y += x
                p.M -= x * 12
                _compute_floor(p)
                p.validJD = False
                r -= int(r)
            elif i == 5:  # years
                _compute_ymd_hms(p)
                p.Y += int(r)
                _compute_floor(p)
                p.validJD = False
                r -= int(r)
            _compute_jd(p)
            p.iJD += int(r * 1000.0 * seconds + rounder)
            failed = False
            break
    _clear_ymd_hms_tz(p)
    return failed


def _is_date(args: tuple[SQLValue, ...]) -> DateTime | None:
    """The time value and modifiers applied, or None if they are invalid."""
    p = DateTime()
    if not args:
        _set_now(p)
        return p
    value = args[0]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _set_raw_number(p, float(value))
    else:
        if value is None:
            return None
        if _parse_date_or_time(values._c_string(values.to_text(value)), p):
            return None
    for i, modifier in enumerate(args[1:], 1):
        if modifier is None or _parse_modifier(values._c_string(values.to_text(modifier)), p, i):
            return None
    _compute_jd(p)
    if p.isError or not _valid_jd(p.iJD):
        return None
    if len(args) == 1 and p.validYMD and p.D > 28:
        p.validYMD = False  # normalize: 2023-02-31 is 2023-03-03
    return p


def julianday(*args: SQLValue) -> float | None:
    p = _is_date(args)
    if p is None:
        return None
    _compute_jd(p)
    return p.iJD / 86400000.0


def unixepoch(*args: SQLValue) -> int | float | None:
    p = _is_date(args)
    if p is None:
        return None
    _compute_jd(p)
    if p.useSubsec:
        return (p.iJD - UNIX_EPOCH_JD_MS) / 1000.0
    return _div(p.iJD, 1000) - 21086676 * 10000


def _year(Y: int) -> str:
    text = f"{abs(Y) % 10000:04d}"
    return "-" + text if Y < 0 else text


def _seconds(p: DateTime) -> str:
    if p.useSubsec:
        s = int(1000.0 * p.s + 0.5)
        return f"{(s // 10000) % 10}{(s // 1000) % 10}.{(s // 100) % 10}{(s // 10) % 10}{s % 10}"
    return f"{int(p.s) % 100:02d}"


def datetime_(*args: SQLValue) -> str | None:
    p = _is_date(args)
    if p is None:
        return None
    _compute_ymd_hms(p)
    return f"{_year(p.Y)}-{p.M % 100:02d}-{p.D % 100:02d} {p.h % 100:02d}:{p.m % 100:02d}:{_seconds(p)}"


def time_(*args: SQLValue) -> str | None:
    p = _is_date(args)
    if p is None:
        return None
    _compute_hms(p)
    return f"{p.h % 100:02d}:{p.m % 100:02d}:{_seconds(p)}"


def date(*args: SQLValue) -> str | None:
    p = _is_date(args)
    if p is None:
        return None
    _compute_ymd(p)
    return f"{_year(p.Y)}-{p.M % 100:02d}-{p.D % 100:02d}"


def _days_after_jan01(p: DateTime) -> int:
    jan01 = p.copy()
    jan01.validJD = False
    jan01.M = jan01.D = 1
    _compute_jd(jan01)
    return _div(p.iJD - jan01.iJD + 43200000, 86400000)


def _days_after_monday(p: DateTime) -> int:
    return _rem(_div(p.iJD + 43200000, 86400000), 7)


def _days_after_sunday(p: DateTime) -> int:
    return _rem(_div(p.iJD + 129600000, 86400000), 7)


def strftime(*args: SQLValue) -> str | None:
    """strftime(FORMAT, TIME, MOD, ...): %d %e %f %F %G %g %H %k %I %l %j %J
    %m %M %p %P %R %s %S %T %u %w %U %V %W %Y %%; any other conversion gives NULL."""
    from minidb.printf import sql_printf
    if not args or args[0] is None:
        return None
    fmt = values._c_string(values.to_text(args[0]))
    p = _is_date(args[1:])
    if p is None:
        return None
    _compute_jd(p)
    _compute_ymd_hms(p)
    out = []
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch != "%":
            out.append(ch)
            i += 1
            continue
        cf = fmt[i + 1] if i + 1 < len(fmt) else ""
        i += 2
        if cf in ("d", "e"):
            out.append(f"{p.D:02d}" if cf == "d" else f"{p.D:2d}")
        elif cf == "f":
            out.append(sql_printf("%06.3f", [min(p.s, 59.999)]))
        elif cf == "F":
            out.append(f"{p.Y:04d}-{p.M:02d}-{p.D:02d}")
        elif cf in ("G", "g"):
            y = p.copy()
            y.iJD += (3 - _days_after_monday(p)) * 86400000
            y.validYMD = False
            _compute_ymd(y)
            out.append(f"{_rem(y.Y, 100):02d}" if cf == "g" else f"{y.Y:04d}")
        elif cf in ("H", "k"):
            out.append(f"{p.h:02d}" if cf == "H" else f"{p.h:2d}")
        elif cf in ("I", "l"):
            h = p.h - 12 if p.h > 12 else p.h
            h = 12 if h == 0 else h
            out.append(f"{h:02d}" if cf == "I" else f"{h:2d}")
        elif cf == "j":
            out.append(f"{_days_after_jan01(p) + 1:03d}")
        elif cf == "J":
            out.append(sql_printf("%.16g", [p.iJD / 86400000.0]))
        elif cf == "m":
            out.append(f"{p.M:02d}")
        elif cf == "M":
            out.append(f"{p.m:02d}")
        elif cf in ("p", "P"):
            out.append(("PM" if cf == "p" else "pm") if p.h >= 12 else ("AM" if cf == "p" else "am"))
        elif cf == "R":
            out.append(f"{p.h:02d}:{p.m:02d}")
        elif cf == "s":
            if p.useSubsec:
                out.append(sql_printf("%.3f", [(p.iJD - UNIX_EPOCH_JD_MS) / 1000.0]))
            else:
                out.append(str(_div(p.iJD, 1000) - 21086676 * 10000))
        elif cf == "S":
            out.append(f"{int(p.s):02d}")
        elif cf == "T":
            out.append(f"{p.h:02d}:{p.m:02d}:{int(p.s):02d}")
        elif cf in ("u", "w"):
            day = _days_after_sunday(p)
            out.append("7" if day == 0 and cf == "u" else str(day))
        elif cf == "U":
            out.append(f"{_div(_days_after_jan01(p) - _days_after_sunday(p) + 7, 7):02d}")
        elif cf == "V":
            y = p.copy()
            y.iJD += (3 - _days_after_monday(p)) * 86400000
            y.validYMD = False
            _compute_ymd(y)
            out.append(f"{_div(_days_after_jan01(y), 7) + 1:02d}")
        elif cf == "W":
            out.append(f"{_div(_days_after_jan01(p) - _days_after_monday(p) + 7, 7):02d}")
        elif cf == "Y":
            out.append(f"{p.Y:04d}")
        elif cf == "%":
            out.append("%")
        else:
            return None
    return "".join(out)


def timediff(a: SQLValue, b: SQLValue) -> str | None:
    """The time to add to B to get A: [+-]YYYY-MM-DD HH:MM:SS.SSS."""
    from minidb.printf import sql_printf
    d1, d2 = _is_date((a,)), _is_date((b,))
    if d1 is None or d2 is None:
        return None
    _compute_ymd_hms(d1)
    _compute_ymd_hms(d2)
    if d1.iJD >= d2.iJD:
        sign = "+"
        Y = d1.Y - d2.Y
        if Y:
            d2.Y = d1.Y
            d2.validJD = False
            _compute_jd(d2)
        M = d1.M - d2.M
        if M < 0:
            Y -= 1
            M += 12
        if M != 0:
            d2.M = d1.M
            d2.validJD = False
            _compute_jd(d2)
        while d1.iJD < d2.iJD:
            M -= 1
            if M < 0:
                M = 11
                Y -= 1
            d2.M -= 1
            if d2.M < 1:
                d2.M = 12
                d2.Y -= 1
            d2.validJD = False
            _compute_jd(d2)
        d1.iJD -= d2.iJD
    else:
        sign = "-"
        Y = d2.Y - d1.Y
        if Y:
            d2.Y = d1.Y
            d2.validJD = False
            _compute_jd(d2)
        M = d2.M - d1.M
        if M < 0:
            Y -= 1
            M += 12
        if M != 0:
            d2.M = d1.M
            d2.validJD = False
            _compute_jd(d2)
        while d1.iJD > d2.iJD:
            M -= 1
            if M < 0:
                M = 11
                Y -= 1
            d2.M += 1
            if d2.M > 12:
                d2.M = 1
                d2.Y += 1
            d2.validJD = False
            _compute_jd(d2)
        d1.iJD = d2.iJD - d1.iJD
    d1.iJD += 1486995408 * 100000
    _clear_ymd_hms_tz(d1)
    _compute_ymd_hms(d1)
    return sql_printf("%c%04d-%02d-%02d %02d:%02d:%06.3f", [sign, Y, M, d1.D - 1, d1.h, d1.m, d1.s])


def _pure_checked(name: str, function: Callable[..., SQLValue]) -> Callable[..., SQLValue]:
    """``function`` reporting a use of the time where it must not be used."""
    def checked(*args: SQLValue) -> SQLValue:
        try:
            return function(*args)
        except _NotPure:
            raise OperationalError(f"non-deterministic use of {name}() in {pure_context[0]}") from None
    return checked


DATE_FUNCTIONS = {
    "JULIANDAY": (_pure_checked("julianday", julianday), 0, None),
    "UNIXEPOCH": (_pure_checked("unixepoch", unixepoch), 0, None),
    "DATE": (_pure_checked("date", date), 0, None),
    "TIME": (_pure_checked("time", time_), 0, None),
    "DATETIME": (_pure_checked("datetime", datetime_), 0, None),
    "STRFTIME": (_pure_checked("strftime", strftime), 0, None),
    "TIMEDIFF": (_pure_checked("timediff", timediff), 2, 2),
    "CURRENT_DATE": (date, 0, 0),
    "CURRENT_TIME": (time_, 0, 0),
    "CURRENT_TIMESTAMP": (datetime_, 0, 0),
}
