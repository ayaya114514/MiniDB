"""SQLite's JSON SQL functions on top of minidb.jsonb: json(), jsonb(),
json_extract() and the ``->`` / ``->>`` operators, the editing functions,
json_each() / json_tree(), the aggregates.  The function bodies follow
json.c, including which errors they raise and when a result carries the
JSON subtype (minidb.jsonb.JSONText)."""

from __future__ import annotations

import re
from typing import Callable, Iterator

from minidb import jsonb, values
from minidb.errors import OperationalError
from minidb.jsonb import (
    ARRAY, EDIT_DEL, EDIT_INS, EDIT_REPL, EDIT_SET, FALSE, FLOAT, FLOAT5, INT, INT5, LOOKUP_ERROR, LOOKUP_NOTFOUND,
    LOOKUP_PATHERROR, NULL, OBJECT, TEXT, TEXT5, TEXTJ, TEXTRAW, TRUE, JSONText, Malformed, payload_size,
)
from minidb.values import SQLValue

INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1


def _bytes(text: str) -> bytes:
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass")


def _text(data: bytes | bytearray) -> str:
    try:
        return bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return bytes(data).decode("utf-8", "surrogateescape")


def _json(data: bytes | bytearray) -> JSONText:
    return JSONText(_text(data))


def malformed() -> OperationalError:
    return OperationalError("malformed JSON")


def bad_path(path: object) -> OperationalError:
    text = values.to_text(path)
    return OperationalError("bad JSON path: '" + text.replace("'", "''") + "'")  # (SQLite's %Q)


# ---- arguments ---------------------------------------------------------------------------

def parse_arg(value: SQLValue, keep_error: bool = False) -> tuple[bytes, bool] | None:
    """The JSONB of a JSON argument and whether it used JSON5 (SQLite's
    jsonParseFuncArg): None for NULL.  A BLOB is JSONB if it looks like it,
    else its bytes are taken as text.  Raises "malformed JSON" (``keep_error``:
    raises Malformed instead)."""
    if value is None:
        return None
    if isinstance(value, bytes):
        if jsonb.is_jsonb(value):
            return value, False
        data = value
    else:
        data = _bytes(values.to_text(value))
    try:
        if not data:
            raise Malformed(0)
        return jsonb.parse_text(data)
    except Malformed:
        if keep_error:
            raise
        raise malformed() from None


def might_be_binary(value: SQLValue) -> bool:
    """SQLite's jsonFuncArgMightBeBinary: a BLOB whose first header fits it exactly."""
    if not isinstance(value, bytes) or not value or value[0] & 0x0F > OBJECT:
        return False
    n, sz = payload_size(value, 0)
    if n == 0 or sz + n != len(value):
        return False
    return not (value[0] & 0x0F <= FALSE and sz > 0)


def real_text(value: float) -> str:
    return values.to_text(value)


def append_sql_value(out: bytearray, value: SQLValue) -> None:
    """SQLite's jsonAppendSqlValue: an SQL value as JSON text."""
    if value is None:
        out += b"null"
    elif isinstance(value, bool):
        out += str(int(value)).encode()
    elif isinstance(value, int):
        out += str(value).encode()
    elif isinstance(value, float):
        out += _real_json(value)
    elif isinstance(value, str):
        if isinstance(value, JSONText):
            out += _bytes(value)
        else:
            out += jsonb.quote_string(_bytes(value))
    else:
        if might_be_binary(value):
            renderer = jsonb.Renderer(value)
            renderer.element(0)
            if renderer.error:
                raise malformed()
            out += renderer.out
        else:
            raise OperationalError("JSON cannot hold BLOB values")


def _real_json(value: float) -> bytes:
    if value != value:
        return b"null"
    if value in (float("inf"), float("-inf")):
        return b"9.0e+999" if value > 0 else b"-9.0e+999"
    return values.to_text(value).encode()


