"""INSERT, UPDATE and DELETE, compiled once per statement text: the rows
they write, upserts, RETURNING, INSTEAD OF triggers on views, and the
statement journal decision (whether SQLite would undo a failed statement)."""

from __future__ import annotations

from operator import itemgetter
from typing import TYPE_CHECKING, Any

from minidb import functions, values
from minidb.triggers import TriggerIgnore
from minidb.btree import BTree
from minidb.catalog import IndexInfo, TableInfo, ViewInfo
from minidb.errors import Error, IntegrityError, OperationalError
from minidb.parser import Delete, Insert, Join, Literal, Parameter, TableRef, Update, Upsert
from minidb.parser import Expr
from minidb.values import ascii_lower
from minidb.expressions import Compiler, ROWID_NAMES, Result, Row, RowFunction, SUBTYPED, Scope, calls_function
from minidb.generated import check_positions, generated_dependents, replace_possible, trigger_names
from minidb.sources import reads_table
from minidb.queries import passes_constants

if TYPE_CHECKING:
    from minidb.executor import Executor
else:
    Executor = Any  # (only in annotations: their modules import this one)


class PreparedInsert:
    def __init__(self, executor: Executor, stmt: Insert) -> None:
        self.executor = executor
        table = self.table = executor.catalog.table_to_modify(stmt.table, stmt.schema or executor.default_schema)
        width = len(table.columns)
        if stmt.columns is None:
            self.positions = [p for p in range(width) if table.columns[p].generated is None] if table.generated \
                else list(range(width))
        else:
            self.positions = []
            for name in stmt.columns:
                position = table.column_index(name)
                if position is None:
                    if ascii_lower(name) not in ROWID_NAMES or not table.has_rowid:
                        raise OperationalError(f"table {table.name} has no column named {name}")
                    # The row id by name; "width" when it is not a column.
                    position = width if table.rowid_column is None else table.rowid_column
                elif table.columns[position].generated is not None:
                    raise OperationalError(f'cannot INSERT into generated column "{table.columns[position].name}"')
                self.positions.append(position)
        rowid_position = width if table.rowid_column is None else table.rowid_column
        self.rowid_given = rowid_position in self.positions
        # A column named twice takes its first value; the row id its last (sqlite3Insert).
        self.assign = None
        if len(set(self.positions)) < len(self.positions):
            self.assign, seen = [], set()
            for i, position in enumerate(self.positions):
                if position == rowid_position or position not in seen:
                    self.assign.append((i, position))
                    seen.add(position)
        compiler = Compiler(Scope(executor.outer_scope), executor=executor)
        self.defaults = [(p, compiler.compile(c.default)) for p, c in enumerate(table.columns)
                         if p not in self.positions and c.default is not None]
        self.conflict = stmt.conflict
        self.upserts = [PreparedUpsert(executor, table, clause) for clause in stmt.upsert]
        # SQLite checks the row id for a conflict only when the INSERT gives it.
        checks = ["rowid"] if self.rowid_given else []
        checks += [index for index in table.indexes if index.unique]
        for upsert in self.upserts:
            if any(next((u for u in self.upserts if u.constraint in (check, None)), None) is upsert
                   for check in checks):
                upsert.resolve()
        scope = Scope()
        scope.add(table)
        self.returning = executor.compile_returning(stmt.returning, scope)
        self.rows = []
        self.query = None
        if stmt.query is not None:
            self.query = executor.compile_query(stmt.query)
            self.check_count(stmt, len(self.query.names))
        for exprs in stmt.rows:
            self.check_count(stmt, len(exprs))
            self.rows.append([compiler.compile(e) for e in exprs])
        self.tree = executor.catalog.table_tree(table)
        if table.generated:
            executor.generator(table)
        multi_write = self.multi_write = self.query is not None or len(self.rows) > 1
        # SQLite puts the rows of a SELECT or of several VALUES rows in a
        # temporary table first (records: the values lose the JSON subtype)
        # when the table has INSERT triggers - RETURNING is one - or the rows
        # read the table (sqlite3Insert's useTempTable); else its co-routine's
        # registers keep the subtype (for the generated columns too).
        self.temp_table = multi_write and (
            self.returning is not None or bool(executor.catalog.any_triggers and executor.triggers.exist(
                table.name, "INSERT")) or reads_table(
                [stmt.query] + [e for exprs in stmt.rows for e in exprs if type(e) not in (Literal, Parameter)],
                table))
        self.prepare_programs()
        # SQLite keeps a statement journal for a multi-row write (a SELECT,
        # several rows, triggers) that may abort: a constraint checked as
        # ABORT, a function call, a trigger program that may abort.
        triggers = executor.triggers
        # (A REPLACE that may delete a row with foreign keys or DELETE triggers
        # to run is a multi-row write too: sqlite3MultiWrite in
        # sqlite3GenerateConstraintChecks.)
        replace_deletes = replace_possible(table, self.conflict, self.rowid_given,
                                           handled=[u.constraint for u in self.upserts]) and (
            (executor.foreign_keys.enabled and executor.foreign_keys.involved(table))
            or (bool(executor.settings["recursive_triggers"]) and triggers.exist(table.name, "DELETE")))
        multi = multi_write or replace_deletes or bool(
            executor.catalog.any_triggers and triggers.exist(table.name, "INSERT"))
        # (Only a multi-row write needs it, or a statement of a trigger program: Program.may_abort.)
        self.aborts = (multi or bool(executor.compiling_trigger)) and (self.may_abort() or calls_function(stmt) or (
            table.generated and calls_function([c.generated for c in table.columns])) or triggers.may_abort(
            table.name, "INSERT", None, self.conflict) or any(
            # (an upsert's UPDATE runs its triggers' programs as OR ABORT)
            upsert.assignments and triggers.may_abort(table.name, "UPDATE", [
                table.columns[p].name if p < width else "rowid" for p, _ in upsert.assignments], "ABORT")
            for upsert in self.upserts) or (
            bool(executor.settings["recursive_triggers"]) and replace_possible(
                table, self.conflict, self.rowid_given, handled=[u.constraint for u in self.upserts])
            and triggers.may_abort(table.name, "DELETE", None, "REPLACE")) or self.foreign_keys_abort())
        self.statement_journal = multi and self.aborts

    def prepare_programs(self) -> None:
        """What SQLite compiles with the statement, in its order (so the
        first error is the one SQLite reports): the BEFORE triggers; the
        constraint checks' work - an upsert's UPDATE, REPLACE's DELETE (with
        their triggers and foreign keys); the foreign keys of the new row;
        the AFTER triggers."""
        executor, table = self.executor, self.table
        triggers, keys = executor.triggers, executor.foreign_keys
        self.unchecked = None
        if not executor.catalog.any_triggers and not keys.enabled:
            self.fk_multi = True  # (nothing to compile; used only with foreign keys)
            return
        triggers.prepare(table.name, "INSERT", None, self.conflict, ("BEFORE",))
        # A statement is a multi-row write (sqlite3MultiWrite) with a SELECT,
        # RETURNING or INSERT triggers, in a trigger program, and when an
        # upsert's UPDATE or a REPLACE's DELETE (coded within it) has triggers
        # or foreign key work.
        multi = (self.multi_write or self.returning is not None or bool(executor.compiling_trigger)
                 or triggers.exist(table.name, "INSERT"))
        width = len(table.columns)
        for upsert in self.upserts:
            if upsert.assignments:
                positions = {position for position, _ in upsert.assignments}
                upsert.unchecked = executor.compile_update(table, positions, "ABORT")
                names = trigger_names(table, positions)
                multi = multi or triggers.exist(table.name, "UPDATE", names) or (
                    keys.enabled and keys.required(table, positions))
        replaces = replace_possible(table, self.conflict, self.rowid_given, handled=[u.constraint for u in self.upserts])
        if replaces:
            recursive = bool(executor.settings["recursive_triggers"])
            executor.compile_delete(table, "REPLACE", recursive)
            multi = multi or keys.involved(table) or (recursive and triggers.exist(table.name, "DELETE"))
        self.fk_multi = multi
        if keys.enabled:
            keys.prepare(table, "insert", single_insert=not self.fk_multi)
            self.unchecked = executor.set_null_link(table)
        triggers.prepare(table.name, "INSERT", None, self.conflict, ("AFTER",))

    def foreign_keys_abort(self) -> bool:
        """Whether its foreign key code may abort (fkey.c's sqlite3MayAbort,
        whatever the statement's OR clause): a child row checked, a REPLACE's
        delete, an upsert's update."""
        keys, table = self.executor.foreign_keys, self.table
        return keys.enabled and (keys.may_abort(table, "insert") or (
            replace_possible(table, self.conflict, True) and keys.may_abort(table, "delete")) or any(
            u.do_update and keys.may_abort(table, "update", u.changed_positions()) for u in self.upserts))

    def may_abort(self) -> bool:
        """Whether a constraint check could abort the statement.

        SQLite decides when compiling whether a statement might abort: a
        constraint checked under ABORT, or a call of a (not inlined)
        function, which may raise an error.  Only then, and when it writes
        several rows, does it keep a statement journal; so only then does an
        error inside a transaction that is not a constraint violation (such
        as a datatype mismatch) undo the rows the statement already wrote.
        Otherwise they stay (see Database.execute_statement)."""
        table, conflict = self.table, self.conflict
        if self.executor.replace_rechecks(table) and replace_possible(
                table, conflict, self.rowid_given, handled=[u.constraint for u in self.upserts]):
            return True  # (the recheck after a REPLACE halts as ABORT: Executor.recheck_unique)
        if any(c.not_null and (conflict or c.not_null_conflict or "ABORT") in ("ABORT", "REPLACE")
               for c in table.columns):
            return True  # REPLACE fixes NOT NULL with a default value; MiniDB has none
        if table.checks and (conflict in (None, "ABORT", "REPLACE")
                             or any(calls_function(check.expr) for check in table.checks)):
            return True  # (a function call in a CHECK may raise an error, like one in the statement)

        def handled(constraint, own):
            return (conflict or own or "ABORT") != "ABORT" or any(
                u.constraint in (constraint, None) for u in self.upserts)

        if self.rowid_given and not handled("rowid", table.rowid_conflict()):
            return True
        if any(index.unique and not handled(index, index.conflict) for index in table.indexes):
            return True
        checked = {i for i, c in enumerate(table.columns) if c.not_null}
        checked.update(p for index in table.indexes if index.unique for p in index.positions)
        checked.update(p for check in table.checks for p in check_positions(table, check))
        checked.update((table.rowid_column, len(table.columns)))

        def changes(upsert):  # (with the generated columns that follow them)
            positions = {p for p, _ in upsert.assignments}
            return positions | generated_dependents(table, positions)

        return any(u.assignments and changes(u) & checked for u in self.upserts)

    def check_count(self, stmt: Insert, count: int) -> None:
        if count != len(self.positions):
            if stmt.columns is None:
                raise OperationalError(
                    f"table {self.table.name} has {len(self.positions)} columns "
                    f"but {count} values were supplied"
                )
            raise OperationalError(f"{count} values for {len(self.positions)} columns")

    def run(self) -> Result:
        executor, table, width = self.executor, self.table, len(self.table.columns)
        # All rows are computed first: SQLite evaluates the (constant)
        # subqueries of VALUES once, and a SELECT from the table being
        # inserted into does not see the new rows.
        rows = []
        sources = self.query.run() if self.query is not None else (
            [function([]) for function in functions] for functions in self.rows
        )
        if self.temp_table:
            sources = ([values.record_value(v) if type(v) in SUBTYPED else v for v in source] for source in sources)
        assign, positions = self.assign, self.positions
        for source in sources:
            row = [None] * (width + 1)  # the last: a row id given by name
            if assign is None:
                for position, value in zip(positions, source):
                    row[position] = value
            else:
                for i, position in assign:
                    row[position] = source[i]
            for position, default in self.defaults:
                row[position] = default([])
            rows.append(row)
        keys = executor.foreign_keys
        saved_unchecked, keys.unchecked = keys.unchecked, self.unchecked  # (prepare_programs did the compiling)
        changed = []  # rows inserted or updated by an upsert, with their row ids
        defaults = DefaultRegisters([position for position, _ in self.defaults])
        sequence = None
        if table.autoincrement:
            start = executor.sequence_value(table)
            sequence = [start or 0]
        try:
            for row in rows:
                rowid = row.pop()
                outcome = executor.insert_row(table, self.tree, row, self.conflict, self.upserts, rowid, defaults,
                                              sequence, not self.fk_multi)
                if outcome is not None:
                    kind, stored = outcome
                    if kind == "insert" and table.has_rowid:
                        executor.last_insert_rowid = stored[-1]
                    changed.append(stored)
        except Error as exc:
            exc.changes = len(changed) + exc.__dict__.pop("row_done", 0)  # the rows that FAIL (or no statement journal) keeps
            raise
        finally:
            keys.unchecked = saved_unchecked
        if sequence is not None and sequence[0] != start:
            executor.set_sequence_value(table, sequence[0])
        return returning_result(self.returning, changed)


