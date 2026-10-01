"""What the playground page calls in Pyodide: run SQL on an in-memory MiniDB
and describe its B+ trees.  Everything returns JSON text."""

import json

from minidb.database import Database
from minidb.errors import Error
from minidb.pager import USABLE_SIZE
from minidb.parser import Compound, Delete, Explain, Select, Update
from minidb.tokenizer import tokenize
from minidb.values import plain_value

MAX_NODES_PER_LEVEL = 48
MAX_KEYS_SHOWN = 4

db = Database()


def reset() -> None:
    global db
    db = Database()


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
    for table in db.catalog.tables.values():
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
