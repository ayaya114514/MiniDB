"""Cross-process concurrency differential testing.

Several processes - MiniDB connections and sqlite3 connections - run random
transactions on one database file at the same time: transfers between
accounts (the total never changes) that also log themselves, increments of
shared counters (upserts), and read transactions that check what a
snapshot must show (the total, every own commit and no other change of
their own, nothing going backwards).  Each process records what it saw
committed.  Afterwards sqlite3 and MiniDB each open the file: both must
find it intact, holding exactly the committed changes, and give the same
results to the same queries.

    .venv/bin/python tests/concurrency_fuzz.py --seeds 0-9 [--mode journal|wal|minidb]

``journal``: a SQLite-format file with the rollback journal; ``wal``: in
WAL mode; ``minidb``: MiniDB's own format (MiniDB processes only).
"""

from __future__ import annotations

import argparse
import os
import pickle
import random
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minidb.database import Database  # noqa: E402
from minidb.errors import Error  # noqa: E402

ACCOUNTS = 12
BALANCE = 1000
KEYS = 8
QUERIES = (
    "SELECT id, balance FROM accounts ORDER BY id",
    "SELECT worker, seq, amount, src, dst FROM log ORDER BY worker, seq",
    "SELECT k, v FROM kv ORDER BY k",
    "SELECT worker, count(*), sum(amount), min(seq), max(seq) FROM log GROUP BY worker ORDER BY worker",
    "SELECT src, dst, count(*) FROM log GROUP BY src, dst ORDER BY 1, 2",
    "SELECT a.id, count(l.id) FROM accounts AS a LEFT JOIN log AS l ON l.src = a.id GROUP BY a.id ORDER BY a.id",
    "SELECT count(*) FROM log WHERE worker = 'w0' AND amount > 20",
    "SELECT k FROM kv WHERE v > 10 ORDER BY v DESC, k",
    "SELECT type, name, tbl_name FROM sqlite_schema ORDER BY name",
)


def create(path: str, mode: str) -> None:
    with Database(path, format=None if mode == "minidb" else "sqlite") as db:
        db.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance INTEGER NOT NULL)")
        db.execute("CREATE TABLE log (id INTEGER PRIMARY KEY, worker TEXT, seq INTEGER, amount INTEGER, "
                   "src INTEGER, dst INTEGER, UNIQUE (worker, seq))")
        db.execute("CREATE INDEX log_src ON log (src)")
        db.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v INTEGER) WITHOUT ROWID")
        db.execute("INSERT INTO accounts (balance) VALUES " + ", ".join(["(?)"] * ACCOUNTS), [BALANCE] * ACCOUNTS)
        if mode == "wal":
            db.execute("PRAGMA journal_mode = WAL")


# ---- a worker process --------------------------------------------------------------


class Connection:
    """The same calls on a MiniDB or a sqlite3 connection."""

    def __init__(self, engine: str, path: str) -> None:
        self.engine = engine
        if engine == "sqlite3":
            self.connection = sqlite3.connect(path, isolation_level=None, timeout=20)
        else:
            self.connection = Database(path, timeout=20)

    def run(self, sql: str, parameters: tuple = ()) -> list:
        self.last = sql
        if self.engine == "sqlite3":
            return self.connection.execute(sql, parameters).fetchall()
        return self.connection.execute(sql, parameters)

    @property
    def in_transaction(self) -> bool:
        return self.connection.in_transaction

    def close(self) -> None:
        self.connection.close()


def busy(exc: BaseException) -> bool:
    return "locked" in str(exc) or "busy" in str(exc)


