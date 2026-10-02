"""Pages of a database in SQLite's own file format.

``SqlitePager`` has the interface of ``minidb.pager.Pager`` but stores what
SQLite would, so the sqlite3 library and MiniDB can use the same file - one
after the other, and (on POSIX) even at the same time:

* SQLite's locks (unix VFS, rollback-journal mode) on the database file:
  SHARED while reading (a read lock on a 510-byte range, taken while
  holding a read lock on the PENDING byte, so a waiting writer keeps new
  readers out), RESERVED (one writer), PENDING then EXCLUSIVE (a write lock
  on the whole SHARED range: no readers) while the file is written.  As
  with MiniDB's own locks, a process opens the file once (``_LockFile``)
  and arbitrates between its connections itself.
* SQLite's rollback journal ``<db>-journal``: before a commit changes the
  file, the original content of every page it changes (that existed) goes
  to the journal - SQLite's header (magic, page count, checksum seed,
  original size, sector and page size) and records (page number, page,
  checksum of every 200th byte) - and is fsynced; then the pages are
  written and fsynced, and the journal is deleted.  A journal left by a
  crash ("hot": present, valid, and no one holding RESERVED) is played back
  by whoever opens the database next - MiniDB or SQLite.
* pages: of the size the header says (512-65536 bytes; 4096 for a new
  database unless PRAGMA page_size chose another), numbered from 1; page 1
  starts with the database header (kept as page 0 of the cache, as
  MiniDB's own header is); free pages on SQLite's freelist of trunk and
  leaf pages.

* auto_vacuum (FULL or INCREMENTAL): pointer map pages say what each page
  is and which page points to it; root pages stay at the front of the
  file; a FULL database moves pages from the end into free pages and
  shrinks at every commit, an INCREMENTAL one on PRAGMA incremental_vacuum.

Not supported (refused when opening): WAL mode, UTF-16, reserved bytes at
the end of pages.  Large transactions keep every changed page in memory (no
spilling, unlike MiniDB's own format).
"""

from __future__ import annotations

import io
import os
import struct
from typing import Any

from minidb.errors import DatabaseError
from minidb.locking import LockTimeout, _LockFile, fsync_directory
from minidb.pager import PageCache
from minidb.sqlite_format import (
    DEFAULT_PAGE_SIZE, HEADER_SIZE, PENDING_BYTE, PTRMAP_BTREE, PTRMAP_FREEPAGE, PTRMAP_OVERFLOW1,
    PTRMAP_OVERFLOW2, PTRMAP_ROOTPAGE, TABLE_LEAF, BtreePage, DbHeader, FreePage, Geometry, OverflowPage,
    PtrmapPage, TrunkPage, corrupt, valid_page_size,
)

PENDING, RESERVED, SHARED = 0, 1, 2  # lock numbers (see SPANS)
SPANS = {PENDING: (PENDING_BYTE, 1), RESERVED: (PENDING_BYTE + 1, 1), SHARED: (PENDING_BYTE + 2, 510)}

JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
SECTOR_SIZE = 4096  # the journal header occupies one sector
_journal_header = struct.Struct(">8sIIIII")  # magic, records, checksum seed, pages, sector, page size
_u32 = struct.Struct(">I")


def page_checksum(seed: int, data: bytes) -> int:
    """SQLite's journal record checksum: the seed plus every 200th byte
    counting back from the end of the page."""
    total, i = seed, len(data) - 200
    while i > 0:
        total += data[i]
        i -= 200
    return total & 0xFFFFFFFF


