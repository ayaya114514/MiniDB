"""Differential testing helper: run the same SQL on MiniDB and sqlite3."""

import re
import sqlite3

from minidb.database import Database
from minidb.errors import Error, IntegrityError
from minidb.values import sort_key, type_name

# MiniDB follows SQLite as released by sqlite.org (tools/reference_sqlite.py
# builds the pinned version).  A build with the ICU extension changes
# upper(), lower() and LIKE, so comparing against it would be misleading.
REFERENCE_VERSION = "3.53.4"
_connection = sqlite3.connect(":memory:")
_options = {row[0] for row in _connection.execute("PRAGMA compile_options")}
_connection.close()
if "ENABLE_ICU" in _options:
    raise RuntimeError(
        f"sqlite3 is linked against an ICU-enabled SQLite {sqlite3.sqlite_version}; "
        'run eval "$(python tools/reference_sqlite.py)" first'
    )


def typed(row):
    """Make int/float differences visible when comparing rows (1 != 1.0)."""
    return tuple((type_name(v), v) for v in row)


def _loose_value(v):
    if isinstance(v, (int, float)):
        return ("number", v)
    return (type_name(v), v)


def loose(row):
    """Compare numbers by value only (1 == 1.0, 0 == -0.0)."""
    return tuple(_loose_value(v) for v in row)


def row_order_key(normalized_row):
    """A canonical order for comparing normalized rows as multisets."""
    return tuple((sort_key(value), kind) for kind, value in normalized_row)


SKIPPED = "skipped"  # Pair.run(): sqlite3 interrupted the statement (step_limit)


class Pair:
    """A MiniDB database and a sqlite3 database fed with identical SQL."""

    open_pairs = set()  # not yet closed; conftest.py closes them after each test

    def __init__(self, path=None, check_messages=False, loose_numbers=False, format=None, step_limit=None):
        """``loose_numbers`` compares numbers by value only.  The fuzzer uses it:
        when several rows hold equal values of different types (1 and 1.0),
        which one DISTINCT, GROUP BY or MIN/MAX reports depends on the order
        SQLite's query plan visits rows in.  ``format``: MiniDB's file format
        (see ``Database``).  ``step_limit``: sqlite3 interrupts a statement
        after about this many virtual machine steps, and MiniDB then skips
        it (run() returns SKIPPED) -- random triggers can cascade into
        millions of changes, which MiniDB would take hours for."""
        self.mini = Database(path, format=format)
        self.lite = sqlite3.connect(":memory:", isolation_level=None)
        # Text made from a BLOB may not be valid UTF-8: keep the bytes, as
        # MiniDB does (sqlite3 would fail to decode it; see minidb.dbapi).
        self.lite.text_factory = lambda data: data.decode("utf-8", "surrogateescape")
        Pair.open_pairs.add(self)
        self.check_messages = check_messages
        self.normalize = loose if loose_numbers else typed
        if step_limit is not None:
            self.steps = 0
            self.lite.set_progress_handler(self.count_steps, 1000)
            self.step_limit = step_limit

    def count_steps(self):
        self.steps += 1000
        return self.steps > self.step_limit

    def run(self, sql, ordered=None, parameters=None):
        """Execute ``sql`` on both; assert that both fail or both return the same rows.

        Rows are compared in order when ``ordered`` is true (default: when the
        statement has ORDER BY), otherwise as multisets.
        """
        self.steps = 0
        try:
            if parameters is None:
                expected = [tuple(r) for r in self.lite.execute(sql).fetchall()]
            else:
                expected = [tuple(r) for r in self.lite.execute(sql, parameters).fetchall()]
            lite_error = None
        except sqlite3.Error as exc:
            expected, lite_error = None, exc
        if lite_error is not None and str(lite_error) == "interrupted":
            # (an interrupted write in a transaction rolls the transaction back)
            if self.mini.in_transaction and not self.lite.in_transaction:
                self.mini.execute("ROLLBACK")
            return SKIPPED
        try:
            actual = list(self.mini.execute(sql, parameters))
            mini_error = None
        except Error as exc:
            actual, mini_error = None, exc
        assert (lite_error is None) == (mini_error is None), (
            f"{sql}\n  sqlite3: {lite_error!r}\n  minidb:  {mini_error!r}"
        )
        if lite_error is not None:
            assert isinstance(lite_error, sqlite3.IntegrityError) == isinstance(
                mini_error, IntegrityError
            ), f"{sql}\n  sqlite3: {lite_error!r}\n  minidb:  {mini_error!r}"
            if self.check_messages:
                assert str(lite_error) == str(mini_error), sql
            return None
        if ordered is None:
            ordered = "ORDER BY" in sql.upper()
        expected_rows = [self.normalize(r) for r in expected]
        actual_rows = [self.normalize(r) for r in actual]
        if not ordered:
            expected_rows.sort(key=row_order_key)
            actual_rows.sort(key=row_order_key)
        assert actual_rows == expected_rows, f"{sql}\n  sqlite3: {expected}\n  minidb:  {actual}"
        return actual

    def script(self, sql_lines):
        for sql in sql_lines:
            self.run(sql)

    def close(self):
        self.mini.close()
        self.lite.close()
        Pair.open_pairs.discard(self)
