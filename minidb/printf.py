"""SQLite's printf(): the SQL function printf() / format().

A port of sqlite3_str_vappendf() (printf.c of SQLite 3.53) with SQL values
as arguments: flags ``- + space # ! 0 ,``, width and precision (``*`` takes
them from the arguments), conversions ``d i u x X o p r c s z q Q w f e E g
G n %``.  Widths count bytes (characters with ``!``).  An unknown conversion
ends the output.  Missing arguments count as 0, 0.0 or no text.

The digits of floating point conversions come from fp.fp_decode, SQLite's
own algorithm, not from C's printf.
"""

from __future__ import annotations

from minidb import fp, values
from minidb.errors import OperationalError
from minidb.values import SQLValue

_OUTPUT_LIMIT = 100_000_000  # refuse widths and precisions that would build huge strings
_PREFIXES = {"x": b"0x", "X": b"0X", "o": b"0", "p": b"0x"}


class _Arguments:
    """The SQL values after the format, read as SQLite's getIntArg,
    getDoubleArg and getTextArg do."""

    def __init__(self, args: list[SQLValue]) -> None:
        self.args = args
        self.used = 0

    def _next(self) -> tuple[bool, SQLValue]:
        if self.used >= len(self.args):
            return False, None
        self.used += 1
        return True, self.args[self.used - 1]

    def integer(self) -> int:
        present, value = self._next()
        return values.to_int64(value) if present and value is not None else 0

    def real(self) -> float:
        present, value = self._next()
        if not present or value is None:
            return 0.0
        return float(values.numeric_prefix(value))

    def text(self) -> bytes | None:
        """The argument as UTF-8 bytes up to a NUL (None for NULL or missing)."""
        present, value = self._next()
        if not present or value is None:
            return None
        data = values.to_text(value).encode("utf-8", "surrogateescape")
        return data.split(b"\x00", 1)[0]


class _Spec:
    __slots__ = ("leftjustify", "prefix", "alternate", "altform2", "zeropad", "thousands",
                 "width", "precision")

    def __init__(self) -> None:
        self.leftjustify = self.alternate = self.altform2 = self.zeropad = self.thousands = False
        self.prefix = b""
        self.width = 0
        self.precision = -1


