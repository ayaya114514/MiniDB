"""Statement execution: ``Executor`` runs each parsed statement - DDL
itself, queries and writes through compiled plans kept on the syntax tree.

The executor is split into layers, each using only the ones before it:

* ``expressions``: name resolution (``Scope``) and expression compilation
  (``Compiler``), aggregate and window function calls;
* ``generated``: generated columns and rows as queries see them (``load_row``);
* ``ordering``: ORDER BY keys, DISTINCT, compound SELECTs;
* ``sources``: subqueries, views, CTEs, ``pragma_xxx()`` and ``json_each()`` in FROM;
* ``planner``: access paths (row id, index, hash lookups) and join loops;
* ``queries``: compiled SELECT / VALUES / compound queries;
* ``dml``: INSERT, UPDATE, DELETE, upserts and RETURNING;
* this module: the ``Executor`` (statements, DDL, constraints, triggers).
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import math
import os
import random
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

from minidb import dates, functions, pragmas, values
from minidb.foreign_keys import ForeignKeys, Link
from minidb.triggers import Program, TriggerIgnore, Triggers
from minidb.btree import BTree, IntKey
from minidb.catalog import (
    HIGH, Catalog, IndexInfo, IndexKeyCodec, TableInfo, TriggerInfo, ViewInfo, constant_default, is_constant_default,
    quote,
)
from minidb.errors import Error, IntegrityError, OperationalError
from minidb.parser import (
    AlterTable, Analyze, Binary, Call, Collate, Column, Compound, CreateIndex, CreateTable, CreateTrigger,
    CreateView, Cte, Delete, DerivedTable, DropIndex, DropTable, DropTrigger, DropView, Exists, Explain,
    InSelect, Insert, Join, Parameter, Pragma, Reindex, Select, SelectItem, Star, Subquery, TableFunction, TableRef,
    Update, Vacuum, Values,
)
from minidb.parser import CheckConstraint, ColumnDef, Expr, ForeignKey, KeyConstraint, Statement, parse
from minidb.tokenizer import tokenize
from minidb.values import SQLValue, ascii_lower, ascii_upper
from minidb.record import decode_record, decode_row, encode_record
from minidb.pager import Pager
from minidb.sqlite_pager import SqlitePager
from minidb.expressions import (
    AggregateCollector, CompiledQuery, Compiler, NOT_INDEXED, OrderTerm, PreparedStatement,
    RECORD_CONVERTED, Result, Row, RowFunction, Scope, ScopeEntry, calls_function, constant_integer, contains_aggregate,
    fold_and, split_conjuncts, strip_collate, tables_referenced, walk,
)
from minidb.generated import (
    check_generated, check_positions, compile_generated, expand_virtual, generated_order, load_row, new_columns_used,
    replace_possible, trigger_names, unused_virtual, walk_nodes,
)
from minidb.ordering import unique_names
from minidb.sources import (
    CteInfo, DerivedSource, JsonEachSource, PragmaSource, RecursiveSource, WorkingSource, carries_json,
)
from minidb.planner import (
    DerivedScan, FullScan, HashLookup, IndexScan, JoinLevel, MAX_GENERATED_LEVELS, RANGE_FACTOR, ROWID,
    covering_index_scan, full_scan, grouping_index_scan, index_rooted, inner_join_loop, plan_access, table_rows,
)
from minidb.queries import CompiledCompound, CompiledSelect, CompiledValues, PreparedSelect, _PLANNED
from minidb.dml import (
    DefaultRegisters, PreparedDelete, PreparedInsert, PreparedUpdate, PreparedUpsert, PreparedViewChange,
    PreparedViewInsert,
)


class Executor:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.last_insert_rowid = 0
        self.parameters = []  # values of ?-parameters; compiled plans read this list
        self.once_caches = []  # caches of uncorrelated subqueries of the plan being compiled
        self.expanding = []  # views and CTEs being compiled (to detect one that uses itself)
        self.cte_scopes = []  # the WITH clauses in effect: dicts of lower-case name -> CteInfo
        self.column_hook = None  # called with (Column, table) for each column reference compiled
        self.statement_journal = True  # see PreparedInsert.statement_journal
        self.ran = False  # see execute
        self.triggers = Triggers(self)
        self.outer_scope = None  # the NEW / OLD scope while a trigger's statements are compiled
        self.compiling_trigger = 0  # > 0: RAISE() is allowed
        # "main" while a view or trigger of the main database is compiled: its
        # names mean main's tables (SQLite's sqlite3FixSrcList); a temporary
        # object's search the temp database first.
        self.default_schema = None
        self.pragma_schema = None  # the schema of the PRAGMA being run ("main", "temp" or None)
        self.frame_depth = 0  # trigger programs and foreign key actions running (SQLite's nFrame)
        self.pinned = {}  # table name -> frame depth of an UPDATE whose REPLACE runs DELETE triggers (check_pinned)
        self.compile_depth = 0  # prepare() calls in progress
        self.program_log = []  # the programs the statement being compiled compiled, in order (log_program)
        self.program_seen = set()
        self.program_stack = []  # for each trigger program being compiled: the programs it asks for
        self.changes = 0  # changes() and total_changes(), kept up to date by Database
        self.total_changes = 0
        # PRAGMA settings of the connection (see minidb.pragmas).
        self.settings = {"foreign_keys": 0, "defer_foreign_keys": 0, "ignore_check_constraints": 0,
                         "recursive_triggers": 0, "cache_size": -2000, "synchronous": 2}
        self.data_version = 1  # PRAGMA data_version: bumped when another connection commits
        self.integrity_problems = None  # Database.integrity_check, for PRAGMA integrity_check
        self.in_transaction = lambda: False  # set by Database
        self.foreign_keys = ForeignKeys(self)

    def execute(self, stmt: Statement, parameters: Sequence[SQLValue] = ()) -> Result:
        """Execute a parsed statement with the given parameter values (a list
        indexed by parameter number - 1)."""
        self.parameters[:] = parameters
        self.statement_journal = True
        self.ran = False  # whether the statement got past compiling (Database: what an error ends)
        dates.statement_time[0] = None  # 'now' is fixed for the length of a statement
        keys = self.foreign_keys
        keys.immediate = 0
        keys.unchecked = None
        if type(stmt) in _PLANNED:
            plan = self.prepare(stmt)
            for cache in plan.once_caches:
                cache.clear()
            self.ran = True
            self.statement_journal = getattr(plan, "statement_journal", True)
            if keys.enabled and isinstance(stmt, (Insert, Update, Delete)) and getattr(plan, "view", None) is None \
                    and keys.involved(plan.table):
                self.statement_journal = self.statement_journal or self.foreign_keys_may_abort(stmt, plan)
            result = plan.run()
            if keys.enabled or keys.immediate or keys.deferred or keys.deferred_immediate:
                self.check_foreign_keys()
            return result
        if isinstance(stmt, CreateTable):
            if stmt.query is not None:
                return self.create_table_as(stmt)
            self.catalog.create_table(stmt, self.check_new_table)
            return Result()
        if isinstance(stmt, DropTable):
            self.drop_table(stmt)
            return Result()
        if isinstance(stmt, CreateIndex):
            return self.create_index(stmt)
        if isinstance(stmt, DropIndex):
            self.catalog.drop_index(stmt.name, stmt.if_exists, stmt.schema)
            return Result()
        if isinstance(stmt, CreateView):
            self.catalog.create_view(stmt)
            return Result()
        if isinstance(stmt, Reindex):
            return self.reindex(stmt.name)
        if isinstance(stmt, Vacuum):
            return self.vacuum(stmt)
        if isinstance(stmt, AlterTable):
            return self.alter_table(stmt)
        if isinstance(stmt, DropView):
            self.catalog.drop_view(stmt.name, stmt.if_exists, stmt.schema)
            return Result()
        if isinstance(stmt, Explain):
            return self.explain(stmt.statement)
        if isinstance(stmt, Analyze):
            self.catalog.analyze(stmt.name)
            return Result()
        if isinstance(stmt, CreateTrigger):
            self.catalog.create_trigger(stmt)
            return Result()
        if isinstance(stmt, DropTrigger):
            self.catalog.drop_trigger(stmt.name, stmt.if_exists, stmt.schema)
            return Result()
        if isinstance(stmt, Pragma):
            self.ran = True
            rows, columns = pragmas.run(self, stmt.name, stmt.value, stmt.schema)
            return Result(rows, columns)
        raise OperationalError(f"unsupported statement: {type(stmt).__name__}")

    # ---- what SQLite compiles with a statement ------------------------------------

    def log_program(self, key: tuple, entry: tuple) -> bool:
        """Note that the statement being compiled compiles a trigger program
        or a foreign key action (SQLite's list pParse->pTriggerPrg, newest
        last); False if it already did (SQLite compiles each once)."""
        for requested in self.program_stack:
            requested.append((key, entry))
        if key in self.program_seen:
            return False
        self.program_seen.add(key)
        self.program_log.append(entry)
        return True

    def set_null_link(self, table: TableInfo) -> Link | None:
        """A SQLite quirk (isSetNullAction): where it codes the check of a new
        row's foreign keys, it leaves out foreign key F if the program it
        compiled last is F's ON DELETE / ON UPDATE SET NULL action."""
        if self.program_log:
            entry = self.program_log[-1]
            if entry[0] == "action" and entry[1].child is table and entry[3] == "SET NULL":
                return entry[1]
        return None

    def compile_delete(self, table: TableInfo, orconf: str | None, triggers: bool = True) -> None:
        """What SQLite compiles to delete a row of ``table`` (sqlite3GenerateRowDelete):
        the triggers (all of them first, for their column masks), the foreign
        keys of the row, their actions."""
        if triggers:
            self.triggers.prepare_listed(table.name, "DELETE", None, orconf)
        keys = self.foreign_keys
        if keys.involved(table):
            keys.prepare(table, "delete")

    def compile_update(self, table: TableInfo, changed: set[int], orconf: str | None,
                       replace: bool = False, rowid_changed: bool = False) -> Link | None:
        """What SQLite compiles to update rows of ``table`` setting ``changed``
        (sqlite3Update): the triggers (all of them first, for their column
        masks); the constraint checks (REPLACE's DELETE); the foreign keys of
        the old and the new row; their actions.  Returns the foreign key whose
        new-row check it leaves out."""
        names = trigger_names(table, changed)
        triggers, keys = self.triggers, self.foreign_keys
        triggers.prepare_listed(table.name, "UPDATE", names, orconf)
        checked = None if keys.every_index(table, changed) else changed  # (update.c's hasFK>1)
        if replace and replace_possible(table, orconf, rowid_changed, checked):
            self.compile_delete(table, "REPLACE", bool(self.settings["recursive_triggers"]))
        unchecked = None
        if keys.enabled:
            keys.prepare(table, "update", changed, actions=False)
            unchecked = self.set_null_link(table)
            if keys.required(table, changed):
                keys.prepare_actions(table, "update", changed)
        return unchecked

    def foreign_keys_may_abort(self, stmt: Insert | Update | Delete, plan: Any) -> bool:
        """Whether SQLite gives a statement on a table with foreign keys a
        statement journal for them: when it writes several rows (any UPDATE
        or DELETE with foreign key work does) and their code may abort."""
        keys = self.foreign_keys
        if isinstance(stmt, Insert):
            # (a REPLACE deletes rows; an upsert updates them)
            return plan.multi_write and plan.foreign_keys_abort()
        if isinstance(stmt, Update):
            return keys.required(plan.table, plan.changed) and keys.may_abort(plan.table, "update", plan.changed)
        return keys.may_abort(plan.table, "delete")

    def check_foreign_keys(self) -> None:
        """At the end of a statement: immediate foreign key violations fail
        it; deferred ones too when it is not inside a transaction (it
        commits now)."""
        keys = self.foreign_keys
        if keys.statement_failed() or (not self.in_transaction() and keys.transaction_failed()):
            raise self.constraint_error("FOREIGN KEY constraint failed", "ABORT")

    def drop_table(self, stmt: DropTable) -> None:
        """DROP TABLE; with foreign keys on, a parent table is emptied first
        (its children's actions run, violations count), as SQLite does."""
        keys = self.foreign_keys
        lowered = ascii_lower(stmt.name)
        owner = next((c for c in self.catalog.search(stmt.schema) if lowered in c.tables or lowered in c.views), None)
        table = None if owner is None else owner.tables.get(lowered)  # (None: a view, DROP TABLE refuses it)
        if keys.enabled and table is not None and ascii_lower(table.name) != "sqlite_sequence":
            self.catalog.check_writable(table, "dropped")
            deferred_child = any(link.deferred or keys.defer_all() for link in keys.children_of(table))
            if keys.parents_of(table) or (deferred_child and keys.transaction_failed()):
                tree = self.catalog.table_tree(table)
                for rowid in list(tree.keys()):
                    if rowid in tree:
                        self.delete_row(table, tree, rowid, fire=False)  # (SQLite disables its triggers)
                        keys.extra_changes += 1  # (total_changes() counts them, as in SQLite)
                if not keys.defer_all() and keys.statement_failed():
                    raise self.constraint_error("FOREIGN KEY constraint failed", "ABORT")
        self.catalog.drop_table(stmt.name, stmt.if_exists, stmt.schema)

    def constant(self, expr: Expr) -> SQLValue:
        """The value of a constant expression (a DEFAULT)."""
        return Compiler(Scope(), executor=self).compile(expr)([])

    def foreign_key_violations(self, table_name: object = None) -> list[tuple]:
        """PRAGMA foreign_key_check: (table, rowid, parent, foreign key number)
        for each child row whose parent is missing, as SQLite reports them."""
        keys = self.foreign_keys
        catalog = self.catalog
        if table_name is not None:
            table = next((c.tables[ascii_lower(str(table_name))] for c in catalog.search()
                          if ascii_lower(str(table_name)) in c.tables), None)
            if table is None:
                raise OperationalError(f"no such table: {table_name}")
            tables = [table]
        else:  # (the main database's, as SQLite's pragma without a schema)
            tables = sorted(catalog.tables.values(), key=lambda t: t.schema_key or 0, reverse=True)
        found = []
        for table in tables:
            links = keys.children_of(table)
            if not links:
                continue
            parents = catalog.owner(table).tables  # (a parent is in its child's database)
            for link in links:
                if ascii_lower(link.key.parent) in parents:
                    try:
                        link.locate(parents)
                    except Exception as exc:  # (a mismatch)
                        raise OperationalError(str(exc)) from None
            for rowid, record in catalog.table_tree(table).scan():
                row = self.load_row(table, rowid, record)
                for link in links:
                    if ascii_lower(link.key.parent) not in parents:
                        missing = all(row[table.column_index(n)] is not None for n in link.key.columns)
                    else:
                        missing = keys.parent_exists(link, row) is False
                    if missing:
                        found.append((table.name, rowid if table.has_rowid else None,
                                      link.key.parent, link.number))
        return found

    def prepare(self, stmt: Select | Compound | Insert | Update | Delete) -> PreparedStatement:
        """The compiled plan of a SELECT/INSERT/UPDATE/DELETE.  Plans are kept
        on the (cached) syntax tree and reused until the schema changes."""
        cached = getattr(stmt, "_plan", None)
        if cached is not None and cached[0] == self.catalog.version:
            return cached[1]
        if self.compile_depth == 0:
            self.program_log, self.program_seen = [], set()
        self.compile_depth += 1
        try:
            return self._prepare(stmt)
        finally:
            self.compile_depth -= 1

    def _prepare(self, stmt: Select | Compound | Insert | Update | Delete) -> PreparedStatement:
        self.once_caches = []
        if isinstance(stmt, (Select, Compound, Values)):
            plan = PreparedSelect(self.compile_query(stmt))
            plan.aborts = bool(self.compiling_trigger) and calls_function(stmt)  # (see Program.may_abort)
        else:
            with self.cte_scope(stmt.ctes or []):
                view = self.catalog.find_view(stmt.table, stmt.schema or self.default_schema)
                event = "INSERT" if isinstance(stmt, Insert) else "UPDATE" if isinstance(stmt, Update) else "DELETE"
                names = [name for name, _ in stmt.assignments] if isinstance(stmt, Update) else None
                # (With RETURNING any trigger on the view will do: SQLite's own
                # RETURNING trigger makes the list it checks non-empty.)
                if view is not None and (self.triggers.matching(view.name, "INSTEAD OF", event, names) or (
                        stmt.returning is not None and self.catalog.triggers_on(view.name))):
                    plan = (PreparedViewInsert(self, stmt, view) if isinstance(stmt, Insert)
                            else PreparedViewChange(self, stmt, view))
                elif isinstance(stmt, Insert):
                    plan = PreparedInsert(self, stmt)
                elif isinstance(stmt, Update):
                    plan = PreparedUpdate(self, stmt)
                else:
                    plan = PreparedDelete(self, stmt)
        plan.once_caches = self.once_caches
        stmt._plan = (self.catalog.version, plan)
        return plan

    # ---- reading rows ------------------------------------------------------

    load_row = staticmethod(load_row)

    def compile_query(self, stmt: Select | Compound | Values, parent: Scope | None = None) -> CompiledQuery:
        """Compile a SELECT, VALUES or compound SELECT (``parent``: the
        enclosing query's scope when this is a subquery), with its WITH clause."""
        if parent is None:
            parent = self.outer_scope  # (a trigger's NEW / OLD)
        if stmt.ctes:
            with self.cte_scope(stmt.ctes):
                return self._compile_query(stmt, parent)
        return self._compile_query(stmt, parent)

    def _compile_query(self, stmt: Select | Compound | Values, parent: Scope | None) -> CompiledQuery:
        if isinstance(stmt, Compound):
            return CompiledCompound(self, stmt, parent)
        if isinstance(stmt, Values):
            return CompiledValues(self, stmt, parent)
        return CompiledSelect(self, stmt, parent)

    # ---- common table expressions (WITH) -------------------------------------

    @contextlib.contextmanager
    def cte_scope(self, ctes: list[Cte]) -> Iterator[None]:
        """Make the CTEs of a WITH clause visible (to each other too)."""
        names = {}
        for cte in ctes:
            lowered = ascii_lower(cte.name)
            if lowered in names:
                raise OperationalError(f"duplicate WITH table name: {cte.name}")
            names[lowered] = CteInfo(cte, len(self.cte_scopes))
        self.cte_scopes.append(names)
        try:
            yield
        finally:
            self.cte_scopes.pop()

    def find_cte(self, name: str) -> CteInfo | WorkingSource | None:
        lowered = ascii_lower(name)
        for names in reversed(self.cte_scopes):
            if lowered in names:
                return names[lowered]
        return None

    def cte_source(self, found: CteInfo | WorkingSource, scope: Scope) -> DerivedSource:
        """A CTE used in FROM, compiled in the scope of its WITH clause."""
        if isinstance(found, WorkingSource):  # a recursive CTE inside its recursive part
            if scope.parent is not found.parent_scope:
                raise OperationalError(f"circular reference: {found.name}")
            if found.used:
                raise OperationalError(f"multiple references to recursive table: {found.name}")
            found.used = True
            return found
        if found in self.expanding:
            raise OperationalError(f"circular reference: {found.cte.name}")
        saved = self.cte_scopes
        self.expanding.append(found)
        self.cte_scopes = saved[:found.level + 1]
        try:
            return self.compile_cte(found.cte, scope.parent)
        finally:
            self.cte_scopes = saved
            self.expanding.pop()

    def compile_cte(self, cte: Cte, parent: Scope | None) -> DerivedSource:
        body = cte.query
        parts = body.selects if isinstance(body, Compound) else [body]
        operators = body.operators if isinstance(body, Compound) else []
        recursive = [self_reference_count(part, cte.name) for part in parts]
        if not any(recursive):
            compiled = self.compile_query(body, parent)
            check_cte_columns(cte, compiled.names)
            return DerivedSource(cte.name, compiled, cte.columns, body)
        k = next(i for i, count in enumerate(recursive) if count)
        if k == 0 or operators[k - 1] not in ("UNION", "UNION ALL"):
            raise OperationalError(f"circular reference: {cte.name}")
        initial_stmt = parts[0] if k == 1 else Compound(parts[:k], operators[:k - 1])
        initial = self.compile_query(initial_stmt, parent)
        check_cte_columns(cte, initial.names)
        names = cte.columns or unique_names(initial.names)
        working = WorkingSource(cte.name, names, initial.affinities, parent, initial.collations)
        compiled_parts = []
        for part in parts[k:]:
            if isinstance(part, Select) and (part.group_by or any(
                contains_aggregate(item.expr) for item in part.items if not isinstance(item.expr, Star)
            )):
                raise OperationalError("recursive aggregate queries not supported")
            working.used = False
            self.cte_scopes.append({ascii_lower(cte.name): working})
            try:
                compiled = self.compile_query(part, parent)
            finally:
                self.cte_scopes.pop()
            if len(compiled.names) != len(names):
                raise OperationalError(
                    f"SELECTs to the left and right of {operators[k - 1]} "
                    "do not have the same number of result columns"
                )
            compiled_parts.append(compiled)
        order_terms, limit = [], None
        if isinstance(body, Compound):
            if body.order_by:
                order_terms = self.compound_order_terms(body, [initial] + compiled_parts)
            limit = self.compile_limit(body)
        source = RecursiveSource(cte.name, initial, names, working, compiled_parts,
                                 operators[k - 1] == "UNION", order_terms, limit)
        source.strip = tuple(range(len(names))) if carries_json(body) else ()  # (never flattened)
        return source

    def view_source(self, view: ViewInfo) -> DerivedSource:
        """A view used in FROM: its SELECT, compiled as a subquery that sees
        no enclosing query."""
        if view in self.expanding:
            raise OperationalError(f"view {view.name} is circularly defined")
        self.expanding.append(view)
        saved, self.cte_scopes = self.cte_scopes, []  # a view sees no CTE of the query using it
        outer, self.outer_scope = self.outer_scope, None  # (nor a trigger's NEW / OLD)
        schema, self.default_schema = self.default_schema, None if view.temp else "main"
        try:
            compiled = self.compile_query(view.query)
        finally:
            self.default_schema = schema
            self.outer_scope = outer
            self.expanding.pop()
            self.cte_scopes = saved
        return DerivedSource(view.name, compiled, view.columns, view.query)

    def build_from(self, joins: list[Join], scope: Scope) -> tuple[list[Join], list[DerivedSource]]:
        """Add the FROM clause's tables to ``scope``.

        Returns the joins with USING / NATURAL turned into ON conditions, and
        the derived tables (which must be materialized before each run)."""
        derived = []
        normalized = []
        scope.last_right = max((i for i, j in enumerate(joins) if j.kind in ("RIGHT", "FULL")), default=-1)
        for index, join in enumerate(joins):
            ref = join.table
            if isinstance(ref, DerivedTable):
                # A subquery in FROM cannot see its sibling tables, only
                # the queries enclosing this one.
                compiled = self.compile_query(ref.query, parent=scope.parent)
                if compiled.correlated:
                    scope.uses_outer = True
                source = DerivedSource(ref.alias or "", compiled, query=ref.query)
                derived.append(source)
                scope.add(source, ref.alias or "")
            elif isinstance(ref, TableRef) and ref.schema is not None:
                derived += self.add_table(ref, scope, ref.schema)
            elif ascii_lower(ref.name) in ("json_each", "json_tree", "jsonb_each", "jsonb_tree") and (
                    isinstance(ref, TableFunction) or self.find_cte(ref.name) is None
                    and not self.catalog.has_table(ref.name) and self.catalog.find_view(ref.name) is None):
                self.json_each_source(ref, scope)
            elif isinstance(ref, TableFunction) or (
                    pragmas.function_spec(ref.name) is not None and self.find_cte(ref.name) is None
                    and not self.catalog.has_table(ref.name) and self.catalog.find_view(ref.name) is None):
                if not isinstance(ref, TableFunction):
                    ref = TableFunction(ascii_lower(ref.name), [], ref.alias, ref.pos)
                source, condition = self.table_function_source(ref, scope)
                derived.append(source)
                if source.correlated:
                    scope.uses_outer = True
                scope.add(source, ref.alias or ref.name)
                if condition is not None:  # its argument comes from a table before it in FROM
                    on = Binary("=", Column("arg", ref.alias or ref.name), condition)
                    join = dataclasses.replace(join, on=on if join.on is None else Binary("AND", join.on, on))
            elif self.find_cte(ref.name) is not None:
                source = self.cte_source(self.find_cte(ref.name), scope)
                if not isinstance(source, WorkingSource):
                    derived.append(source)
                    if source.correlated:
                        scope.uses_outer = True
                scope.add(source, ref.alias)
            else:
                derived += self.add_table(ref, scope, self.default_schema)
            if join.natural or join.using is not None:
                join = self.using_condition(scope, index, join)
            normalized.append(join)
        return normalized, derived

    def add_table(self, ref: TableRef, scope: Scope, schema: str | None) -> list[DerivedSource]:
        """Add the table or view ``ref`` names (in ``schema``; None: temp
        first, then main) to ``scope``; returns the view's source, if a view."""
        view = self.catalog.find_view(ref.name, schema)
        if view is not None:
            source = self.view_source(view)
            scope.add(source, ref.alias)
            return [source]
        table = self.catalog.get_table(ref.name, schema)
        self.catalog.check_index_hint(table, ref.indexed_by)
        scope.add(table, ref.alias)
        if ref.not_indexed:
            scope.entries[-1].hint = NOT_INDEXED
        elif ref.indexed_by is not None:
            scope.entries[-1].hint = self.catalog.owner(table).indexes[ascii_lower(ref.indexed_by)]
        return []

    def json_each_source(self, ref: TableFunction | TableRef, scope: Scope) -> None:
        """Add json_each() / json_tree() to ``scope``.  Its arguments see it
        too, as in SQLite: one that uses its own columns (or row id) leaves
        SQLite's virtual table without its argument, and it has no rows."""
        name = ascii_lower(ref.name)
        args = ref.args if isinstance(ref, TableFunction) else []
        if len(args) > 2:
            raise OperationalError(f"too many arguments on {name}() - max 2")
        source = JsonEachSource(name)
        scope.add(source, ref.alias or name)
        me = len(scope.entries) - 1
        compiler = Compiler(scope, executor=self)
        saved, scope.watch = scope.watch, set()  # (the tables the references resolve to, subqueries' too)
        try:
            compiled = [compiler.compile(arg) for arg in args]
        finally:
            depends, scope.watch = scope.watch, saved
            if saved is not None:
                saved |= depends
        if me not in depends:
            source.args, source.depends = compiled, depends

    def table_function_source(self, ref: TableFunction, scope: Scope) -> tuple[PragmaSource, Expr | None]:
        """The source of ``pragma_<name>(arg, schema)`` in FROM, and an
        expression the hidden column ``arg`` must equal, when the argument
        uses a table before it in the FROM clause."""
        spec = pragmas.function_spec(ref.name)
        if spec is None:
            raise OperationalError(f"no such table: {ref.name}")
        takes_arg = spec.arg is not None
        if len(ref.args) > 1 + takes_arg:
            raise OperationalError(f"too many arguments on {ref.name}() - max {1 + takes_arg}")
        arg = ref.args[0] if takes_arg and ref.args else None
        schema = ref.args[-1] if len(ref.args) > takes_arg else None
        compiler = Compiler(scope, executor=self)
        if schema is not None:
            if tables_referenced(schema, scope):
                raise OperationalError(f"MiniDB needs a constant schema argument for {ref.name}()")
            schema = compiler.compile(schema)
        lateral = arg is not None and bool(tables_referenced(arg, scope))
        source = PragmaSource(self, ref.name, spec, None if lateral or arg is None else compiler.compile(arg),
                              schema, lateral, scope)
        return source, (Collate(arg, "NOCASE") if lateral else None)

    @staticmethod
    def using_condition(scope: Scope, index: int, join: Join) -> Join:
        """``JOIN t USING (c, ...)`` / ``NATURAL JOIN t`` as an ON condition.

        As in SQLite, an unqualified ``c`` afterwards means the left table's
        column after an inner or LEFT JOIN (the right table's copy becomes
        reachable only by qualified name), the right table's after a RIGHT
        JOIN, and the first non-NULL of them (a Merge) after a FULL JOIN.
        The left side of the condition is the left-most table with the
        column; in a FROM clause with a RIGHT or FULL JOIN, the first
        non-NULL of all the left tables with it (all but the first must have
        joined on it with USING)."""
        right = scope.entries[index]
        left_entries = scope.entries[:index]

        def having(name: str) -> list[ScopeEntry]:
            return [e for e in left_entries if e.table.column_index(name) is not None]

        if join.natural:
            names = [c.name for c in right.table.columns if having(c.name)]
        else:
            names = join.using
        condition = None
        for name in names:
            key = ascii_lower(name)
            lefts = having(name)
            if not lefts or right.table.column_index(name) is None:
                raise OperationalError(
                    f"cannot join using column {name} - column not present in both tables"
                )
            if scope.last_right < 0 or len(lefts) == 1:
                left = Column(name, lefts[0].name)
            else:
                if any(key not in e.using for e in lefts[1:]):
                    raise OperationalError(f"ambiguous reference to {name} in USING()")
                left = Call("COALESCE", tuple(Column(name, e.name) for e in lefts), defer_affinity=True)
            equal = Binary("=", left, Column(name, right.name))
            condition = equal if condition is None else Binary("AND", condition, equal)
            right.using.add(key)
            if join.kind not in ("RIGHT", "FULL"):
                right.hidden.add(key)
                continue
            # What the unqualified name meant so far, for FULL JOIN's Merge.
            position = right.table.column_index(name)
            affinity = right.table.affinities[position]
            parts = []
            if key in scope.merged:
                merge = scope.merged.pop(key)
                parts, affinity = merge.parts, merge.affinity
            else:
                visible = [e for e in lefts if key not in e.hidden]
                if visible:
                    first = visible[0].table.column_index(name)
                    parts, affinity = [visible[0].offset + first], visible[0].table.affinities[first]
            for entry in lefts:
                entry.hidden.add(key)
            if join.kind == "FULL":
                right.hidden.add(key)
                scope.add_merge(name, index, parts + [right.offset + position], affinity)
        return dataclasses.replace(join, on=condition, using=None, natural=False)

    def where_compiler(self, scope: Scope, aggregate: bool) -> Compiler:
        """The compiler for WHERE and ON.  As in SQLite, an aggregate there is
        an error either way, reported differently in an aggregate query."""
        if aggregate:
            return Compiler(scope, misuse="misuse of aggregate: {name}()", executor=self, allow_aggregates=True)
        return Compiler(scope, executor=self)

    def plan_joins(self, scope: Scope, joins: list[Join], where: Expr | None, order_hint: int | None = None, covering: bool = False, aggregate: bool = False, group_hint: list[tuple[int, str]] | None = None) -> tuple[list[JoinLevel], list[RowFunction]]:
        """Plan a nested loop over ``joins``; returns (levels, constants).

        WHERE conjuncts and the ON conditions of inner joins form one pool of
        filters; each is checked at the first level where all the tables it
        uses are bound.  Conjuncts that use none of the tables (and no
        subquery) are the ``constants``: like SQLite, callers test them once
        before the loop starts and skip it entirely when one is false.  An
        outer (LEFT, RIGHT, FULL) JOIN's ON condition decides which rows
        match at its own level (and is the only thing its access path may
        use).  A RIGHT or FULL JOIN adds its unmatched rows after the loop
        (see join_rows), so a condition on the joined rows is never tested
        before its level.  Without outer joins the tables are joined in the
        cheapest order.
        """
        compiler = self.where_compiler(scope, aggregate)
        rights = [i for i, join in enumerate(joins) if join.kind in ("RIGHT", "FULL")]
        for j, join in enumerate(joins):
            # As in SQLite, an outer join's ON may not use a table to its right
            # (nor any ON, with a RIGHT or FULL JOIN in the FROM clause).
            if join.on is not None and (join.kind != "INNER" or rights):
                scope.watch = set()
                try:
                    Compiler(scope, executor=self, allow_aggregates=True).compile(join.on)
                except OperationalError:
                    pass  # reported below, when compiled for real
                finally:
                    used, scope.watch = scope.watch, None
                if any(index > j for index in used):
                    raise OperationalError("ON clause references tables to its right")

        def floor(j: int) -> int:
            """The lowest level for a condition of join j (len(joins): WHERE)."""
            return max((i for i in rights if i < j), default=0)

        # (conjunct, lowest level, join whose ON it comes from or None)
        pool = [(c, floor(len(joins)), None) for c in split_conjuncts(fold_and(where))]
        for j, join in enumerate(joins):
            if join.kind == "INNER":
                pool += [(c, floor(j), j) for c in split_conjuncts(fold_and(join.on))]
        referenced = []
        constants = []
        for conjunct, lowest, home in pool:
            tables = tables_referenced(conjunct, scope)
            if tables or (home is not None and any(i > home for i in rights)):
                # (A constant ON condition before a RIGHT JOIN only filters
                # the rows it joins to.)
                referenced.append((conjunct, tables, lowest if tables else home, home))
            else:
                constants.append(compiler.compile(conjunct))
        order = list(range(len(joins)))
        if len(joins) > 1 and all(join.kind == "INNER" for join in joins):
            order = self.join_order(scope, [(c, tables) for c, tables, _, _ in referenced], compiler)
            if group_hint and order[0] != 0 and all(len(tables) < 2 for _, tables, _, _ in referenced) and not any(
                    0 in tables for _, tables, _, _ in referenced):
                # A cross join whose first table has an index ordering the
                # groups: SQLite keeps that table outermost to save the sort.
                entry = scope.entries[0]
                if not isinstance(entry.table, DerivedSource):
                    scan = full_scan(self.catalog.table_tree(entry.table), entry.table,
                                     table_rows(self.catalog, entry.table))
                    found = grouping_index_scan(scope, 0, self.catalog, scan, group_hint)
                    if found is not None and found.grouping == "all":
                        order = [0] + [i for i in order if i != 0]
        position = {table: i for i, table in enumerate(order)}
        placed = {}
        for conjunct, tables, lowest, home in referenced:
            level = max([position[t] for t in tables] + [lowest])
            if home is not None and any(i > home for i in rights):
                # An ON condition before a RIGHT JOIN uses no table after its
                # join (checked above); one with a subquery counts as using
                # every table, yet must be tested at its join.
                level = min(level, home)
            placed.setdefault(level, []).append(conjunct)
        # Compile all conditions first: the access paths may then check which
        # columns the query uses (covering indexes).
        compiled = []
        for level, index in enumerate(order):
            join = joins[index]
            match = None
            if join.kind != "INNER" and join.on is not None:
                match = compiler.compile(join.on)
            compiled.append((match, [(f, compiler.compile(f)) for f in placed.get(level, [])]))
        levels = []
        for level, (index, (match, filters)) in enumerate(zip(order, compiled)):
            join = joins[index]
            entry = scope.entries[index]
            if join.kind == "INNER":
                usable = [c for c, lowest, _ in pool if lowest <= level]
            else:
                usable = split_conjuncts(join.on)
            hint = order_hint if level == 0 and index == 0 and not rights else None
            access = plan_access(scope, index, self.catalog, usable, compiler, hint,
                                 bound=set(order[:level]))
            if group_hint and type(access) is FullScan and level == 0 and index == 0 and not rights:
                access = grouping_index_scan(scope, index, self.catalog, access, group_hint) or access
            if isinstance(access, IndexScan) and not covering:
                access.index_values = False  # (UPDATE / DELETE read the table)
            if covering and isinstance(access, IndexScan):
                access.cover_if_possible(scope, index)
            elif covering and type(access) is FullScan:
                access = covering_index_scan(scope, index, self.catalog, access) or access
            if join.kind == "INNER" and isinstance(access, IndexScan) and access.consumed:
                filters = [(c, f) for c, f in filters if not any(c is used for used in access.consumed)]
            levels.append(JoinLevel(entry.table, entry.offset, access,
                                    join.kind in ("LEFT", "FULL"), match, [f for _, f in filters]))
            if join.kind in ("RIGHT", "FULL"):
                scan = plan_access(scope, index, self.catalog, [], compiler)
                levels[-1].unmatched = JoinLevel(entry.table, entry.offset, scan, False, None, [])
            if covering and isinstance(entry.table, TableInfo) and entry.table.virtual:
                # (A SELECT computes only the VIRTUAL columns it uses, as SQLite.)
                unused = unused_virtual(entry.table, {p for i, p in scope.used if i == index})
                if unused:
                    for level in (levels[-1], levels[-1].unmatched):
                        if level is not None:
                            level.skip_virtual(unused)
            levels[-1].merges = [(m.slot, m.parts) for m in scope.merged.values() if m.index == index]
        return levels, constants

    def join_order(self, scope: Scope, referenced: list[tuple[Expr, set[int]]], compiler: Compiler) -> list[int]:
        """The table order with the lowest estimated nested loop cost: every
        permutation for up to 6 tables, greedy beyond.  A condition that only
        filters (no lookup uses it) is guessed to keep a quarter of the rows."""
        count = len(scope.entries)
        pool = [conjunct for conjunct, _ in referenced]
        accesses = {}

        def access(table: int, bound: frozenset[int]) -> tuple[float, float]:
            key = (table, bound)
            if not getattr(scope.entries[table].table, "depends", set()) <= bound:
                return math.inf, math.inf  # (json_each() after the tables its arguments use)
            if key not in accesses:
                plan = plan_access(scope, table, self.catalog, pool, compiler, bound=set(bound))
                rows, cost = plan.estimate()
                if isinstance(plan, (FullScan, DerivedScan)):
                    newly = sum(1 for _, tables in referenced
                                if table in tables and tables <= bound | {table})
                    rows = max(1, rows / RANGE_FACTOR ** newly)
                accesses[key] = rows, cost
            return accesses[key]

        def total(order: Sequence[int]) -> float:
            cost, outer, bound = 0, 1, frozenset()
            for table in order:
                rows, probe = access(table, bound)
                cost += outer * probe
                outer *= rows
                bound |= {table}
            return cost

        if count <= 6:
            best = min(itertools.permutations(range(count)), key=total)  # first of equals
            return list(best)
        order, bound, outer = [], frozenset(), 1
        while len(order) < count:
            table = min((t for t in range(count) if t not in bound),
                        key=lambda t: outer * access(t, bound)[1])
            outer *= access(table, bound)[0]
            order.append(table)
            bound |= {table}
        return order

    @staticmethod
    def join_rows(scope: Scope, levels: list[JoinLevel]) -> Iterator[Row]:
        """Yield every row of the nested loop join.  The same list object is
        yielded each time; callers must copy it to keep it."""
        row = [None] * scope.width
        truth = values.truth
        depth = len(levels)
        for level in levels:
            if isinstance(level.access, HashLookup):
                level.access.reset()
        # (Python allows at most 20 nested blocks: many tables use visit.)
        if levels and len(levels) <= MAX_GENERATED_LEVELS and all(level.plain for level in levels):
            loop = levels[0].loop
            if loop is None:
                loop = levels[0].loop = inner_join_loop(levels)
            return loop(row)

        def passes(conditions: list[RowFunction]) -> bool:
            for condition in conditions:
                if not truth(condition(row)):
                    return False
            return True

        # Row ids a RIGHT / FULL JOIN level matched, by level.
        matched_ids = {i: set() for i, level in enumerate(levels) if level.unmatched is not None}

        def merge(level: JoinLevel) -> None:
            for slot, parts in level.merges:
                row[slot] = next((row[p] for p in parts if row[p] is not None), None)

        def visit(i: int) -> Iterator[Row]:
            if i == depth:
                yield row
                return
            level = levels[i]
            start, stop = level.offset, level.offset + len(level.table.columns) + 1
            load = level.load
            seen = matched_ids.get(i)
            matched = False
            for rowid, record in level.access.candidates(row):
                row[start:stop] = load(rowid, record)
                if level.merges:
                    merge(level)
                if level.match is not None and not truth(level.match(row)):
                    continue
                matched = True
                if seen is not None:
                    seen.add(row[stop - 1])
                if passes(level.filters):
                    yield from visit(i + 1)
            if level.outer and not matched:
                row[start:stop] = [None] * (stop - start)
                merge(level)
                if passes(level.filters):
                    yield from visit(i + 1)

        def unmatched(i: int) -> Iterator[Row]:
            """The rows of a RIGHT / FULL JOIN's table that nothing before it
            matched, with NULL for the tables before, joined to the rest."""
            level = levels[i]
            for before in levels[:i]:
                row[before.offset:before.offset + len(before.table.columns) + 1] = \
                    [None] * (len(before.table.columns) + 1)
                for slot, _ in before.merges:
                    row[slot] = None
            start, stop = level.offset, level.offset + len(level.table.columns) + 1
            scan, seen = level.unmatched, matched_ids[i]
            for rowid, record in scan.access.candidates(row):
                row[start:stop] = scan.load(rowid, record)
                merge(level)
                if row[stop - 1] not in seen and passes(level.filters):
                    yield from visit(i + 1)

        def run() -> Iterator[Row]:
            yield from visit(0)
            for i in matched_ids:
                yield from unmatched(i)

        return run() if matched_ids else visit(0)

    def explain(self, stmt: Select | Compound | Update | Delete) -> Result:
        """One row (table, access path) per table the statement reads, in join order."""
        if isinstance(stmt, (Select, Compound)):
            compiled = self.compile_query(stmt)
            parts = compiled.parts if isinstance(compiled, CompiledCompound) else [compiled]
            levels = [level for part in parts for level in (part.levels or [])]
        else:
            scope = Scope()
            joins, _ = self.build_from([Join(TableRef(stmt.table))], scope)
            levels, _ = self.plan_joins(scope, joins, stmt.where)
        return Result(
            [(level.table.name, level.access.describe()) for level in levels], ["table", "plan"]
        )

    # ---- SELECT -------------------------------------------------------------

    def expand_items(self, stmt: Select, scope: Scope) -> tuple[list[Expr], list[str]]:
        """Select-list expressions with ``*`` expanded, and the column names."""
        exprs, names = [], []
        for item in stmt.items:
            if isinstance(item.expr, Star):
                for table_name, column_name in scope.star_columns(item.expr.table):
                    exprs.append(Column(column_name, table_name))
                    names.append(column_name)
                continue
            exprs.append(item.expr)
            if item.alias is not None:
                names.append(item.alias)
            elif isinstance(item.expr, Column):
                names.append(item.expr.name)
            else:
                names.append(item.text)
        return exprs, names

    @staticmethod
    def result_column_reference(expr: Expr, names: list[str], clause: str, position: int, scope: Scope) -> int | None:
        """Resolve ORDER BY / GROUP BY shorthands: a column number or an alias.

        Returns the 0-based result column index, or None for a plain expression.
        """
        number = constant_integer(expr)
        if number is not None:
            if not 1 <= number <= len(names):
                raise OperationalError(
                    f"{ordinal(position)} {clause} term out of range - "
                    f"should be between 1 and {len(names)}"
                )
            return number - 1
        if isinstance(expr, Column) and expr.table is None:
            lowered = [ascii_lower(name) for name in names]
            if ascii_lower(expr.name) in lowered:
                if clause == "GROUP BY":
                    try:
                        scope.resolve(expr)
                        return None  # an input column wins over an alias in GROUP BY
                    except OperationalError:
                        pass
                return lowered.index(ascii_lower(expr.name))
        return None

    def order_terms(self, stmt: Select, exprs: list[Expr], names: list[str], compiler: Compiler) -> tuple[list[OrderTerm], list[RowFunction]]:
        """Returns ([(source, index, descending, nulls first, collation)], [functions]).

        ``source`` is "output" (index into the result row) or "key" (index into
        the extra sort values computed by ``functions``).  A COLLATE on a
        column number or alias applies to that result column."""
        terms, functions = [], []
        for position, item in enumerate(stmt.order_by, 1):
            nulls_first = item.nulls_first if item.nulls_first is not None else not item.descending
            inner = strip_collate(item.expr)
            index = self.result_column_reference(inner, names, "ORDER BY", position, compiler.scope)
            if index is not None:
                collation = compiler.collation(item.expr if inner is not item.expr else exprs[index])
                terms.append(("output", index, item.descending, nulls_first, collation))
            else:
                terms.append(("key", len(functions), item.descending, nulls_first, compiler.collation(item.expr)))
                functions.append(compiler.compile(item.expr))
        return terms, functions

    @staticmethod
    def compound_order_terms(stmt: Compound, parts: list[CompiledSelect],
                             collations: list[str | None] | None = None) -> list[OrderTerm]:
        """ORDER BY of a compound SELECT: every term must name a result column
        (by number, by name or alias, or as the same expression).  A term
        sorts by its COLLATE, else by the column's ``collations``."""
        terms = []
        count = len(parts[0].names)
        for position, item in enumerate(stmt.order_by, 1):
            nulls_first = item.nulls_first if item.nulls_first is not None else not item.descending
            explicit = item.expr
            item = dataclasses.replace(item, expr=strip_collate(item.expr))
            index = constant_integer(item.expr)
            if index is not None:
                if not 1 <= index <= count:
                    raise OperationalError(
                        f"{ordinal(position)} ORDER BY term out of range - "
                        f"should be between 1 and {count}"
                    )
                index -= 1
            else:
                for part in reversed(parts):
                    if isinstance(item.expr, Column) and item.expr.table is None:
                        lowered = [ascii_lower(name) for name in part.names]
                        if ascii_lower(item.expr.name) in lowered:
                            index = lowered.index(ascii_lower(item.expr.name))
                            break
                    if item.expr in part.exprs:
                        index = part.exprs.index(item.expr)
                        break
                if index is None:
                    raise OperationalError(
                        f"{ordinal(position)} ORDER BY term does not match any column in the result set"
                    )
            if explicit is not item.expr:
                collation = values.collation_name(explicit.collation)
            else:
                collation = collations[index] if collations else None
            terms.append(("output", index, item.descending, nulls_first, collation))
        return terms

    def group_functions(self, stmt: Select, exprs: list[Expr], names: list[str],
                        scope: Scope) -> tuple[list[RowFunction], list[Callable[[SQLValue], tuple]]]:
        """The GROUP BY terms' functions and sort key functions (by their
        collations; a COLLATE on a column number or alias applies to it)."""
        compiler = Compiler(
            scope, misuse="aggregate functions are not allowed in the GROUP BY clause", executor=self
        )
        functions, keys = [], []
        for position, expr in enumerate(stmt.group_by, 1):
            inner = strip_collate(expr)
            index = self.result_column_reference(inner, names, "GROUP BY", position, scope)
            collation = compiler.collation(expr)
            if index is not None:
                if inner is expr:
                    collation = compiler.collation(exprs[index])
                expr = exprs[index]
            functions.append(compiler.compile(expr))
            keys.append(values.collation_sort_key(collation))
        return functions, keys

    @staticmethod
    def group_rows(rows: Iterable[Row], scope: Scope, group_functions: list[RowFunction], aggregates: AggregateCollector,
                   group_keys: list[Callable[[SQLValue], tuple]] | None = None, in_order: bool = False) -> Iterator[Row]:
        """Aggregate ``rows`` into groups; yield each group's representative row
        followed by its aggregate results, ordered by group key (``in_order``:
        as the groups came, when an index scan delivered them in order)."""
        groups = {}
        keys = group_keys if group_keys is not None else [values.sort_key] * len(group_functions)
        aggregates.grouping_loop(group_functions, keys)(rows, groups, aggregates.new_state)
        if not groups and not group_functions:
            groups[()] = [[None] * scope.width, aggregates.new_state()]
        for key in (groups if in_order else sorted(groups)):
            representative, state = groups[key]
            yield representative + aggregates.results(state)

    def compile_limit(self, stmt: Select | Compound) -> Callable[[], tuple[int, int | None]] | None:
        """Functions () -> (offset, end) for LIMIT/OFFSET, or None."""
        if stmt.limit is None:
            return None
        compiler = Compiler(Scope(), executor=self)
        limit = compiler.compile(stmt.limit)
        offset = compiler.compile(stmt.offset) if stmt.offset is not None else None

        def integer(function: RowFunction) -> int:
            value = values.numeric_affinity(function([]))
            if not isinstance(value, int):
                raise IntegrityError("datatype mismatch")
            return value

        def bounds() -> tuple[int, int | None]:
            count = integer(limit)
            if count == 0:
                return 0, 0  # (SQLite stops here: the OFFSET is not evaluated)
            start = max(integer(offset), 0) if offset is not None else 0
            return start, None if count < 0 else start + count
        return bounds


    # ---- INSERT --------------------------------------------------------------

    def prepare_row(self, table: TableInfo, row: Row) -> int | None:
        """Apply column affinities; returns the requested row id.  The row
        is SQLite's registers: JSON values keep their subtype (triggers,
        RETURNING and generated columns see it) until stored_row."""
        for i, affinity in enumerate(table.affinities):
            row[i] = values.apply_affinity(row[i], affinity)
        if table.rowid_column is None:
            return None
        rowid = row[table.rowid_column]
        if rowid is not None and not isinstance(rowid, int):
            raise IntegrityError("datatype mismatch")
        return rowid

    @staticmethod
    def stored_row(table: TableInfo, row: Row) -> Row:
        """A new row's values as its record and index entries hold them
        (values.record_value): no JSON subtype, no IntReal."""
        if RECORD_CONVERTED.isdisjoint(map(type, row)):
            return row
        return [values.record_value(v, a) for v, a in zip(row, table.affinities)]

    @staticmethod
    def generator(table: TableInfo) -> Callable[[Row], None]:
        """The function computing the generated columns of a new row (an
        error at once when they make a loop, as SQLite reports it when it
        compiles the statement)."""
        fill = table.fill_generated
        if fill is None:
            fill = table.fill_generated = compile_generated(table, generated_order(table, table.generated))
        return fill

    def not_null_violation(self, table: TableInfo, row: Row, conflict: str | None = None,
                           raw: Row | None = None) -> tuple[str, str] | None:
        """The NOT NULL constraint ``row`` violates, if any, and how to
        resolve it (IGNORE, ABORT, FAIL or ROLLBACK).  Under REPLACE a NULL
        becomes the column's default first, if that is not NULL (also in
        ``raw``, the values before affinities, which upserts may see)."""
        # As SQLite, in two passes: in column order, a REPLACE column with a
        # default gets it, the others are checked; then the REPLACE columns
        # that are still NULL fail as ABORT.
        # Generated columns are checked in the second pass, computed again
        # first if a REPLACE column could have taken its default.
        replaced = []
        for i, column in enumerate(table.columns):
            if not column.not_null or i == table.rowid_column or column.generated is not None:
                continue  # (a NULL row id alias: a new row id)
            how = conflict or column.not_null_conflict or "ABORT"
            if how == "REPLACE":
                if column.default is not None:
                    if row[i] is None:
                        value = Compiler(Scope(), executor=self).compile(column.default)([])
                        row[i] = values.apply_affinity(value, table.affinities[i])
                        if raw is not None:
                            real = table.affinities[i] == values.REAL and type(value) is int
                            raw[i] = float(value) if real else value
                    replaced.append(i)
                    continue
                how = "ABORT"
            if row[i] is None:
                return f"NOT NULL constraint failed: {table.name}.{column.name}", how
        if replaced and table.generated:
            self.generator(table)(row)
            if raw is not None:
                for i in table.generated:
                    raw[i] = row[i]
        for i, column in enumerate(table.columns):
            if row[i] is not None or not (i in replaced or (column.not_null and column.generated is not None)):
                continue
            how = "ABORT" if i in replaced else conflict or column.not_null_conflict or "ABORT"
            return f"NOT NULL constraint failed: {table.name}.{column.name}", "ABORT" if how == "REPLACE" else how
        return None

    def check_violation(self, table: TableInfo, row: Row, conflict: str | None,
                        changed: set[int] | None = None) -> tuple[str, str] | None:
        """The CHECK constraint ``row`` (values and row id) violates, if
        any, and how to resolve it.  An UPDATE (``changed``: the positions
        it assigns, the row id as ``len(columns)``) checks only the
        constraints that use a changed column, as SQLite does."""
        checks = table.compiled_checks
        if checks is None:
            checks = table.compiled_checks = self.compile_checks(table)
        for message, failed, positions in checks:
            if changed is not None and not changed & positions:
                continue
            if failed(row):
                how = conflict or "ABORT"
                return message, "ABORT" if how == "REPLACE" else how
        return None

    def check_new_table(self, table: TableInfo) -> None:
        """The errors CREATE TABLE reports for its expressions."""
        self.check_constraints_compile(table)
        check_generated(table)

    def check_constraints_compile(self, table: TableInfo) -> None:
        """The errors CREATE TABLE reports for its CHECK constraints."""
        for check in table.checks:
            for node in walk(check.expr):
                if isinstance(node, (Subquery, InSelect, Exists)):
                    raise OperationalError("subqueries prohibited in CHECK constraints")
                if isinstance(node, Parameter):
                    raise OperationalError("parameters prohibited in CHECK constraints")
        self.compile_checks(table)

    def compile_checks(self, table: TableInfo) -> list[tuple[str, RowFunction, set[int]]]:
        """(message, failed(row), positions used) for each CHECK constraint."""
        scope = Scope()
        scope.add(table)
        compiler = Compiler(scope, executor=self)
        width = len(table.columns)
        truth = values.truth
        checks = []
        for check in table.checks:
            test = compiler.compile(check.expr)
            positions = check_positions(table, check)

            def failed(row: Row, test: RowFunction = test) -> bool:
                value = test(row)
                return value is not None and not truth(value)
            checks.append((f"CHECK constraint failed: {check.name or sqlite_dequote(check.text)}", failed, positions))
        return checks

    @staticmethod
    def constraint_error(message: str, conflict: str) -> IntegrityError:
        """A constraint violation under the statement's conflict resolution:
        Database keeps the statement's earlier changes for FAIL and rolls
        back the whole transaction for ROLLBACK (ABORT: just the statement)."""
        error = IntegrityError(message)
        error.resolution = conflict
        return error

    def find_conflict(self, index: IndexInfo, row: Row, own_rowid: int | None) -> int | None:
        """The row id of a row that ``row`` collides with in UNIQUE ``index``
        (not counting row ``own_rowid``: the row being updated)."""
        key_values = [row[p] for p in index.positions]
        if any(v is None for v in key_values):
            return None  # NULLs never conflict
        prefix = index.prefix(key_values)
        row_id = index.row_id
        for key, _ in self.catalog.index_tree(index).scan(prefix, prefix + (HIGH,)):
            other = row_id(key)
            if other != own_rowid:
                return other
        return None

    @staticmethod
    def unique_error(table: TableInfo, index: IndexInfo) -> str:
        return "UNIQUE constraint failed: " + ", ".join(f"{table.name}.{c}" for c in index.column_names)

    def replace_fires(self, table: TableInfo) -> bool:
        """Whether a REPLACE's delete of a row of ``table`` runs triggers."""
        return bool(self.settings["recursive_triggers"]) and self.triggers.exist(table.name, "DELETE")

    def replace_rechecks(self, table: TableInfo) -> bool:
        """Whether SQLite checks the uniqueness constraints again after a
        REPLACE deleted a row of ``table`` (its regTrigCnt): DELETE triggers
        may run, or foreign key work (the table is in a foreign key)."""
        return self.replace_fires(table) or self.foreign_keys.involved(table)

    def recheck_unique(self, table: TableInfo, tree: BTree, row: Row, rowid: int, own: int | None,
                       indexes: list[IndexInfo], last_found: int | None = None) -> None:
        """After a REPLACE that may have run triggers or foreign key actions,
        SQLite checks the row id and the REPLACE ``indexes`` again, as ABORT
        (the triggers may have kept a row - RAISE(IGNORE) - or added one).
        ``own``: the row id of the row being updated.  The recheck copies the
        first pass's code without reading the found entry's row id: it
        compares the row id the first pass found last (``last_found``) with
        ``own``, so an UPDATE trips over its own entry of an index whose key
        it did not change when the first pass last found another row.  (A
        WITHOUT ROWID table's code reads the found entry's PRIMARY KEY, so
        there the comparison is right; its PRIMARY KEY is among ``indexes``.)"""
        if not table.has_rowid:
            for index in indexes:
                if self.find_conflict(index, row, None) not in (None, own):
                    raise self.constraint_error(self.unique_error(table, index), "ABORT")
            return
        if rowid != own and rowid in tree:
            raise self.constraint_error(self.rowid_conflict(table).args[0], "ABORT")
        for index in indexes:
            if self.find_conflict(index, row, None) is not None and (own is None or last_found != own):
                raise self.constraint_error(self.unique_error(table, index), "ABORT")

    def check_pinned(self, table: TableInfo) -> None:
        """SQLite pins the cursor of the row an UPDATE is changing while a
        REPLACE of a UNIQUE conflict deletes another row and runs its DELETE
        triggers (OP_CursorLock); a write to that table from them fails with
        SQLITE_CONSTRAINT_PINNED."""
        depth = self.pinned.get(table.name)
        if depth is not None and self.frame_depth > depth:
            raise self.constraint_error("constraint failed", "ABORT")

    def delete_row(self, table: TableInfo, tree: BTree, rowid: int, replace: bool = False,
                   orconf: str | None = None, fire: bool = True) -> Row | None:
        """Delete a row and its index entries; returns it (with its row id),
        or None if a BEFORE trigger's RAISE(IGNORE) kept it or the trigger
        deleted it.  With foreign keys on: their checks before, their
        actions after.  ``replace``: deleted by REPLACE, which fires DELETE
        triggers only with PRAGMA recursive_triggers (as in SQLite);
        ``fire``: False for DROP TABLE's implicit DELETE."""
        row = self.load_row(table, rowid, tree.get(rowid))
        triggers = self.triggers
        fire = fire and (not replace or self.settings["recursive_triggers"])
        if replace:
            orconf = "REPLACE"
        current = row
        if fire and triggers.matching(table.name, "BEFORE", "DELETE"):
            try:
                triggers.fire(table.name, "BEFORE", "DELETE", row, None, None, orconf)
            except TriggerIgnore:
                return None
            if rowid not in tree:
                return None
            current = self.load_row(table, rowid, tree.get(rowid))
        keys = self.foreign_keys
        involved = keys.involved(table)
        if involved:
            keys.row_removing(table, row)
        if self.pinned:
            self.check_pinned(table)
        self.remove_index_entries(table, current, rowid)
        tree.delete(rowid)
        if involved:
            keys.convert_old(table, row)
            keys.actions(table, row)
        if fire and triggers.matching(table.name, "AFTER", "DELETE"):
            try:
                triggers.fire(table.name, "AFTER", "DELETE", row, None, None, orconf)
            except TriggerIgnore:
                pass
            except Error as exc:
                exc.row_done = 1  # (SQLite counted the row before its AFTER triggers)
                raise
        return row

    def check_unique(self, table: TableInfo, row: Row, rowid: int) -> None:
        """Raise if another row has the same values in a UNIQUE index.

        NULLs never conflict.  Indexes are checked newest first, like SQLite.
        """
        for index in table.indexes:
            if not index.unique:
                continue
            key_values = [row[p] for p in index.positions]
            if any(v is None for v in key_values):
                continue
            prefix = index.prefix(key_values)
            for key, _ in self.catalog.index_tree(index).scan(prefix, prefix + (HIGH,)):
                if index.row_id(key) != rowid:
                    columns = ", ".join(f"{table.name}.{c}" for c in index.column_names)
                    raise IntegrityError(f"UNIQUE constraint failed: {columns}")

    def add_index_entries(self, table: TableInfo, row: Row, rowid: int) -> None:
        for index in table.indexes:
            if not index.table_pk:  # (a WITHOUT ROWID table's PRIMARY KEY tree is the table's)
                self.catalog.index_tree(index).insert(index.key(row, rowid), b"")

    def remove_index_entries(self, table: TableInfo, row: Row, rowid: int) -> None:
        for index in table.indexes:
            if not index.table_pk:
                self.catalog.index_tree(index).delete(index.key(row, rowid))

    @staticmethod
    def row_key(table: TableInfo, row: Row) -> tuple:
        """A WITHOUT ROWID table's key for a row: its PRIMARY KEY's sort keys,
        of the values as the record holds them (no JSON subtype: the key's
        values are read back by covering index scans)."""
        pk = table.pk_index
        record_value, affinities = values.record_value, table.affinities
        return pk.prefix([record_value(row[p], affinities[p]) for p in pk.positions])

    @staticmethod
    def encode(table: TableInfo, row: Row, tree: BTree | None = None) -> bytes | list:
        """The record to store for ``row``: as the values themselves for a
        ``tree`` that takes them (a SQLite file's, which encodes its own)."""
        stored = list(row)
        if table.rowid_column is not None:
            stored[table.rowid_column] = None  # kept in the key, not the record
        if table.storage is not None:
            stored = [stored[p] for p in table.storage]  # (not the VIRTUAL columns)
        if getattr(tree, "rows", False):
            return stored
        return encode_record(stored)

    def insert_row(self, table: TableInfo, tree: BTree, row: Row, conflict: str | None = None,
                   upserts: Sequence[PreparedUpsert] = (), rowid: SQLValue = None,
                   defaults: DefaultRegisters | None = None, sequence: list[int] | None = None,
                   single: bool = False) -> tuple[str, Row] | None:
        """Insert ``row`` under a conflict resolution (INSERT OR ...; None:
        each constraint's ON CONFLICT, else ABORT) and the statement's ON
        CONFLICT clauses.  Returns ("insert", row + [rowid]), ("update",
        row + [rowid]) when an upsert updated an existing row, or None when
        nothing changed (IGNORE, DO NOTHING, DO UPDATE ... WHERE false).

        As in SQLite: NOT NULL is checked first, then CHECK, then the upsert
        targets in clause order, then the row id and the other UNIQUE
        indexes (newest first, those to REPLACE last).  REPLACE deletes each
        conflicting row and goes on.  ``rowid`` is a row id given by name
        for a table without an INTEGER PRIMARY KEY; ``defaults``: the
        statement's columns filled with their defaults; ``sequence``: the
        AUTOINCREMENT counter of the statement (a one-item list)."""
        # The values before column affinities (see below); SQLite has already
        # made integers in REAL columns REALs (OP_RealAffinity).
        raw = [float(v) if a == values.REAL and type(v) is int else v for v, a in zip(row, table.affinities)] \
            if values.REAL in table.affinities else row[:]
        given, rowid = rowid, self.prepare_row(table, row)
        if defaults is not None and defaults.converted:
            for position in defaults.positions:
                raw[position] = row[position]
        if given is not None:
            rowid = values.apply_affinity(given, values.INTEGER)
            if not isinstance(rowid, int):
                raise IntegrityError("datatype mismatch")
        triggers, any_triggers = self.triggers, self.catalog.any_triggers
        if any_triggers and triggers.matching(table.name, "BEFORE", "INSERT"):
            # NEW has the values with their affinities, and row id -1 when it is not known yet.
            new = row + [-1 if rowid is None else rowid]
            if table.rowid_column is not None:
                new[table.rowid_column] = new[-1]
            if table.generated:
                self.generator(table)(new)
            try:
                triggers.fire(table.name, "BEFORE", "INSERT", None, new, None, conflict)
            except TriggerIgnore:
                return None
        if sequence is not None and rowid is not None:
            sequence[0] = max(sequence[0], rowid)
        fresh = rowid is None  # (a new row id is never taken)
        keyed = not table.has_rowid  # (WITHOUT ROWID: the PRIMARY KEY is the key)
        if table.generated:
            # SQLite computes them once the row id is known, before the
            # constraints - and first applies the column affinities in place
            # (sqlite3ComputeGeneratedColumns calls sqlite3TableAffinity), so
            # an upsert's "excluded" row shows the converted values.
            if fresh and not keyed:
                rowid = self.new_rowid(tree, sequence)
            if table.rowid_column is not None:
                row[table.rowid_column] = rowid
            self.generator(table)(row)
            raw = row[:]
            if defaults is not None:
                defaults.converted = True
        violation = self.not_null_violation(table, row, conflict, raw)
        if violation is not None:
            if violation[1] == "IGNORE":
                return None
            raise self.constraint_error(*violation)
        if keyed:
            rowid = self.row_key(table, row)
        elif fresh and not table.generated:
            rowid = self.new_rowid(tree, sequence)
        if table.rowid_column is not None:
            row[table.rowid_column] = raw[table.rowid_column] = rowid
        if table.checks and not self.settings["ignore_check_constraints"]:
            # (SQLite applies the column affinities in place before it tests CHECK constraints.)
            if defaults is not None:
                defaults.converted = True
            violation = self.check_violation(table, row + [rowid], conflict)
            if violation is not None:
                if violation[1] == "IGNORE":
                    return None
                raise self.constraint_error(*violation)
        constraints = [u.constraint for u in upserts if u.constraint is not None] if upserts else []
        rowid_how = conflict or table.rowid_conflict() or "ABORT"
        unique = [i for i in table.indexes if i.unique] if table.indexes else []
        if keyed:
            pass  # (the PRIMARY KEY's uniqueness is that of its index)
        elif rowid_how == "REPLACE" and conflict is None and unique and "rowid" not in constraints:
            unique.append("rowid")  # SQLite defers a REPLACE of the row id until after the others
        else:
            unique.insert(0, "rowid")
        constraints += [c for c in unique if c not in constraints]
        # SQLite applies the column affinities to the new values in place when
        # it checks the first index (or a CHECK constraint); an upsert's
        # "excluded" row shows them converted only if the conflict was found
        # after that.
        converted = bool(table.generated) or (bool(table.checks) and not self.settings["ignore_check_constraints"])
        replaced = False  # a REPLACE deleted a row and its DELETE triggers ran
        for constraint in constraints:
            if constraint == "rowid":
                other = rowid if not fresh and rowid in tree else None
            else:
                converted = True
                if defaults is not None:
                    defaults.converted = True
                other = self.find_conflict(constraint, row, None)
            if other is None:
                continue
            upsert = next((u for u in upserts if u.constraint in (constraint, None)), None)
            if upsert is not None:
                return upsert.apply(tree, other, (row if converted else raw) + [rowid])
            how = rowid_how if constraint == "rowid" else conflict or constraint.conflict or "ABORT"
            if how == "IGNORE":
                return None
            if how == "REPLACE":
                self.delete_row(table, tree, other, replace=True)
                replaced = replaced or self.replace_rechecks(table)
                continue
            message = self.rowid_conflict(table).args[0] if constraint == "rowid" else self.unique_error(table, constraint)
            raise self.constraint_error(message, how)
        if replaced:
            self.recheck_unique(table, tree, row, rowid, None,
                                [c for c in constraints if c != "rowid" and (conflict or c.conflict) == "REPLACE"])
        if defaults is not None:
            defaults.converted = True  # (OP_MakeRecord converts in place too)
        keys = self.foreign_keys
        if keys.enabled and keys.involved(table):
            keys.row_inserted(table, row + [rowid], single)
        if self.pinned:
            self.check_pinned(table)
        stored = self.stored_row(table, row)
        tree.insert(rowid, self.encode(table, stored, tree))
        if table.indexes:
            self.add_index_entries(table, stored, rowid)
        if any_triggers and triggers.matching(table.name, "AFTER", "INSERT"):
            if not keyed:
                self.last_insert_rowid = rowid  # (the trigger sees it)
            try:
                triggers.fire(table.name, "AFTER", "INSERT", None, row + [rowid], None, conflict)
            except TriggerIgnore:
                pass
            except Error as exc:
                exc.row_done = 1
                raise
        return "insert", row + [rowid]

    def update_row(self, table: TableInfo, tree: BTree, rowid: int, old: Row, new: Row, conflict: str | None = None,
                   changed: set[int] | None = None) -> Row | None:
        """Replace row ``rowid`` (``old``: its values and row id) with ``new``
        (values and row id).  Returns the stored row with its row id, or
        None if IGNORE skipped it.  ``changed``: the positions assigned (for
        the CHECK constraints)."""
        width = len(table.columns)
        row = new[:width]
        keyed = not table.has_rowid  # (WITHOUT ROWID: the PRIMARY KEY is the key)
        if keyed:
            self.prepare_row(table, row)
            new_rowid = self.row_key(table, row)
        elif table.rowid_column is None:
            new_rowid = values.numeric_affinity(new[width])
            if not isinstance(new_rowid, int):
                raise IntegrityError("datatype mismatch")
            self.prepare_row(table, row)
        else:
            new_rowid = self.prepare_row(table, row)
            if new_rowid is None:
                raise IntegrityError("datatype mismatch")
        if table.generated:
            self.generator(table)(row)
        triggers = self.triggers
        names = None if changed is None else trigger_names(table, changed)
        current = old
        before = triggers.matching(table.name, "BEFORE", "UPDATE", names)
        if before:
            new = row
            if table.generated and changed is not None:
                # SQLite loads only the columns the UPDATE sets or the BEFORE
                # triggers name as new.x (sqlite3TriggerColmask): NEW's
                # generated columns see NULL for the others.
                used = new_columns_used(table, before)
                new = [None if (c.generated is None and i not in changed and i != table.rowid_column and i < 32
                                and used is not None and i not in used) else v
                       for i, (c, v) in enumerate(zip(table.columns, row))]
                self.generator(table)(new)
            try:
                triggers.fire(table.name, "BEFORE", "UPDATE", old, new + [new_rowid], names, conflict)
            except TriggerIgnore:
                return None
            if rowid not in tree:
                return None  # (the trigger deleted it)
            # The trigger may have changed the row: the columns the UPDATE does
            # not set take their values from it now (SQLite's trigger1-18.0).
            current = self.load_row(table, rowid, tree.get(rowid))
            for i in range(width):
                if (changed is None or i not in changed) and i != table.rowid_column:
                    row[i] = current[i]
            if table.generated:
                self.generator(table)(row)
            if keyed:
                new_rowid = self.row_key(table, row)
        violation = self.not_null_violation(table, row, conflict)
        if violation is not None:
            if violation[1] == "IGNORE":
                return None
            raise self.constraint_error(*violation)
        if keyed:
            new_rowid = self.row_key(table, row)  # (REPLACE may have given a PRIMARY KEY column its default)
        if table.checks and not self.settings["ignore_check_constraints"]:
            if changed is not None and table.rowid_column in changed:
                changed = changed | {width}
            violation = self.check_violation(table, row + [new_rowid], conflict, changed)
            if violation is not None:
                if violation[1] == "IGNORE":
                    return None
                raise self.constraint_error(*violation)
        # SQLite checks the indexes with a column the UPDATE sets, or all of
        # them when it sets the row id or a foreign key needs it (update.c's aRegIdx).
        width = len(table.columns)
        every = (changed is None or table.rowid_column in changed or width in changed
                 or self.foreign_keys.every_index(table, changed))
        if keyed and not every:  # (a new PRIMARY KEY changes every index's entry: update.c's chngPk)
            every = any(p in changed for p in table.pk_index.positions)
        constraints = [i for i in table.indexes if i.unique and (every or any(p in changed for p in i.positions))]
        rowid_how = conflict or table.rowid_conflict() or "ABORT"
        if keyed:
            pass  # (the PRIMARY KEY's uniqueness is that of its index)
        elif rowid_how == "REPLACE" and conflict is None and constraints:
            constraints.append("rowid")
        else:
            constraints.insert(0, "rowid")
        replaced = False  # a REPLACE deleted a row and SQLite will check again (replace_rechecks)
        last_found = None  # the row id the last index lookup found (recheck_unique)
        for constraint in constraints:
            if constraint == "rowid":
                if new_rowid == rowid or new_rowid not in tree:
                    continue
                other, how = new_rowid, rowid_how
            else:
                other = self.find_conflict(constraint, row, None)
                if other is None:
                    continue
                last_found = other
                if other == rowid:
                    continue
                how = conflict or constraint.conflict or "ABORT"
            if how == "IGNORE":
                return None
            if how != "REPLACE":
                message = (self.rowid_conflict(table).args[0] if constraint == "rowid"
                           else self.unique_error(table, constraint))
                raise self.constraint_error(message, how)
            fires = self.replace_fires(table)
            pin = fires and constraint != "rowid"
            if pin:
                saved_pin = self.pinned.get(table.name)
                self.pinned[table.name] = self.frame_depth
            try:
                self.delete_row(table, tree, other, replace=True)
            finally:
                if pin:
                    if saved_pin is None:
                        del self.pinned[table.name]
                    else:
                        self.pinned[table.name] = saved_pin
            replaced = replaced or fires or self.foreign_keys.involved(table)
        if replaced:
            self.recheck_unique(table, tree, row, new_rowid, rowid,
                                [c for c in constraints if c != "rowid" and (conflict or c.conflict) == "REPLACE"],
                                last_found)
        keys = self.foreign_keys
        involved = keys.involved(table) and keys.required(table, changed)
        if involved:
            keys.row_removing(table, old, changed)
        if self.pinned:
            self.check_pinned(table)
        self.remove_index_entries(table, current, rowid)
        if involved:
            keys.convert_old(table, old, changed)
        if new_rowid != rowid or (keyed and involved and (
                changed is None or keys.every_index(table, changed) or changed & set(table.pk_index.positions))):
            # (SQLite deletes the old row first when the SET assigns the
            # PRIMARY KEY - chngPk, whatever the value - or a foreign key
            # needs it - hasFK>1: a WITHOUT ROWID table's row is then gone
            # while its new foreign keys are looked up)
            tree.delete(rowid)
        if involved:
            keys.row_adding(table, row + [new_rowid], changed)
        stored = self.stored_row(table, row)
        tree.insert(new_rowid, self.encode(table, stored, tree), replace=True)
        self.add_index_entries(table, stored, new_rowid)
        if involved:
            keys.actions(table, old, row + [new_rowid], changed)
        if triggers.matching(table.name, "AFTER", "UPDATE", names):
            try:
                triggers.fire(table.name, "AFTER", "UPDATE", old, row + [new_rowid], names, conflict)
            except TriggerIgnore:
                pass
            except Error as exc:
                exc.row_done = 1
                raise
        return row + [new_rowid]

    def compile_returning(self, items: list[SelectItem] | None, scope: Scope) -> tuple[list[RowFunction], list[str]] | None:
        """RETURNING: functions of a changed row (its values and row id) and the column names."""
        if items is None:
            return None
        exprs, names = self.expand_items(Select(items), scope)
        compiler = Compiler(scope, executor=self)
        return [compiler.compile(e) for e in exprs], names

    @staticmethod
    def new_rowid(tree: BTree, sequence: list[int] | None = None) -> int:
        """One more than the largest row id; if that is taken by the maximum
        integer, try random ones like SQLite does.  With AUTOINCREMENT
        (``sequence``: the largest row id the table ever had) one more than
        the larger of the two, or the database is "full"."""
        last = tree.last_key()
        if sequence is not None:
            rowid = max(sequence[0], last or 0) + 1
            if rowid > values.INT_MAX:
                raise OperationalError("database or disk is full")
            sequence[0] = rowid
            return rowid
        if last is None:
            return 1
        if last < values.INT_MAX:
            return last + 1
        for _ in range(100):
            candidate = random.randint(1, 2**62)
            if candidate not in tree:
                return candidate
        raise OperationalError("database or disk is full")

    def sequence_value(self, table: TableInfo) -> int | None:
        """The AUTOINCREMENT counter of ``table`` in sqlite_sequence, if any."""
        sequence = self.catalog.owner(table).tables.get("sqlite_sequence")
        if sequence is None:
            return None
        for rowid, record in self.catalog.table_tree(sequence).scan():
            row = self.load_row(sequence, rowid, record)
            if row[0] == table.name:
                return values.to_int64(row[1]) if row[1] is not None else 0
        return None

    def set_sequence_value(self, table: TableInfo, value: int) -> None:
        sequence = self.catalog.owner(table).tables.get("sqlite_sequence")
        if sequence is None:
            return
        tree = self.catalog.table_tree(sequence)
        for rowid, record in tree.scan():
            if self.load_row(sequence, rowid, record)[0] == table.name:
                tree.insert(rowid, encode_record([table.name, value]), replace=True)
                return
        tree.insert(self.new_rowid(tree), encode_record([table.name, value]))

    @staticmethod
    def rowid_conflict(table: TableInfo) -> IntegrityError:
        if table.rowid_column is None:
            return IntegrityError(f"UNIQUE constraint failed: {table.name}.rowid")
        name = table.columns[table.rowid_column].name
        return IntegrityError(f"UNIQUE constraint failed: {table.name}.{name}")

    # ---- indexes -------------------------------------------------------------

    # ---- ALTER TABLE ------------------------------------------------------------

    def alter_table(self, stmt: AlterTable) -> Result:
        """ALTER TABLE edits the stored SQL text the way SQLite does (so
        whatever MiniDB does not model in it survives)."""
        catalog = self.catalog
        table = catalog.get_table(stmt.table, stmt.schema)
        catalog.check_writable(table, "altered")
        if stmt.action != "add":
            self.check_schema_resolves(table)
        if stmt.action == "rename":
            self.rename_table(table, stmt.new_name)
        elif stmt.action == "rename column":
            self.rename_column(table, stmt.column, stmt.new_name, stmt.new_quoted)
        elif stmt.action == "add":
            self.add_column(table, stmt.definition, stmt.definition_text)
        else:
            self.drop_column(table, stmt.column)
        catalog.load()
        return Result()

    def check_schema_resolves(self, table: TableInfo) -> None:
        """SQLite's check before RENAME and DROP COLUMN (renameTestSchema,
        sqlite_rename_table): every view and trigger of the table's database
        (in schema order) and, for a main table, of the temp database too
        must still compile."""
        catalogs = [self.catalog.temp] if table.temp else list(reversed(self.catalog.search()))
        for catalog in catalogs:
            items = sorted([*catalog.views.values(), *catalog.triggers.values()], key=lambda o: o.schema_key or 0)
            for item in items:
                saved = self.cte_scopes
                self.cte_scopes = []
                self.triggers.disabled += 1
                try:
                    if isinstance(item, ViewInfo):
                        self.view_source(item)
                    else:
                        owner = self.catalog.temp if item.on_temp else self.catalog
                        source = owner.tables.get(ascii_lower(item.table_name))
                        if source is None:
                            source = self.view_source(owner.views[ascii_lower(item.table_name)])
                        Program(self, item, source, None)
                except OperationalError as exc:
                    kind = "view" if isinstance(item, ViewInfo) else "trigger"
                    raise OperationalError(f"error in {kind} {item.name}: {exc.args[0]}") from None
                finally:
                    self.triggers.disabled -= 1
                    self.cte_scopes = saved

    def _views(self) -> list[ViewInfo]:
        return self.catalog.all_views()

    def _view_references(self, view: ViewInfo, table: TableInfo) -> list[Column] | None:
        """The column references of a view that resolve to ``table`` (None if
        the view does not compile)."""
        found = []
        saved = self.cte_scopes
        self.cte_scopes = []
        self.column_hook = lambda expr, source: found.append(expr) if source is table else None
        try:
            self.compile_query(view.query)
        except Error:
            return None
        finally:
            self.column_hook = None
            self.cte_scopes = saved
        return found

    def _trigger_references(self, trigger: TriggerInfo, table: TableInfo) -> list[Column] | None:
        """The column references of a trigger's program that resolve to
        ``table`` (None if the program does not compile)."""
        found = []
        catalog = self.catalog.temp if trigger.on_temp else self.catalog
        source = catalog.tables.get(ascii_lower(trigger.table_name))
        if source is None:
            view = catalog.views.get(ascii_lower(trigger.table_name))
            if view is None:
                return None
        self.column_hook = lambda expr, owner: found.append(expr) if owner is table else None
        self.triggers.disabled += 1  # (not the programs of the triggers its statements fire)
        try:
            Program(self, trigger, source if source is not None else self.view_source(view), None)
        except Error:
            return None
        finally:
            self.column_hook = None
            self.triggers.disabled -= 1
        return found

    @staticmethod
    def _trigger_nodes(trigger: TriggerInfo) -> Iterator[object]:
        yield from walk_nodes(trigger.stmt.body)
        yield from walk_nodes(trigger.stmt.when)

    def _referencing_tables(self, table: TableInfo) -> list[tuple[TableInfo, CreateTable]]:
        """The tables (``table`` too) with a foreign key to ``table``, and their parsed SQL."""
        found = []
        for other in self.catalog.owner(table).tables.values():  # (a parent is in its child's database)
            if any(ascii_lower(key.parent) == ascii_lower(table.name) for key in other.foreign_keys):
                found.append((other, parse(other.sql)))
        return found

    def rename_table(self, table: TableInfo, new: str) -> None:
        catalog = self.catalog
        lowered = ascii_lower(new)
        own = catalog.owner(table)
        if lowered in own.tables or lowered in own.views or lowered in own.indexes:
            raise OperationalError(f"there is already another table or index with this name: {new}")
        if lowered.startswith(catalog.reserved_prefixes):
            raise OperationalError(f"object name reserved for internal use: {new}")
        old = table.name
        for view in self._views():
            stmt = parse(view.sql)
            edits = [(node.pos, quote(new)) for node in walk_nodes(stmt.query)
                     if isinstance(node, TableRef) and ascii_lower(node.name) == ascii_lower(old)]
            edits += [(node.table_pos, quote(new)) for node in walk_nodes(stmt.query)
                      if isinstance(node, Column) and node.table is not None
                      and ascii_lower(node.table) == ascii_lower(old) and node.table_pos >= 0]
            if edits:
                catalog.rewrite_view(view, apply_edits(view.sql, edits))
        # Triggers: their table, and the table in their statements (as alter.c does).
        for trigger in catalog.all_triggers():
            changes, target = [], trigger.table_name
            if ascii_lower(trigger.table_name) == ascii_lower(old):
                changes.append((trigger.stmt.table_pos, quote(new)))
                target = new
            for node in self._trigger_nodes(trigger):
                if isinstance(node, TableRef) and ascii_lower(node.name) == ascii_lower(old) and node.pos >= 0:
                    changes.append((node.pos, quote(new)))
                elif (isinstance(node, (Insert, Update, Delete)) and ascii_lower(node.table) == ascii_lower(old)
                      and node.table_pos >= 0):
                    changes.append((node.table_pos, quote(new)))
                elif (isinstance(node, Column) and node.table is not None
                      and ascii_lower(node.table) == ascii_lower(old) and node.table_pos >= 0):
                    changes.append((node.table_pos, quote(new)))
            if changes:
                catalog.rewrite_trigger(trigger, apply_edits(trigger.sql, changes), target)
        # The foreign keys naming it (its own too), and its own name.
        edits = {table: [(parse(table.sql).name_pos, quote(new))]}
        for other, stmt in self._referencing_tables(table):
            edits.setdefault(other, []).extend((key.parent_pos, quote(new)) for key in all_foreign_keys(stmt)
                                               if ascii_lower(key.parent) == ascii_lower(old))
        for other, changes in edits.items():
            other.sql = apply_edits(other.sql, changes)
            if other is not table:
                catalog.rewrite_table_entries(other)
        table.name = new
        for index in table.indexes:
            if index.is_auto:
                prefix = catalog.auto_prefix
                index.name = prefix + new + index.name[len(prefix) + len(old):]
                index.sql = None
            else:
                index.sql = apply_edits(index.sql, [(parse(index.sql).table_pos, quote(new))])
        sequence = own.tables.get("sqlite_sequence")
        if sequence is not None:
            tree = catalog.table_tree(sequence)
            for rowid, record in list(tree.scan()):
                row = self.load_row(sequence, rowid, record)
                if row[0] == old:
                    tree.insert(rowid, encode_record([new, row[1]]), replace=True)
        catalog.rewrite_table_entries(table)

    def rename_column(self, table: TableInfo, old: str, new: str, new_quoted: bool = False) -> None:
        catalog = self.catalog
        position = table.column_index(old)
        if position is None:
            raise OperationalError(f'no such column: "{old}"')
        if any(ascii_lower(c.name) == ascii_lower(new) for i, c in enumerate(table.columns) if i != position):
            raise OperationalError(f"error in table {table.name} after rename: duplicate column name: {new}")
        old = ascii_lower(table.columns[position].name)

        def renamed(text: str, pos: int) -> str:
            """The new name for the token at ``pos``, quoted as SQLite does."""
            bare = text[pos:pos + 1].isalnum() or text[pos:pos + 1] in "_$" or ord(text[pos]) > 127
            return new if bare and not new_quoted else quote(new)

        for view in self._views():
            references = self._view_references(view, table)
            edits = [(node.pos, plain_identifier(new)) for node in references or ()
                     if node.pos >= 0 and ascii_lower(node.name) == old]
            if edits:
                catalog.rewrite_view(view, apply_edits(view.sql, edits))
        for trigger in catalog.all_triggers():
            on_table = ascii_lower(trigger.table_name) == ascii_lower(table.name)
            stmt, positions = trigger.stmt, []
            if on_table and stmt.columns:
                positions += [p for name, p in zip(stmt.columns, stmt.column_pos) if ascii_lower(name) == old]
            for node in self._trigger_nodes(trigger):
                if (on_table and isinstance(node, Column) and node.table is not None
                        and ascii_lower(node.table) in ("new", "old") and ascii_lower(node.name) == old):
                    positions.append(node.pos)
                elif isinstance(node, Insert) and ascii_lower(node.table) == ascii_lower(table.name) and node.columns:
                    positions += [p for name, p in zip(node.columns, node.column_pos) if ascii_lower(name) == old]
                elif isinstance(node, Update) and ascii_lower(node.table) == ascii_lower(table.name):
                    positions += [p for (name, _), p in zip(node.assignments, node.assignment_pos)
                                  if ascii_lower(name) == old]
            positions += [node.pos for node in self._trigger_references(trigger, table) or ()
                          if ascii_lower(node.name) == old]
            positions = sorted({p for p in positions if p >= 0})
            if positions:
                catalog.rewrite_trigger(trigger, apply_edits(trigger.sql, [(p, renamed(trigger.sql, p))
                                                                           for p in positions]), trigger.table_name)
        own = parse(table.sql)
        edits = {table: [c.pos for c in own.columns if ascii_lower(c.name) == old]}
        expressions = [c.expr for c in all_constraints(own) if isinstance(c, CheckConstraint)]
        expressions += [c.generated for c in own.columns if c.generated is not None]
        for expr in expressions:
            edits[table] += [node.pos for node in walk_nodes(expr)
                             if isinstance(node, Column) and ascii_lower(node.name) == old and node.pos >= 0
                             and (node.table is None or ascii_lower(node.table) == ascii_lower(table.name))]
        for key in [c for c in all_constraints(own) if isinstance(c, KeyConstraint)]:
            edits[table] += [c.pos for c in key.columns if ascii_lower(c.name) == old and not key.column_level]
        for key in all_foreign_keys(own):
            edits[table] += [p for name, p in zip(key.columns, key.column_pos)
                             if ascii_lower(name) == old and p not in edits[table]]
        for other, stmt in self._referencing_tables(table):
            for key in all_foreign_keys(stmt):
                if ascii_lower(key.parent) == ascii_lower(table.name):
                    edits.setdefault(other, []).extend(
                        p for name, p in zip(key.parent_columns, key.parent_column_pos) if ascii_lower(name) == old)
        for other, positions in edits.items():
            other.sql = apply_edits(other.sql, [(p, renamed(other.sql, p)) for p in positions])
            if other is not table:
                catalog.rewrite_table_entries(other)
        for index in table.indexes:
            if index.sql is not None and not index.is_auto:
                stmt = parse(index.sql)
                positions = [p for name, p in zip(stmt.columns, stmt.column_pos) if ascii_lower(name) == old]
                index.sql = apply_edits(index.sql, [(p, renamed(index.sql, p)) for p in positions])
        catalog.rewrite_table_entries(table)

    def add_column(self, table: TableInfo, column: ColumnDef, text: str) -> None:
        if table.column_index(column.name) is not None:
            raise OperationalError(f"duplicate column name: {column.name}")
        if column.primary_key:
            raise OperationalError("Cannot add a PRIMARY KEY column")
        if column.unique:
            raise OperationalError("Cannot add a UNIQUE column")
        tree = self.catalog.table_tree(table)
        if column.generated is None:
            if not is_constant_default(column.default):
                raise OperationalError("Cannot add a column with non-constant default")
            if column.not_null and constant_default(column.default) is None:
                raise OperationalError("Cannot add a NOT NULL column with default value NULL")
        elif column.stored and next(iter(tree.scan()), None) is not None:
            raise OperationalError("cannot add a STORED column")  # (to a table with rows)
        if column.collation is not None:
            values.collation_name(column.collation)
        stmt = parse(table.sql)
        sql = table.sql[:stmt.columns_end] + ", " + text.rstrip("; \t\n\r\f\v") + table.sql[stmt.columns_end:]
        added = TableInfo(table.name, table.columns + [column], table.root, table.schema_key,
                          table.table_constraints, sql, table.without_rowid)
        added.validate()
        self.check_constraints_compile(added)
        try:
            check_generated(added, loops=False)
        except OperationalError as exc:
            raise OperationalError(f"error in table {table.name} after add column: {exc.args[0]}") from None
        if added.checks or (column.not_null and column.generated is not None):
            # As SQLite: the rows must pass the table's CHECK and NOT NULL
            # constraints now (the first problem PRAGMA quick_check finds).
            checks = self.compile_checks(added)
            not_null = [i for i, c in enumerate(added.columns) if c.not_null and i != added.rowid_column]
            # (Only the VIRTUAL columns those constraints use are computed.)
            unused = unused_virtual(added, set(not_null).union(*(check_positions(added, c) for c in added.checks)))
            for rowid, record in tree.scan():
                stored = record if type(record) is list else decode_row(record)
                row = expand_virtual(added, stored, rowid, unused) if added.virtual else self.load_row(added, rowid, record)
                if any(row[i] is None for i in not_null):
                    raise OperationalError("NOT NULL constraint failed")  # (SQLite's raise() in a nested statement)
                if any(failed(row) for _, failed, _ in checks):
                    raise OperationalError("CHECK constraint failed")
        table.sql = sql
        self.catalog.rewrite_table_entries(table)

    def drop_column(self, table: TableInfo, name: str) -> None:
        position = table.column_index(name)
        if position is None:
            raise OperationalError(f'no such column: "{name}"')
        column = table.columns[position]
        primary = table.primary_key
        if primary is not None and any(ascii_lower(c.name) == ascii_lower(column.name) for c in primary.columns):
            raise OperationalError(f'cannot drop PRIMARY KEY column: "{column.name}"')
        if column.unique:
            raise OperationalError(f'cannot drop UNIQUE column: "{column.name}"')
        if len(table.columns) == 1:
            raise OperationalError(f'cannot drop column "{column.name}": no other columns exist')
        stmt = parse(table.sql)
        start = stmt.columns[position].pos
        if position < len(table.columns) - 1:
            sql = table.sql[:start] + table.sql[stmt.columns[position + 1].pos:]
        else:
            start = table.sql.rindex(",", 0, start)
            sql = table.sql[:start] + table.sql[stmt.columns_end:]
        try:
            changed = parse(sql)
            dropped = TableInfo(table.name, changed.columns, table.root, None, changed.constraints, sql,
                                changed.without_rowid)
            dropped.validate()
            self.check_new_table(dropped)
        except Error as exc:
            raise OperationalError(f"error in table {table.name} after drop column: {exc.args[0]}") from None
        for index in table.indexes:
            if position in index.positions and not index.is_auto:
                raise OperationalError(
                    f"error in index {index.name} after drop column: no such column: {column.name}")
        for view in self._views():
            references = self._view_references(view, table)
            if any(ascii_lower(node.name) == ascii_lower(column.name) for node in references or ()):
                raise OperationalError(
                    f"error in view {view.name} after drop column: no such column: {column.name}")
        for trigger in self.catalog.all_triggers():
            dropped_name = ascii_lower(column.name)
            used = [node for node in self._trigger_references(trigger, table) or ()
                    if ascii_lower(node.name) == dropped_name]
            if ascii_lower(trigger.table_name) == ascii_lower(table.name):
                used += [node for node in self._trigger_nodes(trigger) if isinstance(node, Column)
                         and node.table is not None and ascii_lower(node.table) in ("new", "old")
                         and ascii_lower(node.name) == dropped_name]
            if used:
                first = min(used, key=lambda node: node.pos)  # (SQLite reports the first, as written)
                name = first.name if first.table is None else f"{first.table}.{first.name}"
                raise OperationalError(f"error in trigger {trigger.name} after drop column: no such column: {name}")
        if position not in table.virtual:  # (a VIRTUAL column is in no record)
            tree = self.catalog.table_tree(table)
            rows = [(rowid, self.load_row(table, rowid, record)) for rowid, record in tree.scan()]
            for rowid, row in rows:
                tree.insert(rowid, self.encode(dropped, row[:position] + row[position + 1:-1], tree), replace=True)
        table.sql = sql
        self.catalog.rewrite_table_entries(table)

    def reindex(self, name: str | None) -> Result:
        """Rebuild the indexes of ``name`` (an index, a table, or the default
        collation BINARY), or all of them."""
        catalog = self.catalog
        lowered = None if name is None else ascii_lower(name)
        named = next((c for c in catalog.search() if lowered in c.indexes or lowered in c.tables), None)
        if lowered is None or lowered == "binary":
            indexes = catalog.all_indexes()
        elif named is not None and lowered in named.indexes:
            indexes = [named.indexes[lowered]]
        elif named is not None:
            indexes = list(named.tables[lowered].indexes)
        elif lowered in ("nocase", "rtrim"):
            indexes = []  # no index uses these collations
        else:
            raise OperationalError("unable to identify the object to be reindexed")
        for index in indexes:
            if index.table_pk:
                continue  # (the table itself)
            catalog.index_tree(index).clear()
            self.build_index(index)
        return Result()

    def vacuum(self, stmt: Vacuum) -> Result:
        """Rebuild the database compactly: every table and index is copied, in
        schema order, into a new in-memory database with bulk_load, whose
        pages then replace the database's own (the page count shrinks and the
        free list is gone; a checkpoint shrinks the file).  ``VACUUM INTO``
        writes the copy to a new file instead."""
        if stmt.schema == "temp":
            return Result()  # temporary views live in memory only
        if stmt.into is not None:
            path = Compiler(Scope(), executor=self).compile(stmt.into)([])
            if not isinstance(path, str):
                raise OperationalError("non-text filename")
            if os.path.exists(path) and os.path.getsize(path) > 0:
                raise OperationalError("output file already exists")
            target = SqlitePager(path, page_size=self.vacuum_page_size()) if self.catalog.sqlite else Pager(path)
            try:
                if self.catalog.sqlite:
                    self.vacuum_auto_vacuum(target)
                self.copy_database(target, keep_rowids=True)
                target.commit()
                target.end_transaction()
                target.checkpoint()
            finally:
                target.close_files()
            return Result()
        pager = self.catalog.pager
        copy = SqlitePager(page_size=self.vacuum_page_size()) if self.catalog.sqlite else Pager()
        if self.catalog.sqlite:
            self.vacuum_auto_vacuum(copy)
        self.copy_database(copy, keep_rowids=False)
        if self.catalog.sqlite and copy.geometry.page_size != pager.geometry.page_size:
            pager.resize(copy.geometry.page_size)
        count = copy.page_count
        for pgno in range(1, count + self.catalog.sqlite):  # (SQLite's pages count from 1)
            if pgno in copy.cache:
                pager.write(copy.cache[pgno])
        pager.write(pager.header)
        pager.header.page_count = count
        last = count  # the last page kept
        if self.catalog.sqlite:
            pager.header.freelist_trunk = pager.header.freelist_count = 0
            pager.header.autovacuum_root = copy.header.autovacuum_root
            pager.header.incremental_vacuum = copy.header.incremental_vacuum
            pager.note_schema_change()
        else:
            pager.header.freelist_head = 0
            last = count - 1
        for pgno in [p for p in pager.cache if p > last]:
            del pager.cache[pgno]
            pager.dirty.discard(pgno)
        self.catalog.load()
        return Result()

    def vacuum_auto_vacuum(self, target: SqlitePager) -> None:
        """The new database VACUUM writes has the auto_vacuum mode PRAGMA
        auto_vacuum asked for, or the current one (as SQLite)."""
        pager = self.catalog.pager
        mode = pager.next_auto_vacuum if pager.next_auto_vacuum is not None else pager.auto_vacuum
        target.write(target.header)
        target.header.autovacuum_root = 1 if mode else 0
        target.header.incremental_vacuum = int(mode == 2)

    def vacuum_page_size(self) -> int:
        """The page size VACUUM writes: what PRAGMA page_size asked for, except
        for an in-memory database (as SQLite)."""
        pager = self.catalog.pager
        if pager.next_page_size is not None and pager.path is not None:
            return pager.next_page_size
        return pager.geometry.page_size

    def copy_database(self, target: Pager | SqlitePager, keep_rowids: bool) -> None:
        """Copy the schema, tables and indexes into the empty database ``target``
        (the schema table keeps its keys, objects get new root pages).

        Without ``keep_rowids`` (VACUUM, but not VACUUM INTO) a table with
        neither an INTEGER PRIMARY KEY nor an index gets new rowids 1, 2, 3...
        in rowid order, as SQLite's VACUUM gives them (its transfer
        optimization keeps rowids only where they may be referenced)."""
        if self.catalog.sqlite:
            self._copy_sqlite_database(target, keep_rowids)
            return
        catalog = Catalog(target)
        source = self.catalog
        for key, value in list(source.schema.scan()):
            kind, name, table_name, root, sql = decode_record(value)[0]
            if kind in ("table", "index"):
                index = source.indexes.get(ascii_lower(name)) if kind == "index" else None
                table = source.tables[ascii_lower(name)] if kind == "table" else None
                # (an index's own codec: NOCASE / RTRIM keys must stay collation keys in the page cache;
                # a WITHOUT ROWID table's is its PRIMARY KEY's)
                codec = (IntKey if table.has_rowid else table.pk_index.codec) if kind == "table" \
                    else index.codec if index is not None else IndexKeyCodec
                tree = BTree.create(target, codec)
                entries = BTree(source.pager, root, codec).scan()
                if kind == "table" and not keep_rowids and table.has_rowid:
                    if table.rowid_column is None and not table.indexes:
                        entries = ((rowid, record) for rowid, (_, record) in enumerate(entries, 1))
                tree.bulk_load(entries)
                root = tree.root
            catalog.schema.insert(key, encode_record([kind, name, table_name, root, sql]))

    def _copy_sqlite_database(self, target: SqlitePager, keep_rowids: bool) -> None:
        """copy_database for SQLite-format files: every tree is copied cell
        by cell in its order, so even objects MiniDB cannot parse survive."""
        from minidb.sqlite_btree import IndexTree, SqliteTable, TableTree

        source = self.catalog
        pager = source.pager
        rows = [(key, decode_record(value)[0]) for key, value in list(source.schema.scan())]
        roots = {}  # schema key -> root page made beforehand (auto_vacuum)
        if target.auto_vacuum:
            # Root pages first, at the front of the file, in the order SQLite's
            # VACUUM creates them: the tables, then the indexes (sqlite_sequence
            # comes with the first AUTOINCREMENT table).
            tables = [(k, r) for k, r in rows if r[0] == "table" and r[3]]
            sequence = next((k for k, r in tables if ascii_lower(r[1]) == "sqlite_sequence"), None)
            for key, row in tables:
                if key == sequence:
                    continue
                roots[key] = (IndexTree if index_rooted(pager, row[3]) else TableTree).create(target)
                info = source.tables.get(ascii_lower(row[1]))
                if sequence is not None and sequence not in roots and info is not None and info.autoincrement:
                    roots[sequence] = TableTree.create(target)
            if sequence is not None and sequence not in roots:
                roots[sequence] = TableTree.create(target)
            for key, row in rows:
                if row[0] == "index" and row[3]:
                    roots[key] = IndexTree.create(target)
        for key, row in rows:
            kind, name, root = row[0], row[1], row[3]
            if kind in ("table", "index") and root:
                if kind == "table" and index_rooted(pager, root):
                    kind = "index"  # (a WITHOUT ROWID table: an index tree, copied as one)
                if kind == "table":
                    tree = TableTree(pager, root)
                    table = source.tables.get(ascii_lower(name))
                    renumber = (not keep_rowids and table is not None and table.rowid_column is None
                                and not table.indexes)
                    entries = ((n if renumber else rowid, tree.payload(cell))
                               for n, (rowid, cell) in enumerate(tree.scan(), 1))
                    row[3] = TableTree.build(target, entries)
                else:
                    tree = IndexTree(pager, root)
                    row[3] = IndexTree.build(target, ((0, tree.payload(cell)) for cell in tree.cells()))
                if key in roots:  # (into the root made for it)
                    (TableTree if kind == "table" else IndexTree)(target, roots[key]).adopt(row[3])
                    row[3] = roots[key]
        # The schema last, built rather than inserted into (no balancing that frees pages).
        TableTree.build_into(target, 1, ((key, SqliteTable._payload(row)) for key, row in rows))
        if target.auto_vacuum and target.header.freelist_count:
            target.vacuum_pages()  # (the pages adopt() freed: the copy has none)
        target.header.user_version = pager.header.user_version
        target.header.application_id = pager.header.application_id

    def create_table_as(self, stmt: CreateTable) -> Result:
        """CREATE TABLE ... AS SELECT, as SQLite's sqlite3EndTable: the
        table's columns are the query's (unique names, the type names of
        their affinities), its SQL is made up (createTableStmt), and the rows
        go in with row ids 1, 2, ... (changing neither changes() nor
        last_insert_rowid())."""
        catalog = self.catalog
        database = catalog.temp_catalog() if stmt.temp else catalog
        database._check_reserved(stmt.name)
        if database._exists(stmt.name, stmt.if_not_exists):
            return Result()
        database._check_new_name(stmt.name)
        compiled = self.compile_query(stmt.query)
        names = [f"column{i}" if ascii_lower(n) in ("true", "false") else n
                 for i, n in enumerate(compiled.names, 1)]
        source = DerivedSource(stmt.name, compiled, unique_names(names), stmt.query)
        sql = create_table_sql(stmt.name, [c.name for c in source.columns], source.affinities)
        created = parse(sql)
        created.temp = stmt.temp
        table = catalog.create_table(created)
        tree = catalog.table_tree(table)
        for rowid, row in enumerate(list(compiled.run()), 1):
            row = list(row)
            self.prepare_row(table, row)
            tree.insert(rowid, self.encode(table, self.stored_row(table, row), tree))
        return Result()

    def create_index(self, stmt: CreateIndex) -> Result:
        index = self.catalog.create_index(stmt)
        if index is None:
            return Result()
        self.ran = True  # (a duplicate found while filling it is a run-time error)
        self.build_index(index)
        return Result()

    def build_index(self, index: IndexInfo) -> None:
        """Fill the (empty) tree of ``index`` from its table: the keys are
        sorted, checked for duplicates and loaded bottom up."""
        table = index.table
        load_row, key = self.load_row, (index.build_key if index.raw_reals else index.key)
        keys = sorted(key(load_row(table, rowid, record), rowid)
                      for rowid, record in self.catalog.table_tree(table).scan())
        if index.unique:
            width = len(index.positions)
            for a, b in zip(keys, keys[1:]):
                # NULLs never conflict (their sort keys start with 0).
                if a[:width] == b[:width] and all(part[0] != 0 for part in a[:width]):
                    raise IntegrityError(self.unique_error(table, index))
        self.catalog.index_tree(index).bulk_load((k, b"") for k in keys)


