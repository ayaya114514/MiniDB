"""Interactive command line interface."""

import sys

from minidb.executor import ExecutionError, Table
from minidb.parser import ParseError, parse

PROMPT = "minidb> "


def format_row(row):
    return "|".join("" if value is None else str(value) for value in row)


def run(stdin=sys.stdin, stdout=sys.stdout, interactive=None):
    if interactive is None:
        interactive = stdin.isatty()
    table = Table()
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
            stdout.write(f"Error: unknown command: {line}\n")
            continue
        try:
            rows = table.execute(parse(line))
        except (ParseError, ExecutionError) as exc:
            stdout.write(f"Error: {exc}\n")
            continue
        for row in rows:
            stdout.write(format_row(row) + "\n")


def main(argv=None):
    run()
    return 0
