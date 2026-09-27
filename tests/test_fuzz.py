"""A modest slice of the differential fuzzer (tests/fuzz.py) on every test run.

Larger campaigns: ``.venv/bin/python tests/fuzz.py --seeds 0-999 --statements 600``.
"""

import pytest

from fuzz import run_seed


@pytest.mark.parametrize("seed", range(25))
def test_fuzz_in_memory(seed):
    failure = run_seed(seed, 300)
    assert failure is None, failure


@pytest.mark.parametrize("seed", range(100, 105))
def test_fuzz_with_database_file(seed, tmp_path):
    failure = run_seed(seed, 300, str(tmp_path / "fuzz.db"))
    assert failure is None, failure
