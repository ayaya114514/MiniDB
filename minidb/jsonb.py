"""JSON as SQLite (json.c) handles it: text is parsed into JSONB, SQLite's
binary format, and every function works on that.

JSONB is a tree of elements, each a header and a payload.  The header's low
four bits are the type; its high four bits are the payload size (0-11), or
say that the size follows in 1, 2, 4 or 8 bytes (12-15).  Numbers and text
keep the text they were written with (so ``json('1.50')`` stays ``1.50``),
marked by type as plain JSON, JSON with escapes, or JSON5; arrays and
objects hold their elements (objects: label, value, label, value ...).  The
byte offsets matter: json_each() and json_tree() report them as ``id``.

The parser, the renderer, the path lookup with its in-place edits and the
merge patch below follow json.c step by step, so that results - texts,
JSONB bytes, error positions - are SQLite's.  Positions are in bytes of the
UTF-8 text (with a NUL after it, as SQLite's parser sees it).
"""

from __future__ import annotations

import sys

from minidb.errors import OperationalError

NULL, TRUE, FALSE, INT, INT5, FLOAT, FLOAT5, TEXT, TEXTJ, TEXT5, TEXTRAW, ARRAY, OBJECT = range(13)
TYPE_NAMES = ["null", "true", "false", "integer", "integer", "real", "real", "text", "text", "text", "text",
              "array", "object"]
MAX_DEPTH = 1000  # SQLite's JSON_MAX_DEPTH

LOOKUP_ERROR = -1  # malformed JSONB
LOOKUP_NOTFOUND = -2
LOOKUP_PATHERROR = -3
LOOKUP_TOODEEP = -4  # the path goes MAX_DEPTH levels down
LOOKUP_NOTARRAY = -5  # json_array_insert() at a path that does not end in [N]

EDIT_DEL, EDIT_REPL, EDIT_INS, EDIT_SET, EDIT_AINS = 1, 2, 3, 4, 5

# Python takes a few frames per level of JSON: MAX_DEPTH levels need more
# than its default recursion limit (triggers.py raises it too).
STACK_LIMIT = 20_000


def make_room(size: int) -> None:
    """Before recursing through JSON of ``size`` bytes (its nesting is at
    most that deep): make sure Python can go MAX_DEPTH levels down."""
    if size >= 100 and sys.getrecursionlimit() < STACK_LIMIT:
        sys.setrecursionlimit(STACK_LIMIT)

INVALID_CHAR = 0x99999  # SQLite's JSON_INVALID_CHAR
STATIC_SPACE = 100  # the bytes a JsonString holds before it allocates (zSpace)


class JSONText(str):
    """Text with SQLite's JSON subtype: what JSON functions return.  Another
    JSON function takes it as JSON rather than as a string; storing it, or
    any other function, loses the mark."""

    __slots__ = ()


class JSONBlob(bytes):
    """JSONB with the JSON subtype: what jsonb_array(), jsonb_object(),
    jsonb_group_object() and a container's value in jsonb_each() return
    (SQLite's other jsonb functions return a plain BLOB).  CAST to TEXT
    keeps the mark (JSONText); the subtype is lost where JSONText's is."""

    __slots__ = ()


class Malformed(Exception):
    """Malformed JSON text (``position``: where, in bytes) or JSONB."""

    def __init__(self, position: int = 0) -> None:
        super().__init__(position)
        self.position = position


# ---- character classes --------------------------------------------------------

_DIGITS = frozenset(b"0123456789")
_XDIGITS = frozenset(b"0123456789abcdefABCDEF")
_ALNUM = frozenset(b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_SPACES = frozenset(b"\t\n\r ")
# jsonIsOk: bytes that need no escape in a JSON string.
_OK = [c >= 0x20 and c not in (0x22, 0x27, 0x5c) for c in range(256)]
# sqlite3JsonId1 / sqlite3JsonId2: JSON5 identifier characters.
_ID1 = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$") | frozenset(range(0x80, 0x100))
_ID2 = _ID1 | _DIGITS


def _is_hex4(z: bytes, i: int) -> bool:
    return all(z[i + k] in _XDIGITS for k in range(4))


def _hex_value(c: int) -> int:
    return int(chr(c), 16)


def json5_whitespace(z: bytes, n: int) -> int:
    """How many bytes of JSON5 white space (comments included) start at ``n``."""
    start = n
    while True:
        c = z[n]
        if c in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20):
            n += 1
        elif c == 0x2F:  # '/'
            if z[n + 1] == 0x2A and z[n + 2] != 0:  # /* ... */
                j = n + 3
                while z[j] != 0x2F or z[j - 1] != 0x2A:
                    if z[j] == 0:
                        return n - start
                    j += 1
                n = j + 1
            elif z[n + 1] == 0x2F:  # // ...
                j = n + 2
                while z[j] != 0:
                    c = z[j]
                    if c in (0x0A, 0x0D):
                        break
                    if c == 0xE2 and z[j + 1] == 0x80 and z[j + 2] in (0xA8, 0xA9):
                        j += 2
                        break
                    j += 1
                n = j
                if z[n]:
                    n += 1
            else:
                return n - start
        elif c == 0xC2:
            if z[n + 1] != 0xA0:
                return n - start
            n += 2
        elif c == 0xE1:
            if not (z[n + 1] == 0x9A and z[n + 2] == 0x80):
                return n - start
            n += 3
        elif c == 0xE2:
            if z[n + 1] == 0x80:
                c2 = z[n + 2]
                if c2 < 0x80 or not (c2 <= 0x8A or c2 in (0xA8, 0xA9, 0xAF)):
                    return n - start
                n += 3
            elif z[n + 1] == 0x81 and z[n + 2] == 0x9F:
                n += 3
            else:
                return n - start
        elif c == 0xE3:
            if not (z[n + 1] == 0x80 and z[n + 2] == 0x80):
                return n - start
            n += 3
        elif c == 0xEF:
            if not (z[n + 1] == 0xBB and z[n + 2] == 0xBF):
                return n - start
            n += 3
        else:
            return n - start


# ---- JSONB headers --------------------------------------------------------------

def header(kind: int, size: int) -> bytes:
    """The smallest header for an element of ``kind`` with a payload of ``size`` bytes."""
    if size <= 11:
        return bytes((kind | size << 4,))
    if size <= 0xFF:
        return bytes((kind | 0xC0, size))
    if size <= 0xFFFF:
        return bytes((kind | 0xD0, size >> 8, size & 0xFF))
    return bytes((kind | 0xE0,)) + size.to_bytes(4, "big")


