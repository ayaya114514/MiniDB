"""Run SQLite's sqllogictest corpus against MiniDB.

    python tools/sqllogictest.py --fetch               # download the pinned corpus once
    python tools/sqllogictest.py                       # run every file, print a summary
    python tools/sqllogictest.py test/select1.test -v  # one file, show each failure
    python tools/sqllogictest.py --jobs 8 --json out.json
    python tools/sqllogictest.py --min-passed 3743727    # fail on a regression (CI)

sqllogictest (https://www.sqlite.org/sqllogictest/) is SQLite's own
engine-independent test suite: a few million queries whose expected results
were produced by SQLite and checked against other engines.  It is data, not
code, so it is not stored in this repository: ``--fetch`` downloads the files
of one pinned check-in into ``.sqllogictest/`` and checks each against
``tools/sqllogictest.sha3``.  sqlite.org serves archives of its Fossil
repository only to logged-in users, so the files come from the git mirror
github.com/gregrahn/sqllogictest; the pinned commit is Fossil check-in
db57eba95d (2026-04-15), and the manifest pins every file's content.

The file format (one record per blank-line-separated block)::

    statement ok | statement error
    SQL...

    query <types> [nosort|rowsort|valuesort] [label]
    SQL...
    ----
    one value per line, or "<n> values hashing to <md5>"

``skipif <engine>`` / ``onlyif <engine>`` lines before a record restrict it;
MiniDB runs the records meant for ``sqlite``.  ``hash-threshold <n>`` and
``halt`` are directives.  Result values are formatted as the reference C
runner does: NULL as ``NULL``, the empty string as ``(empty)``, ``I``
columns as integers, ``R`` columns with three decimals, and in ``T`` columns
every character outside printable ASCII becomes ``@``.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb import Database, Error  # noqa: E402
from minidb.values import INT_MAX, INT_MIN, to_text  # noqa: E402

ENGINE = "sqlite"  # the engine name used in skipif / onlyif
CORPUS = os.path.join(ROOT, ".sqllogictest")
MANIFEST = os.path.join(ROOT, "tools", "sqllogictest.sha3")
# The mirror commit that the manifest describes (Fossil check-in db57eba95d).
CHECKIN = "c67f97bf3ca7e590d12e073408bcacaf2ff0f3a0"
TARBALL = "https://codeload.github.com/gregrahn/sqllogictest/tar.gz/{checkin}"


# ---- parsing -------------------------------------------------------------------


@dataclasses.dataclass
class Record:
    kind: str  # "statement", "query", "halt" or "hash-threshold"
    line: int  # 1-based line number of the record's first line
    sql: str = ""
    error: bool = False  # statement error: the statement must fail
    types: str = ""  # query: one of T, I, R per result column
    sort: str = "nosort"
    label: str | None = None
    expected: list[str] | None = None  # the result lines after ----
    threshold: int = 0  # hash-threshold


def applies(conditions: list[tuple[str, str]]) -> bool:
    """Whether ``skipif`` / ``onlyif`` lines let ENGINE run a record."""
    for condition, engine in conditions:
        if condition == "skipif" and engine == ENGINE:
            return False
        if condition == "onlyif" and engine != ENGINE:
            return False
    return True


def parse(text: str) -> list[Record]:
    """The records of a test file that apply to ENGINE, in order."""
    lines = text.split("\n")
    records = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        if not line.strip() or line.startswith("#"):
            i += 1
            continue
        start = i + 1
        conditions = []
        words = line.split()
        while words and words[0] in ("skipif", "onlyif"):
            conditions.append((words[0], words[1]))
            i += 1
            words = lines[i].rstrip("\r").split() if i < len(lines) else []
        block = []  # the rest of the record, up to a blank line
        i += 1
        while i < len(lines) and lines[i].strip():
            block.append(lines[i].rstrip("\r"))
            i += 1
        if not words:
            continue
        keyword = words[0]
        if keyword == "halt":
            record = Record("halt", start)
        elif keyword == "hash-threshold":
            record = Record("hash-threshold", start, threshold=int(words[1]))
        elif keyword == "statement":
            record = Record("statement", start, sql="\n".join(block), error=words[1] == "error")
        elif keyword == "query":
            record = Record("query", start, types=words[1])
            for word in words[2:]:
                if word in ("nosort", "rowsort", "valuesort"):
                    record.sort = word
                else:
                    record.label = word
            if "----" in block:
                split = block.index("----")
                record.sql = "\n".join(block[:split])
                record.expected = block[split + 1:]
            else:
                record.sql = "\n".join(block)
                record.expected = []
        else:
            raise ValueError(f"line {start}: unknown record {keyword!r}")
        if applies(conditions):
            records.append(record)
    return records


# ---- result formatting -----------------------------------------------------------

_INTEGER_PREFIX = re.compile(r"[ \t\n\v\f\r]*([+-]?[0-9]+)")
_REAL_PREFIX = re.compile(r"[ \t\n\v\f\r]*([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")


def as_integer(value: int | float | str) -> int:
    """sqlite3_column_int64(): REAL truncates (clamped), TEXT uses its integer prefix."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value <= INT_MIN:
            return INT_MIN
        if value >= INT_MAX:
            return INT_MAX
        return int(value)
    match = _INTEGER_PREFIX.match(value)
    if not match:
        return 0
    return max(INT_MIN, min(INT_MAX, int(match.group(1))))


