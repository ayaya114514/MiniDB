"""Triggers: their programs and when they fire, as SQLite's trigger.c does.

A trigger's program is compiled once per catalog version and per conflict
resolution of the statement that fires it (SQLite's "orconf": an OR clause
of that statement overrides the ones of the program's own INSERTs and
UPDATEs).  Its statements see the row being changed as ``NEW`` and ``OLD``:
a scope with those pseudo-tables is the parent of every statement in the
program, so references to them work like those of a correlated subquery
(through the scope's ``cell``, set before the program runs).

``NEW.x`` and ``OLD.x`` have the column's collation but no affinity (SQLite's
TK_TRIGGER expressions).  Triggers fire newest first.  With ``PRAGMA
recursive_triggers`` off (the default) a trigger does not fire while its own
program is running; programs and foreign key actions nest at most
``MAX_DEPTH`` deep.

``RAISE(IGNORE)`` abandons the program and the change of the row that fired
it (the statement goes on with its next row); ``RAISE(ABORT | FAIL |
ROLLBACK, message)`` is a constraint error resolved that way.
"""

from __future__ import annotations

import copy
import sys
from typing import TYPE_CHECKING, Any

from minidb import values
from minidb.catalog import TableInfo, TriggerInfo
from minidb.errors import IntegrityError, OperationalError
from minidb.parser import Compound, Insert, Raise, Select, Update, Values
from minidb.values import ascii_lower

ROWID_NAMES = ("rowid", "oid", "_rowid_")  # (as in minidb.executor)

if TYPE_CHECKING:
    from minidb.executor import Executor
else:
    Executor = Any

MAX_DEPTH = 1000  # SQLite's SQLITE_MAX_TRIGGER_DEPTH (trigger programs and foreign key actions)


def walk_tree(node: object) -> Any:
    """Every syntax tree node under ``node``, subqueries included."""
    import dataclasses

    yield node
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from walk_tree(item)
    elif dataclasses.is_dataclass(node) and not isinstance(node, type):
        for f in dataclasses.fields(node):
            yield from walk_tree(getattr(node, f.name))


class TriggerIgnore(Exception):
    """RAISE(IGNORE): leave the row alone, go on with the statement."""


def raise_error(kind: str, message: str) -> IntegrityError:
    """The error of RAISE(ABORT | FAIL | ROLLBACK, message)."""
    error = IntegrityError(message)
    error.resolution = kind
    return error


class PseudoSource:
    """``NEW`` or ``OLD`` in a trigger program: the table's (or view's)
    columns with their collations, without affinities - except the INTEGER
    PRIMARY KEY, which is the row id.  Reachable only by qualified names."""

    indexes = ()

    def __init__(self, name: str, source: Any) -> None:
        self.name = name
        self.has_rowid = source.has_rowid  # (a view's rows have none)
        self.columns = source.columns
        self.rowid_column = getattr(source, "rowid_column", None)
        self.affinities = [None] * len(source.columns)
        if self.rowid_column is not None:
            self.affinities[self.rowid_column] = values.INTEGER  # (it is the row id, as in SQLite)
        self.collations = source.collations
        self.column_index = source.column_index


class Program:
    """A trigger's WHEN and statements, compiled for one table and conflict resolution."""

    def __init__(self, executor: Executor, trigger: TriggerInfo, source: Any, orconf: str | None) -> None:
        from minidb.executor import Compiler, Scope

        self.trigger = trigger
        self.executor = executor
        self.scope = scope = Scope()
        self.has_new = trigger.event in ("INSERT", "UPDATE")
        self.has_old = trigger.event in ("UPDATE", "DELETE")
        self.width = len(source.columns) + 1
        for name, present in (("new", self.has_new), ("old", self.has_old)):
            if present:
                scope.add(PseudoSource(name, source))
                scope.entries[-1].hidden.update(ascii_lower(c.name) for c in source.columns)
                scope.entries[-1].hidden.update(ROWID_NAMES)
        self.changes = 0
        saved = executor.outer_scope
        executor.outer_scope = scope
        executor.compiling_trigger += 1
        try:
            self.when = None
            if trigger.when is not None:
                self.when = Compiler(scope, executor=executor).compile(trigger.when)
            self.statements = []
            for stmt in copy.deepcopy(trigger.body):
                if orconf is not None and isinstance(stmt, (Insert, Update)):
                    stmt.conflict = orconf
                self.statements.append(stmt)
            self.plans = [(executor.prepare(stmt), not isinstance(stmt, (Select, Compound, Values)))
                          for stmt in self.statements]
            # Whether running it may abort the statement (SQLite's mayAbort, which
            # with a multi-row write asks for a statement journal).
            self.may_abort = any(getattr(plan, "aborts", False) for plan, _ in self.plans) or any(
                isinstance(node, Raise) and node.kind == "ABORT"
                for node in walk_tree([trigger.when, trigger.body]))
        except OperationalError as exc:
            message = str(exc)
            if message.startswith("no such table: ") and "." not in message:
                # A trigger's tables are looked up in the main schema.
                raise OperationalError(message.replace(": ", ": main.", 1)) from None
            raise
        finally:
            executor.outer_scope = saved
            executor.compiling_trigger -= 1

    def run(self, new: list | None, old: list | None) -> None:
        """Run the program for one row (``new`` / ``old``: values and row id)."""
        cell = self.scope.cell
        saved = cell[0]
        row = []
        if self.has_new:
            row += new if new is not None else [None] * self.width
        if self.has_old:
            row += old if old is not None else [None] * self.width
        cell[0] = row
        try:
            if self.when is not None:
                value = self.when(row)
                if value is None or not values.truth(value):
                    return
            for plan, changes in self.plans:
                for cache in plan.once_caches:
                    cache.clear()  # (each run of a program evaluates its subqueries anew)
                result = plan.run()
                if changes:
                    self.changes += max(result.rowcount, 0)
        finally:
            cell[0] = saved