class DefaultRegisters:
    """SQLite computes a column's (constant) DEFAULT once per INSERT, into
    the register the rows are built in, and applies the column affinities
    to those registers in place - at the first index check, or when the
    record is made.  So once a row got that far, the "excluded" row of a
    later upsert shows the default converted (a SQLite quirk kept here)."""

    def __init__(self, positions: list[int]) -> None:
        self.positions = positions
        self.converted = False


class PreparedSingleTable:
    """The part of UPDATE / DELETE that finds the rows matching WHERE."""

    def __init__(self, executor: Executor, table_name: str, where: Expr | None, indexed_by: str | None = None,
                 not_indexed: bool = False, schema: str | None = None) -> None:
        self.executor = executor
        self.table = executor.catalog.table_to_modify(table_name, schema or executor.default_schema)
        executor.catalog.check_index_hint(self.table, indexed_by)
        self.tree = executor.catalog.table_tree(self.table)
        self.scope = Scope(executor.outer_scope)
        ref = TableRef(self.table.name, indexed_by=indexed_by, not_indexed=not_indexed,
                       schema="temp" if self.table.temp else "main")
        joins, _ = executor.build_from([Join(ref)], self.scope)
        self.levels, self.constants = executor.plan_joins(self.scope, joins, where)
        self.rowid_slot = self.scope.rowid_slot(0)

    def matching_rows(self, two_pass: bool = False) -> list[tuple[int, Row]]:
        """(rowid, row copy) of every matching row, all found before any
        change.  When SQLite can't change the rows during its scan (foreign
        keys, RETURNING, REPLACE, a new rowid), it collects the rowids in a
        RowSet or a temporary table first, and so works through them in
        rowid order: an order foreign key actions and REPLACE can show."""
        slot = self.rowid_slot
        if not passes_constants(self.constants, self.scope):
            return []
        rows = [(row[slot], list(row)) for row in self.executor.join_rows(self.scope, self.levels)]
        if two_pass:
            rows.sort(key=itemgetter(0))
        return rows