def node(kind: int, payload: bytes = b"") -> bytes:
    return header(kind, len(payload)) + payload


def payload_size(blob: bytes | bytearray, i: int, limit: int | None = None) -> tuple[int, int]:
    """(header size, payload size) of the element at ``i``; (0, 0) if it is malformed
    (SQLite's jsonbPayloadSize)."""
    n_blob = len(blob) if limit is None else limit
    if i >= n_blob:
        return 0, 0
    x = blob[i] >> 4
    if x <= 11:
        sz, n = x, 1
    elif x == 12:
        if i + 1 >= n_blob:
            return 0, 0
        sz, n = blob[i + 1], 2
    elif x == 13:
        if i + 2 >= n_blob:
            return 0, 0
        sz, n = (blob[i + 1] << 8) + blob[i + 2], 3
    elif x == 14:
        if i + 4 >= n_blob:
            return 0, 0
        sz, n = int.from_bytes(blob[i + 1:i + 5], "big"), 5
    else:
        if i + 8 >= n_blob or any(blob[i + 1:i + 5]):
            return 0, 0
        sz, n = int.from_bytes(blob[i + 5:i + 9], "big"), 9
    if i + sz + n > n_blob:
        return 0, 0
    return n, sz


# ---- text to JSONB (jsonTranslateTextToBlob) ------------------------------------------

