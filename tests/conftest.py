import os
import sys

import pytest

# Let tests import helpers such as ``sqlcompare`` from this directory.
sys.path.insert(0, os.path.dirname(__file__))

from sqlcompare import Pair  # noqa: E402


@pytest.fixture(autouse=True)
def close_pairs():
    """Close the databases of every Pair a test left open (warnings are
    errors, and unclosed sqlite3 connections warn on Python 3.13+)."""
    yield
    for pair in list(Pair.open_pairs):
        pair.close()