class PreparedUpdate(PreparedSingleTable):
    def __init__(self, executor: Executor, stmt: Update) -> None:
        super().__init__(executor, stmt.table, stmt.where, stmt.indexed_by, stmt.not_indexed, stmt.schema)
        table, width = self.table, len(self.table.columns)
        compiler = Compiler(self.scope, executor=executor)
        self.assignments = []
        for name, expr in stmt.assignments:
            position = table.column_index(name)
            if position is None:
                if ascii_lower(name) not in ROWID_NAMES or not table.has_rowid:
                    raise OperationalError(f"no such column: {name}")
                position = width if table.rowid_column is None else table.rowid_column
            elif table.columns[position].generated is not None:
                raise OperationalError(f'cannot UPDATE generated column "{table.columns[position].name}"')
            self.assignments.append((position, compiler.compile(expr)))
        self.conflict = stmt.conflict
        self.returning = executor.compile_returning(stmt.returning, self.scope)
        # Like PreparedInsert.may_abort: the constraints the changed columns
        # take part in, under ABORT (REPLACE, for NOT NULL); with the
        # generated columns that use them.
        changed = self.changed = {p for p, _ in self.assignments}
        if table.generated:
            executor.generator(table)
            changed |= generated_dependents(table, changed)
        width = len(table.columns)
        rowid_changed = self.rowid_changed = bool(changed & {width, table.rowid_column})
        conflict = stmt.conflict
        # (A function in a generated column counts too: it is computed with the statement.)
        self.statement_journal = calls_function(stmt) or bool(
            table.generated and calls_function([c.generated for c in table.columns])) or any(
            (conflict or table.columns[p].not_null_conflict or "ABORT") in ("ABORT", "REPLACE")
            for p in changed if p < width and table.columns[p].not_null
        ) or any(conflict in (None, "ABORT", "REPLACE") or calls_function(check.expr)
                 for check in table.checks
                 if (changed | ({width} if rowid_changed else set())) & check_positions(table, check)) or (
            rowid_changed and (conflict or table.rowid_conflict() or "ABORT") == "ABORT"
        ) or any(index.unique and changed & set(index.positions) and (conflict or index.conflict or "ABORT") == "ABORT"
                 for index in table.indexes)
        # Whether a REPLACE may delete a row the statement has yet to update.
        self.may_replace = "REPLACE" in (conflict, table.rowid_conflict(), *(i.conflict for i in table.indexes))
        self.names = trigger_names(table, changed)
        # What SQLite compiles with the statement, in its order (Executor.compile_update).
        self.unchecked = executor.compile_update(table, self.changed, conflict, True, rowid_changed)
        self.aborts = self.statement_journal or executor.triggers.may_abort(table.name, "UPDATE", self.names, conflict)
        if executor.triggers.exist(table.name, "UPDATE", self.names):
            self.statement_journal = self.aborts  # (triggers make it a multi-row write)

    def run(self) -> Result:
        executor, table, tree = self.executor, self.table, self.tree
        keys = executor.foreign_keys
        saved_unchecked, keys.unchecked = keys.unchecked, self.unchecked
        changed = []
        try:
            triggered = executor.triggers.exist(table.name, "UPDATE", self.names)
            two_pass = self.may_replace or self.rowid_changed or self.returning is not None or triggered or (
                keys.enabled and keys.required(table, self.changed))
            for rowid, old in self.matching_rows(two_pass):
                if self.may_replace or keys.enabled or triggered:
                    if rowid not in tree:
                        continue  # an earlier row's REPLACE (or a foreign key action, a trigger) deleted it
                    if triggered or keys.enabled:
                        old = executor.load_row(table, rowid, tree.get(rowid))  # (as it is now)
                new = list(old)
                for position, function in self.assignments:
                    new[position] = function(old)
                stored = executor.update_row(table, tree, rowid, old, new, self.conflict, self.changed)
                if stored is not None:
                    changed.append(stored)
        except Error as exc:
            exc.changes = len(changed) + exc.__dict__.pop("row_done", 0)
            raise
        finally:
            keys.unchecked = saved_unchecked
        return returning_result(self.returning, changed)