def work(path: str, engine: str, name: str, seed: int, operations: int, mode: str, start: str) -> dict:
    """One worker's run; returns what it saw committed and what was wrong."""
    rng = random.Random(seed)
    while not os.path.exists(start):  # (all start together)
        time.sleep(0.005)
    connection = Connection(engine, path)

    def pause():  # (inside a transaction: let the others interleave)
        if rng.random() < 0.3:
            time.sleep(rng.random() * 0.002)

    transfers, increments, problems = [], [], []
    seq, seen, busy_count = 0, 0, 0
    for _ in range(operations):
        roll = rng.random()
        try:
            if roll < 0.45:
                source, target = rng.sample(range(1, ACCOUNTS + 1), 2)
                amount = rng.randint(1, 50)
                connection.run(rng.choice(["BEGIN IMMEDIATE", "BEGIN"]))
                connection.run("UPDATE accounts SET balance = balance - ? WHERE id = ?", (amount, source))
                pause()
                connection.run("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, target))
                pause()
                connection.run("INSERT INTO log (worker, seq, amount, src, dst) VALUES (?, ?, ?, ?, ?)",
                               (name, seq, amount, source, target))
                if rng.random() < 0.1:
                    connection.run("ROLLBACK")
                    continue
                connection.run("COMMIT")
                transfers.append((name, seq, amount, source, target))
                seq += 1
            elif roll < 0.7:
                key, increment = f"k{rng.randrange(KEYS)}", rng.randint(1, 5)
                connection.run("INSERT INTO kv VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = v + excluded.v",
                               (key, increment))
                increments.append((key, increment))
            elif roll < 0.95:
                connection.run("BEGIN")
                total = connection.run("SELECT sum(balance) FROM accounts")[0][0]
                pause()
                mine = connection.run("SELECT count(*), coalesce(max(seq), -1) FROM log WHERE worker = ?", (name,))[0]
                count = connection.run("SELECT count(*) FROM log")[0][0]
                pause()
                again = connection.run("SELECT sum(balance) FROM accounts")[0][0]
                connection.run("COMMIT")
                if total != ACCOUNTS * BALANCE or again != total:
                    problems.append(f"{name}: a snapshot's total is {total}, then {again}")
                if mine != (seq, seq - 1):
                    problems.append(f"{name}: sees {mine} of its own transfers, committed {seq}")
                if count < seen:
                    problems.append(f"{name}: the log went back from {seen} to {count} rows")
                seen = count
            elif roll < 0.97:  # schema changes the others must notice
                connection.run(rng.choice(["CREATE INDEX IF NOT EXISTS log_amount ON log (amount)",
                                           "DROP INDEX IF EXISTS log_amount",
                                           "CREATE INDEX IF NOT EXISTS kv_v ON kv (v)", "DROP INDEX IF EXISTS kv_v"]))
                connection.run("SELECT count(*) FROM log WHERE amount > ?", (rng.randint(1, 50),))
            elif roll < 0.98:
                connection.run("VACUUM")
            elif mode == "wal":
                connection.run(f"PRAGMA wal_checkpoint({rng.choice(['PASSIVE', 'RESTART', 'TRUNCATE'])})")
            else:
                connection.run("SELECT count(*) FROM log WHERE src = ?", (rng.randint(1, ACCOUNTS),))
        except (sqlite3.Error, Error) as exc:
            # Nothing here should ever be busy: writers start with a write
            # (or BEGIN IMMEDIATE) and so wait for the lock, readers never
            # write, and every connection waits up to 20 seconds.
            problems.append(f"{name} ({engine}): {exc!r} at {connection.last}")
            busy_count += busy(exc)
            if connection.in_transaction:
                try:
                    connection.run("ROLLBACK")
                except (sqlite3.Error, Error) as rollback_error:
                    problems.append(f"{name}: ROLLBACK failed: {rollback_error!r}")
    connection.close()
    return {"transfers": transfers, "increments": increments, "problems": problems, "busy": busy_count}


# ---- one run -----------------------------------------------------------------------


def final_state(path: str, engine: str) -> dict:
    connection = Connection(engine, path)
    try:
        check = connection.run("PRAGMA integrity_check")
        return {"check": check, "rows": [connection.run(sql) for sql in QUERIES]}
    finally:
        connection.close()


def run_seed(seed: int, mode: str, workers: int, operations: int, directory: str) -> str | None:
    """One run; returns a description of what went wrong, or None."""
    path = os.path.join(directory, f"c{seed}.db")
    start = path + ".start"
    create(path, mode)
    engines = ["minidb"] * workers if mode == "minidb" else [("minidb", "sqlite3")[i % 2] for i in range(workers)]
    processes = []
    for i, engine in enumerate(engines):
        out = f"{path}.w{i}"
        processes.append((out, subprocess.Popen(
            [sys.executable, __file__, "--worker", path, engine, f"w{i}", str(seed * 1000 + i), str(operations),
             mode, start, out], stderr=subprocess.PIPE, text=True)))
    with open(start, "w"):
        pass
    results = []
    for out, process in processes:
        try:
            _, stderr = process.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            process.kill()
            return f"seed {seed} ({mode}): a worker hung"
        if process.returncode != 0:
            return f"seed {seed} ({mode}): a worker failed\n{stderr[-3000:]}"
        with open(out, "rb") as f:
            results.append(pickle.load(f))
    problems = [p for r in results for p in r["problems"]]
    if problems:
        return f"seed {seed} ({mode}):\n  " + "\n  ".join(problems[:10])
    # What the committed changes add up to.
    balances = [BALANCE] * ACCOUNTS
    log = []
    for r in results:
        for worker, seq, amount, source, target in r["transfers"]:
            balances[source - 1] -= amount
            balances[target - 1] += amount
            log.append((worker, seq, amount, source, target))
    counters = {}
    for r in results:
        for key, increment in r["increments"]:
            counters[key] = counters.get(key, 0) + increment
    expected = [list(enumerate(balances, 1)), sorted(log), sorted(counters.items())]
    states = {}
    for engine in (("minidb",) if mode == "minidb" else ("minidb", "sqlite3")):
        state = final_state(path, engine)
        if state["check"] not in ([], [("ok",)]):
            return f"seed {seed} ({mode}): {engine} integrity check: {state['check'][:5]}"
        got = [[tuple(r) for r in rows] for rows in state["rows"][:3]]
        if got != expected:
            names = ("accounts", "log", "kv")
            wrong = [names[i] for i in range(3) if got[i] != expected[i]]
            return f"seed {seed} ({mode}): {engine} finds {', '.join(wrong)} differing from the committed changes"
        states[engine] = [[tuple(r) for r in rows] for rows in state["rows"]]
    if len(states) == 2 and states["minidb"] != states["sqlite3"]:
        differing = [QUERIES[i] for i in range(len(QUERIES)) if states["minidb"][i] != states["sqlite3"][i]]
        return f"seed {seed} ({mode}): sqlite3 and MiniDB answer differently: {differing}"
    return None


def parse_range(text: str) -> list[int]:
    seeds = []
    for part in text.split(","):
        start, _, end = part.partition("-")
        seeds.extend(range(int(start), int(end or start) + 1))
    return seeds


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        path, engine, name, seed, operations, mode, start, out = sys.argv[2:]
        result = work(path, engine, name, int(seed), int(operations), mode, start)
        with open(out, "wb") as f:
            pickle.dump(result, f)
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", default="0-4")
    parser.add_argument("--mode", choices=["journal", "wal", "minidb"], default="wal")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--operations", type=int, default=150, help="per worker")
    args = parser.parse_args()
    seeds = parse_range(args.seeds)
    failures = 0
    for seed in seeds:
        with tempfile.TemporaryDirectory() as directory:
            failure = run_seed(seed, args.mode, args.workers, args.operations, directory)
        if failure is not None:
            failures += 1
            print(failure, flush=True)
    print(f"{len(seeds)} seeds x {args.workers} processes x {args.operations} operations ({args.mode}): "
          f"{failures} failing seeds")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
