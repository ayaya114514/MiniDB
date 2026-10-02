"""What the playground page calls in Pyodide: run SQL on an in-memory MiniDB
(or on an SQLite file the page opened, also kept in memory), describe its
B-trees and, for SQLite's format, the layout of its pages.  Everything
returns JSON text, except the exported file."""

import json
import struct

from minidb import sqlite_format as F
from minidb.database import Database
from minidb.errors import Error
from minidb.pager import USABLE_SIZE
from minidb.parser import Compound, Delete, Explain, Select, Update
from minidb.sqlite_pager import SqlitePager
from minidb.tokenizer import tokenize
from minidb.values import plain_value

MAX_NODES_PER_LEVEL = 48
MAX_KEYS_SHOWN = 4
MAX_CELLS_LISTED = 400

db = Database()
file_name = None  # the SQLite file opened, or None for the new in-memory database


def reset() -> None:
    global db, file_name
    db = Database()
    file_name = None
    _cached.clear()


def open_file(data: object, name: str) -> str:
    """Open the SQLite file ``data`` (its bytes, from the page) in memory."""
    global db, file_name
    image = data.to_bytes() if hasattr(data, "to_bytes") else bytes(data)
    if image[:16] != F.MAGIC:
        raise ValueError(f"{name} 不是 SQLite 数据库文件（文件头不是 “SQLite format 3”）")
    opened = Database()
    opened.deserialize(image)
    db.close()
    db, file_name = opened, name
    _cached.clear()
    return info()


def export() -> bytes:
    """The database as an SQLite file (uncommitted changes included)."""
    return db.serialize()


def info() -> str:
    sqlite = isinstance(db.pager, SqlitePager)
    return json.dumps({
        "name": file_name,
        "format": "sqlite" if sqlite else "minidb",
        "pages": db.pager.header.page_count if sqlite else None,
        "bytes": db.pager.header.page_count * F.PAGE_SIZE if sqlite else None,
    })


def cell(value: object) -> object:
    """A result value as JSON: BLOBs as hex, infinities as text (SQLite
    has no NaN: it becomes NULL)."""
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        return {"real": "Inf" if value > 0 else "-Inf"}  # as SQLite prints them
    return value


def split(sql: str) -> list[str]:
    """The statements of a script, as text (split at the top-level ``;``)."""
    pieces, start = [], 0
    for token in tokenize(sql):
        if token.kind == "OP" and token.value == ";":
            pieces.append(sql[start:token.pos])
            start = token.pos + 1
    pieces.append(sql[start:])
    return [piece.strip() for piece in pieces if piece.strip()]


def run(sql: str) -> str:
    """Run the statements in ``sql``; one entry per statement, stopping at
    the first error (like the command line shell).  Queries, UPDATE and
    DELETE also get the plan EXPLAIN shows for them."""
    results = []
    try:
        pieces = split(sql)
    except Error as exc:
        return json.dumps([{"sql": sql.strip(), "error": str(exc)}])
    for piece in pieces:
        entry = {"sql": piece}
        results.append(entry)
        try:
            statements = db.parse(piece)
            for stmt in statements:
                if isinstance(stmt, (Select, Compound, Update, Delete)):
                    entry["plan"] = [list(row) for row in db.execute_statement(Explain(stmt))]
                result = db.execute_statement(stmt)
        except Error as exc:
            entry["error"] = str(exc)
            break
        entry["columns"] = result.columns
        entry["rows"] = [[cell(v) for v in row] for row in result]
        entry["rowcount"] = result.rowcount
    return json.dumps(results)


def objects() -> str:
    """The tables and indexes, for the tree view."""
    found = []
    if isinstance(db.pager, SqlitePager):
        found.append({"name": "sqlite_schema", "kind": "table"})
    for table in db.catalog.tables.values():
        if table.is_schema:
            continue
        found.append({"name": table.name, "kind": "table"})
        for index in reversed(table.indexes):
            found.append({"name": index.name, "kind": "index", "table": table.name})
    return json.dumps(found)