class PreparedDelete(PreparedSingleTable):
    def __init__(self, executor: Executor, stmt: Delete) -> None:
        super().__init__(executor, stmt.table, stmt.where, stmt.indexed_by, stmt.not_indexed, stmt.schema)
        self.returning = executor.compile_returning(stmt.returning, self.scope)
        self.delete_all = stmt.where is None and self.returning is None
        executor.compile_delete(self.table, None)  # (what SQLite compiles with it, in its order)
        keys = executor.foreign_keys
        # (SQLite: a statement journal for a multi-row write - triggers, foreign
        # keys, RETURNING - that may abort; a plain DELETE cannot fail half way.)
        multi = executor.triggers.exist(self.table.name, "DELETE") or keys.involved(self.table) or (
            self.returning is not None)
        self.aborts = (multi or bool(executor.compiling_trigger)) and (
            calls_function(stmt) or executor.triggers.may_abort(self.table.name, "DELETE", None, None) or (
                keys.enabled and keys.involved(self.table) and keys.may_abort(self.table, "delete")))
        self.statement_journal = multi and self.aborts

    def run(self) -> Result:
        executor, table, tree = self.executor, self.table, self.tree
        keys = executor.foreign_keys
        involved = keys.involved(table) or executor.triggers.exist(table.name, "DELETE")
        if self.delete_all and not involved:
            count = len(tree)
            tree.clear()
            for index in table.indexes:
                executor.catalog.index_tree(index).clear()
            return Result(rowcount=count)
        matches = self.matching_rows(involved or self.returning is not None)
        if not involved:
            for rowid, row in matches:
                executor.remove_index_entries(table, row, rowid)
                tree.delete(rowid)
            return returning_result(self.returning, [row for _, row in matches])
        deleted = []
        try:
            for rowid, row in matches:
                if rowid not in tree:
                    continue  # (a foreign key action or a trigger deleted it)
                row = executor.delete_row(table, tree, rowid)
                if row is not None:
                    deleted.append(row)
        except Error as exc:
            exc.changes = len(deleted) + exc.__dict__.pop("row_done", 0)
            raise
        return returning_result(self.returning, deleted)


