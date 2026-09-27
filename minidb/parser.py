"""Statement parsing.

Stage 1 only understands two statements:

    insert <id> <name> <age>
    select
"""

from minidb.executor import InsertStatement, SelectStatement


class ParseError(Exception):
    pass


def parse(text):
    words = text.split()
    if not words:
        raise ParseError("empty statement")
    keyword = words[0].lower()
    if keyword == "insert":
        if len(words) != 4:
            raise ParseError("usage: insert <id> <name> <age>")
        try:
            row_id = int(words[1])
            age = int(words[3])
        except ValueError:
            raise ParseError("id and age must be integers") from None
        if row_id < 0:
            raise ParseError("id must be non-negative")
        return InsertStatement(row_id, words[2], age)
    if keyword == "select":
        if len(words) != 1:
            raise ParseError("usage: select")
        return SelectStatement()
    raise ParseError(f"unrecognized statement: {words[0]}")
