"""B-trees in SQLite's file format.

A table is a B+ tree keyed by row id: leaves hold (row id, record) cells,
interior pages hold (left child, row id) cells meaning "the child holds row
ids <= this one", and a right-most child pointer.  An index is a B-tree of
records (the indexed values, then the row id): interior cells are entries
themselves, with the entries of their left child before them.

Both keep pages within size by SQLite's way of balancing: a page that is
too full or too empty is redistributed together with up to two siblings
(and the parent's dividers between them) over as many pages as needed, and
the parent is fixed the same way if that made it too full or too empty.
The root keeps its page number: when it overflows its content moves to a
new child first; when it is left with one child that fits, the child moves
back up.

``SqliteTable`` and ``SqliteIndex`` give these trees the interface of
``minidb.btree.BTree`` (row id -> MiniDB record, index key -> b""), so the
catalog and executor use them unchanged; values are converted between
MiniDB's and SQLite's record formats on the way.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from minidb import record as minidb_record
from minidb import values
from minidb.btree import DuplicateKeyError
from minidb.sqlite_format import (
    INDEX_INTERIOR, INDEX_LEAF, TABLE_INTERIOR, TABLE_LEAF, BtreePage, Cell, OverflowPage, corrupt,
    decode_record, encode_record,
)

_HEADER = {TABLE_LEAF: 8, INDEX_LEAF: 8, TABLE_INTERIOR: 12, INDEX_INTERIOR: 12}


class _Tree:
    """What table and index trees share: pages, payloads, balancing."""

    table = True  # table (B+ tree) or index (B-tree)

    def __init__(self, pager: Any, root: int) -> None:
        self.pager = pager
        self.root = root

    @classmethod
    def create(cls, pager: Any) -> int:
        """A new empty tree; returns its root page."""
        return pager.allocate_root(TABLE_LEAF if cls.table else INDEX_LEAF).pgno

    def page(self, pgno: int) -> BtreePage:
        return self.pager.get(pgno, BtreePage)

    # ---- payloads -----------------------------------------------------------

    def _cell(self, payload: bytes, rowid: int = 0) -> Cell:
        """A new leaf cell for ``payload`` (writing its overflow pages)."""
        size = len(payload)
        geometry = self.pager.geometry
        local = geometry.local_size(size, self.table)
        cell = Cell(rowid=rowid, local=payload[:local], size=size)
        if local < size:
            rest = payload[local:]
            step = geometry.overflow_data
            chunks = [rest[i:i + step] for i in range(0, len(rest), step)]
            pages = [self.pager.allocate(OverflowPage) for _ in chunks]
            for page, chunk, following in zip(pages, chunks, pages[1:] + [None]):
                page.data = chunk
                page.next_page = following.pgno if following is not None else 0
            cell.overflow = pages[0].pgno
        return cell

    def payload(self, cell: Cell) -> bytes:
        if not cell.overflow:
            return cell.local
        parts, pgno, remaining = [cell.local], cell.overflow, cell.size - len(cell.local)
        while remaining > 0:
            if not pgno:
                raise corrupt("overflow chain too short")
            page = self.pager.get(pgno, OverflowPage)
            parts.append(page.data[:remaining])
            remaining -= len(parts[-1])
            pgno = page.next_page
        return b"".join(parts)

    def _free_chain(self, cell: Cell) -> None:
        pgno = cell.overflow
        while pgno:
            following = self.pager.get(pgno, OverflowPage).next_page
            self.pager.free(pgno)
            pgno = following

    # ---- balancing -------------------------------------------------------------

    def _fix(self, path: list[tuple[BtreePage, int]], page: BtreePage) -> None:
        """Bring ``page`` (reached through ``path``) and then its ancestors
        back within size limits."""
        while True:
            used = page.used()
            if page.pgno == self.root:
                if used > page.capacity:
                    child = self._move_root_down(page)
                    path, page = [(page, 0)], child
                    continue
                if not page.is_leaf and not page.cells:
                    self._move_root_up(page)
                return
            if page.capacity // 3 <= used <= page.capacity and page.cells:
                return
            parent, index = path.pop()
            self._balance(parent, index)
            page = parent

    def _move_root_down(self, root: BtreePage) -> BtreePage:
        child = self.pager.allocate(BtreePage, root.kind)
        child.cells, child.right = root.cells, root.right
        self.pager.write(root)
        root.kind = TABLE_INTERIOR if self.table else INDEX_INTERIOR
        root.cells, root.right = [], child.pgno
        return child

    def _move_root_up(self, root: BtreePage) -> None:
        child = self.page(root.right)
        if sum(c.byte_size(child.kind) for c in child.cells) > root.capacity - (_HEADER[child.kind] - root.header_size):
            return  # (the root on page 1 has less room)
        self.pager.write(root)
        root.kind, root.cells, root.right = child.kind, child.cells, child.right
        self.pager.free(child.pgno)
        if not root.is_leaf and not root.cells:
            self._move_root_up(root)

    def _balance(self, parent: BtreePage, index: int) -> None:
        """Redistribute the child ``index`` of ``parent`` with up to two
        siblings over as many pages as their cells need."""
        count = len(parent.cells) + 1
        low = max(0, min(index - 1, count - 3))
        high = min(count - 1, low + 2)

        def child(i: int) -> int:
            return parent.cells[i].child if i < len(parent.cells) else parent.right

        pages = [self.page(child(i)) for i in range(low, high + 1)]
        kind = pages[0].kind
        if any(page.kind != kind for page in pages):
            raise corrupt(f"sibling pages of different kinds under page {parent.pgno}")
        cells = []
        for k, page in enumerate(pages):
            cells.extend(page.cells)
            if k < len(pages) - 1 and kind != TABLE_LEAF:
                divider = parent.cells[low + k].copy()  # comes down between the two pages
                divider.child = page.right
                cells.append(divider)
        groups, dividers = self._distribute(cells, kind)
        last_right = pages[-1].right  # (before the pages are reused below)
        new_pages = []
        for g, group in enumerate(groups):
            if g < len(pages):
                page = pages[g]
                self.pager.write(page)
            else:
                page = self.pager.allocate(BtreePage, kind)
            page.cells = group
            if kind in (TABLE_INTERIOR, INDEX_INTERIOR):
                page.right = dividers[g].child if g < len(dividers) else last_right
            new_pages.append(page)
        for page in pages[len(groups):]:
            self.pager.free(page.pgno)
        up = []
        for g, divider in enumerate(dividers):
            if kind == TABLE_LEAF:
                divider = Cell(child=new_pages[g].pgno, rowid=groups[g][-1].rowid)
            else:
                divider = divider.copy()
                divider.child = new_pages[g].pgno
            up.append(divider)
        self.pager.write(parent)
        rest = parent.cells[high:]
        if rest:
            rest[0] = rest[0].copy()
            rest[0].child = new_pages[-1].pgno
        else:
            parent.right = new_pages[-1].pgno
        parent.cells = parent.cells[:low] + up + rest

    def _distribute(self, cells: list[Cell], kind: int) -> tuple[list[list[Cell]], list[Cell]]:
        """Split ``cells`` into page-sized groups, as evenly as they allow.
        Except on table leaves, one cell between two groups becomes their
        divider in the parent; returns (groups, dividers)."""
        capacity = self.pager.geometry.usable - _HEADER[kind]
        sizes = [cell.byte_size(kind) for cell in cells]
        moves_up = kind != TABLE_LEAF

        def split(limit: Callable[[int, int], bool]) -> tuple[list[list[Cell]], list[Cell]] | None:
            groups, dividers, group, used = [], [], [], 0
            for cell, size in zip(cells, sizes):
                if group and (used + size > capacity or limit(len(groups), used)):
                    groups.append(group)
                    if moves_up:
                        dividers.append(cell)
                        group, used = [], 0
                        continue
                    group, used = [], 0
                group.append(cell)
                used += size
            if not group:  # the last cell became a divider: give the group one back
                if len(groups[-1]) < 2:
                    return None
                group = [dividers.pop()]
                dividers.append(groups[-1].pop())
            groups.append(group)
            if not moves_up:
                dividers = [None] * (len(groups) - 1)
            return groups, dividers

        greedy = split(lambda g, used: False)
        count = len(greedy[0])
        if count == 1:
            return greedy
        total = sum(sizes)
        target = total / count
        even = split(lambda g, used: used >= target)
        if even is not None and len(even[0]) == count and all(
                sum(c.byte_size(kind) for c in group) <= capacity for group in even[0]):
            return even
        return greedy

    # ---- building a tree bottom up ---------------------------------------------------

    @classmethod
    def build(cls, pager: Any, entries: Iterable[tuple[int, bytes]]) -> int:
        """A new tree holding ``entries`` - (row id, payload) for a table,
        (0, payload) for an index - in that order, which must be the tree's
        order (it is not checked: VACUUM copies trees as they are).  Pages
        are filled up, as SQLite's VACUUM leaves them; returns the root."""
        tree = cls(pager, 0)
        leaf_kind = TABLE_LEAF if cls.table else INDEX_LEAF
        level, dividers, page, used = [], [], [], 0
        capacity = pager.geometry.usable - _HEADER[leaf_kind]
        for rowid, payload in entries:
            cell = tree._cell(payload, rowid)
            size = cell.byte_size(leaf_kind)
            if page and used + size > capacity:
                level.append(page)
                if not cls.table:  # this entry goes up between the two leaves
                    dividers.append(cell)
                    page, used = [], 0
                    continue
                page, used = [], 0
            page.append(cell)
            used += size
        if not page and level:
            # The last entry went up: it comes back down as the last leaf, and
            # the previous leaf sends its own last entry up in its place.
            page = [dividers.pop()]
            dividers.append(level[-1].pop())
        level.append(page)
        children = [tree._new_page(leaf_kind, cells, 0) for cells in level]
        if cls.table:
            dividers = [Cell(child=child.pgno, rowid=child.cells[-1].rowid) for child in children[:-1]]
        while len(children) > 1:
            children, dividers = tree._build_level(children, dividers)
        return children[0].pgno

    def _new_page(self, kind: int, cells: list[Cell], right: int) -> BtreePage:
        page = self.pager.allocate(BtreePage, kind)
        page.cells, page.right = cells, right
        return page

    def _build_level(self, children: list[BtreePage], dividers: list[Cell]) -> tuple[list[BtreePage], list[Cell]]:
        """The interior pages above ``children`` (``dividers[i]`` lies
        between children i and i + 1), and the dividers between them."""
        kind = TABLE_INTERIOR if self.table else INDEX_INTERIOR
        capacity = self.pager.geometry.usable - _HEADER[kind]
        pages, up, cells, used = [], [], [], 0
        for i, divider in enumerate(dividers):
            divider = divider.copy()
            divider.child = children[i].pgno
            size = divider.byte_size(kind)
            if cells and used + size > capacity:
                # children[i] becomes this page's right child; the divider goes up
                pages.append(self._new_page(kind, cells, children[i].pgno))
                divider.child = pages[-1].pgno
                up.append(divider)
                cells, used = [], 0
                continue
            cells.append(divider)
            used += size
        if not cells and pages:
            # The last divider went up: it comes back as this page's only
            # cell, and the previous page sends its own last cell up instead.
            previous = pages[-1]
            divider = up.pop()
            divider.child = previous.right
            last = previous.cells.pop()
            previous.right = last.child
            last.child = previous.pgno
            up.append(last)
            cells = [divider]
        pages.append(self._new_page(kind, cells, children[-1].pgno))
        if self.table:
            up = [Cell(child=page.pgno, rowid=self._last_rowid(page)) for page in pages[:-1]]
        return pages, up

    def _last_rowid(self, page: BtreePage) -> int:
        while not page.is_leaf:
            page = self.page(page.right)
        return page.cells[-1].rowid

    # ---- whole-tree operations ---------------------------------------------------

    def cells(self, pgno: int | None = None) -> Iterator[Cell]:
        """Every cell of the tree in key order (interior cells of an index
        between the subtrees on either side)."""
        page = self.page(self.root if pgno is None else pgno)
        for cell in page.cells:
            if not page.is_leaf:
                yield from self.cells(cell.child)
                if self.table:
                    continue
            yield cell
        if not page.is_leaf:
            yield from self.cells(page.right)

    def adopt(self, root: int) -> None:
        """Move the tree rooted at ``root`` (just built) to this tree's root page."""
        built = self.page(root)
        page = self.page(self.root)
        self.pager.write(page)
        page.kind, page.cells, page.right = built.kind, built.cells, built.right
        self.pager.free(root)

    def _pages(self) -> Iterator[BtreePage]:
        stack = [self.root]
        while stack:
            page = self.page(stack.pop())
            yield page
            if not page.is_leaf:
                stack.extend(cell.child for cell in page.cells)
                stack.append(page.right)

    def destroy(self) -> None:
        """Free every page of the tree, including the root."""
        for page in list(self._pages()):
            for cell in page.cells:
                self._free_chain(cell)
            self.pager.free(page.pgno)

    def clear(self) -> None:
        """Remove every entry, leaving an empty root leaf."""
        root = self.page(self.root)
        for page in list(self._pages()):
            for cell in page.cells:
                self._free_chain(cell)
            if page.pgno != self.root:
                self.pager.free(page.pgno)
        self.pager.write(root)
        root.kind = TABLE_LEAF if self.table else INDEX_LEAF
        root.cells, root.right = [], 0

    def depth(self) -> int:
        depth, page = 1, self.page(self.root)
        while not page.is_leaf:
            depth += 1
            page = self.page(page.cells[0].child if page.cells else page.right)
        return depth

    def estimated_count(self) -> int:
        estimate, page = 1, self.page(self.root)
        while not page.is_leaf:
            estimate *= len(page.cells) + 1
            page = self.page(page.cells[0].child if page.cells else page.right)
        return max(1, estimate * len(page.cells))