def _integer(conversion: str, args: _Arguments, spec: _Spec) -> bytes:
    value = args.integer()
    if conversion in "dir":
        if value < 0:
            number, prefix = -value, b"-"
        else:
            number, prefix = value, spec.prefix
    else:
        number, prefix = value & fp.U64, b""
    alternate = spec.alternate and number != 0
    thousands = spec.thousands and conversion in "diu"
    precision = spec.precision
    if spec.zeropad and precision < spec.width - (prefix != b""):
        precision = spec.width - (prefix != b"")
    if conversion == "x":
        digits = format(number, "x")
    elif conversion in "Xp":
        digits = format(number, "X")
    elif conversion == "o":
        digits = format(number, "o")
    else:
        digits = str(number)
    if conversion == "r":  # ordinal: 1st, 2nd, 3rd, 4th, ... 11th, 12th, 13th
        x = number % 10
        if x >= 4 or (number // 10) % 10 == 1:
            x = 0
        digits += "thstndrd"[x * 2:x * 2 + 2]
    if precision > len(digits):
        digits = "0" * (precision - len(digits)) + digits
    if thousands:
        head = (len(digits) - 1) % 3 + 1
        digits = ",".join([digits[:head]] + [digits[i:i + 3] for i in range(head, len(digits), 3)])
    text = prefix + digits.encode()
    if alternate and conversion in _PREFIXES:
        text = _PREFIXES[conversion] + text
    return text


def _float(conversion: str, args: _Arguments, spec: _Spec) -> bytes:
    value = args.real()
    precision = 6 if spec.precision < 0 else spec.precision
    kind = {"f": "float", "e": "exp", "E": "exp", "g": "generic", "G": "generic"}[conversion]
    if kind == "float":
        round_to = -precision
    elif kind == "generic":
        if precision == 0:
            precision = 1
        round_to = precision
    else:
        round_to = precision + 1
    sign, digits, point, special = fp.fp_decode(value, round_to, 20 if spec.altform2 else 16)
    if special:
        if special == 2:
            return b"null" if spec.zeropad else b"NaN"
        if spec.zeropad:
            digits, point = "9", 1000
        else:
            if sign == "-":
                return b"-Inf"
            return spec.prefix + b"Inf" if spec.prefix else b"Inf"
    if sign == "-":
        # %#f of a value that shows as zero drops the minus sign
        zero = spec.alternate and not spec.prefix and kind == "float" and point <= round_to
        prefix = b"" if zero else b"-"
    else:
        prefix = spec.prefix
    exponent = point - 1
    if kind == "generic":
        precision -= 1
        remove_zeros = not spec.alternate
        if exponent < -4 or exponent > precision:
            kind = "exp"
        else:
            precision -= exponent
            kind = "float"
    else:
        remove_zeros = spec.altform2
    e2 = 0 if kind == "exp" else point - 1
    has_point = precision > 0 or spec.alternate or spec.altform2
    out = [prefix.decode()]
    j = 0
    if e2 < 0:
        out.append("0")
    elif spec.thousands:
        while e2 >= 0:
            out.append(digits[j] if j < len(digits) else "0")
            if j < len(digits):
                j += 1
            if e2 % 3 == 0 and e2 > 1:
                out.append(",")
            e2 -= 1
    else:
        j = min(e2 + 1, len(digits))
        out.append(digits[:j])
        e2 -= j
        if e2 >= 0:
            out.append("0" * (e2 + 1))
            e2 = -1
    if has_point:
        out.append(".")
    if e2 < -1 and precision > 0:
        nn = min(-1 - e2, precision)
        out.append("0" * nn)
        precision -= nn
    if precision > 0:
        nn = min(len(digits) - j, precision)
        if nn > 0:
            out.append(digits[j:j + nn])
            precision -= nn
        if precision > 0 and not remove_zeros:
            out.append("0" * precision)
    text = "".join(out)
    if remove_zeros and has_point:
        text = text.rstrip("0")
        if text.endswith("."):
            text = text + "0" if spec.altform2 else text[:-1]
    if kind == "exp":
        exp = point - 1
        text += "e" if conversion in "eg" else "E"
        text += "-" if exp < 0 else "+"
        exp = abs(exp)
        if exp >= 100:
            text += str(exp // 100)
            exp %= 100
        text += f"{exp:02d}"
    if len(text) < spec.width:
        pad = spec.width - len(text)
        if spec.leftjustify:
            text += " " * pad
        elif not spec.zeropad:
            text = " " * pad + text
        else:
            adj = 1 if prefix else 0
            text = text[:adj] + "0" * pad + text[adj:]
    return text.encode()


def _prefix(data: bytes, count: int, characters: bool) -> bytes:
    """The first ``count`` bytes of ``data``, or characters with ``!``."""
    if not characters:
        return data[:count]
    i = 0
    while count > 0 and i < len(data):
        byte = data[i]
        i += 1
        if byte >= 0xC0:
            while i < len(data) and data[i] & 0xC0 == 0x80:
                i += 1
        count -= 1
    return data[:i]


def _escape(conversion: str, args: _Arguments, spec: _Spec) -> bytes:
    """%q, %Q and %w: quotes doubled; %Q in quotes, or NULL.  %#q and %#Q
    write control characters as \\u00XX (and a backslash as two); %#Q then
    wraps the text in unistr('...'), but only if it has a control character."""
    data = args.text()
    need_quote = 0
    if data is None:
        data = b"NULL" if conversion == "Q" else b"(NULL)"
    elif conversion == "Q":
        need_quote = 1
    alternate = spec.alternate and conversion != "w"
    q = b'"' if conversion == "w" else b"'"
    if spec.precision >= 0:
        data = _prefix(data, spec.precision, spec.altform2)
    if alternate:
        control = any(b <= 0x1F for b in data)
        if control or conversion == "q":
            if conversion == "Q":
                need_quote = 2
        else:
            alternate = False
    out = bytearray()
    if need_quote:
        out += b"unistr('" if need_quote == 2 else b"'"
    for byte in data:
        if byte == q[0]:
            out += q + q
        elif alternate and byte == 0x5C:
            out += b"\\\\"
        elif alternate and byte <= 0x1F:
            out += b"\\u00%02x" % byte
        else:
            out.append(byte)
    if need_quote:
        out += b"')" if need_quote == 2 else b"'"
    return bytes(out)


def _parse_spec(fmt: str, i: int, args: _Arguments) -> tuple[_Spec, str, int]:
    """The flags, width and precision starting at fmt[i] (after the %):
    returns them, the conversion character ('' at the end) and its index."""
    spec = _Spec()
    n = len(fmt)
    c = fmt[i] if i < n else ""
    while c:
        if c == "-":
            spec.leftjustify = True
        elif c == "+":
            spec.prefix = b"+"
        elif c == " ":
            spec.prefix = b" "
        elif c == "#":
            spec.alternate = True
        elif c == "!":
            spec.altform2 = True
        elif c == "0":
            spec.zeropad = True
        elif c == ",":
            spec.thousands = True
        elif c == "l":
            i += 1
            c = fmt[i] if i < n else ""
            if c == "l":
                i += 1
                c = fmt[i] if i < n else ""
            return spec, c, i
        elif c in "123456789":
            start = i
            while i < n and fmt[i].isdigit():
                i += 1
            spec.width = int(fmt[start:i]) & 0x7FFFFFFF
            c = fmt[i] if i < n else ""
            if c not in (".", "l"):
                return spec, c, i
            continue
        elif c == "*":
            width = args.integer()
            if width < 0:
                spec.leftjustify = True
                width = -width if width >= -2147483647 else 0
            spec.width = width
            i += 1
            c = fmt[i] if i < n else ""
            if c not in (".", "l"):
                return spec, c, i
            continue
        elif c == ".":
            i += 1
            c = fmt[i] if i < n else ""
            if c == "*":
                precision = args.integer()
                if precision < 0:
                    precision = -precision if precision >= -2147483647 else -1
                spec.precision = precision
                i += 1
            else:
                start = i
                while i < n and fmt[i].isdigit():
                    i += 1
                spec.precision = int(fmt[start:i] or "0") & 0x7FFFFFFF
            c = fmt[i] if i < n else ""
            if c != "l":
                return spec, c, i
            continue
        else:
            return spec, c, i
        i += 1
        c = fmt[i] if i < n else ""
    return spec, c, i


def sql_printf(format_text: str, arguments: list[SQLValue]) -> str | None:
    """printf(FORMAT, ...) as SQLite's SQL function computes it."""
    fmt = format_text.encode("utf-8", "surrogateescape").split(b"\x00", 1)[0].decode(
        "utf-8", "surrogateescape")
    args = _Arguments(arguments)
    out = bytearray()
    touched = False  # SQLite's result is NULL if nothing at all was appended
    i, n = 0, len(fmt)
    while i < n:
        if fmt[i] != "%":
            j = fmt.find("%", i)
            j = n if j == -1 else j
            out += fmt[i:j].encode("utf-8", "surrogateescape")
            touched = True
            i = j
            continue
        i += 1
        if i >= n:
            out += b"%"
            touched = True
            break
        spec, c, i = _parse_spec(fmt, i, args)
        if max(spec.width, spec.precision) > _OUTPUT_LIMIT:
            raise OperationalError("string or blob too big")
        i += 1
        width = spec.width
        if c and c in "diurxXop":
            text = _integer(c, args, spec)
        elif c and c in "feEgG":
            text = _float(c, args, spec)
        elif c and c in "sz":
            data = args.text() or b""
            text = _prefix(data, spec.precision, spec.altform2) if spec.precision >= 0 else data
            if spec.altform2 and width > 0:
                width += sum(1 for byte in text if byte & 0xC0 == 0x80)
        elif c and c in "qQw":
            text = _escape(c, args, spec)
            if spec.altform2 and width > 0:
                width += sum(1 for byte in text if byte & 0xC0 == 0x80)
        elif c == "c":
            data = args.text()
            char = _prefix(data, 1, True)[:4] if data else b"\x00"
            if spec.precision > 1:
                width -= spec.precision - 1
                if width > 1 and not spec.leftjustify:
                    out += b" " * (width - 1)
                    width = 0
                out += char * (spec.precision - 1)
            text = char
            if width > 0:
                width += sum(1 for byte in text if byte & 0xC0 == 0x80)
        elif c == "%":
            text = b"%"
        elif c == "n":
            text, width = b"", 0
        else:
            break  # an unknown conversion (or the end of the format) ends the output
        pad = width - len(text)
        if pad > 0 and not spec.leftjustify:
            out += b" " * pad
        out += text
        if pad > 0 and spec.leftjustify:
            out += b" " * pad
        touched = True
    return out.decode("utf-8", "surrogateescape") if touched else None