class SqliteLocks:
    """SQLite's lock levels on the database file (see the module docstring)."""

    def __init__(self, path: str, timeout: float) -> None:
        self.file = _LockFile.acquire(path, SPANS)
        self.timeout = timeout
        self.shared_held = self.reserved = self.exclusive = False
        self.closed = False

    def _wait(self, attempt: Any, timeout: float) -> None:
        import time

        deadline = time.monotonic() + timeout
        delay = 0.0005
        while not attempt():
            if time.monotonic() >= deadline:
                raise LockTimeout("database is locked")
            time.sleep(delay)
            delay = min(delay * 2, 0.02)

    def shared(self) -> None:
        if self.shared_held:
            return
        file = self.file

        def attempt() -> bool:
            if not file.try_lock(self, PENDING, False):
                return False  # a writer is waiting for the readers to finish
            try:
                return file.try_lock(self, SHARED, False)
            finally:
                file.unlock(self, PENDING)

        self._wait(attempt, self.timeout)
        self.shared_held = True

    def reserve(self, wait: bool = True) -> None:
        if self.reserved:
            return
        self._wait(lambda: self.file.try_lock(self, RESERVED, True), self.timeout if wait else 0)
        self.reserved = True

    def try_reserve(self) -> bool:
        if not self.reserved and self.file.try_lock(self, RESERVED, True):
            self.reserved = True
        return self.reserved

    def reserved_by_other(self) -> bool:
        return not self.reserved and self.file.held_by_others(self, RESERVED)

    def lock_exclusive(self) -> None:
        """PENDING, then EXCLUSIVE once the readers are gone (we hold SHARED)."""
        if self.exclusive:
            return
        file = self.file
        self._wait(lambda: file.try_lock(self, PENDING, True), self.timeout)
        try:
            self._wait(lambda: file.try_lock(self, SHARED, True), self.timeout)
        except LockTimeout:
            file.unlock(self, PENDING)
            raise
        self.exclusive = self.shared_held = True

    def downgrade(self) -> None:
        """EXCLUSIVE -> SHARED, giving up PENDING and RESERVED."""
        if self.exclusive:
            if not self.file.try_lock(self, SHARED, False):  # atomic with POSIX locks
                self.shared_held = False
            self.file.unlock(self, PENDING)
            self.exclusive = False
        self.release_reserved()

    def release_reserved(self) -> None:
        if self.reserved:
            self.file.unlock(self, RESERVED)
            self.reserved = False

    def release_all(self) -> None:
        if self.exclusive:
            self.file.unlock(self, PENDING)
            self.exclusive = False
        if self.shared_held:
            self.file.unlock(self, SHARED)
            self.shared_held = False
        self.release_reserved()

    def close(self) -> None:
        if not self.closed:
            self.release_all()
            self.file.release()
            self.closed = True


class _MemoryFile:
    """A database file kept in memory (the same calls as ``_LockFile``)."""

    def __init__(self, image: bytes = b"") -> None:
        self.data = io.BytesIO(image)

    def read(self, offset: int, size: int) -> bytes:
        self.data.seek(offset)
        return self.data.read(size)

    def write(self, offset: int, data: bytes) -> None:
        self.data.seek(offset)
        self.data.write(data)

    def size(self) -> int:
        return len(self.data.getbuffer())

    def truncate(self, size: int) -> None:
        self.data.truncate(size)

    def sync(self) -> None:
        pass


