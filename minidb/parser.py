"""Statement parsing.

Until the SQL parser exists, only these statements are understood:

    insert <id> <name> <age>
    select
    delete <id>
"""

from minidb.executor import DeleteStatement, InsertStatement, SelectStatement


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
    if keyword == "delete":
        if len(words) != 2 or not words[1].lstrip("-").isdigit():
            raise ParseError("usage: delete <id>")
        return DeleteStatement(int(words[1]))
    raise ParseError(f"unrecognized statement: {words[0]}")