class PreparedViewInsert:
    """INSERT into a view with INSTEAD OF INSERT triggers: each row, as NEW
    (the values as given, without affinities), fires them instead."""

    def __init__(self, executor: Executor, stmt: Insert, view: ViewInfo) -> None:
        self.executor = executor
        self.view = view
        if stmt.upsert:
            raise OperationalError("cannot UPSERT a view")
        source = self.source = executor.view_source(view)
        width = len(source.columns)
        if stmt.columns is None:
            self.positions = list(range(width))
        else:
            self.positions = []
            for name in stmt.columns:
                position = source.column_index(name)
                if position is None:
                    if ascii_lower(name) not in ROWID_NAMES:
                        raise OperationalError(f"table {view.name} has no column named {name}")
                    position = width  # (SQLite accepts a row id for a view, and ignores it)
                self.positions.append(position)
        compiler = Compiler(Scope(executor.outer_scope), executor=executor)
        self.query = None
        self.rows = []
        if stmt.query is not None:
            self.query = executor.compile_query(stmt.query)
            self.check_count(stmt, len(self.query.names))
        for exprs in stmt.rows:
            self.check_count(stmt, len(exprs))
            self.rows.append([compiler.compile(e) for e in exprs])
        scope = Scope(executor.outer_scope)
        scope.add(source)
        self.returning = executor.compile_returning(stmt.returning, scope)
        self.conflict = stmt.conflict
        executor.triggers.prepare(view.name, "INSERT", None, self.conflict)
        self.instead = bool(executor.triggers.matching(view.name, "INSTEAD OF", "INSERT"))
        self.aborts = calls_function(stmt) or executor.triggers.may_abort(view.name, "INSERT", None, self.conflict)
        self.statement_journal = self.aborts

    def check_count(self, stmt: Insert, count: int) -> None:
        if count != len(self.positions):
            if stmt.columns is None:
                raise OperationalError(f"table {self.view.name} has {len(self.source.columns)} columns "
                                       f"but {count} values were supplied")
            raise OperationalError(f"{count} values for {len(self.positions)} columns")

    def run(self) -> Result:
        executor, width = self.executor, len(self.source.columns)
        if self.query is not None:
            sources = self.query.run()
        else:
            sources = [[f([]) for f in functions] for functions in self.rows]
        done = []
        for values_ in sources:
            new = [None] * (width + 1)
            for position, value in zip(self.positions, values_):
                new[position] = value
            if self.instead and new[width] is not None and not isinstance(
                    values.apply_affinity(new[width], values.INTEGER), int):
                raise IntegrityError("datatype mismatch")  # (building NEW, SQLite checks the row id it ignores)
            new[width] = None
            try:
                executor.triggers.fire(self.view.name, "INSTEAD OF", "INSERT", None, new, None, self.conflict)
            except TriggerIgnore:
                continue
            except Error as exc:
                exc.changes = 0  # (a view's changes() is 0, also after RAISE(FAIL))
                raise
            done.append(new)
        if self.returning is not None:
            # (SQLite reads a REAL column of the new rows with OP_RealAffinity - not in typeof())
            real = [i for i, a in enumerate(self.source.affinities) if a == values.REAL]
            done = [[float(v) if i in real and type(v) is int else v for i, v in enumerate(row)] for row in done]
        return view_result(self.returning, done)


