"""A few seeds of tests/concurrency_fuzz.py: MiniDB and sqlite3 processes
writing one file at once (CI runs more)."""

import pytest

import concurrency_fuzz


@pytest.mark.parametrize("mode", ["journal", "wal", "minidb"])
def test_processes_writing_at_once(tmp_path, mode):
    for seed in range(2):
        assert concurrency_fuzz.run_seed(seed, mode, 4, 120, str(tmp_path)) is None
