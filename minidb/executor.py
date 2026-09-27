"""Statement execution.

Stage 1: a single hard-coded table ``users(id, name, age)`` kept in memory.
"""

from dataclasses import dataclass


class ExecutionError(Exception):
    pass


@dataclass
class InsertStatement:
    id: int
    name: str
    age: int


@dataclass
class SelectStatement:
    pass


class Table:
    """The single fixed table of stage 1, stored as a list of rows."""

    columns = ("id", "name", "age")

    def __init__(self):
        self.rows = []

    def execute(self, stmt):
        if isinstance(stmt, InsertStatement):
            if any(row[0] == stmt.id for row in self.rows):
                raise ExecutionError(f"duplicate id {stmt.id}")
            self.rows.append((stmt.id, stmt.name, stmt.age))
            return []
        if isinstance(stmt, SelectStatement):
            return sorted(self.rows)
        raise ExecutionError(f"unsupported statement {stmt!r}")