class SqlitePager(PageCache):
    """Pages of an SQLite-format database file (``path``; None: in memory,
    starting from the file contents ``image``).  The new pager is inside a
    read transaction, like ``Pager``."""

    format = "sqlite"
    committed = 0  # no log: Database's checkpoint condition never fires
    checkpoint_frames = 1 << 62

    def __init__(self, path: str | None = None, timeout: float = 5.0, image: bytes = b"",
                 page_size: int = DEFAULT_PAGE_SIZE) -> None:
        self.path = path
        self.journal_path = None if path is None else path + "-journal"
        self.crash_hook = None
        self.cache = {}
        self.dirty = set()
        self.journal = None  # (the statement journal of PageCache)
        self.journal_dirty = None
        self.header = None
        self.reading = False
        self.schema_changed = False
        self.original_pages = 0  # the page count before this transaction
        self.read_counter = None  # the change counter when the cache was last validated
        self.new_page_size = page_size  # for the database, if it is still to be created
        self.geometry = Geometry(page_size)
        self.next_page_size = None  # PRAGMA page_size on an existing database: for the next VACUUM
        self.fresh = False  # this connection created the database and nothing was written since
        self.resized_from = None  # the geometry before this transaction changed the page size
        self.next_auto_vacuum = None  # PRAGMA auto_vacuum on an existing database: for the next VACUUM
        if path is None:
            self.locks = None
            self.io = _MemoryFile(image)
        else:
            created = not os.path.exists(path)
            self.locks = SqliteLocks(path, timeout)
            self.io = self.locks.file
            if created:
                fsync_directory(path)
        try:
            self.begin_read()
        except BaseException:
            self.close_files()
            raise

    # ---- the file -------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return self.header.page_count

    @property
    def closed(self) -> bool:
        return self.locks is not None and self.locks.closed

    @property
    def is_new(self) -> bool:
        return 0 in self.dirty and self.header.change_counter == 0 and self.header.page_count == 1

    def _file_size(self) -> int:
        return self.io.size()

    def _read_header(self) -> DbHeader | None:
        data = self.io.read(0, HEADER_SIZE)
        if len(data) == 0:
            return None
        if len(data) < HEADER_SIZE:
            raise DatabaseError("file is not a database")
        header = DbHeader(data)
        header.check(self._file_size())
        return header

    def get(self, pgno: int, page_class: Any) -> Any:
        page = self.cache.get(pgno)
        if page is None:
            geometry = self.geometry
            if not 1 <= pgno <= self.header.page_count or pgno == geometry.lock_page:
                raise corrupt(f"page {pgno} out of range")
            size = geometry.page_size
            data = self.io.read((pgno - 1) * size, size)
            if len(data) != size:
                raise corrupt(f"short read of page {pgno}")
            try:
                page = page_class.from_bytes(pgno, data, geometry)
            except DatabaseError:
                raise
            except Exception as exc:
                raise corrupt(f"page {pgno}: {exc}") from None
            self.cache[pgno] = page
        return page

    # ---- allocation (SQLite's freelist) ------------------------------------------

    def allocate(self, page_class: Any, *args: Any) -> Any:
        header = self.header
        self.write(header)
        if header.freelist_count:
            trunk = self.get(header.freelist_trunk, TrunkPage)
            if trunk.leaves:
                self.write(trunk)
                pgno = trunk.leaves.pop()
            else:
                pgno = trunk.pgno
                header.freelist_trunk = trunk.next_trunk
            header.freelist_count -= 1
        else:
            pgno = self._grow()
        page = page_class(pgno, *args, geometry=self.geometry)
        self.write(page)
        return page

    def _grow(self) -> int:
        """A new page at the end of the file (as SQLite's allocateBtreePage):
        not the lock page, and with auto_vacuum not a pointer map page - that
        one is added, empty, before it."""
        header, geometry = self.header, self.geometry
        self.write(header)  # (before changing it: a statement rollback must undo this)
        pgno = header.page_count + 1
        if pgno == geometry.lock_page:  # never used (SQLite's locks live there)
            pgno += 1
        if self.auto_vacuum and geometry.is_ptrmap(pgno):
            self.write(PtrmapPage(pgno, geometry=geometry))
            pgno += 1
            if pgno == geometry.lock_page:
                pgno += 1
        header.page_count = pgno
        return pgno

    # ---- auto_vacuum ---------------------------------------------------------------------

    @property
    def auto_vacuum(self) -> int:
        """0 (NONE), 1 (FULL) or 2 (INCREMENTAL), as the header says."""
        header = self.header
        if not header.autovacuum_root:
            return 0
        return 2 if header.incremental_vacuum else 1

    def set_auto_vacuum(self, mode: int) -> None:
        """PRAGMA auto_vacuum = ``mode``: between FULL and INCREMENTAL at once;
        on or off only for a new database (for an existing one at the next
        VACUUM), as SQLite."""
        self.next_auto_vacuum = mode
        current = self.auto_vacuum
        if mode == current:
            return
        header = self.header
        if current and mode:
            self.write(header)
            header.incremental_vacuum = int(mode == 2)
        elif self.fresh and header.page_count == 1:
            self.write(header)
            header.autovacuum_root = 1 if mode else 0
            header.incremental_vacuum = int(mode == 2)

    def allocate_root(self, kind: int) -> BtreePage:
        """A new root page: with auto_vacuum the page after the largest root
        (SQLite's btreeCreateTable), moving the page that is there."""
        if not self.auto_vacuum:
            return self.allocate(BtreePage, kind)
        self.sync_ptrmap()
        header, geometry = self.header, self.geometry
        target = header.autovacuum_root + 1
        while geometry.is_ptrmap(target) or target == geometry.lock_page:
            target += 1
        if header.page_count < target:  # (the file grows up to it; pages before it become free)
            while header.page_count < target:
                pgno = self._grow()
                if pgno != target:
                    self.free(pgno)
        else:
            free = self.free_list()
            if target in free:
                free.remove(target)
                self.set_free_list(free)
            else:
                kind_there, parent = self.ptrmap_get(target)
                if kind_there in (PTRMAP_ROOTPAGE, PTRMAP_FREEPAGE) or not kind_there:
                    raise corrupt(f"page {target}: bad pointer map entry where a new root page goes")
                destination = self.allocate(FreePage).pgno  # (any page; its content comes from target)
                self.relocate(target, destination, kind_there, parent)
        page = BtreePage(target, kind, geometry=geometry)
        self.write(page)
        self.write(header)
        header.autovacuum_root = target
        self.ptrmap_set(target, PTRMAP_ROOTPAGE, 0)  # (a page write: a statement rollback undoes it too)
        return page

    def release_root(self, root: int) -> tuple[int, int] | None:
        """After the tree at ``root`` was freed (DROP): with auto_vacuum the
        largest root moves into its place (SQLite's btreeDropTable).  Returns
        (old, new) root page of the tree that moved, if one did."""
        if not self.auto_vacuum:
            return None
        self.sync_ptrmap()
        header, geometry = self.header, self.geometry
        largest = header.autovacuum_root
        moved = None
        if root != largest:
            free = self.free_list()
            free.remove(root)
            self.set_free_list(free)
            self.relocate(largest, root, PTRMAP_ROOTPAGE, 0)
            self.free(largest)
            moved = (largest, root)
        largest -= 1
        while largest == geometry.lock_page or geometry.is_ptrmap(largest):
            largest -= 1
        self.write(header)
        header.autovacuum_root = largest
        return moved

    def ptrmap_get(self, pgno: int) -> tuple[int, int]:
        page = self.get(self.geometry.ptrmap_page(pgno), PtrmapPage)
        return page.entry(pgno)

    def ptrmap_set(self, pgno: int, kind: int, parent: int) -> None:
        if pgno < 3 or self.geometry.is_ptrmap(pgno):
            return
        map_pgno = self.geometry.ptrmap_page(pgno)
        if map_pgno > self.header.page_count:
            return
        page = self.get(map_pgno, PtrmapPage)
        if page.entry(pgno) != (kind, parent):
            self.write(page)
            page.set_entry(pgno, kind, parent)

    def set_child_ptrmaps(self, page: Any) -> None:
        """The pointer map entries of what ``page`` points to (SQLite's setChildPtrmaps)."""
        pgno = page.pgno
        if isinstance(page, BtreePage):
            leaf = page.is_leaf
            for cell in page.cells:
                if not leaf:
                    self.ptrmap_set(cell.child, PTRMAP_BTREE, pgno)
                if cell.overflow:
                    self.ptrmap_set(cell.overflow, PTRMAP_OVERFLOW1, pgno)
            if not leaf:
                self.ptrmap_set(page.right, PTRMAP_BTREE, pgno)
        elif isinstance(page, OverflowPage):
            if page.next_page:
                self.ptrmap_set(page.next_page, PTRMAP_OVERFLOW2, pgno)
        elif isinstance(page, TrunkPage):
            self.ptrmap_set(pgno, PTRMAP_FREEPAGE, 0)
            for leaf_pgno in page.leaves:
                self.ptrmap_set(leaf_pgno, PTRMAP_FREEPAGE, 0)
        elif isinstance(page, FreePage):
            self.ptrmap_set(pgno, PTRMAP_FREEPAGE, 0)

    def sync_ptrmap(self) -> None:
        """Bring the pointer map up to date with this transaction's changes:
        a changed parent-child link always has a changed page on its parent
        side, so the changed pages say it all."""
        if not self.auto_vacuum:
            return
        for pgno in sorted(self.dirty - {0}):
            page = self.cache.get(pgno)
            if page is not None and pgno <= self.header.page_count:
                self.set_child_ptrmaps(page)

    def relocate(self, source: int, target: int, kind: int, parent: int) -> None:
        """Move page ``source`` (of pointer map ``kind``, pointed to from
        ``parent``) to page ``target`` (SQLite's relocatePage)."""
        page_class = BtreePage if kind in (PTRMAP_ROOTPAGE, PTRMAP_BTREE) else OverflowPage
        moved = self.get(source, page_class).copy()
        moved.pgno = target
        self.write(moved)
        self.set_child_ptrmaps(moved)
        if kind == PTRMAP_BTREE:
            owner = self.get(parent, BtreePage)
            self.write(owner)
            if owner.right == source:
                owner.right = target
            else:
                for i, cell in enumerate(owner.cells):
                    if cell.child == source:
                        owner.cells[i] = cell = cell.copy()
                        cell.child = target
                        break
        elif kind == PTRMAP_OVERFLOW1:
            owner = self.get(parent, BtreePage)
            self.write(owner)
            for i, cell in enumerate(owner.cells):
                if cell.overflow == source:
                    owner.cells[i] = cell = cell.copy()
                    cell.overflow = target
                    break
        elif kind == PTRMAP_OVERFLOW2:
            owner = self.get(parent, OverflowPage)
            self.write(owner)
            owner.next_page = target
        self.ptrmap_set(target, kind, parent)

    def free_list(self) -> list[int]:
        """Every free page, trunks first in chain order."""
        pages, trunk = [], self.header.freelist_trunk
        while trunk:
            page = self.get(trunk, TrunkPage)
            pages.append(trunk)
            pages.extend(page.leaves)
            trunk = page.next_trunk
        return pages

    def set_free_list(self, pages: list[int]) -> None:
        """Rewrite the freelist to hold ``pages``."""
        header, geometry = self.header, self.geometry
        self.write(header)
        header.freelist_count = len(pages)
        header.freelist_trunk = 0
        per_trunk = geometry.max_leaves + 1
        chunks = [pages[i:i + per_trunk] for i in range(0, len(pages), per_trunk)]
        for chunk in reversed(chunks):
            trunk, leaves = chunk[0], chunk[1:]
            self.write(TrunkPage(trunk, header.freelist_trunk, list(leaves), geometry=geometry))
            for leaf in leaves:
                self.write(FreePage(leaf, geometry=geometry))
            header.freelist_trunk = trunk

    def final_size(self, original: int, free: int) -> int:
        """The page count once ``free`` pages are gone (SQLite's finalDbSize)."""
        geometry = self.geometry
        entries = geometry.usable // 5
        maps = (free - original + geometry.ptrmap_page(original) + entries) // entries
        final = original - free - maps
        if original > geometry.lock_page and final < geometry.lock_page:
            final -= 1
        while geometry.is_ptrmap(final) or final == geometry.lock_page:
            final -= 1
        return final

    def vacuum_pages(self, limit: int | None = None) -> int:
        """Give free pages back by moving pages from the end of the file into
        them: all of them (a FULL database's commit, SQLite's
        autoVacuumCommit), or ``limit`` steps (PRAGMA incremental_vacuum,
        SQLite's incrVacuumStep: each takes the last page off the file).
        Returns the number of steps taken (with ``limit``)."""
        self.sync_ptrmap()
        header, geometry = self.header, self.geometry
        free = self.free_list()
        if not free:
            return 0
        self.write(header)
        original, steps = header.page_count, 0
        if limit is None:  # (everything, at once)
            final = self.final_size(original, len(free))
            available = sorted(p for p in free if p <= final)
            for last in range(original, final, -1):
                if geometry.is_ptrmap(last) or last == geometry.lock_page or last in free:
                    continue
                kind, parent = self.ptrmap_get(last)
                if kind == PTRMAP_ROOTPAGE:
                    raise corrupt(f"root page {last} at the end of an auto_vacuum database")
                self.relocate(last, available.pop(0), kind, parent)
            free = []
        else:
            final = original
            for _ in range(limit):
                if not free:
                    break
                steps += 1
                final = self.final_size(header.page_count, len(free))
                last = header.page_count
                if last in free:
                    free.remove(last)
                else:
                    kind, parent = self.ptrmap_get(last)
                    if kind == PTRMAP_ROOTPAGE:
                        raise corrupt(f"root page {last} at the end of an auto_vacuum database")
                    target = max((p for p in free if p <= final), default=min(free))
                    free.remove(target)
                    self.relocate(last, target, kind, parent)
                last -= 1
                while last == geometry.lock_page or geometry.is_ptrmap(last):
                    last -= 1
                header.page_count = last
                final = last
        self.write(header)
        header.page_count = final
        for pgno in [p for p in self.cache if p > final]:
            del self.cache[pgno]
            self.dirty.discard(pgno)
        self.set_free_list(free)
        return steps

    def free(self, pgno: int) -> None:
        header = self.header
        self.write(header)
        header.freelist_count += 1
        if header.freelist_trunk:
            trunk = self.get(header.freelist_trunk, TrunkPage)
            if len(trunk.leaves) < self.geometry.max_leaves:
                self.write(trunk)
                trunk.leaves.append(pgno)
                self.write(FreePage(pgno, geometry=self.geometry))
                return
        self.write(TrunkPage(pgno, header.freelist_trunk, geometry=self.geometry))
        header.freelist_trunk = pgno

    def free_page_count(self) -> int:
        return self.header.freelist_count

    def check_checksums(self) -> list[int]:
        return []  # SQLite's format has no page checksums

    def check_pages(self, roots: list[int]) -> list[str]:
        """Every page must belong to exactly one B-tree (rooted at one of
        ``roots``), overflow chain or the freelist, as sqlite3's
        integrity_check demands ("never used", "2nd reference"); with
        auto_vacuum the pointer map must say so ("Bad ptr map entry")."""
        self.sync_ptrmap()  # (inside a transaction: what its commit would write)
        owner = {}
        problems = []
        expected = {}  # page -> its pointer map entry (auto_vacuum)

        def claim(pgno: int, what: str, entry: tuple[int, int]) -> bool:
            if pgno in owner:
                problems.append(f"page {pgno}: used by {owner[pgno]} and by {what}")
                return False
            owner[pgno] = what
            expected[pgno] = entry
            return True

        for root in roots:
            stack = [(root, (PTRMAP_ROOTPAGE, 0))]
            while stack:
                pgno, entry = stack.pop()
                if not claim(pgno, f"the tree at page {root}", entry):
                    continue
                page = self.get(pgno, BtreePage)
                for cell in page.cells:
                    chain, link = cell.overflow, (PTRMAP_OVERFLOW1, pgno)
                    while chain and claim(chain, f"an overflow chain of the tree at page {root}", link):
                        link = (PTRMAP_OVERFLOW2, chain)
                        chain = self.get(chain, OverflowPage).next_page
                if not page.is_leaf:
                    stack.extend((child, (PTRMAP_BTREE, pgno)) for child in [c.child for c in page.cells] + [page.right])
        pgno, count = self.header.freelist_trunk, 0
        while pgno and claim(pgno, "the freelist", (PTRMAP_FREEPAGE, 0)):
            trunk = self.get(pgno, TrunkPage)
            for leaf in trunk.leaves:
                claim(leaf, "the freelist", (PTRMAP_FREEPAGE, 0))
            count += 1 + len(trunk.leaves)
            pgno = trunk.next_trunk
        if count != self.header.freelist_count:
            problems.append(f"freelist: {count} pages, the header says {self.header.freelist_count}")
        geometry = self.geometry
        if self.auto_vacuum:
            for pgno in range(2, self.header.page_count + 1):
                if geometry.is_ptrmap(pgno):
                    claim(pgno, "the pointer map", None)
            for pgno, entry in sorted(expected.items()):
                if entry is not None and pgno > 2 and self.ptrmap_get(pgno) != entry:
                    found = self.ptrmap_get(pgno)
                    problems.append(f"Bad ptr map entry key={pgno} expected=({entry[0]},{entry[1]}) "
                                    f"got=({found[0]},{found[1]})")
        unused = [p for p in range(1, self.header.page_count + 1) if p not in owner and p != geometry.lock_page]
        if unused:
            problems.append(f"pages never used: {unused[:10]}")
        return problems

    def shrink_cache(self, limit: int = 10_000) -> None:
        super().shrink_cache(limit)

    def note_schema_change(self) -> None:
        self.schema_changed = True

    # ---- transactions ---------------------------------------------------------------

    def begin_read(self) -> bool:
        """SHARED, roll back a hot journal, validate the cache.  Returns True
        if the cache was dropped because the database changed."""
        if self.dirty - {0} and not self.is_new:  # (a new database: header and page 1)
            raise AssertionError("begin_read() inside a transaction with changes")
        if self.locks is not None:
            self.locks.shared()
            self._recover()
        self.reading = True
        header = self._read_header()
        if header is None:  # an empty file: a new database
            header = DbHeader(page_size=self.new_page_size)
            self.geometry = Geometry(header.page_size)
            self.cache = {0: header, 1: BtreePage(1, TABLE_LEAF, geometry=self.geometry)}
            self.header = header
            self.dirty = {0, 1}
            self.original_pages = 0
            return True
        self.original_pages = header.page_count
        if self.header is not None and not self.dirty and header.change_counter == self.read_counter:
            return False
        self.cache = {0: header}
        self.header = header
        if header.page_size != self.geometry.page_size:
            self.geometry = Geometry(header.page_size)
        self.read_counter = header.change_counter
        self.dirty = set()
        return True

    def begin_write(self, wait: bool = True) -> None:
        if self.locks is not None:
            self.locks.reserve(wait)

    def end_transaction(self) -> None:
        self.reading = False
        if self.locks is not None:
            self.locks.release_all()

    def set_page_size(self, size: int) -> None:
        """PRAGMA page_size = ``size``: at once for a database nothing was
        written to yet (as SQLite, whose file is still empty then), at the
        next VACUUM otherwise; anything but a power of two from 512 to 65536
        is ignored."""
        if not valid_page_size(size):
            return
        self.next_page_size = size
        if self.fresh and self.header.page_count == 1 and size != self.geometry.page_size:
            self.resize(size)
            self.write(BtreePage(1, TABLE_LEAF, geometry=self.geometry))  # (sqlite_schema is empty)

    def resize(self, size: int) -> None:
        """Change the page size in this transaction (every page is rewritten:
        VACUUM, or a new database)."""
        if self.resized_from is None:
            self.resized_from = self.geometry
        self.geometry = Geometry(size)
        self.write(self.header)
        self.header.page_size = size
        self.cache = {pgno: page for pgno, page in self.cache.items() if pgno == 0 or pgno in self.dirty}

    def _crash_point(self, point: str, detail: int | None = None) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point, detail)

    def commit(self) -> None:
        """Write the changed pages through the rollback journal.  May raise
        ``OperationalError("database is locked")`` before anything is
        written; the transaction is then still intact."""
        if not self.dirty:
            return
        header = self.header
        if self.auto_vacuum == 1 and header.freelist_count:
            self.vacuum_pages()  # (SQLite's autoVacuumCommit)
        self.sync_ptrmap()
        if self.locks is not None:
            self.locks.reserve()
            self.locks.lock_exclusive()
        self.write(header)
        header.change_counter = (header.change_counter + 1) & 0xFFFFFFFF
        if self.schema_changed:
            header.schema_cookie = (header.schema_cookie + 1) & 0xFFFFFFFF
        self.write(self.get(1, BtreePage))  # page 1 holds the header
        pages = sorted(self.dirty - {0})
        if self.locks is not None:  # (also for a new file: a crash then leaves it empty)
            if self.resized_from is not None:  # the whole file changes: keep all of it, in the old page size
                old = self.resized_from
                self._write_journal([p for p in range(1, self.original_pages + 1) if p != old.lock_page], old)
            else:
                # The changed pages, and the pages the file loses (SQLite's CommitPhaseOne
                # journals those too: a rollback must bring them back).
                kept = set(p for p in pages if p <= self.original_pages)
                kept.update(range(header.page_count + 1, self.original_pages + 1))
                kept.discard(self.geometry.lock_page)
                self._write_journal(sorted(kept), self.geometry)
        size = self.geometry.page_size
        for i, pgno in enumerate(pages):
            self._crash_point("db_page", i)
            image = self.cache[pgno].to_bytes()
            if pgno == 1:
                image = header.to_bytes() + image[HEADER_SIZE:]
            self.io.write((pgno - 1) * size, image)
        if self.io.size() > header.page_count * size:
            self.io.truncate(header.page_count * size)
        self._crash_point("db_sync")
        self.io.sync()
        if self.locks is not None:
            self._crash_point("journal_delete")
            os.unlink(self.journal_path)
            fsync_directory(self.journal_path)
        self.dirty.clear()
        self.schema_changed = False
        self.fresh = False
        self.resized_from = None
        self.original_pages = header.page_count
        self.read_counter = header.change_counter
        if self.locks is not None:
            self.locks.downgrade()

    def serialize(self) -> bytes:
        """The database file as this connection sees it, uncommitted changes
        included (sqlite3_serialize)."""
        page_size = self.geometry.page_size
        size = self.header.page_count * page_size
        image = bytearray(self.io.read(0, size).ljust(size, b"\x00"))
        for pgno in self.dirty - {0}:
            if pgno <= self.header.page_count:
                image[(pgno - 1) * page_size:pgno * page_size] = self.cache[pgno].to_bytes()
        image[:HEADER_SIZE] = self.header.to_bytes()
        return bytes(image)

    def _write_journal(self, pgnos: list[int], geometry: Geometry) -> None:
        seed = int.from_bytes(os.urandom(4), "big")
        records = []
        size = geometry.page_size
        for pgno in pgnos:
            data = self.io.read((pgno - 1) * size, size)
            records.append(_u32.pack(pgno) + data + _u32.pack(page_checksum(seed, data)))
        header = _journal_header.pack(JOURNAL_MAGIC, len(records), seed, self.original_pages, SECTOR_SIZE, size)
        with open(self.journal_path, "wb", buffering=0) as journal:
            self._crash_point("journal_header")
            journal.write(header.ljust(SECTOR_SIZE, b"\x00"))
            for i, data in enumerate(records):
                self._crash_point("journal_page", i)
                journal.write(data)
            self._crash_point("journal_sync")
            os.fsync(journal.fileno())
        fsync_directory(self.journal_path)

    def rollback(self) -> None:
        """Discard every uncommitted change."""
        for pgno in self.dirty:
            self.cache.pop(pgno, None)
        self.dirty.clear()
        self.end_statement()
        self.schema_changed = False
        if self.resized_from is not None:
            self.geometry, self.resized_from = self.resized_from, None
            self.cache = {}
        header = self._read_header()
        if header is None:
            header = DbHeader(page_size=self.new_page_size)
            self.geometry = Geometry(header.page_size)
            self.cache[1] = BtreePage(1, TABLE_LEAF, geometry=self.geometry)
            self.dirty = {0, 1}
        self.header = header
        self.cache[0] = header

    # ---- hot journals --------------------------------------------------------------

    def _recover(self) -> None:
        """Roll back a journal left by a crashed writer (MiniDB's or
        SQLite's), as SQLite does when it opens a database: needs
        EXCLUSIVE, which another connection's SHARED may hold off for a while."""
        path = self.journal_path
        try:
            empty = os.path.getsize(path) == 0
        except FileNotFoundError:
            return
        if self.locks.reserved_by_other():
            return  # a live writer's journal (it has not touched the file yet)
        if empty:  # a writer died while creating it: nothing to play back
            os.unlink(path)
            return
        with open(path, "rb") as journal:
            data = journal.read()
        if not data.startswith(JOURNAL_MAGIC):
            os.unlink(path)  # a zeroed or foreign journal: not hot
            return
        self.locks.reserve()
        try:
            self.locks.lock_exclusive()
            if os.path.exists(path):
                self._play_back(data)
                self.io.sync()
                os.unlink(path)
                fsync_directory(path)
            self.cache = {}
            self.header = None
        finally:
            self.locks.downgrade()

    def _play_back(self, data: bytes) -> None:
        offset, original_pages, journal_page_size = 0, None, None
        while offset + _journal_header.size <= len(data):
            magic, count, seed, pages, sector, page_size = _journal_header.unpack_from(data, offset)
            if magic != JOURNAL_MAGIC or not valid_page_size(page_size):
                break
            if original_pages is None:
                original_pages, journal_page_size = pages, page_size
            elif page_size != journal_page_size:
                break
            if not sector or sector & (sector - 1):
                sector = SECTOR_SIZE
            offset += sector
            size = 8 + page_size
            if count == 0xFFFFFFFF:
                count = (len(data) - offset) // size
            for _ in range(count):
                if offset + size > len(data):
                    break
                pgno = _u32.unpack_from(data, offset)[0]
                image = data[offset + 4:offset + 4 + page_size]
                if _u32.unpack_from(data, offset + 4 + page_size)[0] != page_checksum(seed, image):
                    break  # a torn record: everything before it is what counts
                if pgno and pgno <= pages:
                    self.io.write((pgno - 1) * page_size, image)
                offset += size
            else:
                offset = -(-offset // sector) * sector  # the next segment starts on a sector boundary
                continue
            break
        if original_pages is not None:
            self.io.truncate(original_pages * journal_page_size)

    # ---- the end ------------------------------------------------------------------

    def checkpoint(self) -> bool:
        return True  # no log

    def spill(self) -> None:
        pass  # changed pages stay in memory until the commit

    def close_files(self) -> None:
        """Close without committing (also used after a crash)."""
        if self.locks is not None:
            self.locks.close()

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.commit()
            self.end_transaction()
        finally:
            self.close_files()