def show_key(key: object) -> str:
    if isinstance(key, tuple):  # an index key: sort keys of the columns, then the row id
        parts = [plain_value(part) for part in key]
        *columns, rowid = parts
        return ", ".join(show_value(v) for v in columns) + f" | {rowid}"
    return show_value(key)


def show_value(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        text = value if len(value) <= 12 else value[:11] + "…"
        return "'" + text.replace("'", "''") + "'"
    if isinstance(value, bytes):
        return "x'" + value[:6].hex() + ("…" if len(value) > 6 else "") + "'"
    return repr(value)


def tree(name: str) -> str:
    """The B+ tree of a table or index, level by level (at most
    MAX_NODES_PER_LEVEL nodes per level, in key order)."""
    catalog = db.catalog
    if isinstance(db.pager, SqlitePager):
        return cached("tree " + name, lambda: sqlite_tree(name))
    table = catalog.tables.get(name.lower())
    if table is not None:
        btree = catalog.table_tree(table)
    else:
        index = next((i for t in catalog.tables.values() for i in t.indexes if i.name.lower() == name.lower()), None)
        if index is None:
            return json.dumps({"error": f"no such table or index: {name}"})
        btree = catalog.index_tree(index)
    levels, level, hidden = [], [btree.root], 0
    while level:
        nodes, following = [], []
        for pgno in level:
            node = btree.node(pgno)
            keys = node.keys
            shown = keys if len(keys) <= MAX_KEYS_SHOWN else keys[:MAX_KEYS_SHOWN - 1] + [keys[-1]]
            nodes.append({
                "page": pgno,
                "leaf": node.is_leaf,
                "count": len(keys),
                "keys": [show_key(k) for k in shown],
                "elided": len(keys) > MAX_KEYS_SHOWN,
                "fill": round(node.size / USABLE_SIZE, 3),
                "children": [] if node.is_leaf else list(node.children),
            })
            if not node.is_leaf:
                following.extend(node.children)
        if len(following) > MAX_NODES_PER_LEVEL:
            hidden += len(following) - MAX_NODES_PER_LEVEL
            following = following[:MAX_NODES_PER_LEVEL]
        levels.append(nodes)
        level = following
    return json.dumps({"name": name, "levels": levels, "depth": len(levels), "hidden": hidden,
                       "keys": btree.check()})


# ---- SQLite's file format: B-trees and pages as they are in the file ------------------

_cached = {}  # (what, change counter) -> JSON: a large file takes seconds to walk in the browser


def cached(what: str, compute) -> str:
    """``compute()``, kept while the file does not change (no uncommitted
    changes, the same change counter in its header)."""
    pager = db.pager
    if pager.dirty:
        return compute()
    key = (what, pager.header.change_counter)
    if key not in _cached:
        if len(_cached) > 64:
            _cached.clear()
        _cached[key] = compute()
    return _cached[key]


KINDS = {F.TABLE_LEAF: "table-leaf", F.TABLE_INTERIOR: "table-interior",
         F.INDEX_LEAF: "index-leaf", F.INDEX_INTERIOR: "index-interior"}


def page_bytes(pgno: int) -> bytes:
    """Page ``pgno`` as it is in the file - or as MiniDB will write it, if
    the open transaction changed it."""
    pager = db.pager
    if pgno in pager.dirty:
        data = pager.cache[pgno].to_bytes()
        return pager.header.to_bytes() + data[F.HEADER_SIZE:] if pgno == 1 else data
    return pager.io.read((pgno - 1) * F.PAGE_SIZE, F.PAGE_SIZE)


def find_root(name: str) -> int:
    if name.lower() in ("sqlite_schema", "sqlite_master"):
        return 1
    catalog = db.catalog
    table = catalog.tables.get(name.lower())
    if table is not None and not table.is_schema:
        return table.root
    for table in catalog.tables.values():
        for index in table.indexes:
            if index.name.lower() == name.lower():
                return index.root
    raise ValueError(f"no such table or index: {name}")


def payload(cell: F.Cell) -> bytes:
    """A cell's whole payload, following its overflow pages."""
    parts, pgno, remaining = [cell.local], cell.overflow, cell.size - len(cell.local)
    while pgno and remaining > 0:
        data = page_bytes(pgno)
        parts.append(data[4:4 + min(remaining, F.OVERFLOW_DATA)])
        remaining -= len(parts[-1])
        pgno = struct.unpack_from(">I", data)[0]
    return b"".join(parts)


def cell_key(page: F.BtreePage, cell: F.Cell) -> str:
    """What a cell says, for display: a row id, or an index entry."""
    if page.kind == F.TABLE_INTERIOR:
        return f"≤ {cell.rowid}"
    if page.kind == F.TABLE_LEAF:
        return str(cell.rowid)
    values = F.decode_record(payload(cell))
    *columns, rowid = values
    return ", ".join(show_value(v) for v in columns) + f" | {show_value(rowid)}"


def sqlite_tree(name: str) -> str:
    """tree() for SQLite's format, from the pages themselves."""
    try:
        root = find_root(name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    levels, level, hidden = [], [root], 0
    while level:
        nodes, following = [], []
        for pgno in level:
            page = F.BtreePage.from_bytes(pgno, page_bytes(pgno))
            cells = page.cells
            shown = cells if len(cells) <= MAX_KEYS_SHOWN else cells[:MAX_KEYS_SHOWN - 1] + [cells[-1]]
            children = [] if page.is_leaf else [c.child for c in cells] + [page.right]
            nodes.append({
                "page": pgno,
                "leaf": page.is_leaf,
                "count": len(cells),
                "keys": [cell_key(page, c) for c in shown],
                "elided": len(cells) > MAX_KEYS_SHOWN,
                "fill": round(page.used() / page.capacity, 3),
                "children": children,
                "kind": KINDS[page.kind],
            })
            following.extend(children)
        if len(following) > MAX_NODES_PER_LEVEL:
            hidden += len(following) - MAX_NODES_PER_LEVEL
            following = following[:MAX_NODES_PER_LEVEL]
        levels.append(nodes)
        level = following
    return json.dumps({"name": name, "levels": levels, "depth": len(levels), "hidden": hidden,
                       "keys": count_entries(root), "root": root})


def count_entries(root: int) -> int:
    """The rows of a table or the entries of an index (whose interior cells are entries too)."""
    entries, stack = 0, [root]
    while stack:
        pgno = stack.pop()
        page = F.BtreePage.from_bytes(pgno, page_bytes(pgno))
        if page.kind != F.TABLE_INTERIOR:
            entries += len(page.cells)
        if not page.is_leaf:
            stack.extend([c.child for c in page.cells] + [page.right])
    return entries


def cell_extent(data: bytes, start: int, kind: int) -> tuple[int, F.Cell]:
    """The bytes a cell takes on its page (SQLite's cellSizePtr), and the cell."""
    cell = F.parse_cell(data, start, kind)
    if kind == F.TABLE_INTERIOR:
        return 4 + F.varint_size(cell.rowid), cell
    size = F.varint_size(cell.size) + len(cell.local) + (4 if cell.overflow else 0)
    if kind == F.TABLE_LEAF:
        size += F.varint_size(cell.rowid)
    elif kind == F.INDEX_INTERIOR:
        size += 4
    return max(size, 4), cell


def overflow_pages(first: int) -> int:
    count, pgno = 0, first
    while pgno and count <= db.pager.header.page_count:
        count += 1
        pgno = struct.unpack_from(">I", page_bytes(pgno))[0]
    return count


def page(pgno: int) -> str:
    """The layout of B-tree page ``pgno``: its header, the cell pointer
    array, the unallocated space, the cells, free blocks and fragments."""
    data = page_bytes(pgno)
    offset = F.HEADER_SIZE if pgno == 1 else 0
    kind = data[offset]
    if kind not in KINDS:
        return json.dumps({"page": pgno, "error": "不是 B 树页"})
    leaf = kind in (F.TABLE_LEAF, F.INDEX_LEAF)
    header = 8 if leaf else 12
    first_free, count, content, fragments = struct.unpack_from(">HHHB", data, offset + 1)
    content = content or 65536
    regions = []
    if offset:
        regions.append({"kind": "file-header", "start": 0, "end": offset})
    regions.append({"kind": "page-header", "start": offset, "end": offset + header})
    pointers = offset + header
    regions.append({"kind": "pointers", "start": pointers, "end": pointers + 2 * count})
    regions.append({"kind": "unallocated", "start": pointers + 2 * count, "end": content})
    cells = []
    for i in range(count):
        start = struct.unpack_from(">H", data, pointers + 2 * i)[0]
        size, cell = cell_extent(data, start, kind)
        regions.append({"kind": "cell", "start": start, "end": start + size, "cell": i})
        if i < MAX_CELLS_LISTED:
            entry = {"index": i, "offset": start, "size": size, "key": cell_key(F.BtreePage(pgno, kind), cell)}
            if not kind == F.TABLE_INTERIOR:
                entry["payload"] = cell.size
            if cell.child:
                entry["child"] = cell.child
            if cell.overflow:
                entry["overflow"] = [cell.overflow, overflow_pages(cell.overflow)]
            cells.append(entry)
    free_bytes, block = 0, first_free
    while block and len(regions) < 10_000:
        following, size = struct.unpack_from(">HH", data, block)
        regions.append({"kind": "freeblock", "start": block, "end": block + size})
        free_bytes += size
        block = following
    # What is left in the content area: fragments (gaps of 1-3 bytes).
    covered = sorted((r["start"], r["end"]) for r in regions if r["start"] >= content)
    position = content
    for start, end in covered + [(F.PAGE_SIZE, F.PAGE_SIZE)]:
        if start > position:
            regions.append({"kind": "fragment", "start": position, "end": start})
        position = max(position, end)
    regions.sort(key=lambda r: r["start"])
    return json.dumps({
        "page": pgno, "kind": KINDS[kind], "size": F.PAGE_SIZE, "cells": count, "listed": cells,
        "first_freeblock": first_free, "content_start": content, "fragmented": fragments,
        "free": content - (pointers + 2 * count) + free_bytes + fragments,
        "right": None if leaf else struct.unpack_from(">I", data, offset + 8)[0], "regions": regions,
    })


def file_map() -> str:
    """What each page of the file is: [kind, owner] per page (owner: an
    index into "owners"), from the B-trees, their overflow chains and the
    freelist."""
    return cached("map", walk_file)


def walk_file() -> str:
    header = db.pager.header
    count = header.page_count
    kinds = ["unknown"] * (count + 1)
    owners_of = [-1] * (count + 1)
    owners = []
    roots = [("sqlite_schema", 1)]
    for table in db.catalog.tables.values():
        if not table.is_schema:
            roots.append((table.name, table.root))
            roots.extend((index.name, index.root) for index in table.indexes)
    for owner, (name, root) in enumerate(roots):
        owners.append(name)
        stack = [root]
        while stack:
            pgno = stack.pop()
            if not 1 <= pgno <= count or kinds[pgno] != "unknown":
                continue
            page = F.BtreePage.from_bytes(pgno, page_bytes(pgno))
            kinds[pgno], owners_of[pgno] = KINDS[page.kind], owner
            for cell in page.cells:
                chain = cell.overflow
                while chain and 1 <= chain <= count and kinds[chain] == "unknown":
                    kinds[chain], owners_of[chain] = "overflow", owner
                    chain = struct.unpack_from(">I", page_bytes(chain))[0]
            if not page.is_leaf:
                stack.extend([page.right] + [c.child for c in reversed(page.cells)])
    trunk = header.freelist_trunk
    while trunk and 1 <= trunk <= count and kinds[trunk] == "unknown":
        kinds[trunk] = "freelist-trunk"
        data = page_bytes(trunk)
        following, leaves = struct.unpack_from(">II", data)
        for leaf in struct.unpack_from(f">{min(leaves, F.USABLE // 4 - 2)}I", data, 8):
            if 1 <= leaf <= count:
                kinds[leaf] = "freelist-leaf"
        trunk = following
    if F.LOCK_PAGE <= count:
        kinds[F.LOCK_PAGE] = "lock-byte"
    return json.dumps({"pages": [[kinds[p], owners_of[p]] for p in range(1, count + 1)], "owners": owners})