def sqlite_dequote(text: str) -> str:
    """SQLite's sqlite3Dequote, which names an unnamed CHECK constraint after
    its text: text starting with a quote keeps only what is quoted there
    (so "CHECK ([UnitPrice] >= 0)" fails as "UnitPrice")."""
    if not text or text[0] not in "\"'`[":
        return text
    quote = "]" if text[0] == "[" else text[0]
    result = []
    i = 1
    while i < len(text):
        if text[i] == quote:
            if text[i + 1:i + 2] != quote:
                break
            i += 1
        result.append(text[i])
        i += 1
    return "".join(result)


def all_constraints(stmt: CreateTable) -> list[object]:
    """The constraints of a CREATE TABLE: the columns', then the table's."""
    return [c for column in stmt.columns for c in column.constraints] + stmt.constraints


def all_foreign_keys(stmt: CreateTable) -> list[ForeignKey]:
    return [c for c in all_constraints(stmt) if isinstance(c, ForeignKey)]


def apply_edits(text: str, edits: list[tuple[int, str]]) -> str:
    """Replace the SQL token starting at each position with new text."""
    lengths = {token.pos: len(token.text) for token in tokenize(text)}
    for pos, replacement in sorted(set(edits), reverse=True):
        text = text[:pos] + replacement + text[pos + lengths[pos]:]
    return text


