"""SQL lexical analysis: turns a string into a list of tokens."""

from __future__ import annotations

import re
from dataclasses import dataclass

from minidb.errors import OperationalError
from minidb.fp import atof
from minidb.values import ascii_upper

KEYWORDS = {
    "ALL", "ANALYZE", "AND", "AS", "ASC", "BEGIN", "BETWEEN", "BY", "CASE", "CAST", "COMMIT", "CREATE",
    "CROSS", "DELETE", "DESC", "DISTINCT", "DROP", "ELSE", "END", "EXCEPT", "EXISTS", "EXPLAIN",
    "FROM", "GROUP", "HAVING", "IF", "IN", "INDEX", "INNER", "INSERT", "INTERSECT", "INTO", "IS",
    "JOIN", "LEFT", "LIKE", "LIMIT", "NATURAL", "NOT", "NULL", "OFFSET", "ON", "OR", "ORDER",
    "OUTER", "PRIMARY", "RETURNING", "ROLLBACK", "SELECT", "SET", "TABLE", "THEN", "TRANSACTION", "UNION",
    "UNIQUE", "UPDATE", "USING", "VALUES", "WHEN", "WHERE",
}

# Longest operators first so that "<=" wins over "<".
OPERATORS = ["->>", "->", "<<", ">>", "<>", "<=", ">=", "==", "!=", "||", "<", ">", "=", "+", "-", "*", "/",
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


@dataclass(slots=True)
class Token:
    kind: str     # KEYWORD, IDENT, INTEGER, FLOAT, STRING, BLOB, OP, PARAM or EOF
    value: object  # keyword in upper case, identifier name, number, string,
                   # operator, or for PARAM the text after "?" / the whole ":name"
    pos: int
    text: str     # the exact source text of the token


# The common tokens, matched by one regular expression; anything else
# (REAL and hex numbers, strings with doubled quotes, comments, parameters,
# quoted identifiers, BLOBs, errors) goes through the code below it.
# As SQLite's tokenizer: white space is ASCII only, and every character from
# U+0080 up may be part of an identifier (SQLite looks at UTF-8 bytes >= 0x80).
SPACES = " \t\n\f\r"
DIGITS = "0123456789"
HEX_DIGITS = "0123456789abcdefABCDEF"
_FAST = re.compile(
    r"[ \t\n\f\r]*(?:"  # white space before the token
    r"((?![xX]')[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_$\x80-\U0010ffff]*)"  # 1: a word (not the x of x'...')
    r"|([0-9]+)(?![A-Za-z0-9_$.\x80-\U0010ffff])"  # 2: an integer
    r"|'([^']*)'(?!')"  # 3: a string without doubled quotes
    r"|(->>|->|<<|>>|<>|<=|>=|==|!=|\|\||(?!--)-|(?!/\*)/|\.(?![0-9])|[<>=+*%&|~(),;])"  # 4: an operator
    r")"
)
_KEYWORDS = {word: word for word in KEYWORDS}


def tokenize(text: str) -> list[Token]:
    tokens = []
    append = tokens.append
    fast = _FAST.match
    keywords = _KEYWORDS
    i = 0
    n = len(text)
    while i < n:
        m = fast(text, i)
        if m is not None:
            group = m.lastindex
            i = m.start(group)
            end = m.end()
            if group == 1:
                word = m.group(1)
                upper = keywords.get(word.upper() if word.isascii() else ascii_upper(word))
                if upper is not None:
                    append(Token("KEYWORD", upper, i, word))
                else:
                    append(Token("IDENT", word, i, word))
            elif group == 2:
                literal = m.group(2)
                value = int(literal)
                if value >= 2**63:  # too large for 64 bits: a REAL, as SQLite
                    append(Token("FLOAT", atof(literal), i, literal))
                else:
                    append(Token("INTEGER", value, i, literal))
            elif group == 3:
                i -= 1  # (the opening quote)
                append(Token("STRING", m.group(3), i, text[i:end]))
            else:
                op = m.group(4)
                append(Token("OP", op, i, op))
            i = end
            continue
        ch = text[i]
        if ch in SPACES:
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
        if ch in DIGITS or (ch == "." and i + 1 < n and text[i + 1] in DIGITS):
            tokens.append(_number(text, i))
            i += len(tokens[-1].text)
            continue
        if ch == "'":
            value, i = _quoted(text, i, "'")
            tokens.append(Token("STRING", value, start, text[start:i]))
            continue
        if ch == "?":
            i += 1
            while i < n and text[i] in DIGITS:
                i += 1
            tokens.append(Token("PARAM", text[start:i], start, text[start:i]))
            continue
        if ch in ":@$" and i + 1 < n and is_id_char(text[i + 1]):
            i += 1
            while i < n and is_id_char(text[i]):
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


def is_id_char(c: str) -> bool:
    """SQLite's IdChar: a letter, digit, '_', '$' or any character from U+0080 up."""
    return c >= "\x80" or c.isascii() and (c.isalnum() or c in "_$")


def _unrecognized(text: str, start: int, i: int) -> None:
    """A number run into letters (12abc, 1e, 0xg): SQLite's error names the
    whole run."""
    while i < len(text) and is_id_char(text[i]):
        i += 1
    raise SQLSyntaxError(f'unrecognized token: "{text[start:i]}"', text, start)


def _number(text: str, start: int) -> Token:
    """A numeric literal as SQLite's sqlite3GetToken reads it, '_' digit
    separators included (sqlite3DequoteNumber: each between two digits)."""
    n = len(text)

    def run(i: int, digits: str) -> int:
        while i < n and (text[i] in digits or text[i] == "_"):
            i += 1
        return i

    is_hex = text.startswith(("0x", "0X"), start) and start + 2 < n and text[start + 2] in HEX_DIGITS
    is_float = False
    if is_hex:
        i = run(start + 3, HEX_DIGITS)
    else:
        i = run(start, DIGITS)
        if i < n and text[i] == ".":
            is_float = True
            i = run(i + 1, DIGITS)
        if i < n and text[i] in "eE" and (
                i + 1 < n and text[i + 1] in DIGITS
                or i + 2 < n and text[i + 1] in "+-" and text[i + 2] in DIGITS):
            is_float = True
            i = run(i + 2, DIGITS)
    if i < n and is_id_char(text[i]):
        _unrecognized(text, start, i)
    literal = text[start:i]
    if "_" in literal:
        digits = HEX_DIGITS if is_hex else DIGITS
        if any(c == "_" and (literal[k - 1] not in digits or literal[k + 1:k + 2] not in tuple(digits))
               for k, c in enumerate(literal)):
            raise SQLSyntaxError(f'unrecognized token: "{literal}"', text, start)
    number = literal.replace("_", "")
    if is_hex:
        if len(number[2:].lstrip("0")) > 16:
            raise SQLSyntaxError(f"hex literal too big: {literal}", text, start)
        value = int(number[2:], 16)
        return Token("INTEGER", value - (1 << 64) if value >= 1 << 63 else value, start, literal)
    if is_float:
        return Token("FLOAT", atof(number), start, literal)
    value = int(number)
    if value >= 2**63:
        # Like SQLite, integer literals too large for 64 bits become REAL.
        return Token("FLOAT", atof(number), start, literal)
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
