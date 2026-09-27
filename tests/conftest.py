import os
import sys

import pytest

# Let tests import helpers such as ``sqlcompare`` from this directory.
sys.path.insert(0, os.path.dirname(__file__))

from sqlcompare import REFERENCE_VERSION, Pair  # noqa: E402


def pytest_report_header():
    import sqlite3
    note = "" if sqlite3.sqlite_version == REFERENCE_VERSION else (
        f" (expected {REFERENCE_VERSION}: a few edge cases differ between versions;"
        ' eval "$(python tools/reference_sqlite.py)")')
    return f"reference SQLite {sqlite3.sqlite_version}{note}"


@pytest.fixture(autouse=True)
def close_pairs():
    """Close the databases of every Pair a test left open (warnings are
    errors, and unclosed sqlite3 connections warn on Python 3.13+)."""
    yield
    for pair in list(Pair.open_pairs):
        pair.close()