class TableTree(_Tree):
    """A table: row id -> record (SQLite's format)."""

    table = True

    def _find(self, rowid: int) -> tuple[list[tuple[BtreePage, int]], BtreePage, int]:
        """(path, leaf, position of the first cell with a row id >= rowid)"""
        path, page = [], self.page(self.root)
        while not page.is_leaf:
            cells = page.cells
            i = bisect_left(cells, rowid, key=_rowid)
            path.append((page, i))
            page = self.page(cells[i].child if i < len(cells) else page.right)
        return path, page, bisect_left(page.cells, rowid, key=_rowid)

    def get(self, rowid: int) -> bytes | None:
        _, leaf, i = self._find(rowid)
        if i < len(leaf.cells) and leaf.cells[i].rowid == rowid:
            return self.payload(leaf.cells[i])
        return None

    def insert(self, rowid: int, payload: bytes, replace: bool = False) -> None:
        path, leaf, i = self._find(rowid)
        cells = leaf.cells
        if i < len(cells) and cells[i].rowid == rowid:
            if not replace:
                raise DuplicateKeyError(rowid)
            self._free_chain(cells[i])
            self.pager.write(leaf)
            cells[i] = self._cell(payload, rowid)
        else:
            self.pager.write(leaf)
            cells.insert(i, self._cell(payload, rowid))
            if i == len(cells) - 1 and leaf.pgno != self.root and all(j == len(p.cells) for p, j in path) \
                    and leaf.used() > leaf.capacity:
                self._append(path, leaf)
                return
        self._fix(path, leaf)

    def _append(self, path: list[tuple[BtreePage, int]], leaf: BtreePage) -> None:
        """A new largest row id overflowed the right-most leaf: start a new
        leaf with just that cell (SQLite's balance_quick), so that rows
        appended in order fill their pages."""
        parent, _ = path[-1]
        new = self.pager.allocate(BtreePage, TABLE_LEAF)
        new.cells = [leaf.cells.pop()]
        self.pager.write(parent)
        parent.cells.append(Cell(child=leaf.pgno, rowid=leaf.cells[-1].rowid))
        parent.right = new.pgno
        path.pop()
        self._fix(path, parent)

    def delete(self, rowid: int) -> bool:
        path, leaf, i = self._find(rowid)
        if i == len(leaf.cells) or leaf.cells[i].rowid != rowid:
            return False
        self.pager.write(leaf)
        self._free_chain(leaf.cells.pop(i))
        self._fix(path, leaf)
        return True

    def scan(self, start: int | None = None, end: int | None = None, start_inclusive: bool = True,
             end_inclusive: bool = True) -> Iterator[tuple[int, Cell]]:
        """(row id, cell) in order within the bounds."""
        if start is None:
            stack, leaf, i = self._leftmost(), None, 0
            leaf = stack.pop()
        else:
            path, leaf, i = self._find(start)
            if not start_inclusive and i < len(leaf.cells) and leaf.cells[i].rowid == start:
                i += 1
            stack = [(page, j + 1) for page, j in path]
        while True:
            for cell in leaf.cells[i:]:
                rowid = cell.rowid
                if end is not None and (rowid > end or (rowid == end and not end_inclusive)):
                    return
                yield rowid, cell
            leaf = self._next_leaf(stack)
            if leaf is None:
                return
            i = 0

    def _leftmost(self) -> list:
        stack, page = [], self.page(self.root)
        while not page.is_leaf:
            stack.append((page, 1))
            page = self.page(page.cells[0].child if page.cells else page.right)
        stack.append(page)
        return stack

    def _next_leaf(self, stack: list[tuple[BtreePage, int]]) -> BtreePage | None:
        """The leaf after the current one; ``stack`` holds, per level, the
        interior page and the index of the next child to visit."""
        while stack:
            page, j = stack.pop()
            if j <= len(page.cells):
                stack.append((page, j + 1))
                child = self.page(page.cells[j].child if j < len(page.cells) else page.right)
                while not child.is_leaf:
                    stack.append((child, 1))
                    child = self.page(child.cells[0].child if child.cells else child.right)
                return child
        return None

    def last_rowid(self) -> int | None:
        page = self.page(self.root)
        while not page.is_leaf:
            page = self.page(page.right)
        return page.cells[-1].rowid if page.cells else None

    def count(self) -> int:
        return sum(len(page.cells) for page in self._pages() if page.is_leaf)

    def check(self) -> int:
        """Verify order, separator bounds and equal leaf depth; returns the
        number of rows."""
        depths = set()

        def visit(pgno: int, low: int | None, high: int | None, depth: int) -> int:
            page = self.page(pgno)
            rowids = [cell.rowid for cell in page.cells]
            assert rowids == sorted(set(rowids)), f"page {pgno}: row ids out of order"
            assert all((low is None or r > low) and (high is None or r <= high) for r in rowids), \
                f"page {pgno}: row id outside its parent's bounds"
            assert page.used() <= page.capacity, f"page {pgno} overfull"
            if page.is_leaf:
                assert page.kind == TABLE_LEAF, f"page {pgno}: not a table page"
                depths.add(depth)
                return len(rowids)
            assert page.kind == TABLE_INTERIOR, f"page {pgno}: not a table page"
            total, previous = 0, low
            for cell in page.cells:
                total += visit(cell.child, previous, cell.rowid, depth + 1)
                previous = cell.rowid
            return total + visit(page.right, previous, high, depth + 1)

        total = visit(self.root, None, None, 1)
        assert len(depths) <= 1, "leaves at different depths"
        return total


