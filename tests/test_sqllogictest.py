"""The sqllogictest runner (tools/sqllogictest.py): parsing, formatting,
sorting and hashing, checked on small hand-written test files."""

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import sqllogictest as slt  # noqa: E402

SAMPLE = """\
# a comment
hash-threshold 10

statement ok
CREATE TABLE t1(a INTEGER, b INTEGER, c TEXT)

statement ok
INSERT INTO t1 VALUES(1, 2, 'x'), (3, NULL, ''), (5, 6, 'y z')

statement error
INSERT INTO nowhere VALUES(1)

query IIT rowsort
SELECT a, b, c FROM t1
----
1
2
x
3
NULL
(empty)
5
6
y z

query I valuesort
SELECT a + 10 FROM t1 UNION ALL SELECT b FROM t1 WHERE b IS NOT NULL
----
11
13
15
2
6

query R nosort
SELECT a / 2.0 FROM t1 ORDER BY a
----
0.500
1.500
2.500

query II rowsort
SELECT a, a FROM t1
----
6 values hashing to {hash}

skipif sqlite
query I nosort
SELECT 'not for sqlite'
----
1

onlyif mysql
halt

onlyif sqlite
query T nosort
SELECT 'café'
----
caf@

halt

query I nosort
SELECT 'never reached'
----
0
"""


def sample() -> str:
    values = sorted([("1", "1"), ("3", "3"), ("5", "5")])
    digest = hashlib.md5("".join(v + "\n" for row in values for v in row).encode()).hexdigest()
    return SAMPLE.replace("{hash}", digest)


def test_parse_records_and_conditions():
    records = slt.parse(sample())
    kinds = [r.kind for r in records]
    assert kinds == [
        "hash-threshold", "statement", "statement", "statement",
        "query", "query", "query", "query", "query", "halt", "query",
    ]
    assert records[0].threshold == 10
    assert records[3].error and not records[1].error
    query = records[4]
    assert (query.types, query.sort, query.label) == ("IIT", "rowsort", None)
    assert query.expected[:3] == ["1", "2", "x"]
    assert query.line == 13
    assert records[8].sql == "SELECT 'café'"


def test_label_and_query_without_results():
    records = slt.parse("query I rowsort label-7\nSELECT 1\n")
    assert records[0].label == "label-7"
    assert records[0].expected == []


def test_unknown_record():
    with pytest.raises(ValueError, match="line 2"):
        slt.parse("\nbogus\n")


@pytest.mark.parametrize("value, kind, text", [
    (None, "I", "NULL"),
    ("", "T", "(empty)"),
    (3.9, "I", "3"),
    (-3.9, "I", "-3"),
    (1e300, "I", str(2**63 - 1)),
    (" 12abc", "I", "12"),
    ("abc", "I", "0"),
    (2, "R", "2.000"),
    ("1.25e1x", "R", "12.500"),
    (-0.0004, "R", "-0.000"),
    (7, "T", "7"),
    (0.5, "T", "0.5"),
    ("tab\there", "T", "tab@here"),
])
def test_format_value(value, kind, text):
    assert slt.format_value(value, kind) == text


def test_sorting_modes():
    rows = [(2, "b"), (10, "a"), (1, "c")]
    assert slt.result_values(rows, "IT", "nosort") == ["2", "b", "10", "a", "1", "c"]
    # rowsort compares the formatted text, so "10" sorts before "2".
    assert slt.result_values(rows, "IT", "rowsort") == ["1", "c", "10", "a", "2", "b"]
    assert slt.result_values(rows, "IT", "valuesort") == ["1", "10", "2", "a", "b", "c"]


def test_matches_hash_and_row_per_line():
    values = ["1", "2", "3", "4"]
    assert slt.matches(values, [slt.hash_line(values)], 0)
    assert not slt.matches(values, [slt.hash_line(values[:3])], 0)
    assert slt.matches(values, ["1 2", "3 4"], 0)
    assert not slt.matches(values, ["1", "2", "4", "3"], 0)


def test_run_file(tmp_path):
    path = tmp_path / "sample.test"
    path.write_text(sample())
    outcome = slt.run_file(str(path))
    assert outcome.failures == []
    assert outcome.halted
    assert (outcome.passed, outcome.records) == (8, 8)


def test_run_file_reports_failures(tmp_path):
    path = tmp_path / "bad.test"
    path.write_text(
        "statement ok\nCREATE TABLE t(a INTEGER)\n\n"
        "statement ok\nSELEC 1\n\n"
        "statement error\nSELECT 1\n\n"
        "query I nosort\nSELECT 1\n----\n2\n\n"
        "query II nosort\nSELECT 1\n----\n1\n\n"
        "query I nosort\nSELECT no_such_function(1)\n----\n1\n"
    )
    outcome = slt.run_file(str(path))
    reasons = [reason for _, reason, _ in outcome.failures]
    assert reasons[0].startswith("syntax error near \"SELEC\"")
    assert reasons[1:4] == ["expected an error", "wrong result", "wrong column count: 1 for 2"]
    assert reasons[4] == "no such function: no_such_function"
    assert (outcome.passed, outcome.records) == (1, 6)


def test_classify_masks_literals():
    class Boom(slt.Error):
        pass

    assert slt.classify(Boom("no such column: 'x1' at 12")) == "no such column: '…' at N"
    try:
        {}["missing"]
    except KeyError as exc:
        assert slt.classify(exc).startswith("crash KeyError at test_sqllogictest.py:")