def arg_to_blob(value: SQLValue) -> bytes:
    """SQLite's jsonFunctionArgToBlob: a value to put into JSON, as JSONB."""
    if value is None:
        return b"\x00"
    if isinstance(value, bytes):
        if not jsonb.is_jsonb(value):
            raise OperationalError("JSON cannot hold BLOB values")
        return value
    if isinstance(value, str):
        if isinstance(value, JSONText):
            try:
                return jsonb.parse_text(_bytes(value))[0]
            except Malformed:
                raise malformed() from None
        return jsonb.node(TEXTRAW, _bytes(value))
    if isinstance(value, float):
        if value != value:
            return b"\x00"
        text = values.to_text(value)
        if text.startswith("I") or value == float("inf"):
            return jsonb.node(FLOAT, b"9e999")
        if text.startswith("-I") or value == float("-inf"):
            return jsonb.node(FLOAT, b"-9e999")
        return jsonb.node(FLOAT, text.encode())
    return jsonb.node(INT, str(int(value)).encode())


def render(blob: bytes | bytearray, i: int = 0) -> JSONText:
    try:
        return _json(jsonb.Renderer(blob).render(i))
    except Malformed:
        raise malformed() from None


def result_parse(blob: bytes | bytearray, binary: bool) -> SQLValue:
    """SQLite's jsonReturnParse: the JSONB, or its text with the JSON subtype."""
    return bytes(blob) if binary else render(blob)


def from_blob(blob: bytes | bytearray, i: int, text_only: bool = False, binary: bool = False) -> SQLValue:
    """SQLite's jsonReturnFromBlob: the SQL value of the element at ``i``."""
    n, sz = payload_size(blob, i)
    if n == 0:
        raise malformed()
    kind = blob[i] & 0x0F
    payload = bytes(blob[i + n:i + n + sz])
    if kind == NULL:
        return None
    if kind == TRUE:
        return 1
    if kind == FALSE:
        return 0
    if kind in (INT, INT5):
        if sz == 0:
            raise malformed()
        negative = payload[:1] == b"-"
        digits = payload[1:] if negative else payload
        if negative and sz < 2:
            raise malformed()
        result = _dec_or_hex(digits)
        if result is None:
            raise malformed()
        if result == "big":
            if negative and digits.lower() == b"9223372036854775808":
                return INT64_MIN
            return _to_double(payload)
        if isinstance(result, float):
            return -result if negative else result
        return -result if negative else result
    if kind in (FLOAT, FLOAT5):
        if sz == 0:
            raise malformed()
        return _to_double(payload)
    if kind in (TEXT, TEXTRAW):
        return _text(payload)
    if kind in (TEXTJ, TEXT5):
        return _text(jsonb.unescape(payload))
    if kind in (ARRAY, OBJECT):
        if binary and not text_only:
            return bytes(blob[i:i + n + sz])
        return render(blob, i)
    raise malformed()


def _dec_or_hex(digits: bytes) -> int | float | str | None:
    """SQLite's sqlite3DecOrHexToI64 for a JSON integer: an int, a float for
    hexadecimal with the high bit set, "big" when too large, None if not a number."""
    if digits[:2] in (b"0x", b"0X"):
        hex_digits = digits[2:]
        if not hex_digits or any(c not in b"0123456789abcdefABCDEF" for c in hex_digits):
            return None
        value = int(hex_digits, 16) if len(hex_digits.lstrip(b"0")) <= 16 else int(hex_digits[-16:], 16)
        value &= (1 << 64) - 1
        if value >> 63:
            return float(value)
        return value
    if not digits or any(c not in b"0123456789" for c in digits):
        return None
    value = int(digits)
    if value > INT64_MAX:
        return "big"
    return value