def as_real(value: int | float | str) -> float:
    """sqlite3_column_double(): TEXT uses its numeric prefix."""
    if isinstance(value, (int, float)):
        return float(value)
    match = _REAL_PREFIX.match(value)
    return float(match.group(1)) if match else 0.0


def format_value(value: object, kind: str) -> str:
    if value is None:
        return "NULL"
    if kind == "I":
        return str(as_integer(value))
    if kind == "R":
        return "%.3f" % as_real(value)
    text = value if isinstance(value, str) else to_text(value)
    if text == "":
        return "(empty)"
    return "".join(c if " " <= c <= "~" else "@" for c in text)


def result_values(rows: list[tuple], types: str, sort: str) -> list[str]:
    """The query's values, formatted and sorted as the record asks."""
    formatted = [tuple(format_value(v, types[i]) for i, v in enumerate(row)) for row in rows]
    if sort == "rowsort":
        formatted.sort()
    values = [value for row in formatted for value in row]
    if sort == "valuesort":
        values.sort()
    return values


def hash_line(values: list[str]) -> str:
    digest = hashlib.md5("".join(v + "\n" for v in values).encode()).hexdigest()
    return f"{len(values)} values hashing to {digest}"


_HASH_LINE = re.compile(r"\d+ values hashing to [0-9a-f]{32}\Z")


def matches(values: list[str], expected: list[str], threshold: int) -> bool:
    if len(expected) == 1 and _HASH_LINE.match(expected[0]):
        return hash_line(values) == expected[0]
    if threshold and len(values) > threshold:
        return hash_line(values) == "\n".join(expected)
    if values == expected:
        return True
    # A few files put a whole row on one line.
    return [w for v in values for w in v.split()] == [w for e in expected for w in e.split()]


# ---- running ---------------------------------------------------------------------


@dataclasses.dataclass
class Outcome:
    path: str
    records: int = 0
    passed: int = 0
    failures: list[tuple[int, str, str]] = dataclasses.field(default_factory=list)  # line, reason, sql
    seconds: float = 0.0
    halted: bool = False


def classify(exc: BaseException) -> str:
    """A short failure category: the error message with literals masked."""
    if not isinstance(exc, Error):
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        where = f"{os.path.basename(frame.filename)}:{frame.lineno}"
        return f"crash {type(exc).__name__} at {where}"
    message = str(exc)
    message = re.sub(r"'[^']*'", "'…'", message)
    message = re.sub(r"\b\d+\b", "N", message)
    return message[:100]


def run_file(path: str, verbose: bool = False) -> Outcome:
    outcome = Outcome(os.path.relpath(path, CORPUS) if path.startswith(CORPUS) else path)
    with open(path, encoding="utf-8", errors="replace") as f:
        records = parse(f.read())
    started = time.perf_counter()
    db = Database(None)
    threshold = 0
    try:
        for record in records:
            if record.kind == "halt":
                outcome.halted = True
                break
            if record.kind == "hash-threshold":
                threshold = record.threshold
                continue
            outcome.records += 1
            reason = run_record(db, record, threshold)
            if reason is None:
                outcome.passed += 1
            else:
                outcome.failures.append((record.line, reason, record.sql))
                if verbose:
                    print(f"{outcome.path}:{record.line}: {reason}\n    {record.sql[:300]}")
    finally:
        db.close()
    outcome.seconds = time.perf_counter() - started
    return outcome


def run_record(db: Database, record: Record, threshold: int) -> str | None:
    """None if the record passes, otherwise why it failed."""
    try:
        rows = db.execute(record.sql)
    except Exception as exc:  # noqa: BLE001 - any failure is a result here
        if record.kind == "statement" and record.error:
            return None if isinstance(exc, Error) else classify(exc)
        return classify(exc)
    if record.kind == "statement":
        return "expected an error" if record.error else None
    if rows and len(rows[0]) != len(record.types):
        return f"wrong column count: {len(rows[0])} for {len(record.types)}"
    values = result_values(rows, record.types, record.sort)
    if not matches(values, record.expected, threshold):
        return "wrong result"
    return None


# ---- corpus ----------------------------------------------------------------------


def corpus_files(arguments: list[str]) -> list[str]:
    if arguments:
        found = []
        for argument in arguments:
            path = argument if os.path.exists(argument) else os.path.join(CORPUS, argument)
            if os.path.isdir(path):
                found.extend(_walk(path))
            else:
                found.append(path)
        return found
    return _walk(os.path.join(CORPUS, "test"))


def _walk(directory: str) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames.sort()
        found.extend(os.path.join(dirpath, n) for n in sorted(filenames) if n.endswith(".test"))
    return found


