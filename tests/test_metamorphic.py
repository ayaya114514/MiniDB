"""Metamorphic oracles (tests/metamorphic.py) on a few seeds, and proof that
they catch a planner bug."""

import pytest

import metamorphic
from minidb import values


@pytest.mark.parametrize("seed", range(12))
def test_metamorphic_seed(seed):
    failure, checker = metamorphic.run_seed(seed, queries=60, rows=80)
    assert failure is None, failure
    assert sum(checker.checks.values()) > 20  # most queries were compared, not skipped


def test_every_oracle_runs():
    checks = {}
    for seed in range(12):
        _, checker = metamorphic.run_seed(seed, queries=60, rows=80)
        for name, count in checker.checks.items():
            checks[name] = checks.get(name, 0) + count
    assert set(checks) >= {"TLP where", "TLP distinct", "TLP min", "TLP max", "TLP count",
                           "TLP sum", "TLP having", "NoREC"}


def test_oracles_detect_wrong_truth_value(monkeypatch):
    """If the value 3 counted as neither true nor false (yet is not NULL), a
    row would fall out of all three TLP partitions: the oracles must notice."""
    original = values.truth

    def broken(value):
        return None if value == 3 else original(value)

    monkeypatch.setattr(values, "truth", broken)
    failures = [metamorphic.run_seed(seed, queries=60, rows=80)[0] for seed in range(12)]
    assert any(failures)
