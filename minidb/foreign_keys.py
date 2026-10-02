"""Foreign keys, enforced as SQLite's fkey.c enforces them.

With ``PRAGMA foreign_keys`` on, every change counts violations instead of
failing at once: inserting a child row whose parent key is missing adds one,
removing such a row takes one away (only while there are violations);
removing a parent row adds one for each child row that refers to it, adding
a parent row takes away one for each child row that was waiting for it.  An
immediate constraint's count belongs to the statement - it fails if the
count is not zero at its end - and a deferred one's (DEFERRABLE INITIALLY
DEFERRED, or ``PRAGMA defer_foreign_keys``) to the transaction, checked at
COMMIT.  So rows may come in any order within a statement (a child before
its parent), as in SQLite.

The actions of a removed or changed parent key (ON DELETE / ON UPDATE
CASCADE, SET NULL, SET DEFAULT, RESTRICT) run after the parent row is
removed or changed, as SQLite's action triggers do: they delete or update
the child rows through the same code as statements, so their own foreign
keys are checked (and act) in turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from minidb import values
from minidb.catalog import HIGH, IndexInfo, TableInfo
from minidb.errors import IntegrityError, OperationalError
from minidb.parser import ForeignKey
from minidb.values import ascii_lower

if TYPE_CHECKING:
    from minidb.executor import Executor
else:
    Executor = Any  # (minidb.executor imports this module)

MAX_ACTION_DEPTH = 1000  # SQLite's SQLITE_MAX_TRIGGER_DEPTH


class Mismatch(Exception):
    """A foreign key whose parent key is neither the PRIMARY KEY nor UNIQUE."""


class Link:
    """One foreign key: the child table, its key, its number in PRAGMA
    foreign_key_list, and (once located) the parent's index or row id."""

    def __init__(self, child: TableInfo, key: ForeignKey, number: int) -> None:
        self.child = child
        self.key = key
        self.number = number
        self.parent = None  # the parent table, once located
        self.index = None  # the parent key's index; None: the parent's row id
        self.child_positions = []  # the child columns, in the order of the parent key's columns
        self.parent_positions = []  # the parent key's columns (the row id: the INTEGER PRIMARY KEY's)
        self.located = None  # None: not yet; else the error to report, or True

    @property
    def deferred(self) -> bool:
        return self.key.deferred

    def locate(self, tables: dict[str, TableInfo]) -> None:
        """Find the parent key, as sqlite3FkLocateIndex does; raises
        OperationalError (no such table) or Mismatch."""
        if self.located is True:
            return
        if self.located is not None:
            raise self.located
        try:
            self._locate(tables)
        except (OperationalError, Mismatch) as exc:
            self.located = exc
            raise
        self.located = True

    def _locate(self, tables: dict[str, TableInfo]) -> None:
        key, child = self.key, self.child
        parent = tables.get(ascii_lower(key.parent))
        if parent is None:
            raise OperationalError(f"no such table: main.{key.parent}")
        self.parent = parent
        count = len(key.columns)
        mismatch = Mismatch(f'foreign key mismatch - "{child.name}" referencing "{key.parent}"')
        names = [ascii_lower(n) for n in key.parent_columns]
        child_positions = [child.column_index(n) for n in key.columns]
        if count == 1 and parent.rowid_column is not None and (
                not names or names[0] == ascii_lower(parent.columns[parent.rowid_column].name)):
            self.child_positions, self.parent_positions = child_positions, [parent.rowid_column]
            return
        for index in parent.indexes:
            if not index.unique or len(index.positions) != count:
                continue
            if not names:
                if index.origin != "pk":
                    continue
                self.child_positions = child_positions
                break
            mapping = []
            for position, collation in zip(index.positions, index.collations):
                if collation != parent.collations[position]:
                    break  # (only the columns' own collations)
                name = ascii_lower(parent.columns[position].name)
                if name not in names:
                    break
                mapping.append(child_positions[names.index(name)])
            else:
                self.child_positions = mapping
                break
        else:
            raise mismatch
        self.index = index
        self.parent_positions = list(index.positions)