def read_manifest() -> dict[str, str]:
    manifest = {}
    with open(MANIFEST) as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                digest, name = line.split(maxsplit=1)
                manifest[name.strip()] = digest
    return manifest


def fetch(checkin: str, pin: bool) -> None:
    """Download the test files of ``checkin`` into CORPUS and check (or, with
    ``pin``, record) their SHA3-256 hashes."""
    url = TARBALL.format(checkin=checkin)
    print(f"downloading {url}", file=sys.stderr)
    request = urllib.request.Request(url, headers={"User-Agent": "MiniDB-sqllogictest"})
    with urllib.request.urlopen(request, timeout=600) as response:
        data = response.read()
    hashes = {}
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            name = member.name.split("/", 1)[1]  # drop the top-level directory
            if not (name.startswith("test/") and name.endswith(".test")):
                continue
            content = archive.extractfile(member).read()
            files[name] = content
            hashes[name] = hashlib.sha3_256(content).hexdigest()
    if pin:
        with open(MANIFEST, "w") as f:
            f.write(f"# SHA3-256 of the sqllogictest files at check-in {checkin}\n")
            for name in sorted(hashes):
                f.write(f"{hashes[name]}  {name}\n")
    else:
        manifest = read_manifest()
        if manifest != hashes:
            missing = sorted(set(manifest) - set(hashes))[:5]
            changed = sorted(n for n in hashes if manifest.get(n) != hashes[n])[:5]
            sys.exit(f"corpus does not match {MANIFEST}: missing {missing}, changed {changed}")
    for name, content in files.items():
        path = os.path.join(CORPUS, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
    print(f"{len(files)} files in {CORPUS}", file=sys.stderr)


# ---- report ----------------------------------------------------------------------


def report(outcomes: list[Outcome], top: int) -> dict:
    records = sum(o.records for o in outcomes)
    passed = sum(o.passed for o in outcomes)
    reasons = collections.Counter(reason for o in outcomes for _, reason, _ in o.failures)
    # A missing feature usually fails a file's CREATE TABLE and then every
    # later record; the first failure of each file shows the root causes.
    first = collections.Counter(o.failures[0][1] for o in outcomes if o.failures)
    examples = collections.defaultdict(list)
    bugs = []  # wrong results and crashes: not missing features
    for o in outcomes:
        for line, reason, sql in o.failures:
            if len(examples[reason]) < 5:
                examples[reason].append(f"{o.path}:{line}")
            if reason == "wrong result" or reason.startswith(("crash", "expected an error", "wrong column")):
                bugs.append((f"{o.path}:{line}", reason, sql))
    clean = sum(1 for o in outcomes if not o.failures)
    print(f"{len(outcomes)} files, {clean} without failures")
    print(f"{passed} / {records} records passed ({100 * passed / max(records, 1):.2f}%)")
    print(f"{sum(o.seconds for o in outcomes):.1f} s of execution")
    if first:
        print(f"\nfirst failure of each file ({sum(first.values())} files):")
        for reason, count in first.most_common(top):
            print(f"{count:9d}  {reason}")
    if reasons:
        print(f"\nmost common failures (of {sum(reasons.values())}):")
        for reason, count in reasons.most_common(top):
            print(f"{count:9d}  {reason}")
    if bugs:
        print(f"\nwrong results and crashes ({len(bugs)}):")
        for where, reason, sql in bugs[:top]:
            print(f"  {where}: {reason}\n    {' '.join(sql.split())[:200]}")
    return {
        "files": len(outcomes),
        "clean_files": clean,
        "records": records,
        "passed": passed,
        "first_failures": first.most_common(),
        "reasons": reasons.most_common(),
        "examples": dict(examples),
        "bugs": bugs,
        "per_file": {o.path: [o.passed, o.records] for o in outcomes},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("files", nargs="*", help="test files or directories (default: all)")
    parser.add_argument("--fetch", action="store_true", help="download the pinned corpus")
    parser.add_argument("--pin", metavar="CHECKIN", help="download CHECKIN and rewrite the manifest")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every failure")
    parser.add_argument("--jobs", type=int, default=1, help="files to run in parallel")
    parser.add_argument("--top", type=int, default=40, help="failure categories to list")
    parser.add_argument("--json", metavar="PATH", help="write the summary as JSON")
    parser.add_argument("--min-passed", type=int, default=0,
                        help="exit with an error if fewer records pass (a regression check)")
    args = parser.parse_args()
    if args.pin:
        fetch(args.pin, pin=True)
        return
    if args.fetch:
        fetch(CHECKIN, pin=False)
        return
    files = corpus_files(args.files)
    if not files:
        sys.exit(f"no test files; run `python {sys.argv[0]} --fetch` first")
    if args.jobs > 1:
        with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
            outcomes = list(pool.map(run_file, files, [args.verbose] * len(files)))
    else:
        outcomes = [run_file(path, args.verbose) for path in files]
    summary = report(outcomes, args.top)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(summary, f, indent=1)
    if summary["passed"] < args.min_passed:
        sys.exit(f"only {summary['passed']} records passed, expected at least {args.min_passed}")


if __name__ == "__main__":
    main()
