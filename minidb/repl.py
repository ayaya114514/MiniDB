"""Interactive command line interface.

SQL statements end with ``;`` and may span several lines.  Lines starting
with ``.`` are meta commands (see ``.help``).  Results are printed like the
``sqlite3`` shell's default list mode: values separated by ``|``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import TextIO

from minidb.database import Database
from minidb.errors import Error
from minidb.tokenizer import SQLSyntaxError, tokenize
from minidb.values import SQLValue, to_text

PROMPT = "minidb> "
CONTINUATION_PROMPT = "   ...> "

HELP = """\
.btree TABLE     Print the B+ tree of TABLE
.exit            Exit this program
.help            Show this message
.schema [NAME]   Show CREATE statements (tables with their indexes, views)
.tables          List the tables and views"""


def format_row(row: Sequence[SQLValue]) -> str:
    return "|".join("" if value is None else to_text(value) for value in row)


def statement_complete(text: str) -> bool:
    """Whether ``text`` ends with a ``;`` that is not inside a string or comment."""
    try:
        tokens = tokenize(text)
    except SQLSyntaxError as exc:
        return not exc.message.startswith("unterminated")
    return len(tokens) > 1 and tokens[-2].kind == "OP" and tokens[-2].value == ";"


class Shell:
    def __init__(self, db: Database, stdout: TextIO) -> None:
        self.db = db
        self.out = stdout

    def write(self, text: str) -> None:
        self.out.write(text + "\n")

    def meta_command(self, line: str) -> bool:
        """Run a meta command; returns False when the shell should exit."""
        parts = line.split()
        command, args = parts[0], parts[1:]
        catalog = self.db.catalog
        if command in (".exit", ".quit"):
            return False
        if command == ".help":
            self.write(HELP)
        elif command == ".tables":
            names = sorted([("temp." if t.temp else "") + t.name for t in catalog.all_tables() + catalog.all_views()])
            if names:
                self.write(" ".join(names))
        elif command == ".schema":
            tables = sorted(catalog.all_tables(), key=lambda t: t.name)
            views = sorted(catalog.all_views(), key=lambda v: v.name)
            if args:
                view = catalog.find_view(args[0])
                tables, views = ([], [view]) if view else ([catalog.get_table(args[0])], [])
            for table in tables:
                self.write(table.sql + ";")
                for index in reversed(table.indexes):
                    if not index.is_auto:
                        self.write(index.sql + ";")
            for view in views:
                self.write(view.sql + ";")
        elif command == ".btree":
            if len(args) != 1:
                self.write("Usage: .btree TABLE")
            else:
                table = catalog.get_table(args[0])
                for text in catalog.table_tree(table).dump():
                    self.write(text)
        else:
            self.write(f'Error: unknown command: {command}. Enter ".help" for help')
        return True

    def run_sql(self, text: str) -> None:
        try:
            for result in self.db.execute_each(text):
                for row in result:
                    self.write(format_row(row))
        except SQLSyntaxError as exc:
            self.write(f"Error: {exc}")
            self.write(exc.caret())
        except Error as exc:
            self.write(f"Error: {exc}")


def run(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, path: str | None = None, interactive: bool | None = None,
        format: str | None = None) -> None:
    """Run the shell on the database file ``path`` (``None`` = in memory);
    ``format`` is for a new database (see ``Database``)."""
    if interactive is None:
        interactive = stdin.isatty()
    with Database(path, format=format) as db:
        shell = Shell(db, stdout)
        buffer = ""
        while True:
            if interactive:
                stdout.write(CONTINUATION_PROMPT if buffer else PROMPT)
                stdout.flush()
            line = stdin.readline()
            if not line:
                if buffer.strip():
                    shell.run_sql(buffer)
                break
            if not buffer and line.strip().startswith("."):
                try:
                    if not shell.meta_command(line.strip()):
                        break
                except Error as exc:
                    shell.write(f"Error: {exc}")
                continue
            buffer += line
            if statement_complete(buffer):
                shell.run_sql(buffer)
                buffer = ""
            elif not buffer.strip():
                buffer = ""


def main(argv: list[str]) -> int:
    format = None
    if argv and argv[0] == "--sqlite":  # a new database in SQLite's file format
        format, argv = "sqlite", argv[1:]
    if len(argv) > 1 or (argv and argv[0].startswith("-")):
        sys.stderr.write("usage: python -m minidb [--sqlite] [database-file]\n")
        return 2
    try:
        run(path=argv[0] if argv else None, format=format)
    except Error as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    return 0