def plain_identifier(name: str) -> str:
    """A name as written in SQL: bare if that reads back the same, else quoted."""
    tokens = tokenize(name)
    if len(tokens) == 2 and tokens[0].kind == "IDENT" and tokens[0].text == name:
        return name
    return quote(name)


def check_cte_columns(cte: Cte, names: list[str]) -> None:
    if cte.columns is not None and len(cte.columns) != len(names):
        raise OperationalError(f"table {cte.name} has {len(names)} values for {len(cte.columns)} columns")


def self_reference_count(query: object, name: str) -> int:
    """How often a SELECT's own FROM clause names table ``name``."""
    if not isinstance(query, Select):
        return 0
    lowered = ascii_lower(name)
    return sum(1 for join in query.source
               if isinstance(join.table, TableRef) and ascii_lower(join.table.name) == lowered)


# Every word SQLite's tokenizer takes as a keyword (sqlite3KeywordCode): a
# name it writes into SQL it makes up is quoted if it is one of them.
SQLITE_KEYWORDS = frozenset("""
    ABORT ACTION ADD AFTER ALL ALTER ALWAYS ANALYZE AND AS ASC ATTACH AUTOINCREMENT BEFORE BEGIN BETWEEN BY
    CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE
    CURRENT_TIME CURRENT_TIMESTAMP DATABASE DEFAULT DEFERRABLE DEFERRED DELETE DESC DETACH DISTINCT DO DROP
    EACH ELSE END ESCAPE EXCEPT EXCLUDE EXCLUSIVE EXISTS EXPLAIN FAIL FILTER FIRST FOLLOWING FOR FOREIGN FROM
    FULL GENERATED GLOB GROUP GROUPS HAVING IF IGNORE IMMEDIATE IN INDEX INDEXED INITIALLY INNER INSERT
    INSTEAD INTERSECT INTO IS ISNULL JOIN KEY LAST LEFT LIKE LIMIT MATCH MATERIALIZED NATURAL NO NOT NOTHING
    NOTNULL NULL NULLS OF OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION PLAN PRAGMA PRECEDING PRIMARY QUERY
    RAISE RANGE RECURSIVE REFERENCES REGEXP REINDEX RELEASE RENAME REPLACE RESTRICT RETURNING RIGHT ROLLBACK
    ROW ROWS SAVEPOINT SELECT SET TABLE TEMP TEMPORARY THEN TIES TO TRANSACTION TRIGGER UNBOUNDED UNION UNIQUE
    UPDATE USING VACUUM VALUES VIEW VIRTUAL WHEN WHERE WINDOW WITH WITHOUT
""".split())
# The type each affinity gets in the SQL SQLite makes up for CREATE TABLE ... AS.
AFFINITY_TYPE_NAMES = {values.TEXT: " TEXT", values.NUMERIC: " NUM", values.INTEGER: " INT", values.REAL: " REAL"}


def ident_put(name: str) -> str:
    """A name as SQLite's identPut writes it: bare when it is letters,
    digits and _ (not starting with a digit, not a keyword), else quoted."""
    plain = all((c.isascii() and c.isalnum()) or c == "_" for c in name)
    if not plain or not name or name[0].isdigit() or ascii_upper(name) in SQLITE_KEYWORDS:
        return quote(name)
    return name


def create_table_sql(name: str, columns: list[str], affinities: list[str | None]) -> str:
    """SQLite's createTableStmt: the CREATE TABLE of CREATE TABLE ... AS,
    on one line when short, else one column per line."""
    size = sum(len(c) + c.count('"') + 2 + 5 for c in columns) + len(name) + name.count('"') + 2
    separator, between, end = ("", ",", ")") if size < 50 else ("\n  ", ",\n  ", "\n)")
    parts = [ident_put(c) + AFFINITY_TYPE_NAMES.get(a, "") for c, a in zip(columns, affinities)]
    return f"CREATE TABLE {ident_put(name)}({separator}{between.join(parts)}{end}"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
