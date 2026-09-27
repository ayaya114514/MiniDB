"""Interactive command line interface."""

import sys

from minidb.executor import ExecutionError, Table
from minidb.pager import DatabaseError, Pager
from minidb.parser import ParseError, parse

PROMPT = "minidb> "


def format_row(row):
    return "|".join("" if value is None else str(value) for value in row)


def run(stdin=sys.stdin, stdout=sys.stdout, path=None, interactive=None):
    """Run the REPL on the database file ``path`` (``None`` = in memory)."""
    if interactive is None:
        interactive = stdin.isatty()
    pager = Pager(path)
    try:
        _loop(Table(pager), stdin, stdout, interactive)
    finally:
        pager.close()


def _loop(table, stdin, stdout, interactive):
    while True:
        if interactive:
            stdout.write(PROMPT)
            stdout.flush()
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        if line.startswith("."):
            if line == ".exit":
                break
            if line == ".btree":
                for text in table.tree.dump():
                    stdout.write(text + "\n")
                continue
            stdout.write(f"Error: unknown command: {line}\n")
            continue
        try:
            rows = table.execute(parse(line))
        except (ParseError, ExecutionError) as exc:
            stdout.write(f"Error: {exc}\n")
            continue
        for row in rows:
            stdout.write(format_row(row) + "\n")


def main(argv):
    if len(argv) > 1:
        sys.stderr.write("usage: python -m minidb [database-file]\n")
        return 2
    try:
        run(path=argv[0] if argv else None)
    except DatabaseError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    return 0
