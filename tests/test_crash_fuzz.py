"""A few seeds of tests/crash_fuzz.py: power failures at random fsyncs and
unlinks of a SQLite-format file, with lost, kept and torn writes; sqlite3
and MiniDB must recover the same intact state (CI runs many more seeds)."""

import pytest

import crash_fuzz


@pytest.mark.parametrize("wal", [False, True])
@pytest.mark.parametrize("seed", range(4))
def test_power_failures(tmp_path, seed, wal):
    assert crash_fuzz.run_seed(seed, wal, 6, str(tmp_path)) is None


def test_the_journal_counts_its_records_only_once_they_are_on_the_disk(tmp_path):
    # Seed 38's trial 7 failed when the journal header held the record
    # count from the start: a power failure before the journal's fsync left
    # torn records that sqlite3 played back.
    assert crash_fuzz.run_seed(38, False, 8, str(tmp_path)) is None