def _rowid(cell: Cell) -> int:
    return cell.rowid


class IndexTree(_Tree):
    """An index: a B-tree of keys.  A key is the MiniDB index key (the sort
    keys of the values, then of the row id); cells store it as a record.
    ``descending`` marks DESC columns, whose order is reversed on disk."""

    table = False

    def __init__(self, pager: Any, root: int, descending: list[bool] | None = None, real: list[int] | None = None,
                 key_functions: list | None = None) -> None:
        super().__init__(pager, root)
        self.descending = [i for i, d in enumerate(descending or []) if d]
        self.real = real or []  # positions of REAL columns (stored as integers when whole)
        # The columns' sort key functions, for NOCASE / RTRIM columns (see
        # values.collation_sort_key); None: all BINARY.
        self.key_functions = key_functions

    def key(self, cell: Cell) -> tuple:
        if cell.key is None:
            row = decode_record(self.payload(cell))
            for i in self.real:
                if i < len(row) - 1 and type(row[i]) is int:
                    row[i] = float(row[i])
            functions = self.key_functions
            if functions is None:
                cell.key = tuple(values.sort_key(v) for v in row)
            else:
                cell.key = tuple(functions[i](v) if i < len(functions) else values.sort_key(v)
                                 for i, v in enumerate(row))
        return cell.key

    def order(self, key: tuple) -> tuple:
        """The key as it orders on disk."""
        if not self.descending:
            return key
        return tuple(_Descending(part) if i in self.descending else part for i, part in enumerate(key))

    def _position(self, page: BtreePage, key: tuple) -> int:
        order, wanted = self.order, self.order(key)
        return bisect_left(page.cells, wanted, key=lambda cell: order(self.key(cell)))

    def _find(self, key: tuple) -> tuple[list[tuple[BtreePage, int]], BtreePage, int, bool]:
        """(path, page, position, found): the page holding ``key`` (any
        level), or the leaf where it would go."""
        path, page = [], self.page(self.root)
        while True:
            i = self._position(page, key)
            if i < len(page.cells) and self.key(page.cells[i]) == key:
                return path, page, i, True
            if page.is_leaf:
                return path, page, i, False
            path.append((page, i))
            page = self.page(page.cells[i].child if i < len(page.cells) else page.right)

    def __contains__(self, key: tuple) -> bool:
        return self._find(key)[3]

    def insert(self, key: tuple, replace: bool = False) -> None:
        path, page, i, found = self._find(key)
        if found:
            if replace:
                return
            raise DuplicateKeyError(key)
        row = [values.plain_value(part) for part in key]
        cell = self._cell(encode_record(row))
        cell.key = key
        self.pager.write(page)
        page.cells.insert(i, cell)
        self._fix(path, page)

    def delete(self, key: tuple) -> bool:
        path, page, i, found = self._find(key)
        if not found:
            return False
        self.pager.write(page)
        old = page.cells[i]
        if page.is_leaf:
            del page.cells[i]
            self._free_chain(old)
            self._fix(path, page)
            return True
        # An interior entry: its predecessor (the largest key under its left
        # child) takes its place, and that leaf loses a cell.
        leaf_path = path + [(page, i)]
        leaf = self.page(old.child)
        while not leaf.is_leaf:
            leaf_path.append((leaf, len(leaf.cells)))
            leaf = self.page(leaf.right)
        self.pager.write(leaf)
        predecessor = leaf.cells.pop().copy()
        predecessor.child = old.child
        page.cells[i] = predecessor
        self._free_chain(old)
        self._fix(leaf_path, leaf)
        # The interior page may now be too full (a longer key): find it again.
        path, page, _, found = self._find(predecessor.key if predecessor.key is not None else self.key(predecessor))
        if found and page.used() > page.capacity:
            self._fix(path, page)
        return True

    def scan(self, start: tuple | None = None, end: tuple | None = None, start_inclusive: bool = True,
             end_inclusive: bool = True) -> Iterator[tuple]:
        """Keys in disk order within the bounds (which must not involve
        DESC columns unless ``descending`` is empty)."""
        for key in self._walk(self.root, start, start_inclusive):
            if end is not None and (key > end or (key == end and not end_inclusive)):
                return
            yield key

    def _walk(self, pgno: int, start: tuple | None, inclusive: bool) -> Iterator[tuple]:
        page = self.page(pgno)
        cells = page.cells
        if start is None:
            i = 0
        else:
            i = self._position(page, start)
        leaf = page.is_leaf
        for j in range(i, len(cells)):
            if not leaf:
                yield from self._walk(cells[j].child, start if j == i else None, inclusive)
            key = self.key(cells[j])
            if j > i or start is None or inclusive or key != start:
                yield key
        if not leaf:
            yield from self._walk(page.right, start if i == len(cells) else None, inclusive)

    def last_key(self) -> tuple | None:
        page = self.page(self.root)
        while not page.is_leaf:
            page = self.page(page.right)
        return self.key(page.cells[-1]) if page.cells else None

    def count(self) -> int:
        return sum(len(page.cells) for page in self._pages())

    def check(self) -> int:
        keys = list(self._walk(self.root, None, True))
        ordered = [self.order(k) for k in keys]
        assert all(a < b for a, b in zip(ordered, ordered[1:])), "index keys out of order"
        depths = set()

        def visit(pgno: int, depth: int) -> None:
            page = self.page(pgno)
            assert page.used() <= page.capacity, f"page {pgno} overfull"
            if page.is_leaf:
                assert page.kind == INDEX_LEAF, f"page {pgno}: not an index page"
                depths.add(depth)
                return
            assert page.kind == INDEX_INTERIOR, f"page {pgno}: not an index page"
            for cell in page.cells:
                visit(cell.child, depth + 1)
            visit(page.right, depth + 1)

        visit(self.root, 1)
        assert len(depths) <= 1, "leaves at different depths"
        return len(keys)