class ForeignKeys:
    """The foreign key work of one connection: its links (cached per schema
    version) and violation counters."""

    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        self.immediate = 0  # the statement's violations of immediate constraints
        self.deferred = 0  # the transaction's violations of deferred constraints
        self.deferred_immediate = 0  # violations of immediate ones while PRAGMA defer_foreign_keys is on
        self.extra_changes = 0  # rows the actions changed (total_changes() counts them)
        self.unchecked = None  # the foreign key whose new-row check SQLite leaves out (Executor.set_null_link)
        self._version = None
        self._children = {}  # lower-case table name -> [Link] (its own foreign keys)
        self._parents = {}  # lower-case table name -> [Link] (the foreign keys naming it)

    @property
    def enabled(self) -> bool:
        return bool(self.executor.settings["foreign_keys"])

    def _links(self) -> None:
        catalog = self.executor.catalog
        if self._version == catalog.version:
            return
        self._version = catalog.version
        self._children, self._parents = {}, {}
        for table in catalog.tables.values():
            links = [Link(table, key, number) for number, key in enumerate(reversed(table.foreign_keys))]
            self._children[ascii_lower(table.name)] = links
            for link in links:
                self._parents.setdefault(ascii_lower(link.key.parent), []).insert(0, link)

    def children_of(self, table: TableInfo) -> list[Link]:
        """The table's own foreign keys."""
        self._links()
        return self._children.get(ascii_lower(table.name), [])

    def parents_of(self, table: TableInfo) -> list[Link]:
        """The foreign keys that name the table as their parent (the newest first, as SQLite)."""
        self._links()
        return self._parents.get(ascii_lower(table.name), [])

    def involved(self, table: TableInfo) -> bool:
        return self.enabled and bool(self.children_of(table) or self.parents_of(table))

    # ---- counters ---------------------------------------------------------------

    def defer_all(self) -> bool:
        return bool(self.executor.settings["defer_foreign_keys"])

    def count(self, link: Link, increment: int) -> None:
        if self.defer_all():
            self.deferred_immediate += increment
        elif link.deferred:
            self.deferred += increment
        else:
            self.immediate += increment

    def zero(self, link: Link) -> bool:
        """SQLite's OP_FkIfZero: no violation that a decrement could resolve."""
        if link.deferred:
            return self.deferred == 0 and self.deferred_immediate == 0
        return self.immediate == 0 and self.deferred_immediate == 0

    def statement_failed(self) -> bool:
        """(Not zero, as in SQLite: removing a parent's last dangling child
        that was there before makes the count negative, and that fails too.)"""
        return self.immediate != 0

    def transaction_failed(self) -> bool:
        return self.deferred + self.deferred_immediate != 0

    def reset_transaction(self) -> None:
        self.deferred = self.deferred_immediate = 0

    # ---- checks before a statement changes anything -----------------------------------

    def prepare(self, table: TableInfo, kind: str, changed: set[int] | None = None, single_insert: bool = False,
                ignore_errors: bool = False, seen: set | None = None, actions: bool = True) -> None:
        """Raise the errors SQLite reports when it compiles the statement:
        a missing parent table, a foreign key mismatch.  ``kind``: "insert",
        "update" (``changed``: the positions set) or "delete".  As SQLite
        does (sqlite3FkCheck, and sqlite3FkOldmask for UPDATE and DELETE),
        every foreign key that names the table is located when an UPDATE
        needs foreign key work at all, or for a DELETE."""
        if not self.enabled:
            return
        if kind == "update" and not self.required(table, changed):
            return
        children = [link for link in self.children_of(table)
                    if kind != "update" or self.processed(link, changed)]
        parents = self.parents_of(table)
        if kind == "insert":
            parents = [link for link in parents
                       if not (single_insert and not link.deferred and not self.defer_all())]
        tables = self.executor.catalog.tables
        for link in children + parents:
            try:
                link.locate(tables)
            except (OperationalError, Mismatch) as exc:
                if not ignore_errors:
                    raise OperationalError(str(exc)) from None
        if kind != "insert" and actions:
            self.prepare_actions(table, kind, changed)

    def prepare_actions(self, table: TableInfo, kind: str, changed: set[int] | None) -> None:
        """SQLite compiles the action triggers of a DELETE or UPDATE with the
        statement (sqlite3FkActions), and the statements they run (a CASCADE
        delete, the UPDATE of SET NULL ...) in turn: their errors come now.
        Each program is compiled once per statement (Executor.log_program)."""
        executor = self.executor
        for link in self.parents_of(table):
            if kind == "update" and not self.parent_changed(link, table, changed):
                continue
            action = link.key.on_delete if kind == "delete" else link.key.on_update
            if action == "NO ACTION" or (action == "RESTRICT" and self.defer_all()) or not self._usable(link):
                continue
            if not executor.log_program(("action", id(link), kind), ("action", link, kind, action)):
                continue
            if action == "RESTRICT":
                continue  # (its program only raises an error)
            child = link.child
            if action == "CASCADE" and kind == "delete":
                executor.compile_delete(child, "ABORT")
            else:
                width = len(child.columns)
                positions = {width if p == child.rowid_column else p for p in link.child_positions}
                executor.compile_update(child, positions, "ABORT")

    def required(self, table: TableInfo, changed: set[int] | None) -> bool:
        """Whether an UPDATE setting ``changed`` needs foreign key work (sqlite3FkRequired)."""
        return any(self.child_changed(link, changed) for link in self.children_of(table)) or any(
            self.parent_changed(link, table, changed) for link in self.parents_of(table))

    def may_abort(self, table: TableInfo, kind: str, changed: set[int] | None = None,
                  seen: set | None = None) -> bool:
        """Whether the foreign key code of an INSERT / UPDATE / DELETE on
        ``table`` can abort the statement, where SQLite's fkey.c calls
        sqlite3MayAbort (which, for a statement writing several rows, asks
        for a statement journal): a row given an immediate foreign key; a
        parent key removed under an immediate key whose action is not
        CASCADE or SET NULL; RESTRICT; and the same within the statements
        the actions run (somewhat more often: any constraint they touch)."""
        seen = set() if seen is None else seen
        if (id(table), kind) in seen:
            return False
        seen.add((id(table), kind))
        if kind != "delete":
            for link in self.children_of(table):
                if not link.deferred and (kind == "insert" or self.processed(link, changed)):
                    return True
        if kind == "insert":
            return False
        for link in self.parents_of(table):
            if kind == "update" and not self.parent_changed(link, table, changed):
                continue
            action = link.key.on_delete if kind == "delete" else link.key.on_update
            if action == "RESTRICT" or (not link.deferred and action not in ("CASCADE", "SET NULL")):
                return True
            if action == "NO ACTION":
                continue
            child = link.child
            if action == "CASCADE" and kind == "delete":
                if self.may_abort(child, "delete", None, seen):
                    return True
                continue
            width = len(child.columns)
            positions = {width if p == child.rowid_column else p for p in link.child_positions}
            if any(p < width and child.columns[p].not_null for p in positions) or child.checks or any(
                    index.unique and positions & set(index.positions) for index in child.indexes):
                return True
            if self.may_abort(child, "update", positions, seen):
                return True
        return False

    def processed(self, link: Link, changed: set[int] | None) -> bool:
        """Whether an UPDATE checks this foreign key of the table: when it
        sets a child column - or always, if it refers to the table itself."""
        return ascii_lower(link.key.parent) == ascii_lower(link.child.name) or self.child_changed(link, changed)

    @staticmethod
    def child_changed(link: Link, changed: set[int] | None) -> bool:
        if changed is None:
            return True
        table, width = link.child, len(link.child.columns)
        for name in link.key.columns:
            position = table.column_index(name)
            if position in changed or (position == table.rowid_column and width in changed):
                return True
        return False

    @staticmethod
    def parent_changed(link: Link, table: TableInfo, changed: set[int] | None) -> bool:
        """Whether an UPDATE sets a column of the parent key (SQLite's
        fkParentIsModified: by name, or the PRIMARY KEY without names)."""
        if changed is None:
            return True
        width = len(table.columns)
        if link.key.parent_columns:
            positions = [table.column_index(n) for n in link.key.parent_columns]
        elif table.primary_key is not None:
            positions = [table.column_index(c.name) for c in table.primary_key.columns]
        else:
            positions = []
        for position in positions:
            if position in changed or (position is not None and position == table.rowid_column and width in changed):
                return True
        return False

    def _usable(self, link: Link) -> bool:
        try:
            link.locate(self.executor.catalog.tables)
        except (OperationalError, Mismatch):
            return False  # (reported by prepare(); ignored while a table is dropped)
        return True

    # ---- the parent of a child row ----------------------------------------------------

    def parent_exists(self, link: Link, row: list) -> bool | None:
        """Whether the parent row of child ``row`` exists (None: a NULL in the key)."""
        values_ = [row[p] for p in link.child_positions]
        if any(v is None for v in values_):
            return None
        executor, parent = self.executor, link.parent
        catalog = executor.catalog
        if link.index is None:
            rowid = values.numeric_affinity(values_[0])  # (SQLite's OP_MustBeInt)
            if type(rowid) is float and -2**63 <= rowid < 2**63 and rowid == int(rowid):
                rowid = int(rowid)
            return type(rowid) is int and rowid in catalog.table_tree(parent)
        index = link.index
        converted = [values.apply_affinity(v, parent.affinities[p]) for v, p in zip(values_, index.positions)]
        prefix = index.prefix(converted)
        return next(iter(catalog.index_tree(index).scan(prefix, prefix + (HIGH,))), None) is not None

    def check_parent(self, link: Link, row: list, increment: int, own: list | None = None) -> None:
        """Count a violation (``increment`` +1: a child row added; -1: one
        removed) if the parent of ``row`` is missing.  ``own``: the row being
        inserted or updated, which may be its own parent."""
        if increment < 0 and self.zero(link):
            return
        if increment > 0 and link is self.unchecked:
            return  # (see Executor.set_null_link)
        if increment > 0 and own is not None and link.parent is link.child:
            key = [row[p] for p in link.child_positions]
            parent_key = [own[-1] if link.index is None else own[p] for p in link.parent_positions]
            if link.index is None:
                key = [values.numeric_affinity(key[0])]
            if None not in key and all(values.compare(a, b) == 0 for a, b in zip(key, parent_key)):
                return  # (it refers to itself)
        if self.parent_exists(link, row) is False:
            self.count(link, increment)

    # ---- the children of a parent row -------------------------------------------------

    def matching_children(self, link: Link, parent_row: list, action: bool = False) -> list[tuple[int, list]]:
        """(rowid, row) of the child rows whose key equals the parent key of
        ``parent_row`` (values and row id), compared as SQLite's
        fkScanChildren does: parent column affinity and collation against
        the child column.  An ``action`` compares as SQLite's action
        trigger does, "OLD.parent = child": OLD.parent keeps the parent's
        collation but has no affinity, so the child column's applies."""
        executor = self.executor
        parent, child = link.parent, link.child
        if link.index is None:
            parent_values = [parent_row[-1]]
            affinities, collations = [values.INTEGER], ["BINARY"]
        else:
            parent_values = [parent_row[p] for p in link.parent_positions]
            affinities = [None if action else parent.affinities[p] for p in link.parent_positions]
            collations = [parent.collations[p] for p in link.parent_positions]
        if any(v is None for v in parent_values):
            return []
        from minidb.executor import value_comparator
        tests = [(position, value, value_comparator("=", affinity, child.affinities[position], collation))
                 for position, value, affinity, collation
                 in zip(link.child_positions, parent_values, affinities, collations)]
        catalog = executor.catalog
        lookup = self.child_index_lookup(link, parent_values, affinities, collations)
        if lookup is None:
            candidates = catalog.table_tree(child).scan()
        else:
            index, prefix = lookup
            tree = catalog.table_tree(child)
            rowids = sorted(key[-1][1] for key, _ in catalog.index_tree(index).scan(prefix, prefix + (HIGH,)))
            candidates = ((rowid, tree.get(rowid)) for rowid in rowids)
        found = []
        for rowid, record in candidates:
            row = executor.load_row(child, rowid, record)
            if all(equal(value, row[position]) == 1 for position, value, equal in tests):
                found.append((rowid, row))
        return found

    @staticmethod
    def child_index_lookup(link: Link, parent_values: list, affinities: list,
                           collations: list) -> tuple[IndexInfo, tuple] | None:
        """(index, key prefix) of a child index that finds the candidate
        child rows: one starting with the key's columns, under the
        comparison's collation, whose stored values the comparison's
        affinity leaves alone (SQLite's sqlite3IndexAffinityOk).  None: scan."""
        child = link.child
        wanted = dict(zip(link.child_positions, zip(parent_values, affinities, collations)))
        count = len(wanted)
        for index in child.indexes:
            if set(index.positions[:count]) != set(wanted):
                continue
            probe = []
            for position, collation in zip(index.positions[:count], index.collation_names):
                value, affinity, compared_by = wanted[position]
                stored = child.affinities[position]
                affinity = values.comparison_affinity(affinity, stored)
                if ascii_lower(collation) != ascii_lower(compared_by or "BINARY"):
                    break
                if affinity in values.NUMERIC_AFFINITIES:
                    if stored not in values.NUMERIC_AFFINITIES:
                        break
                    value = values.numeric_affinity(value)
                elif affinity == values.TEXT:
                    if stored != values.TEXT:
                        break
                    value = values.text_affinity(value)
                probe.append(value)
            else:
                return index, index.prefix(probe)
        return None

    def scan_children(self, link: Link, parent_row: list, increment: int, own_rowid: int | None = None) -> None:
        """Count the child rows referring to ``parent_row``: +1 each when the
        parent goes away, -1 each when it arrives (only while violations
        are outstanding).  ``own_rowid``: in a self-referencing table, the
        row being removed does not count."""
        if increment < 0 and self.zero(link):
            return
        for rowid, _ in self.matching_children(link, parent_row):
            if increment > 0 and link.child is link.parent and rowid == own_rowid:
                continue
            self.count(link, increment)

    # ---- the steps of a change ------------------------------------------------------

    def row_inserted(self, table: TableInfo, row: list, single: bool = False) -> None:
        """Before ``row`` (values and row id) is stored (``single``: by a one-row INSERT)."""
        for link in self.children_of(table):
            if self._usable(link):
                self.check_parent(link, row, 1, row)
        for link in self.parents_of(table):
            if single and not link.deferred and not self.defer_all():
                continue  # (SQLite: one inserted row cannot fix an immediate violation)
            if self._usable(link):
                self.scan_children(link, row, -1)

    def row_removing(self, table: TableInfo, row: list, changed: set[int] | None = None) -> None:
        """Before ``row`` is deleted (or replaced by an UPDATE setting ``changed``)."""
        for link in self.children_of(table):
            if self.processed(link, changed) and self._usable(link):
                self.check_parent(link, row, -1)
        for link in self.parents_of(table):
            if self.parent_changed(link, table, changed) and self._usable(link):
                self.scan_children(link, row, 1, row[-1])

    def row_adding(self, table: TableInfo, row: list, changed: set[int]) -> None:
        """Before the new version of an updated row is stored."""
        for link in self.children_of(table):
            if self.processed(link, changed) and self._usable(link):
                self.check_parent(link, row, 1, row)
        for link in self.parents_of(table):
            if self.parent_changed(link, table, changed) and self._usable(link):
                self.scan_children(link, row, -1)

    def actions(self, table: TableInfo, old: list, new: list | None = None, changed: set[int] | None = None) -> None:
        """ON DELETE (``new`` None) or ON UPDATE actions after a parent row
        was removed or changed."""
        for link in self.parents_of(table):
            action = link.key.on_delete if new is None else link.key.on_update
            if action == "RESTRICT" and self.defer_all():
                continue  # (as SQLite: PRAGMA defer_foreign_keys turns RESTRICT into NO ACTION)
            if action == "NO ACTION" or not self._usable(link):
                continue
            if new is not None:
                if not self.parent_changed(link, table, changed):
                    continue
                old_key = [old[-1] if link.index is None else old[p] for p in link.parent_positions]
                new_key = [new[-1] if link.index is None else new[p] for p in link.parent_positions]
                collations = (["BINARY"] if link.index is None
                              else [table.collations[p] for p in link.parent_positions])
                if all(values.collation_compare(c)(a, b) == 0 if a is not None and b is not None else a is b
                       for a, b, c in zip(old_key, new_key, collations)):
                    continue  # (SQLite's action trigger runs WHEN the key IS NOT what it was, by its collation)
            children = self.matching_children(link, old, action=True)
            if not children:
                continue
            if action == "RESTRICT":
                raise IntegrityError("FOREIGN KEY constraint failed")
            self.executor.frame_depth += 1
            try:
                if self.executor.frame_depth > MAX_ACTION_DEPTH:
                    raise OperationalError("too many levels of trigger recursion")
                # (SQLite counts an action's changes in total_changes() once it
                # completes, even if the statement fails afterwards)
                changes = self._act(link, action, children, new)  # (nested actions count first)
                self.extra_changes += changes
            finally:
                self.executor.frame_depth -= 1

    def _act(self, link: Link, action: str, children: list[tuple[int, list]], new: list | None) -> int:
        """Run an action on the child rows; returns how many it changed."""
        executor = self.executor
        changes = 0
        child = link.child
        tree = executor.catalog.table_tree(child)
        width = len(child.columns)
        for rowid, row in children:
            if rowid not in tree:
                continue  # (an earlier action removed it)
            row = executor.load_row(child, rowid, tree.get(rowid))
            if action == "CASCADE" and new is None:
                if executor.delete_row(child, tree, rowid, orconf="ABORT") is not None:
                    changes += 1
                continue
            updated = list(row)
            changed = set()
            for i, position in enumerate(link.child_positions):
                if action == "SET NULL":
                    value = None
                elif action == "SET DEFAULT":
                    default = child.columns[position].default
                    value = None if default is None else executor.constant(default)
                else:  # ON UPDATE CASCADE
                    value = new[-1] if link.index is None else new[link.parent_positions[i]]
                updated[position] = value
                changed.add(width if position == child.rowid_column else position)
            if executor.update_row(child, tree, rowid, row, updated, "ABORT", changed) is not None:
                changes += 1
        return changes