class TextParser:
    """Parse JSON text (``data``: its UTF-8 bytes) into JSONB.  ``nonstandard``:
    whether it used JSON5 features; ``error``: the byte offset of an error."""

    def __init__(self, data: bytes) -> None:
        self.z = data + b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        self.n = len(data)
        self.out = bytearray()
        self.nonstandard = False
        self.error = 0
        self.depth = 0

    def parse(self) -> bytes:
        """The JSONB of the whole text; raises Malformed (SQLite's jsonConvertTextToBlob)."""
        z = self.z
        make_room(self.n)
        i = self.value(0)
        if i > 0:
            while z[i] in _SPACES:
                i += 1
            if z[i]:
                i += json5_whitespace(z, i)
                if z[i]:
                    raise Malformed(self.error)  # (SQLite leaves iErr as it was)
                self.nonstandard = True
        if i <= 0:
            raise Malformed(self.error)
        return bytes(self.out)

    def append(self, kind: int, payload: bytes) -> None:
        self.out += header(kind, len(payload)) + payload

    def change_payload_size(self, i: int, size: int) -> None:
        """Rewrite the (estimated) header at ``i`` for its real payload size."""
        out = self.out
        old = payload_header_length(out[i] >> 4)
        new = header(out[i] & 0x0F, size)
        out[i:i + old] = new

    def container(self, i: int, kind: int) -> int:
        """An object or array starting at ``i``; returns the offset after it or an error code."""
        z, out = self.z, self.out
        this = len(out)
        self.out += header(kind, self.n - i)  # (an estimate, as SQLite's, rewritten below)
        self.depth += 1
        if self.depth > MAX_DEPTH:
            self.error = i
            return -1
        start = len(out)
        j = i + 1
        if kind == OBJECT:
            while True:
                label = len(out)
                x = self.value(j)
                if x <= 0:
                    if x == -2:
                        j = self.error
                        if len(out) != start:
                            self.nonstandard = True
                        break
                    j += json5_whitespace(z, j)
                    op = TEXT
                    if z[j] in _ID1 or (z[j] == 0x5C and z[j + 1] == 0x75 and _is_hex4(z, j + 2)):
                        if z[j] == 0x5C:
                            op = TEXTJ
                        k = j + 1
                        while (z[k] in _ID2 and json5_whitespace(z, k) == 0) or (
                                z[k] == 0x5C and z[k + 1] == 0x75 and _is_hex4(z, k + 2)):
                            if z[k] == 0x5C:
                                op = TEXTJ
                            k += 1
                        self.append(op, bytes(z[j:k]))
                        self.nonstandard = True
                        x = k
                    else:
                        if x != -1:
                            self.error = j
                        return -1
                t = out[label] & 0x0F
                if t < TEXT or t > TEXTRAW:
                    self.error = j
                    return -1
                j = x
                if z[j] == 0x3A:  # ':'
                    j += 1
                else:
                    found = False
                    if z[j] in _SPACES:
                        j += 1
                        while z[j] in _SPACES:
                            j += 1
                        if z[j] == 0x3A:
                            j += 1
                            found = True
                    if not found:
                        x = self.value(j)
                        if x != -5:
                            if x != -1:
                                self.error = j
                            return -1
                        j = self.error + 1
                x = self.value(j)
                if x <= 0:
                    if x != -1:
                        self.error = j
                    return -1
                j = x
                if z[j] == 0x2C:  # ','
                    j += 1
                    continue
                if z[j] == 0x7D:  # '}'
                    break
                if z[j] in _SPACES:
                    j += 1
                    while z[j] in _SPACES:
                        j += 1
                    if z[j] == 0x2C:
                        j += 1
                        continue
                    if z[j] == 0x7D:
                        break
                x = self.value(j)
                if x == -4:
                    j = self.error + 1
                    continue
                if x == -2:
                    j = self.error
                    break
                self.error = j
                return -1
        else:
            while True:
                x = self.value(j)
                if x <= 0:
                    if x == -3:
                        j = self.error
                        if len(out) != start:
                            self.nonstandard = True
                        break
                    if x != -1:
                        self.error = j
                    return -1
                j = x
                if z[j] == 0x2C:
                    j += 1
                    continue
                if z[j] == 0x5D:  # ']'
                    break
                if z[j] in _SPACES:
                    j += 1
                    while z[j] in _SPACES:
                        j += 1
                    if z[j] == 0x2C:
                        j += 1
                        continue
                    if z[j] == 0x5D:
                        break
                x = self.value(j)
                if x == -4:
                    j = self.error + 1
                    continue
                if x == -3:
                    j = self.error
                    break
                self.error = j
                return -1
        self.change_payload_size(this, len(out) - start)
        self.depth -= 1
        return j + 1

    def string(self, i: int) -> int:
        z = self.z
        delimiter = z[i]
        op = TEXT
        if delimiter == 0x27:
            self.nonstandard = True
        j = i + 1
        while True:
            c = z[j]
            if _OK[c]:
                j += 1
                continue
            if c == delimiter:
                break
            if c == 0x5C:  # backslash
                j += 1
                c = z[j]
                if c in b'"\\/bfnrt' or (c == 0x75 and _is_hex4(z, j + 1)):
                    if op == TEXT:
                        op = TEXTJ
                elif c in (0x27, 0x76, 0x0A) or (c == 0x30 and z[j + 1] not in _DIGITS) or (
                        c == 0xE2 and z[j + 1] == 0x80 and z[j + 2] in (0xA8, 0xA9)) or (
                        c == 0x78 and z[j + 1] in _XDIGITS and z[j + 2] in _XDIGITS):
                    op = TEXT5
                    self.nonstandard = True
                elif c == 0x0D:
                    if z[j + 1] == 0x0A:
                        j += 1
                    op = TEXT5
                    self.nonstandard = True
                else:
                    self.error = j
                    return -1
            elif c <= 0x1F:
                if c == 0:
                    self.error = j
                    return -1
                op = TEXT5  # (control characters are fine in JSON5 strings)
                self.nonstandard = True
            elif c == 0x22:  # '"' in a '...' string
                op = TEXT5
            j += 1
        self.append(op, bytes(z[i + 1:j]))
        return j + 1

    def number(self, i: int, t: int) -> int:
        """A number at ``i`` (t: 0x01 JSON5, 0x02 float); SQLite's parse_number."""
        z = self.z
        c = z[i]
        j = 0
        hexadecimal = False
        if c <= 0x30:  # '+', '-', '.' or '0'
            if c == 0x30:
                if z[i + 1] in (0x78, 0x58) and z[i + 2] in _XDIGITS:
                    self.nonstandard = True
                    t = 0x01
                    j = i + 3
                    while z[j] in _XDIGITS:
                        j += 1
                    hexadecimal = True
                elif z[i + 1] in _DIGITS:
                    self.error = i + 1
                    return -1
            elif c != 0x2E:
                if z[i + 1] not in _DIGITS:
                    if z[i + 1] in (0x49, 0x69) and bytes(z[i + 1:i + 4]).lower() == b"inf":
                        self.nonstandard = True
                        self.append(FLOAT, b"-9e999" if c == 0x2D else b"9e999")
                        return i + (9 if bytes(z[i + 4:i + 9]).lower() == b"inity" else 4)
                    if z[i + 1] == 0x2E:
                        self.nonstandard = True
                        t |= 0x01
                        return self.number_tail(i, t, 0)
                    self.error = i
                    return -1
                if z[i + 1] == 0x30:
                    if z[i + 2] in _DIGITS:
                        self.error = i + 1
                        return -1
                    if z[i + 2] in (0x78, 0x58) and z[i + 3] in _XDIGITS:
                        self.nonstandard = True
                        t |= 0x01
                        j = i + 4
                        while z[j] in _XDIGITS:
                            j += 1
                        hexadecimal = True
        if not hexadecimal:
            return self.number_tail(i, t, 0)
        return self.number_finish(i, j, t)

    def number_tail(self, i: int, t: int, seen_e: int) -> int:
        z = self.z
        j = i + 1
        while True:
            c = z[j]
            if c in _DIGITS:
                j += 1
                continue
            if c == 0x2E:
                if t & 0x02:
                    self.error = j
                    return -1
                t |= 0x02
                j += 1
                continue
            if c in (0x65, 0x45):  # e E
                if z[j - 1] < 0x30:
                    if z[j - 1] == 0x2E and j - 2 >= i and z[j - 2] in _DIGITS:
                        self.nonstandard = True
                        t |= 0x01
                    else:
                        self.error = j
                        return -1
                if seen_e:
                    self.error = j
                    return -1
                t |= 0x02
                seen_e = 1
                c = z[j + 1]
                if c in (0x2B, 0x2D):
                    j += 1
                    c = z[j + 1]
                if c not in _DIGITS:
                    self.error = j
                    return -1
                j += 1
                continue
            break
        if z[j - 1] < 0x30:
            if z[j - 1] == 0x2E and j - 2 >= i and z[j - 2] in _DIGITS:
                self.nonstandard = True
                t |= 0x01
            else:
                self.error = j
                return -1
        return self.number_finish(i, j, t)

    def number_finish(self, i: int, j: int, t: int) -> int:
        if self.z[i] == 0x2B:  # '+'
            i += 1
        self.append(INT + t, bytes(self.z[i:j]))
        return j

    def value(self, i: int) -> int:
        """Parse a value at ``i``: the offset after it, 0 at the end of the
        text, or -1 (error), -2 '}', -3 ']', -4 ',', -5 ':' (self.error: where)."""
        z = self.z
        while True:
            c = z[i]
            if c == 0x7B:  # '{'
                return self.container(i, OBJECT)
            if c == 0x5B:  # '['
                return self.container(i, ARRAY)
            if c in (0x22, 0x27):
                return self.string(i)
            if c == 0x74:  # t
                if bytes(z[i:i + 4]) == b"true" and z[i + 4] not in _ALNUM:
                    self.out.append(TRUE)
                    return i + 4
                self.error = i
                return -1
            if c == 0x66:  # f
                if bytes(z[i:i + 5]) == b"false" and z[i + 5] not in _ALNUM:
                    self.out.append(FALSE)
                    return i + 5
                self.error = i
                return -1
            if c == 0x2B:  # '+'
                self.nonstandard = True
                return self.number(i, 0x00)
            if c == 0x2E:  # '.'
                if z[i + 1] in _DIGITS:
                    self.nonstandard = True
                    return self.number_tail(i, 0x03, 0)
                self.error = i
                return -1
            if c == 0x2D or c in _DIGITS:
                return self.number(i, 0x00)
            if c == 0x7D:
                self.error = i
                return -2
            if c == 0x5D:
                self.error = i
                return -3
            if c == 0x2C:
                self.error = i
                return -4
            if c == 0x3A:
                self.error = i
                return -5
            if c == 0:
                return 0 if i >= self.n else self._nul(i)
            if c in _SPACES:
                i += 1
                while z[i] in _SPACES:
                    i += 1
                continue
            if c in (0x0B, 0x0C, 0x2F, 0xC2, 0xE1, 0xE2, 0xE3, 0xEF):
                j = json5_whitespace(z, i)
                if j > 0:
                    i += j
                    self.nonstandard = True
                    continue
                self.error = i
                return -1
            if c == 0x6E and bytes(z[i:i + 4]) == b"null" and z[i + 4] not in _ALNUM:
                self.out.append(NULL)
                return i + 4
            for first, name, is_float in _NAN_INF:
                if c not in first:
                    continue
                if bytes(z[i:i + len(name)]).lower() != name or z[i + len(name)] in _ALNUM:
                    continue
                if is_float:
                    self.append(FLOAT, b"9e999")
                else:
                    self.out.append(NULL)
                self.nonstandard = True
                return i + len(name)
            self.error = i
            return -1

    def _nul(self, i: int) -> int:
        return 0  # (SQLite's parser stops at a NUL: the text ends there)