class Triggers:
    """The triggers of the executor's catalog: finding, compiling and firing them."""

    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        self.active = []  # programs running, innermost last
        self.disabled = 0  # > 0: no trigger fires (DROP TABLE's implicit DELETE)
        self._version = None
        self._by_table = {}  # lower-case table name -> its triggers, newest first

    def matching(self, name: str, timing: str, event: str, changed: list[str] | None = None) -> list[TriggerInfo]:
        """The triggers that fire for an event on table (or view) ``name``,
        newest first; an UPDATE OF fires if the UPDATE sets one of its
        columns (``changed``: the names set; None: any)."""
        catalog = self.executor.catalog
        if not catalog.triggers or self.disabled:
            return []
        if self._version != catalog.version:
            self._version, self._by_table = catalog.version, {}
        lowered = ascii_lower(name)
        on_table = self._by_table.get(lowered)
        if on_table is None:
            on_table = self._by_table[lowered] = catalog.triggers_on(name)
        found = []
        for trigger in on_table:
            if trigger.timing != timing or trigger.event != event:
                continue
            if trigger.columns is not None and changed is not None and not (
                    set(trigger.columns) & {ascii_lower(c) for c in changed}):
                continue
            found.append(trigger)
        return found

    def may_abort(self, name: str, event: str, changed: list[str] | None, orconf: str | None) -> bool:
        """Whether a program the statement may run may abort it (compiled already by prepare())."""
        return any(getattr(self.program(trigger, orconf), "may_abort", False)
                   for timing in ("BEFORE", "AFTER", "INSTEAD OF")
                   for trigger in self.matching(name, timing, event, changed))

    def exist(self, name: str, event: str, changed: list[str] | None = None) -> bool:
        return any(self.matching(name, timing, event, changed) for timing in ("BEFORE", "AFTER", "INSTEAD OF"))

    def program(self, trigger: TriggerInfo, orconf: str | None, compiling: bool = False) -> Program:
        """The program of ``trigger`` for ``orconf``.  ``compiling``: for a
        statement being compiled, which (like SQLite) compiles each program
        it may run once, and the programs those compile (Executor.log_program)."""
        executor = self.executor
        key = (executor.catalog.version, orconf)
        program = trigger.programs.get(key)
        if compiling:
            if not executor.log_program(("trigger", id(trigger), orconf), ("trigger", trigger, orconf)):
                return program if program is not None else self.program(trigger, orconf)
            if program is not None:
                for log_key, entry in program.compiled:
                    executor.log_program(log_key, entry)  # (it compiled them when it was compiled)
                return program
        if program is None:
            trigger.programs = {k: p for k, p in trigger.programs.items() if k[0] == key[0]}
            source = executor.catalog.tables.get(ascii_lower(trigger.table_name))
            if source is None:
                source = executor.view_source(executor.catalog.find_view(trigger.table_name))
            program = Program.__new__(Program)
            trigger.programs[key] = program  # (a program that fires itself finds it)
            program.compiled = []  # the programs compiling it asked for, in order
            executor.program_stack.append(program.compiled)
            try:
                program.__init__(executor, trigger, source, orconf)
            except BaseException:
                del trigger.programs[key]
                raise
            finally:
                executor.program_stack.pop()
        return program

    def prepare(self, name: str, event: str, changed: list[str] | None, orconf: str | None,
                timings: tuple[str, ...] = ("BEFORE", "AFTER", "INSTEAD OF")) -> None:
        """Compile the programs a statement may run, as SQLite does when it
        compiles the statement (so their errors come first)."""
        for timing in timings:
            for trigger in self.matching(name, timing, event, changed):
                self.program(trigger, orconf, compiling=True)

    def fire(self, name: str, timing: str, event: str, old: list | None, new: list | None,
             changed: list[str] | None = None, orconf: str | None = None) -> None:
        """Run the matching triggers for one row.  Raises TriggerIgnore for RAISE(IGNORE)."""
        triggers = self.matching(name, timing, event, changed)
        if not triggers:
            return
        executor = self.executor
        recursive = executor.settings["recursive_triggers"]
        for trigger in triggers:
            program = self.program(trigger, orconf)
            if not recursive and any(p.trigger is trigger for p in self.active):
                continue  # (SQLite's check is by trigger, whatever the program's conflict resolution)
            if executor.frame_depth >= MAX_DEPTH:
                raise OperationalError("too many levels of trigger recursion")
            executor.frame_depth += 1
            if sys.getrecursionlimit() < 1000 + 60 * executor.frame_depth:
                sys.setrecursionlimit(1000 + 60 * MAX_DEPTH)
            self.active.append(program)
            saved_rowid = executor.last_insert_rowid
            outer_changes, program.changes = program.changes, 0
            try:
                program.run(new, old)
            except TriggerIgnore:
                executor.foreign_keys.extra_changes += program.changes
                raise
            else:
                # (SQLite counts a program's changes in total_changes() when it completes)
                executor.foreign_keys.extra_changes += program.changes
            finally:
                program.changes = outer_changes
                executor.last_insert_rowid = saved_rowid
                self.active.pop()
                executor.frame_depth -= 1
