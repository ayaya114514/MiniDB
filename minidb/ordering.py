"""Sorting and comparing result rows: ORDER BY keys (with collations, DESC
and NULLS FIRST/LAST), top-k, DISTINCT, the set operations of compound
SELECTs, and the names SQLite gives result columns."""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Iterator

from minidb import functions, values
from minidb.values import ascii_lower
from minidb.expressions import OrderTerm, Record


def combine(operator: str, left: list[tuple], right: list[tuple], collations: list[str | None] | None = None) -> list[tuple]:
    """Apply a compound operator.  Like SQLite (which merges the sorted
    sides), the distinct forms return rows in sorted order, comparing values
    by the columns' ``collations``; of equal rows UNION keeps the right
    side's first, the others the left side's first."""
    if operator == "UNION ALL":
        return left + right
    key = row_key(collations or [None] * len(left[0] if left else right[0] if right else ()))
    kept = {}
    for row in left:
        kept.setdefault(key(row), row)
    if operator == "UNION":
        first = {}
        for row in right:
            first.setdefault(key(row), row)
        kept.update(first)
    else:
        right_keys = {key(row) for row in right}
        want = operator == "INTERSECT"
        kept = {k: row for k, row in kept.items() if (k in right_keys) == want}
    return [kept[k] for k in sorted(kept)]


def row_key(collations: list[str | None]) -> Callable[[tuple], tuple]:
    """A function row -> the tuple of its values' sort keys under ``collations``."""
    functions = [values.collation_sort_key(c) for c in collations]
    if all(f is values.sort_key for f in functions):
        sort_key = values.sort_key
        return lambda row: tuple(sort_key(v) for v in row)
    return lambda row: tuple(f(v) for f, v in zip(functions, row))


def unique_names(names: list[str]) -> list[str]:
    """Column names of a subquery or view as SQLite makes them unique
    (sqlite3ColumnsFromExprList): a repeated name gets ":1", ":2", ... in
    place of a ":<digits>" ending it has (ignoring case)."""
    seen = set()
    result = []
    for name in names:
        count = 0
        while ascii_lower(name) in seen:
            base = name
            end = len(base) - 1
            while end > 0 and base[end].isdigit() and base[end].isascii():
                end -= 1
            if end > 0 and base[end] == ":":
                base = base[:end]
            count += 1
            name = f"{base}:{count}"
        seen.add(ascii_lower(name))
        result.append(name)
    return result


def distinct_records(records: Iterable[Record], collations: list[str | None] | None = None) -> Iterator[Record]:
    """Drop records with duplicate output rows, keeping the first;
    1 and 1.0 count as equal, and texts equal under the columns' collations."""
    seen = set()
    key_of = None
    for record in records:
        if key_of is None:
            key_of = row_key(collations or [None] * len(record[0]))
        key = key_of(record[0])
        if key not in seen:
            seen.add(key)
            yield record


class Descending:
    """Wraps a sort key so that it orders in reverse (for DESC terms)."""

    __slots__ = ("key",)

    def __init__(self, key: tuple) -> None:
        self.key = key

    def __lt__(self, other: Descending) -> bool:
        return other.key < self.key

    def __eq__(self, other: object) -> bool:
        return self.key == other.key


def order_key(terms: list[OrderTerm]) -> Callable[[Record], list]:
    """A key function for (output, keys) records ordering by ``terms``."""
    parts = []
    for source, index, descending, nulls_first, collation in terms:
        parts.append((0 if source == "output" else 1, index, descending,
                      (0,) if nulls_first else (2,), values.collation_sort_key(collation)))

    def key(record: Record) -> tuple:
        result = []
        for column, index, descending, null_key, sort_key in parts:
            value = record[column][index]
            if value is None:
                result.append(null_key)
            elif descending:
                result.append((1, Descending(sort_key(value))))
            else:
                result.append((1, sort_key(value)))
        return result

    return key


def order_records(records: Iterable[Record], terms: list[OrderTerm], start: int = 0, end: int | None = None) -> list[Record]:
    """Sort records by ORDER BY ``terms`` (stably) and keep [start:end].
    With a LIMIT only the first ``end`` records are kept while sorting."""
    key = order_key(terms)
    if end is not None:
        return heapq.nsmallest(end, records, key=key)[start:]
    return sorted(records, key=key)[start:end]