class _Descending:
    """Reverses the order of one key part (a DESC index column)."""

    __slots__ = ("part",)

    def __init__(self, part: tuple) -> None:
        self.part = part

    def __lt__(self, other: _Descending) -> bool:
        return self.part > other.part

    def __gt__(self, other: _Descending) -> bool:
        return self.part < other.part

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Descending) and self.part == other.part

    def __le__(self, other: _Descending) -> bool:
        return self.part >= other.part

    def __ge__(self, other: _Descending) -> bool:
        return self.part <= other.part

    def __hash__(self) -> int:
        return hash(self.part)


# ---- the BTree interface -------------------------------------------------------------


class SqliteTable:
    """A table tree with the interface of ``minidb.btree.BTree``: row id ->
    MiniDB record, or with ``rows`` the decoded row itself (a new list each
    time; Executor.load_row takes either), which saves re-encoding every row
    read.  Values to store may be either too.  ``real`` lists the REAL
    columns (SQLite stores whole REAL values as integers; they read back as
    REAL)."""

    def __init__(self, pager: Any, root: int, affinities: list[str] | None = None,
                 on_change: Callable[[], None] | None = None, rows: bool = False) -> None:
        self.tree = TableTree(pager, root)
        self.root = root
        self.real = [i for i, a in enumerate(affinities or []) if a == values.REAL]
        self.on_change = on_change
        self.rows = rows

    def _record(self, cell: Cell) -> bytes | list:
        row = decode_record(self.tree.payload(cell))
        for i in self.real:
            if i < len(row) and type(row[i]) is int:
                row[i] = float(row[i])
        return row if self.rows else minidb_record.encode_record(row)

    @staticmethod
    def _payload(value: bytes | list) -> bytes:
        return encode_record(value if type(value) is list else minidb_record.decode_record(value)[0])

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def get(self, key: int, default: bytes | None = None) -> bytes | list | None:
        _, leaf, i = self.tree._find(key)
        if i < len(leaf.cells) and leaf.cells[i].rowid == key:
            return self._record(leaf.cells[i])
        return default

    def __contains__(self, key: int) -> bool:
        _, leaf, i = self.tree._find(key)
        return i < len(leaf.cells) and leaf.cells[i].rowid == key

    def insert(self, key: int, value: bytes, replace: bool = False) -> None:
        self._changed()
        self.tree.insert(key, self._payload(value), replace)

    def delete(self, key: int) -> bool:
        self._changed()
        return self.tree.delete(key)

    def scan(self, start: int | None = None, end: int | None = None, start_inclusive: bool = True,
             end_inclusive: bool = True) -> Iterator[tuple[int, bytes | list]]:
        for rowid, cell in self.tree.scan(start, end, start_inclusive, end_inclusive):
            yield rowid, self._record(cell)

    def keys(self) -> list:
        return [rowid for rowid, _ in self.tree.scan()]

    def __len__(self) -> int:
        return self.tree.count()

    def last_key(self) -> int | None:
        return self.tree.last_rowid()

    def bulk_load(self, items: Iterable[tuple[int, bytes]]) -> None:
        """Fill the (empty) tree with items in row id order."""
        self._changed()
        rows = ((key, self._payload(value)) for key, value in items)
        self.tree.clear()
        self.tree.adopt(TableTree.build(self.tree.pager, rows))

    def destroy(self) -> None:
        self._changed()
        self.tree.destroy()

    def clear(self) -> None:
        self._changed()
        self.tree.clear()

    def estimated_count(self) -> int:
        return self.tree.estimated_count()

    def depth(self) -> int:
        return self.tree.depth()

    def check(self) -> int:
        return self.tree.check()

    def dump(self, max_keys: int = 8) -> list[str]:
        return _dump(self.tree, max_keys, lambda cell: cell.rowid)


