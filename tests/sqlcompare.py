"""Differential testing helper: run the same SQL on MiniDB and sqlite3."""

import sqlite3

from minidb.database import Database
from minidb.errors import Error, IntegrityError
from minidb.values import sort_key, type_name


def typed(row):
    """Make int/float differences visible when comparing rows (1 != 1.0)."""
    return tuple((type_name(v), v) for v in row)


def row_order_key(row):
    """A canonical order for comparing results as multisets (1 before 1.0)."""
    return tuple((sort_key(v), type_name(v)) for v in row)


class Pair:
    """A MiniDB database and a sqlite3 database fed with identical SQL."""

    def __init__(self, path=None, check_messages=False):
        self.mini = Database(path)
        self.lite = sqlite3.connect(":memory:", isolation_level=None)
        self.check_messages = check_messages

    def run(self, sql, ordered=None):
        """Execute ``sql`` on both; assert that both fail or both return the same rows.

        Rows are compared in order when ``ordered`` is true (default: when the
        statement has ORDER BY), otherwise as multisets.
        """
        try:
            expected = [tuple(r) for r in self.lite.execute(sql).fetchall()]
            lite_error = None
        except sqlite3.Error as exc:
            expected, lite_error = None, exc
        try:
            actual = list(self.mini.execute(sql))
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
        expected_rows = [typed(r) for r in expected]
        actual_rows = [typed(r) for r in actual]
        if not ordered:
            expected_rows.sort(key=lambda r: row_order_key([v for _, v in r]))
            actual_rows.sort(key=lambda r: row_order_key([v for _, v in r]))
        assert actual_rows == expected_rows, f"{sql}\n  sqlite3: {expected}\n  minidb:  {actual}"
        return actual

    def script(self, sql_lines):
        for sql in sql_lines:
            self.run(sql)

    def close(self):
        self.mini.close()
        self.lite.close()