def _to_double(payload: bytes) -> float:
    try:
        return float(payload.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise malformed() from None


# ---- path arguments -------------------------------------------------------------------------

def path_bytes(path: SQLValue) -> bytes:
    return _bytes(values.to_text(path))


def abbreviated_path(path: SQLValue) -> bytes:
    """The ``->`` and ``->>`` operators' right side as a path (after '$')."""
    text = path_bytes(path)
    if text[:1] == b"$":
        return text[1:]
    if isinstance(path, int) and not isinstance(path, bool):
        return b"[" + (b"#" if text[:1] == b"-" else b"") + text + b"]"
    if all(c in b"_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in text):
        return b"." + text
    if text[:1] == b"[" and len(text) >= 3 and text[-1:] == b"]":
        return text
    return b'."' + text + b'"'


# ---- scalar functions ----------------------------------------------------------------------------

def json_(value: SQLValue) -> SQLValue:
    parsed = parse_arg(value)
    return None if parsed is None else result_parse(parsed[0], False)


def jsonb_(value: SQLValue) -> SQLValue:
    parsed = parse_arg(value)
    return None if parsed is None else result_parse(parsed[0], True)


def json_array(*args: SQLValue) -> JSONText:
    out = bytearray(b"[")
    for i, arg in enumerate(args):
        if i:
            out.append(0x2C)
        append_sql_value(out, arg)
    out.append(0x5D)
    return _json(out)


def jsonb_array(*args: SQLValue) -> bytes:
    return _text_to_blob(json_array(*args))


def json_object(*args: SQLValue) -> JSONText:
    if len(args) & 1:
        raise OperationalError("json_object() requires an even number of arguments")
    out = bytearray(b"{")
    for i in range(0, len(args), 2):
        label = args[i]
        if not isinstance(label, str):
            raise OperationalError("json_object() labels must be TEXT")
        if i:
            out.append(0x2C)
        out += jsonb.quote_string(_bytes(label))
        out.append(0x3A)
        append_sql_value(out, args[i + 1])
    out.append(0x7D)
    return _json(out)


def jsonb_object(*args: SQLValue) -> bytes:
    return _text_to_blob(json_object(*args))


def _text_to_blob(text: str) -> bytes:
    try:
        return jsonb.parse_text(_bytes(text))[0]
    except Malformed:
        raise malformed() from None


def json_quote(value: SQLValue) -> JSONText:
    out = bytearray()
    append_sql_value(out, value)
    return _json(out)


def json_array_length(value: SQLValue, path: SQLValue = None, *, with_path: bool = False) -> SQLValue:
    parsed = parse_arg(value)
    if parsed is None:
        return None
    blob = parsed[0]
    i = 0
    if with_path:
        if path is None:
            return None
        text = path_bytes(path)
        i, _ = jsonb.lookup(blob, text[1:] if text[:1] == b"$" else b"@")
        if i < 0:
            if i == LOOKUP_PATHERROR:
                raise bad_path(path)
            if i == LOOKUP_ERROR:
                raise malformed()
            return None
    if blob[i] & 0x0F == ARRAY:
        return jsonb.Editor(blob).array_count(i)
    return 0


def json_type(value: SQLValue, path: SQLValue = None, *, with_path: bool = False) -> SQLValue:
    parsed = parse_arg(value)
    if parsed is None:
        return None
    blob = parsed[0]
    i = 0
    if with_path:
        if path is None:
            return None
        text = path_bytes(path)
        if text[:1] != b"$":
            raise bad_path(path)
        i, _ = jsonb.lookup(blob, text[1:])
        if i < 0:
            if i == LOOKUP_NOTFOUND:
                return None
            if i == LOOKUP_PATHERROR:
                raise bad_path(path)
            raise malformed()
    return jsonb.TYPE_NAMES[blob[i] & 0x0F]


def json_valid(value: SQLValue, flags: SQLValue = 1) -> SQLValue:
    flags = values.to_int64(values.numeric_affinity(flags)) if flags is not None else 0
    if not isinstance(flags, int) or flags < 1 or flags > 15:
        raise OperationalError("FLAGS parameter to json_valid() must be between 1 and 15")
    if value is None:
        return None
    if isinstance(value, bytes) and jsonb.is_jsonb(value):
        if flags & 0x04:
            return 1
        if flags & 0x08:
            return int(jsonb.validity_check(value, 0, len(value)) == 0)
        return 0
    if flags & 0x03 == 0:
        return 0
    try:
        parsed = parse_arg(value, keep_error=True)
    except Malformed:
        return 0
    return int(bool(flags & 0x02) or not parsed[1])


def json_error_position(value: SQLValue) -> SQLValue:
    if might_be_binary(value):
        return jsonb.validity_check(value, 0, len(value))
    if value is None:
        return None
    data = value if isinstance(value, bytes) else _bytes(values.to_text(value))
    parser = jsonb.TextParser(data)
    try:
        parser.parse()
    except Malformed as error:
        position = 0
        for c in data[:error.position]:
            if c == 0:
                break
            if c & 0xC0 != 0x80:
                position += 1
        return position + 1
    return 0


def extract(value: SQLValue, paths: tuple, mode: str) -> SQLValue:
    """json_extract() ("sql"), jsonb_extract() ("blob"), ``->`` ("json", one
    abbreviated path) and ``->>`` ("sql", abbreviated)."""
    parsed = parse_arg(value)
    if parsed is None:
        return None
    blob = parsed[0]
    abbreviated = mode in ("arrow", "arrow2")
    many = len(paths) > 1
    out = bytearray(b"[") if many else None
    for k, path in enumerate(paths):
        if path is None:
            return None
        text = path_bytes(path)
        if text[:1] == b"$":
            j, _ = jsonb.lookup(blob, text[1:])
        elif abbreviated:
            j, _ = jsonb.lookup(blob, abbreviated_path(path))
        else:
            raise bad_path(path)
        if j >= 0:
            if not many:
                if mode == "arrow":
                    return render(blob, j)
                result = from_blob(blob, j, binary=(mode == "blob"))
                if mode == "sql" and blob[j] & 0x0F >= ARRAY and isinstance(result, str):
                    return JSONText(result)
                if mode == "arrow2" and isinstance(result, JSONText):
                    return str.__str__(result)
                return result
            if k:
                out.append(0x2C)
            renderer = jsonb.Renderer(blob)
            renderer.element(j)
            if renderer.error:
                raise malformed()
            out += renderer.out
        elif j == LOOKUP_NOTFOUND:
            if not many:
                return None
            if k:
                out.append(0x2C)
            out += b"null"
        elif j == LOOKUP_ERROR:
            raise malformed()
        else:
            raise bad_path(path)
    out.append(0x5D)
    return _text_to_blob(_text(out)) if mode == "blob" else _json(out)


def json_extract(value: SQLValue, *paths: SQLValue) -> SQLValue:
    return extract(value, paths, "sql") if paths else None


def jsonb_extract(value: SQLValue, *paths: SQLValue) -> SQLValue:
    return extract(value, paths, "blob") if paths else None


def arrow(value: SQLValue, path: SQLValue) -> SQLValue:
    return extract(value, (path,), "arrow")


def arrow2(value: SQLValue, path: SQLValue) -> SQLValue:
    return extract(value, (path,), "arrow2")


def edit(value: SQLValue, args: tuple, how: int, name: str, binary: bool) -> SQLValue:
    """json_insert() / json_replace() / json_set() (SQLite's jsonInsertIntoBlob)."""
    if not len(args) & 1 == 0:
        raise OperationalError(f"json_{name}() needs an odd number of arguments")
    parsed = parse_arg(value)
    if parsed is None:
        return None
    editor = jsonb.Editor(parsed[0])
    for i in range(0, len(args), 2):
        path = args[i]
        if path is None:
            continue
        text = path_bytes(path)
        if text[:1] != b"$":
            raise bad_path(path)
        insert = arg_to_blob(args[i + 1])
        if len(text) == 1:
            if how in (EDIT_REPL, EDIT_SET):
                editor.blob[:] = insert
            rc = 0
        else:
            editor.edit, editor.insert, editor.delta = how, insert, 0
            rc = editor.lookup(0, text[1:], 0)
        if rc == LOOKUP_NOTFOUND:
            continue
        if rc < 0:
            if rc == LOOKUP_ERROR:
                raise malformed()
            raise bad_path(path)
    return result_parse(editor.blob, binary)


def json_remove(value: SQLValue, *paths: SQLValue, binary: bool = False) -> SQLValue:
    parsed = parse_arg(value)
    if parsed is None:
        return None
    editor = jsonb.Editor(parsed[0])
    for path in paths:
        if path is None:
            return None
        text = path_bytes(path)
        if text[:1] != b"$":
            raise bad_path(path)
        if len(text) == 1:
            return None
        editor.edit, editor.delta = EDIT_DEL, 0
        rc = editor.lookup(0, text[1:], 0)
        if rc < 0:
            if rc == LOOKUP_NOTFOUND:
                continue
            if rc == LOOKUP_PATHERROR:
                raise bad_path(path)
            raise malformed()
    return result_parse(editor.blob, binary)


def json_patch(target: SQLValue, patch: SQLValue, binary: bool = False) -> SQLValue:
    parsed = parse_arg(target)
    if parsed is None:
        return None
    patched = parse_arg(patch)
    if patched is None:
        return None
    editor = jsonb.Editor(parsed[0])
    try:
        jsonb.merge_patch(editor, 0, patched[0], 0)
    except (jsonb.BadPatch, IndexError):
        raise malformed() from None
    return result_parse(editor.blob, binary)


def json_pretty(value: SQLValue, indent: SQLValue = None) -> SQLValue:
    parsed = parse_arg(value)
    if parsed is None:
        return None
    unit = b"    " if indent is None else _bytes(values.to_text(indent))
    try:
        return _text(jsonb.Renderer(parsed[0], unit).render(0))
    except Malformed:
        raise malformed() from None


SCALAR_FUNCTIONS = {
    "JSON": (json_, 1, 1),
    "JSONB": (jsonb_, 1, 1),
    "JSON_ARRAY": (json_array, 0, None),
    "JSONB_ARRAY": (jsonb_array, 0, None),
    "JSON_OBJECT": (json_object, 0, None),
    "JSONB_OBJECT": (jsonb_object, 0, None),
    "JSON_QUOTE": (json_quote, 1, 1),
    "JSON_ARRAY_LENGTH": (lambda value, *path: json_array_length(value, *path, with_path=bool(path)), 1, 2),
    "JSON_TYPE": (lambda value, *path: json_type(value, *path, with_path=bool(path)), 1, 2),
    "JSON_VALID": (json_valid, 1, 2),
    "JSON_ERROR_POSITION": (json_error_position, 1, 1),
    "JSON_EXTRACT": (json_extract, 1, None),
    "JSONB_EXTRACT": (jsonb_extract, 1, None),
    "->": (arrow, 2, 2),
    "->>": (arrow2, 2, 2),
    "JSON_INSERT": (lambda value, *args: edit(value, args, EDIT_INS, "insert", False), 1, None),
    "JSONB_INSERT": (lambda value, *args: edit(value, args, EDIT_INS, "insert", True), 1, None),
    "JSON_REPLACE": (lambda value, *args: edit(value, args, EDIT_REPL, "replace", False), 1, None),
    "JSONB_REPLACE": (lambda value, *args: edit(value, args, EDIT_REPL, "replace", True), 1, None),
    "JSON_SET": (lambda value, *args: edit(value, args, EDIT_SET, "set", False), 1, None),
    "JSONB_SET": (lambda value, *args: edit(value, args, EDIT_SET, "set", True), 1, None),
    "JSON_REMOVE": (lambda value, *paths: json_remove(value, *paths), 1, None),
    "JSONB_REMOVE": (lambda value, *paths: json_remove(value, *paths, binary=True), 1, None),
    "JSON_PATCH": (json_patch, 2, 2),
    "JSONB_PATCH": (lambda target, patch: json_patch(target, patch, True), 2, 2),
    "JSON_PRETTY": (json_pretty, 1, 2),
}


# The functions whose results have the JSON subtype (when they are text).
SUBTYPE_FUNCTIONS = frozenset({
    "JSON", "JSON_ARRAY", "JSON_OBJECT", "JSON_QUOTE", "JSON_EXTRACT", "->", "JSON_INSERT", "JSON_REPLACE",
    "JSON_SET", "JSON_REMOVE", "JSON_PATCH", "JSON_GROUP_ARRAY", "JSON_GROUP_OBJECT",
})


# ---- aggregates ----------------------------------------------------------------------------------

class GroupArray:
    """json_group_array() / jsonb_group_array(), also as a window function
    (its inverse cuts the first element off the text, as SQLite's does)."""

    def __init__(self, binary: bool = False) -> None:
        self.text = None
        self.binary = binary

    def step(self, value: SQLValue, *_: SQLValue) -> None:
        if self.text is None:
            self.text = bytearray(b"[")
        elif len(self.text) > 1:
            self.text.append(0x2C)
        append_sql_value(self.text, value)

    def inverse(self, args: tuple) -> None:
        if self.text is None:
            return
        _drop_first(self.text)

    def result(self) -> SQLValue:
        text = (self.text or bytearray(b"[")) + b"]" if self.text is not None else bytearray(b"[]")
        if self.binary:
            return _text_to_blob(_text(text))
        return _json(text)

    value = result


class GroupObject(GroupArray):
    def step(self, label: SQLValue, value: SQLValue = None, *_: SQLValue) -> None:
        key = None if label is None else values.to_text(label)
        if key is not None and "\x00" in key:
            key = key[:key.index("\x00")]  # (sqlite3Strlen30)
        if self.text is None:
            self.text = bytearray(b"{")
        elif len(self.text) > 1 and key is not None:
            self.text.append(0x2C)
        if key is not None:
            self.text += jsonb.quote_string(_bytes(key))
            self.text.append(0x3A)
            append_sql_value(self.text, value)

    def result(self) -> SQLValue:
        text = self.text + b"}" if self.text is not None else bytearray(b"{}")
        if self.binary:
            return _text_to_blob(_text(text))
        return _json(text)

    value = result


def _drop_first(text: bytearray) -> None:
    """SQLite's jsonGroupInverse: remove the first element (or member) of the text."""
    in_string, nest = False, 0
    i = 1
    while i < len(text):
        c = text[i]
        if c == 0x2C and not in_string and not nest:
            break
        if c == 0x22:
            in_string = not in_string
        elif c == 0x5C:
            i += 1
        elif not in_string:
            if c in (0x7B, 0x5B):
                nest += 1
            elif c in (0x7D, 0x5D):
                nest -= 1
        i += 1
    if i < len(text):
        del text[1:i + 1]
    else:
        del text[1:]


AGGREGATES = {
    "JSON_GROUP_ARRAY": (GroupArray, 1, 1),
    "JSONB_GROUP_ARRAY": (lambda: GroupArray(True), 1, 1),
    "JSON_GROUP_OBJECT": (GroupObject, 2, 2),
    "JSONB_GROUP_OBJECT": (lambda: GroupObject(True), 2, 2),
}
values.AGGREGATE_FUNCTIONS.update(AGGREGATES)


class WindowJsonGroup:
    """json_group_array() / json_group_object() as window functions."""

    def __init__(self, factory: Callable[[], GroupArray]) -> None:
        self.aggregate = factory()

    def step(self, args: tuple) -> None:
        self.aggregate.step(*args)

    def inverse(self, args: tuple) -> None:
        self.aggregate.inverse(args)

    def value(self) -> SQLValue:
        return self.aggregate.result()

    finalize = value


# ---- json_each() / json_tree() ---------------------------------------------------------------------

EACH_COLUMNS = ["key", "value", "type", "atom", "id", "parent", "fullkey", "path", "json", "root"]


def each_rows(value: SQLValue, root: SQLValue, recursive: bool, with_root: bool,
              binary: bool = False) -> Iterator[list]:
    """The rows of json_each(value[, root]) or json_tree(...): SQLite's
    JsonEachCursor, step by step (key, value, type, atom, id, parent,
    fullkey, path, json, root); ``binary`` for jsonb_each / jsonb_tree,
    whose container values are JSONB."""
    parsed = parse_arg(value)
    if parsed is None:
        return
    blob = parsed[0]
    root_path = b"$"
    i = position = 0
    kind = 0
    if with_root:
        if root is None:
            return
        root_path = path_bytes(root)
        if root_path[:1] != b"$":
            raise bad_path(root)
        if len(root_path) > 1:
            found, editor = jsonb.lookup(blob, root_path[1:])
            if found < 0:
                if found == LOOKUP_NOTFOUND:
                    return
                raise bad_path(root)
            if editor.label:
                position, kind = editor.label, OBJECT
            else:
                position, kind = found, ARRAY
            i = found
    json_column = value if isinstance(value, bytes) else values.to_text(value)
    root_text = _text(root_path)
    cursor = EachCursor(blob, recursive, root_path, json_column, root_text, binary)
    cursor.start(i, position, kind)
    yield from cursor.rows()


def _atoi64(text: bytes) -> int:
    """SQLite's sqlite3Atoi64 as json_each() uses it: the leading integer, 0 if none."""
    match = re.match(rb"\s*([+-]?)0*([0-9]*)", text)
    digits = match.group(2) or b"0"
    value = int(digits) * (-1 if match.group(1) == b"-" else 1)
    return max(INT64_MIN, min(INT64_MAX, value))


class EachCursor:
    def __init__(self, blob: bytes, recursive: bool, root_path: bytes, json_column: SQLValue,
                 root_text: str, binary: bool) -> None:
        self.blob = blob
        self.binary = binary
        self.recursive = recursive
        self.path = bytearray(root_path)
        self.root_length = len(root_path)
        self.json_column = json_column
        self.root_text = root_text
        self.parents = []  # [key, end, head, value, path length]
        self.rowid = 0

    def start(self, i: int, position: int, kind: int) -> None:
        blob = self.blob
        self.i, self.kind = position, kind
        n, sz = payload_size(blob, i)
        self.end = i + n + sz
        if blob[i] & 0x0F >= ARRAY and not self.recursive:
            self.i = i + n
            self.kind = blob[i] & 0x0F
            self.parents = [[0, self.end, self.i, i, 0]]

    def skip_label(self) -> int:
        if self.kind == OBJECT:
            n, sz = payload_size(self.blob, self.i)
            return self.i + n + sz
        return self.i

    def append_path_name(self) -> None:
        if self.kind == ARRAY:
            self.path += b"[%d]" % self.parents[-1][0]
        else:
            n, sz = payload_size(self.blob, self.i)
            label = bytes(self.blob[self.i + n:self.i + n + sz])
            self.path += jsonb.path_label(label)

    def path_length(self) -> int:
        n = len(self.path)
        z = self.path
        if self.rowid == 0 and self.recursive and n >= 2:
            while n > 1:
                n -= 1
                if z[n] in (0x5B, 0x2E):
                    x, _ = jsonb.lookup(self.blob, bytes(z[1:n]))
                    if x < 0:
                        continue
                    hn, hsz = payload_size(self.blob, x)
                    if x + hn == self.i:
                        break
        return n

    def row(self) -> list:
        blob = self.blob
        # key
        key = None
        if not self.parents:
            if self.root_length != 1:
                j = self.path_length()
                n = self.root_length - j
                if n:
                    tail = bytes(self.path[j:self.root_length])
                    if tail[:1] == b"[":
                        key = _atoi64(tail[1:n])
                    elif tail[1:2] == b'"':
                        key = _text(tail[2:n - 1])
                    else:
                        key = _text(tail[1:n])
        elif self.kind == OBJECT:
            key = from_blob(blob, self.i, text_only=True)
        else:
            key = self.parents[-1][0]
        i = self.skip_label()
        element = blob[i] & 0x0F
        if element >= ARRAY and self.binary:
            n, sz = payload_size(blob, i)
            value = bytes(blob[i:i + n + sz])
        else:
            value = from_blob(blob, i, text_only=True)
            if element >= ARRAY and isinstance(value, str):
                value = JSONText(value)
        atom = from_blob(blob, i, text_only=True) if element < ARRAY else None
        parent = self.parents[-1][2] if self.parents and self.recursive else None
        base = len(self.path)
        if self.parents:
            self.append_path_name()
        fullkey = _text(self.path)
        del self.path[base:]
        path = _text(self.path[:self.path_length()])
        return [key, value, jsonb.TYPE_NAMES[element], atom, self.i, parent, fullkey, path,
                self.json_column, self.root_text]

    def advance(self) -> None:
        blob = self.blob
        if self.recursive:
            level_change = False
            i = self.skip_label()
            x = blob[i] & 0x0F
            n, sz = payload_size(blob, i)
            if x in (OBJECT, ARRAY):
                level_change = True
                parent = [-1, i + n + sz, self.i, i, len(self.path)]
                if self.kind and self.parents:
                    self.append_path_name()
                self.parents.append(parent)
                self.i = i + n
            else:
                self.i = i + n + sz
            while self.parents and self.i >= self.parents[-1][1]:
                parent = self.parents.pop()
                del self.path[parent[4]:]
                level_change = True
            if level_change:
                self.kind = blob[self.parents[-1][3]] & 0x0F if self.parents else 0
        else:
            i = self.skip_label()
            n, sz = payload_size(blob, i)
            self.i = i + n + sz
        if self.kind == ARRAY and self.parents:
            self.parents[-1][0] += 1
        self.rowid += 1

    def rows(self) -> Iterator[list]:
        while self.i < self.end:
            yield self.row()
            self.advance()