class PreparedViewChange:
    """UPDATE or DELETE of a view with INSTEAD OF triggers: the view's rows
    that match WHERE are found first; each fires the triggers, as OLD (and
    NEW: OLD with the SET values, converted by the columns' affinities when
    an INSTEAD OF trigger fires)."""

    def __init__(self, executor: Executor, stmt: Update | Delete, view: ViewInfo) -> None:
        self.executor = executor
        self.view = view
        self.scope = scope = Scope(executor.outer_scope)
        joins, self.derived = executor.build_from([Join(TableRef(view.name))], scope)
        self.source = source = scope.entries[0].table
        self.levels, self.constants = executor.plan_joins(scope, joins, stmt.where)
        compiler = Compiler(scope, executor=executor)
        self.assignments = []
        self.names = None
        self.event = "DELETE"
        self.conflict = None
        if isinstance(stmt, Update):
            self.event = "UPDATE"
            self.conflict = stmt.conflict
            self.names = []
            for name, expr in stmt.assignments:
                position = source.column_index(name)
                if position is None:
                    raise OperationalError(f"no such column: {name}")
                self.assignments.append((position, compiler.compile(expr)))
                self.names.append(name)
        self.returning = executor.compile_returning(stmt.returning, scope)
        executor.triggers.prepare(view.name, self.event, self.names, self.conflict)
        # (SQLite converts NEW only for INSTEAD OF triggers - its BEFORE ones - not for RETURNING alone.)
        self.convert = bool(executor.triggers.matching(view.name, "INSTEAD OF", self.event, self.names))
        self.aborts = calls_function(stmt) or executor.triggers.may_abort(view.name, self.event, self.names,
                                                                          self.conflict)
        self.statement_journal = self.aborts

    def run(self) -> Result:
        executor, source = self.executor, self.source
        width = len(source.columns)
        if not passes_constants(self.constants, self.scope):
            rows = []
        else:
            for derived in self.derived:
                derived.materialize()
            rows = [list(row[:width]) + [None] for row in executor.join_rows(self.scope, self.levels)]
        done = []
        for old in rows:
            new = None
            if self.event == "UPDATE":
                new = list(old)
                for position, function in self.assignments:
                    value = function(old)
                    new[position] = values.apply_affinity(value, source.affinities[position]) if self.convert else value
            try:
                executor.triggers.fire(self.view.name, "INSTEAD OF", self.event, old, new, self.names, self.conflict)
            except TriggerIgnore:
                continue
            except Error as exc:
                exc.changes = 0
                raise
            done.append(new if new is not None else old)
        return view_result(self.returning, done)