class SqliteIndex:
    """An index tree with the interface of ``minidb.btree.BTree``: key -> b"".

    With DESC columns the disk order is not the order of MiniDB's keys:
    range scans then read the whole index and sort (the planner does not use
    such indexes for lookups; they are only kept up to date)."""

    def __init__(self, pager: Any, root: int, descending: list[bool] | None = None,
                 affinities: list[str] | None = None, key_functions: list | None = None) -> None:
        real = [i for i, a in enumerate(affinities or []) if a == values.REAL]
        self.tree = IndexTree(pager, root, descending, real, key_functions)
        self.root = root

    def get(self, key: tuple, default: bytes | None = None) -> bytes | None:
        return b"" if key in self.tree else default

    def __contains__(self, key: tuple) -> bool:
        return key in self.tree

    def insert(self, key: tuple, value: bytes = b"", replace: bool = False) -> None:
        self.tree.insert(key, replace)

    def delete(self, key: tuple) -> bool:
        return self.tree.delete(key)

    def scan(self, start: tuple | None = None, end: tuple | None = None, start_inclusive: bool = True,
             end_inclusive: bool = True) -> Iterator[tuple[tuple, bytes]]:
        if self.tree.descending:
            keys = sorted(self.tree.scan())
            low = 0 if start is None else (bisect_left if start_inclusive else bisect_right)(keys, start)
            for key in keys[low:]:
                if end is not None and (key > end or (key == end and not end_inclusive)):
                    return
                yield key, b""
            return
        for key in self.tree.scan(start, end, start_inclusive, end_inclusive):
            yield key, b""

    def keys(self) -> list:
        return sorted(self.tree.scan()) if self.tree.descending else list(self.tree.scan())

    def __len__(self) -> int:
        return self.tree.count()

    def last_key(self) -> tuple | None:
        return self.keys()[-1] if self.tree.descending and len(self) else self.tree.last_key()

    def bulk_load(self, items: Iterable[tuple[tuple, bytes]]) -> None:
        """Fill the (empty) tree with the keys of ``items`` (in key order)."""
        keys = [key for key, _ in items]
        if self.tree.descending:
            keys.sort(key=self.tree.order)
        records = ((0, encode_record([values.plain_value(part) for part in key])) for key in keys)
        self.tree.clear()
        self.tree.adopt(IndexTree.build(self.tree.pager, records))

    def destroy(self) -> None:
        self.tree.destroy()

    def clear(self) -> None:
        self.tree.clear()

    def estimated_count(self) -> int:
        return self.tree.estimated_count()

    def depth(self) -> int:
        return self.tree.depth()

    def check(self) -> int:
        return self.tree.check()

    def dump(self, max_keys: int = 8) -> list[str]:
        return _dump(self.tree, max_keys, lambda cell: [values.plain_value(p) for p in self.tree.key(cell)])


def _dump(tree: _Tree, max_keys: int, show: Callable[[Cell], object]) -> list[str]:
    lines = []

    def visit(pgno: int, indent: int) -> None:
        page = tree.page(pgno)
        keys = [show(cell) for cell in page.cells]
        text = ", ".join(repr(k) for k in keys[:max_keys])
        if len(keys) > max_keys:
            text += f", ... ({len(keys)} keys)"
        kind = "leaf" if page.is_leaf else "internal"
        lines.append(f"{'  ' * indent}- {kind} (page {pgno}, {len(keys)} keys): {text}")
        if not page.is_leaf:
            for cell in page.cells:
                visit(cell.child, indent + 1)
            visit(page.right, indent + 1)

    visit(tree.root, 0)
    return lines
