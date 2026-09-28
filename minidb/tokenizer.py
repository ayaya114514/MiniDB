"""SQL lexical analysis: turns a string into a list of tokens."""

from __future__ import annotations

from dataclasses import dataclass

from minidb.errors import OperationalError
from minidb.values import ascii_upper

KEYWORDS = {
    "ALL", "ANALYZE", "AND", "AS", "ASC", "BEGIN", "BETWEEN", "BY", "CASE", "CAST", "COMMIT", "CREATE",
    "CROSS", "DELETE", "DESC", "DISTINCT", "DROP", "ELSE", "END", "EXCEPT", "EXISTS", "EXPLAIN",
    "FROM", "GROUP", "HAVING", "IF", "IN", "INDEX", "INNER", "INSERT", "INTERSECT", "INTO", "IS",
    "JOIN", "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT", "NULL", "OFFSET", "ON", "OR", "ORDER",
    "OUTER", "PRIMARY", "ROLLBACK", "SELECT", "SET", "TABLE", "THEN", "TRANSACTION", "UNION",
    "UNIQUE", "UPDATE", "USING", "VALUES", "WHEN", "WHERE",
}

# Longest operators first so that "<=" wins over "<".
OPERATORS = ["<<", ">>", "<>", "<=", ">=", "==", "!=", "||", "<", ">", "=", "+", "-", "*", "/",
             "%", "&", "|", "~", "(", ")", ",", ";", "."]


class SQLSyntaxError(OperationalError):
    """A lexical or syntax error at a position in the SQL text."""

    def __init__(self, message: str, text: str, pos: int) -> None:
        self.message = message
        self.text = text
        self.pos = pos
        self.line = text.count("\n", 0, pos) + 1
        self.column = pos - (text.rfind("\n", 0, pos) + 1) + 1
        super().__init__(f"{message} (line {self.line}, column {self.column})")

    def caret(self) -> str:
        """The offending source line with a ``^`` under the error position."""
        start = self.text.rfind("\n", 0, self.pos) + 1
        end = self.text.find("\n", self.pos)
        line = self.text[start:] if end == -1 else self.text[start:end]
        return line + "\n" + " " * (self.column - 1) + "^"


@dataclass
class Token:
    kind: str     # KEYWORD, IDENT, INTEGER, FLOAT, STRING, BLOB, OP, PARAM or EOF
    value: object  # keyword in upper case, identifier name, number, string,
                   # operator, or for PARAM the text after "?" / the whole ":name"
    pos: int
    text: str     # the exact source text of the token


def tokenize(text: str) -> list[Token]:
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise SQLSyntaxError("unterminated comment", text, i)
            i = end + 2
            continue
        start = i
        if ch in "xX" and text.startswith("'", i + 1):  # BLOB literal x'hex'
            end = text.find("'", i + 2)
            digits = text[i + 2:end] if end != -1 else ""
            if end == -1 or len(digits) % 2 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                bad = text[start:end + 1] if end != -1 else text[start:]
                raise SQLSyntaxError(f'unrecognized token: "{bad}"', text, start)
            i = end + 1
            tokens.append(Token("BLOB", bytes.fromhex(digits), start, text[start:i]))
            continue
        if ch.isalpha() or ch == "_":
            while i < n and (text[i].isalnum() or text[i] in "_$"):
                i += 1
            word = text[start:i]
            if ascii_upper(word) in KEYWORDS:
                tokens.append(Token("KEYWORD", ascii_upper(word), start, word))
            else:
                tokens.append(Token("IDENT", word, start, word))
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            tokens.append(_number(text, i))
            i += len(tokens[-1].text)
            continue
        if ch == "'":
            value, i = _quoted(text, i, "'")
            tokens.append(Token("STRING", value, start, text[start:i]))
            continue
        if ch == "?":
            i += 1
            while i < n and text[i].isdigit():
                i += 1
            tokens.append(Token("PARAM", text[start:i], start, text[start:i]))
            continue
        if ch in ":@$" and i + 1 < n and (text[i + 1].isalnum() or text[i + 1] == "_"):
            i += 1
            while i < n and (text[i].isalnum() or text[i] in "_$"):
                i += 1
            tokens.append(Token("PARAM", text[start:i], start, text[start:i]))
            continue
        if ch in "\"`[":
            value, i = _quoted(text, i, "]" if ch == "[" else ch)
            tokens.append(Token("IDENT", value, start, text[start:i]))
            continue
        for op in OPERATORS:
            if text.startswith(op, i):
                tokens.append(Token("OP", op, start, op))
                i += len(op)
                break
        else:
            raise SQLSyntaxError(f"unrecognized character {ch!r}", text, i)
    tokens.append(Token("EOF", None, n, ""))
    return tokens


def _number(text: str, start: int) -> Token:
    i = start
    n = len(text)
    while i < n and text[i].isdigit():
        i += 1
    is_float = False
    if i < n and text[i] == ".":
        is_float = True
        i += 1
        while i < n and text[i].isdigit():
            i += 1
    if i < n and text[i] in "eE":
        j = i + 1
        if j < n and text[j] in "+-":
            j += 1
        if j < n and text[j].isdigit():
            is_float = True
            i = j
            while i < n and text[i].isdigit():
                i += 1
    if i < n and (text[i].isalpha() or text[i] == "_"):
        raise SQLSyntaxError("malformed number", text, start)
    literal = text[start:i]
    if is_float:
        return Token("FLOAT", float(literal), start, literal)
    value = int(literal)
    if value >= 2**63:
        # Like SQLite, integer literals too large for 64 bits become REAL.
        return Token("FLOAT", float(literal), start, literal)
    return Token("INTEGER", value, start, literal)


def _quoted(text: str, start: int, close: str) -> tuple[str, int]:
    """Read a quoted string or identifier; a doubled quote stands for itself."""
    i = start + 1
    parts = []
    while True:
        end = text.find(close, i)
        if end == -1:
            what = "string" if close == "'" else "identifier"
            raise SQLSyntaxError(f"unterminated {what}", text, start)
        parts.append(text[i:end])
        if close != "]" and text.startswith(close * 2, end):
            parts.append(close)
            i = end + 2
            continue
        return "".join(parts), end + 1