def view_result(returning: tuple[list[RowFunction], list[str]] | None, rows: list[Row]) -> Result:
    """The result of a change of a view: no rows changed (changes() is 0), its RETURNING rows."""
    result = returning_result(returning, rows)
    result.rowcount = 0
    return result


class PreparedUpsert:
    """One ON CONFLICT clause of an INSERT."""

    def __init__(self, executor: Executor, table: TableInfo, clause: Upsert) -> None:
        self.executor = executor
        self.table = table
        self.clause = clause
        self.constraint = self.find_constraint(table, clause)  # None: any uniqueness constraint
        self.do_update = clause.assignments is not None
        self.assignments = []
        self.where = None
        self.unchecked = None  # the foreign key its UPDATE leaves unchecked (Executor.set_null_link)

    def resolve(self) -> None:
        """Compile DO UPDATE's SET and WHERE.  Like SQLite, PreparedInsert
        does this only for a clause that some conflict check can reach: a
        name error in any other clause goes unreported."""
        if not self.do_update or self.assignments:
            return
        table, clause = self.table, self.clause
        # SET and WHERE see the existing row (by the table's name) and "excluded".
        self.scope = Scope(self.executor.outer_scope)
        self.scope.add(table)
        self.scope.add(ExcludedSource(table))
        # An unqualified name is the existing row's column; excluded.x must be qualified.
        self.scope.entries[1].hidden.update(ascii_lower(c.name) for c in table.columns)
        compiler = Compiler(self.scope, executor=self.executor)
        width = len(table.columns)
        assignments = []
        for name, expr in clause.assignments:
            position = table.column_index(name)
            if position is None:
                if ascii_lower(name) not in ROWID_NAMES or not table.has_rowid:
                    raise OperationalError(f"no such column: {name}")
                position = width if table.rowid_column is None else table.rowid_column
            elif table.columns[position].generated is not None:
                raise OperationalError(f'cannot UPDATE generated column "{table.columns[position].name}"')
            assignments.append((position, compiler.compile(expr)))
        self.where = compiler.compile(clause.where) if clause.where is not None else None
        self.assignments = assignments

    def changed_positions(self) -> set[int]:
        """The columns DO UPDATE sets (the row id as len(columns))."""
        table, width = self.table, len(self.table.columns)
        positions = set()
        for name, _ in self.clause.assignments:
            position = table.column_index(name)
            positions.add(width if position is None or position == table.rowid_column else position)
        return positions

    @staticmethod
    def find_constraint(table: TableInfo, clause: Upsert) -> IndexInfo | str | None:
        if clause.columns is None:
            return None
        if clause.target_where is None:
            # As in SQLite, the INTEGER PRIMARY KEY (or rowid) matches only
            # on its own: a UNIQUE index that includes it never matches.
            alias = None if table.rowid_column is None else ascii_lower(table.columns[table.rowid_column].name)
            wanted = [ascii_lower(c) for c in clause.columns]
            for name in clause.columns:
                if table.column_index(name) is None and (ascii_lower(name) not in ROWID_NAMES or not table.has_rowid):
                    raise OperationalError(f"no such column: {name}")
            if len(wanted) == 1 and wanted[0] in (alias, *ROWID_NAMES):
                return "rowid"
            # A target column with a COLLATE matches only an index column of that collation.
            given = {ascii_lower(c): values.collation_name(k) for c, k in zip(clause.columns, clause.collations or ())
                     if k is not None}
            for index in table.indexes:
                names = [ascii_lower(c) for c in index.column_names]
                if (index.unique and alias not in names and len(names) == len(wanted)
                        and set(names) == set(wanted)
                        and all(given.get(n, k) == k for n, k in zip(names, index.collations))):
                    return index
        raise OperationalError("ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint")

    def apply(self, tree: BTree, rowid: int, excluded: Row) -> tuple[str, Row] | None:
        """Handle a conflict with existing row ``rowid``; ``excluded`` is the
        row that could not be inserted (with its row id)."""
        if not self.do_update:
            return None
        executor, table = self.executor, self.table
        old = executor.load_row(table, rowid, tree.get(rowid))
        if values.int_reals_made[0]:
            # (SQLite reads excluded.x of REAL affinity with OP_RealAffinity:
            # a generated column's IntReal is a REAL there.)
            excluded = [float(v) if type(v) is values.IntReal else v for v in excluded]
        context = old + excluded
        if self.where is not None and not values.truth(self.where(context)):
            return None
        new = list(old)
        for position, function in self.assignments:
            new[position] = function(context)
        # (SQLite runs DO UPDATE as an UPDATE OR ABORT: the constraints' own ON CONFLICT does not apply.)
        keys = executor.foreign_keys
        saved, keys.unchecked = keys.unchecked, self.unchecked  # (its UPDATE's own, see compile_update)
        changed = {position for position, _ in self.assignments}
        if table.generated:
            changed |= generated_dependents(table, changed)
        try:
            stored = executor.update_row(table, tree, rowid, old, new, "ABORT", changed)
        finally:
            keys.unchecked = saved
        return None if stored is None else ("update", stored)  # (None: a trigger deleted or kept the row)


def returning_result(returning: tuple[list[RowFunction], list[str]] | None, rows: list[Row]) -> Result:
    """The result of an INSERT, UPDATE or DELETE: its RETURNING rows, if any."""
    if returning is None:
        return Result(rowcount=len(rows))
    functions, names = returning
    return Result([tuple(f(row) for f in functions) for row in rows], names, rowcount=len(rows))


class ExcludedSource:
    """The ``excluded`` row of an upsert: the table's columns without
    affinities (as in SQLite; its values may not be converted either, see
    Executor.insert_row) and without collations (the other operand's
    decides a comparison)."""

    has_rowid = True
    indexes = ()
    uncollated = True

    def __init__(self, table: TableInfo) -> None:
        self.name = "excluded"
        self.columns = table.columns
        self.rowid_column = table.rowid_column
        self.affinities = [None] * len(table.columns)
        self.collations = [None] * len(table.columns)
        self.column_index = table.column_index