_NAN_INF = [(b"iI", b"inf", True), (b"iI", b"infinity", True), (b"nN", b"nan", False),
            (b"qQ", b"qnan", False), (b"sS", b"snan", False)]


def payload_header_length(x: int) -> int:
    return 1 if x <= 11 else (2, 3, 5, 9)[x - 12]


def parse_text(text: str | bytes) -> tuple[bytes, bool]:
    """(JSONB, uses JSON5) for JSON text; raises Malformed."""
    data = text if isinstance(text, bytes) else text.encode("utf-8", "surrogatepass")
    parser = TextParser(data)
    return parser.parse(), parser.nonstandard


# ---- JSONB to text (jsonTranslateBlobToText) -------------------------------------------

_SPECIAL = {8: "b", 9: "t", 10: "n", 12: "f", 13: "r"}


def _control(c: int) -> str:
    if c in _SPECIAL:
        return "\\" + _SPECIAL[c]
    return "\\u%04x" % c


def control_grows(raw: bytes, used: int) -> bool:
    """Whether jsonAppendString, appending ``raw`` (with control characters)
    after ``used`` bytes, grows the JsonString: before each control character
    it makes room for 7 bytes more than what is left."""
    used += 1
    for k, c in enumerate(raw):
        if _OK[c] or c == 0x27:
            used += 1
        elif c in (0x22, 0x5C):
            used += 2
        else:
            if used + len(raw) - k + 7 > STATIC_SPACE:
                return True
            used += len(_control(c))
    return False


def quote_string(raw: bytes) -> bytes:
    """SQLite's jsonAppendString: a string as a JSON string literal."""
    out = bytearray(b'"')
    for c in raw:
        if _OK[c] or c == 0x27:
            out.append(c)
        elif c in (0x22, 0x5C):
            out += b"\\" + bytes((c,))
        else:
            out += _control(c).encode()
    out.append(0x22)
    return bytes(out)


