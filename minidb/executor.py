"""Statement execution.

Stage 3: a single hard-coded table ``users(id, name, age)`` stored in a B+
tree rooted at page 1, keyed by ``id``.
"""

from dataclasses import dataclass

from minidb.btree import BTree, DuplicateKeyError
from minidb.record import decode_record, encode_record


class ExecutionError(Exception):
    pass


class CommandError(Exception):
    pass


@dataclass
class InsertStatement:
    id: int
    name: str
    age: int


@dataclass
class SelectStatement:
    pass


@dataclass
class DeleteStatement:
    id: int


class Table:
    """The single fixed table ``users(id, name, age)``."""

    columns = ("id", "name", "age")
    root_page = 1

    def __init__(self, pager):
        if pager.page_count == 1:
            self.tree = BTree.create(pager)
        else:
            self.tree = BTree(pager, self.root_page)

    def execute(self, stmt):
        if isinstance(stmt, InsertStatement):
            try:
                self.tree.insert(stmt.id, encode_record([stmt.name, stmt.age]))
            except DuplicateKeyError:
                raise ExecutionError(f"duplicate id {stmt.id}") from None
            return []
        if isinstance(stmt, SelectStatement):
            return [(key, *decode_record(value)[0]) for key, value in self.tree.scan()]
        if isinstance(stmt, DeleteStatement):
            if not self.tree.delete(stmt.id):
                raise ExecutionError(f"no row with id {stmt.id}")
            return []
        raise ExecutionError(f"unsupported statement {stmt!r}")


def parse_command(text):
    """Parse the temporary command syntax used until the SQL executor exists:

        insert <id> <name> <age>
        select
        delete <id>
    """
    words = text.split()
    if not words:
        raise CommandError("empty statement")
    keyword = words[0].lower()
    if keyword == "insert":
        if len(words) != 4:
            raise CommandError("usage: insert <id> <name> <age>")
        try:
            row_id = int(words[1])
            age = int(words[3])
        except ValueError:
            raise CommandError("id and age must be integers") from None
        if row_id < 0:
            raise CommandError("id must be non-negative")
        return InsertStatement(row_id, words[2], age)
    if keyword == "select":
        if len(words) != 1:
            raise CommandError("usage: select")
        return SelectStatement()
    if keyword == "delete":
        if len(words) != 2 or not words[1].lstrip("-").isdigit():
            raise CommandError("usage: delete <id>")
        return DeleteStatement(int(words[1]))
    raise CommandError(f"unrecognized statement: {words[0]}")