class Renderer:
    """Render JSONB as JSON text, compact or (``indent``) pretty."""

    def __init__(self, blob: bytes | bytearray, indent: bytes | None = None) -> None:
        self.blob = blob
        self.out = bytearray()
        self.indent = indent
        self.level = 0
        self.depth = 0
        self.error = False
        # Whether SQLite's JsonString would have left its static space before
        # reaching STATIC_SPACE bytes (which only some appends reserve ahead).
        self.grown = False
        make_room(len(blob))

    def render(self, i: int = 0) -> bytes:
        self.element(i)
        if self.error:
            raise Malformed(0)
        return bytes(self.out)

    def element(self, i: int) -> int:
        blob, out = self.blob, self.out
        n, sz = payload_size(blob, i)
        if n == 0:
            self.error = True
            return len(blob) + 1
        kind = blob[i] & 0x0F
        p = blob[i + n:i + n + sz]
        if kind == NULL:
            out += b"null"
            return i + 1
        if kind == TRUE:
            out += b"true"
            return i + 1
        if kind == FALSE:
            out += b"false"
            return i + 1
        if kind in (INT, FLOAT):
            if sz == 0:
                self.error = True
            out += p
        elif kind == INT5:
            if sz == 0:
                self.error = True
                return i + n + sz
            k = 2
            if p[0] == 0x2D:
                out.append(0x2D)
                k += 1
            elif p[0] == 0x2B:
                k += 1
            value, overflow = 0, False
            for c in p[k:]:
                if c not in _XDIGITS:
                    self.error = True
                    break
                if value >> 60:
                    overflow = True
                else:
                    value = value * 16 + _hex_value(c)
            out += b"9.0e999" if overflow else str(value).encode()
            self.grown = True  # (jsonPrintf(100, ...) makes room for 100 bytes)
        elif kind == FLOAT5:
            if sz == 0:
                self.error = True
                return i + n + sz
            k = 0
            if p[0] == 0x2D:
                out.append(0x2D)
                if sz <= 1:
                    self.error = True
                    return i + n + sz
                k = 1
            if p[k] == 0x2E:
                out.append(0x30)
            while k < sz:
                out.append(p[k])
                if p[k] == 0x2E and (k + 1 == sz or p[k + 1] not in _DIGITS):
                    out.append(0x30)
                k += 1
        elif kind in (TEXT, TEXTJ):
            out += b'"' + p + b'"'
        elif kind == TEXT5:
            self.text5(p)
        elif kind == TEXTRAW:
            if not self.grown and any(c < 0x20 for c in p):
                self.grown = control_grows(p, len(out))
            out += quote_string(bytes(p))
        elif kind == ARRAY:
            out.append(0x5B)
            if self.indent is not None:
                self.pretty_container(i + n, i + n + sz, False)
            else:
                j, end = i + n, i + n + sz
                self.deeper()
                while j < end and not self.error:
                    j = self.element(j)
                    out.append(0x2C)
                self.depth -= 1
                if j > end:
                    self.error = True
                if sz > 0:
                    out.pop()
            out.append(0x5D)
        elif kind == OBJECT:
            out.append(0x7B)
            if self.indent is not None:
                self.pretty_container(i + n, i + n + sz, True)
            else:
                j, end, x = i + n, i + n + sz, 0
                self.deeper()
                while j < end and not self.error:
                    j = self.element(j)
                    out.append(0x2C if x & 1 else 0x3A)
                    x += 1
                self.depth -= 1
                if x & 1 or j > end:
                    self.error = True
                if sz > 0:
                    out.pop()
            out.append(0x7D)
        else:
            self.error = True
        return i + n + sz

    def deeper(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:  # (possible with JSONB arguments and edits)
            raise error("JSON nested too deep")

    def pretty_container(self, j: int, end: int, is_object: bool) -> None:
        """As jsonTranslateBlobToPrettyText: object labels are rendered
        compactly, and only a label (not an element) running past the end
        is an error."""
        out = self.out
        if j >= end:
            return
        out.append(0x0A)
        self.level += 1
        if self.level >= MAX_DEPTH:
            raise error("JSON nested too deep")
        if is_object:
            self.depth = self.level
        while not self.error:
            out += self.indent * self.level
            if is_object:
                indent, self.indent = self.indent, None
                j = self.element(j)
                self.indent = indent
                if j > end:
                    self.error = True
                    break
                out += b": "
            j = self.element(j)
            if j >= end:
                break
            out += b",\n"
        out.append(0x0A)
        self.level -= 1
        out += self.indent * self.level

    def text5(self, p: bytes) -> None:
        out = self.out
        out.append(0x22)
        k, size = 0, len(p)
        while k < size:
            c = p[k]
            if _OK[c] or c == 0x27:
                out.append(c)
                k += 1
                continue
            if c == 0x22:
                out += b'\\"'
                k += 1
                continue
            if c <= 0x1F:
                if len(out) + 7 > STATIC_SPACE:
                    self.grown = True
                out += _control(c).encode()
                k += 1
                continue
            # a backslash
            if size - k < 2:
                self.error = True
                break
            e = p[k + 1]
            if e == 0x27:
                out.append(0x27)
            elif e == 0x76:  # \v
                out += b"\\u000b"
            elif e == 0x78:  # \xHH
                if size - k < 4:
                    self.error = True
                    k = size
                    break
                out += b"\\u00" + p[k + 2:k + 4]
                k += 2
            elif e == 0x30:
                out += b"\\u0000"
            elif e == 0x0D:
                if size - k > 2 and p[k + 2] == 0x0A:
                    k += 1
            elif e == 0x0A:
                pass
            elif e == 0xE2:
                if size - k < 4 or p[k + 2] != 0x80 or p[k + 3] not in (0xA8, 0xA9):
                    self.error = True
                    k = size
                    break
                k += 2
            else:
                out += p[k:k + 2]
            k += 2
        out.append(0x22)


def to_text(blob: bytes | bytearray, i: int = 0, indent: bytes | None = None) -> str:
    return Renderer(blob, indent).render(i).decode("utf-8", "surrogatepass")


# ---- unescaping and labels ---------------------------------------------------------------

def _bytes_to_bypass(z: bytes, i: int, n: int) -> int:
    """SQLite's jsonBytesToBypass: a backslash and line terminator to skip."""
    k = 0
    while k + 1 < n and z[i + k] == 0x5C:
        c = z[i + k + 1]
        if c == 0x0D:
            if k + 2 < n and z[i + k + 2] == 0x0A:
                k += 3
            else:
                k += 2
        elif c == 0x0A:
            k += 2
        elif c == 0xE2 and k + 3 < n and z[i + k + 2] == 0x80 and z[i + k + 3] in (0xA8, 0xA9):
            k += 4
        else:
            break
    return k


def unescape_one(z: bytes, i: int, n: int) -> tuple[int, int]:
    """SQLite's jsonUnescapeOneChar for the escape at ``z[i]`` (n bytes
    left): (code point or INVALID_CHAR, bytes used)."""
    if n < 2:
        return INVALID_CHAR, n
    c = z[i + 1]
    if c == 0x75:  # u
        if n < 6:
            return INVALID_CHAR, n
        v = _hex4(z, i + 2)
        if (v & 0xFC00) == 0xD800 and n >= 12 and z[i + 6] == 0x5C and z[i + 7] == 0x75:
            low = _hex4(z, i + 8)
            if (low & 0xFC00) == 0xDC00:
                return ((v & 0x3FF) << 10) + (low & 0x3FF) + 0x10000, 12
        return v, 6
    simple = {0x62: 8, 0x66: 12, 0x6E: 10, 0x72: 13, 0x74: 9, 0x76: 11}
    if c in simple:
        return simple[c], 2
    if c == 0x30:
        return (INVALID_CHAR if n > 2 and z[i + 2] in _DIGITS else 0), 2
    if c in (0x27, 0x22, 0x2F, 0x5C):
        return c, 2
    if c == 0x78:
        if n < 4:
            return INVALID_CHAR, n
        return (_hex_digit(z[i + 2]) << 4) | _hex_digit(z[i + 3]), 4
    if c in (0xE2, 0x0D, 0x0A):
        skip = _bytes_to_bypass(z, i, n)
        if skip == 0:
            return INVALID_CHAR, n
        if skip == n:
            return 0, n
        if z[i + skip] == 0x5C:
            v, size = unescape_one(z, i + skip, n - skip)
            return v, skip + size
        v, size = _read_utf8(z, i + skip, n - skip)
        return v, skip + size
    return INVALID_CHAR, 2


def _hex_digit(c: int) -> int:
    """SQLite's jsonHexToInt, which does not check the digit."""
    return (c + 9 * ((c >> 6) & 1)) & 0x0F


def _hex4(z: bytes, i: int) -> int:
    return (_hex_digit(z[i]) << 12) | (_hex_digit(z[i + 1]) << 8) | (_hex_digit(z[i + 2]) << 4) | _hex_digit(z[i + 3])


# sqlite3Utf8Trans1: the bits a UTF-8 lead byte (0xC0 and up) contributes.
_UTF8_TRANS1 = bytes(list(range(32)) + list(range(16)) + list(range(8)) + [0, 1, 2, 3, 0, 1, 0, 0])


def _read_utf8(z: bytes, i: int, n: int) -> tuple[int, int]:
    """SQLite's sqlite3Utf8ReadLimited: (code point, bytes used); it takes up
    to four bytes of continuation, whatever the lead byte says."""
    c = z[i]
    if c < 0xC0:
        return c, 1
    c = _UTF8_TRANS1[c - 0xC0]
    k = 1
    while k < min(n, 4) and (z[i + k] & 0xC0) == 0x80:
        c = (c << 6) + (z[i + k] & 0x3F)
        k += 1
    return c, k


def _utf8(v: int) -> bytes:
    if v <= 0x7F:
        return bytes((v,))
    if v <= 0x7FF:
        return bytes((0xC0 | v >> 6, 0x80 | v & 0x3F))
    if v < 0x10000:
        return bytes((0xE0 | v >> 12, 0x80 | (v >> 6) & 0x3F, 0x80 | v & 0x3F))
    if v == INVALID_CHAR:
        return b""
    return bytes((0xF0 | v >> 18, 0x80 | (v >> 12) & 0x3F, 0x80 | (v >> 6) & 0x3F, 0x80 | v & 0x3F))


def unescape(p: bytes) -> bytes:
    """The text of a JSON string's payload, its escapes resolved."""
    out = bytearray()
    i, size = 0, len(p)
    while i < size:
        c = p[i]
        if c == 0x5C:
            v, used = unescape_one(p, i, size - i)
            out += _utf8(v)
            i += used
        else:
            out.append(c)
            i += 1
    return bytes(out)


def _label_chars(z: bytes, raw: bool) -> list[int]:
    """The code points jsonLabelCompareEscaped compares: UTF-8 read as
    sqlite3Utf8ReadLimited does, escapes resolved, up to the first 0."""
    out = []
    i, size = 0, len(z)
    while i < size:
        c = z[i]
        if c == 0x5C and not raw:
            c, used = unescape_one(z, i, size - i)
        elif c >= 0xC0:
            c, used = _read_utf8(z, i, size - i)
        else:
            used = 1
        if c == 0:
            break
        out.append(c)
        i += used
    return out


def label_equal(left: bytes, left_raw: bool, right: bytes, right_raw: bool) -> bool:
    """SQLite's jsonLabelCompare."""
    if left_raw and right_raw:
        return left == right
    return _label_chars(left, left_raw) == _label_chars(right, right_raw)


# ---- validity of JSONB (jsonbValidityCheck) ------------------------------------------------------

def validity_check(z: bytes, i: int, end: int, depth: int = 1) -> int:
    """0 if the element at ``i`` (ending at ``end``) is well formed, else an error offset + 1."""
    if depth > MAX_DEPTH:
        return i + 1
    if depth == 1:
        make_room(end - i)
    n, sz = payload_size(z, i)
    if n == 0 or i + n + sz != end:
        return i + 1
    x = z[i] & 0x0F
    if x in (NULL, TRUE, FALSE):
        return 0 if n + sz == 1 else i + 1
    if x == INT:
        if sz < 1:
            return i + 1
        j = i + n
        if z[j] == 0x2D:
            j += 1
            if sz < 2:
                return i + 1
        while j < i + n + sz:
            if z[j] not in _DIGITS:
                return j + 1
            j += 1
        return 0
    if x == INT5:
        if sz < 3:
            return i + 1
        j = i + n
        if z[j] == 0x2D:
            if sz < 4:
                return i + 1
            j += 1
        if z[j] != 0x30:
            return i + 1
        if z[j + 1] not in (0x78, 0x58):
            return j + 2
        j += 2
        while j < i + n + sz:
            if z[j] not in _XDIGITS:
                return j + 1
            j += 1
        return 0
    if x in (FLOAT, FLOAT5):
        seen = 0
        if sz < 2:
            return i + 1
        j = i + n
        k = j + sz
        if z[j] == 0x2D:
            j += 1
            if sz < 3:
                return i + 1
        if z[j] == 0x2E:
            if x == FLOAT:
                return j + 1
            if j + 1 >= k or z[j + 1] not in _DIGITS:
                return j + 1
            j += 2
            seen = 1
        elif z[j] == 0x30 and x == FLOAT:
            if j + 3 > k:
                return j + 1
            if z[j + 1] not in (0x2E, 0x65, 0x45):
                return j + 1
            j += 1
        while j < k:
            c = z[j]
            if c in _DIGITS:
                j += 1
                continue
            if c == 0x2E:
                if seen > 0:
                    return j + 1
                if x == FLOAT and (j == k - 1 or z[j + 1] not in _DIGITS):
                    return j + 1
                seen = 1
                j += 1
                continue
            if c in (0x65, 0x45):
                if seen == 2 or j == k - 1:
                    return j + 1
                if z[j + 1] in (0x2B, 0x2D):
                    j += 1
                    if j == k - 1:
                        return j + 1
                seen = 2
                j += 1
                continue
            return j + 1
        return 0 if seen else i + 1
    if x == TEXT:
        for j in range(i + n, i + n + sz):
            if not _OK[z[j]] and z[j] != 0x27:
                return j + 1
        return 0
    if x in (TEXTJ, TEXT5):
        j, k = i + n, i + n + sz
        while j < k:
            c = z[j]
            if not _OK[c] and c != 0x27:
                if c == 0x22:
                    if x == TEXTJ:
                        return j + 1
                elif c <= 0x1F:
                    if x == TEXTJ:
                        return j + 1
                elif c != 0x5C or j + 1 >= k:
                    return j + 1
                elif z[j + 1] in b'"\\/bfnrt\x00':  # (strchr finds a NUL too: its terminator)
                    j += 1
                elif z[j + 1] == 0x75:
                    if j + 5 >= k or not _is_hex4(z + b"\x00\x00\x00\x00", j + 2):
                        return j + 1
                    j += 1
                elif x != TEXT5:
                    return j + 1
                else:
                    v, used = unescape_one(z, j, k - j)
                    if v == INVALID_CHAR:
                        return j + 1
                    j += used - 1
            j += 1
        return 0
    if x == TEXTRAW:
        return 0
    if x in (ARRAY, OBJECT):
        j, k, count = i + n, i + n + sz, 0
        while j < k:
            n2, sz2 = payload_size(z, j)
            if n2 == 0 or j + n2 + sz2 > k:
                return j + 1
            if x == OBJECT and count & 1 == 0:
                t = z[j] & 0x0F
                if t < TEXT or t > TEXTRAW:
                    return j + 1
            sub = validity_check(z, j, j + n2 + sz2, depth + 1)
            if sub:
                return sub
            count += 1
            j += n2 + sz2
        if x == OBJECT and count & 1:
            return j + 1
        return 0
    return i + 1


def is_jsonb(blob: bytes) -> bool:
    """Whether a BLOB is taken as JSONB (SQLite's jsonArgIsJsonb): a quick look,
    and a full check when a short one could as well be JSON text."""
    if not blob:
        return False
    c = blob[0]
    if c & 0x0F > OBJECT:
        return False
    n, sz = payload_size(blob, 0)
    if n == 0 or sz + n != len(blob):
        return False
    if c & 0x0F <= FALSE and sz != 0:
        return False
    if sz > 7 or (c not in (0x7B, 0x5B) and c not in _DIGITS):
        return True
    return validity_check(blob, 0, len(blob)) == 0


# ---- editing JSONB: path lookup (jsonLookupStep) ------------------------------------------

class Editor:
    """A JSONB value being looked into and perhaps edited, as SQLite's JsonParse."""

    def __init__(self, blob: bytes) -> None:
        self.blob = bytearray(blob)
        self.delta = 0
        self.edit = 0
        self.insert = b""  # the JSONB to put in (EDIT_REPL / INS / SET / AINS)
        self.label = 0  # the label of the element found (iLabel)
        self.depth = 0  # how far down the lookup is (iDepth)
        make_room(len(blob))

    def change_payload_size(self, i: int, size: int) -> int:
        """Rewrite the header at ``i`` for a payload of ``size``; returns the change in bytes."""
        blob = self.blob
        old = payload_header_length(blob[i] >> 4)
        new = header(blob[i] & 0x0F, size)
        blob[i:i + old] = new
        return len(new) - old

    def after_edit(self, root: int) -> None:
        """SQLite's jsonAfterEditSizeAdjust: fix the container at ``root`` after an edit inside it."""
        blob = self.blob
        x = blob[root] >> 4
        n = payload_header_length(x)
        if x <= 11:
            sz = x
        else:
            sz = int.from_bytes(blob[root + 1:root + n], "big")
        self.delta += self.change_payload_size(root, sz + self.delta)

    def replace(self, i: int, remove: int, insert: bytes) -> None:
        """SQLite's jsonBlobEdit."""
        self.blob[i:i + remove] = insert
        self.delta += len(insert) - remove

    def array_count(self, root: int) -> int:
        """As jsonbArrayCount: an element whose header is damaged still
        counts (the loop counts before it tests the size)."""
        n, sz = payload_size(self.blob, root)
        i, end, count = root + n, root + n + sz, 0
        while n > 0 and i < end:
            n, sz = payload_size(self.blob, i)
            i += sz + n
            count += 1
        return count

    def lookup(self, root: int, path: bytes, label: int = 0, indexed: bool = False) -> int:
        """Follow ``path`` (after the '$') from the element at ``root``; returns
        the offset of the element found or a LOOKUP_ code, editing as self.edit
        says.  ``indexed``: the path so far ends in [N]."""
        blob = self.blob
        if not path:
            if self.edit:
                n, sz = payload_size(blob, root)
                sz += n
                if self.edit == EDIT_DEL:
                    if label > 0:
                        sz += root - label
                        root = label
                    self.replace(root, sz, b"")
                elif self.edit == EDIT_AINS:
                    if not indexed:
                        return LOOKUP_NOTARRAY
                    self.replace(root, 0, self.insert)
                elif self.edit != EDIT_INS:
                    self.replace(root, sz, self.insert)
            self.label = label
            return root
        if path[0] == 0x2E:  # '.'
            raw_key = True
            path = path[1:]
            if path[:1] == b'"':
                i = 1
                while i < len(path) and path[i] != 0x22:
                    i += 1
                key = path[1:i]
                if i < len(path):
                    i += 1
                else:
                    return LOOKUP_PATHERROR
                raw_key = b"\\" not in key
            else:
                i = 0
                while i < len(path) and path[i] not in (0x2E, 0x5B):
                    i += 1
                key = path[:i]
                if not key:
                    return LOOKUP_PATHERROR
            if blob[root] & 0x0F != OBJECT:
                return LOOKUP_NOTFOUND
            n, sz = payload_size(blob, root)
            j = root + n
            end = j + sz
            while j < end:
                x = blob[j] & 0x0F
                if x < TEXT or x > TEXTRAW:
                    return LOOKUP_ERROR
                n, sz = payload_size(blob, j)
                if n == 0:
                    return LOOKUP_ERROR
                k = j + n
                if k + sz >= end:
                    return LOOKUP_ERROR
                if label_equal(key, raw_key, bytes(blob[k:k + sz]), x in (TEXT, TEXTRAW)):
                    v = k + sz
                    if blob[v] & 0x0F > OBJECT:
                        return LOOKUP_ERROR
                    n, sz = payload_size(blob, v)
                    if n == 0 or v + n + sz > end:
                        return LOOKUP_ERROR
                    self.depth += 1
                    if self.depth >= MAX_DEPTH:
                        return LOOKUP_TOODEEP
                    rc = self.lookup(v, path[i:], j, path[i - 1:i] == b"]")  # (SQLite looks at zPath[-1])
                    self.depth -= 1
                    if self.delta:
                        self.after_edit(root)
                    return rc
                j = k + sz
                if blob[j] & 0x0F > OBJECT:
                    return LOOKUP_ERROR
                n, sz = payload_size(blob, j)
                if n == 0:
                    return LOOKUP_ERROR
                j += n + sz
            if j > end:
                return LOOKUP_ERROR
            if self.edit >= EDIT_INS:
                if self.edit == EDIT_AINS and path[i:][-1:] != b"]":
                    return LOOKUP_NOTARRAY
                label_header = header(TEXTRAW if raw_key else TEXT5, len(key))
                rc, inserted = self.substructure(path[i:])
                if rc >= 0:
                    self.replace(j, 0, label_header + key + inserted)
                    if self.delta:
                        self.after_edit(root)
                return rc
        elif path[0] == 0x5B:  # '['
            if blob[root] & 0x0F != ARRAY:
                return LOOKUP_NOTFOUND
            n, sz = payload_size(blob, root)
            k = 0
            i = 1
            while i < len(path) and path[i] in _DIGITS:
                k = (k * 10 + path[i] - 0x30) & 0xFFFFFFFF
                i += 1
            if i < 2 or path[i:i + 1] != b"]":
                if path[1:2] == b"#":
                    k = self.array_count(root)
                    i = 2
                    if path[2:3] == b"-" and path[3:4] and path[3] in _DIGITS:
                        nn = 0
                        i = 3
                        while i < len(path) and path[i] in _DIGITS:
                            nn = (nn * 10 + path[i] - 0x30) & 0xFFFFFFFF
                            i += 1
                        if nn > k:
                            return LOOKUP_NOTFOUND
                        k -= nn
                    if path[i:i + 1] != b"]":
                        return LOOKUP_PATHERROR
                else:
                    return LOOKUP_PATHERROR
            j = root + n
            end = j + sz
            while j < end:
                if k == 0:
                    self.depth += 1
                    if self.depth >= MAX_DEPTH:
                        return LOOKUP_TOODEEP
                    rc = self.lookup(j, path[i + 1:], 0, True)
                    self.depth -= 1
                    if self.delta:
                        self.after_edit(root)
                    return rc
                k -= 1
                n2, sz2 = payload_size(blob, j)
                if n2 == 0:
                    return LOOKUP_ERROR
                j += n2 + sz2
            if j > end:
                return LOOKUP_ERROR
            if k > 0:
                return LOOKUP_NOTFOUND
            if self.edit >= EDIT_INS:
                rc, inserted = self.substructure(path[i + 1:])
                if rc >= 0:
                    self.replace(j, 0, inserted)
                if self.delta:
                    self.after_edit(root)
                return rc
        else:
            return LOOKUP_PATHERROR
        return LOOKUP_NOTFOUND

    def substructure(self, tail: bytes) -> tuple[int, bytes]:
        """SQLite's jsonCreateEditSubstructure: what to insert for the rest of
        a path that does not exist yet: (code, JSONB)."""
        if not tail:
            return 0, self.insert
        sub = Editor(bytes((OBJECT if tail[:1] == b"." else ARRAY,)))
        sub.edit = self.edit
        sub.insert = self.insert
        sub.depth = self.depth + 1
        if sub.depth >= MAX_DEPTH:
            return LOOKUP_TOODEEP, b""
        make_room(len(tail))
        rc = sub.lookup(0, tail, 0)
        return rc, bytes(sub.blob)


def lookup(blob: bytes, path: bytes) -> tuple[int, Editor]:
    editor = Editor(blob)
    return editor.lookup(0, path, 0), editor


# ---- json_patch (jsonMergePatch) -------------------------------------------------------------

class BadPatch(Exception):
    pass


class PatchTooDeep(Exception):
    pass


def merge_patch(target: Editor, i_target: int, patch: bytes, i_patch: int, depth: int = 0) -> None:
    """RFC 7396 MergePatch, as SQLite's jsonMergePatch does it on JSONB."""
    if depth == 0:
        make_room(len(patch))
    if patch[i_patch] & 0x0F != OBJECT:
        n, sz = payload_size(patch, i_patch)
        size_patch = n + sz
        n, sz = payload_size(target.blob, i_target)
        target.replace(i_target, n + sz, patch[i_patch:i_patch + size_patch])
        return
    blob = target.blob
    if blob[i_target] & 0x0F != OBJECT:
        n, sz = payload_size(blob, i_target)
        target.replace(i_target + n, sz, b"")
        blob[i_target] = (blob[i_target] & 0xF0) | OBJECT
    n, sz = payload_size(patch, i_patch)
    cursor = i_patch + n
    patch_end = cursor + sz
    n, sz = payload_size(blob, i_target, len(blob) - target.delta)  # (its header is not updated yet)
    t_start = i_target + n
    t_end_before = t_start + sz
    while cursor < patch_end:
        p_label = cursor
        e_label = patch[cursor] & 0x0F
        if e_label < TEXT or e_label > TEXTRAW:
            raise BadPatch()
        n_label, size_label = payload_size(patch, cursor)
        if n_label == 0:
            raise BadPatch()
        p_value = cursor + n_label + size_label
        if p_value >= patch_end:
            raise BadPatch()
        n_value, size_value = payload_size(patch, p_value)
        if n_value == 0:
            raise BadPatch()
        cursor = p_value + n_value + size_value
        if cursor > patch_end:
            raise BadPatch()
        t_cursor = t_start
        t_end = t_end_before + target.delta
        while t_cursor < t_end:
            t_label = t_cursor
            et = blob[t_cursor] & 0x0F
            if et < TEXT or et > TEXTRAW:
                raise BadPatch()  # (SQLite's JSON_MERGE_BADTARGET: the same "malformed JSON")
            nt_label, st_label = payload_size(blob, t_cursor)
            if nt_label == 0:
                raise BadPatch()
            t_value = t_label + nt_label + st_label
            if t_value >= t_end:
                raise BadPatch()
            nt_value, st_value = payload_size(blob, t_value)
            if nt_value == 0 or t_value + nt_value + st_value > t_end:
                raise BadPatch()
            if label_equal(bytes(patch[p_label + n_label:p_label + n_label + size_label]), e_label in (TEXT, TEXTRAW),
                           bytes(blob[t_label + nt_label:t_label + nt_label + st_label]), et in (TEXT, TEXTRAW)):
                break
            t_cursor = t_value + nt_value + st_value
        x = patch[p_value] & 0x0F
        if t_cursor < t_end:
            if x == NULL:
                target.replace(t_label, nt_label + st_label + nt_value + st_value, b"")
            else:
                saved = target.delta
                target.delta = 0
                if depth >= MAX_DEPTH:
                    raise PatchTooDeep()
                merge_patch(target, t_value, patch, p_value, depth + 1)
                target.delta += saved
        elif x != NULL:
            new_label = bytes(patch[p_label:p_label + n_label + size_label])
            if x != OBJECT:
                target.replace(t_end, 0, new_label + bytes(patch[p_value:p_value + n_value + size_value]))
            else:
                target.replace(t_end, 0, new_label + b"\x0c")
                saved = target.delta
                target.delta = 0
                if depth >= MAX_DEPTH:
                    raise PatchTooDeep()
                merge_patch(target, t_end + len(new_label), patch, p_value, depth + 1)
                target.delta += saved
    if target.delta:
        target.after_edit(i_target)


# ---- paths for json_each() / json_tree() -----------------------------------------------------------

def path_label(label: bytes) -> bytes:
    """How a label appears in a fullkey / path (SQLite's jsonAppendObjectPathElement):
    quoted, as stored, unless it is a letter and letters and digits."""
    if label and label[0] in _ALPHA and all(c in _ALNUM for c in label):
        return b"." + label
    return b'."' + label + b'"'


_ALPHA = _ALNUM - _DIGITS


def error(message: str) -> OperationalError:
    return OperationalError(message)
