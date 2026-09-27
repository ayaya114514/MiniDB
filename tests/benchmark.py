"""Simple performance benchmark: MiniDB vs sqlite3 on 100,000 rows.

    .venv/bin/python tests/benchmark.py [--rows 100000]

Both engines work on a database file in a temporary directory and run the
same SQL.  Prints a Markdown table of wall-clock times.
"""

import argparse
import os
import random
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.database import Database  # noqa: E402

CITIES = ["beijing", "shanghai", "tokyo", "paris", "london", "berlin", "rome", "oslo"]


class Engine:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.open()

    def open(self):
        if self.name == "minidb":
            self.db = Database(self.path)
            self.run = self.db.execute
        else:
            self.db = sqlite3.connect(self.path, isolation_level=None)
            self.run = lambda sql, parameters=(): self.db.execute(sql, parameters).fetchall()

    def close(self):
        self.db.close()


def rows(n, seed=1):
    rng = random.Random(seed)
    return [
        (i, f"user{i:06d}", rng.randint(18, 90), rng.choice(CITIES))
        for i in range(1, n + 1)
    ]


def timed(results, label, engine, function):
    start = time.perf_counter()
    value = function()
    elapsed = time.perf_counter() - start
    results.setdefault(label, {})[engine.name] = elapsed
    return value


def benchmark(engine, n, results):
    data = rows(n)
    run = engine.run
    run("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, city TEXT)")

    def insert_single():
        run("BEGIN")
        for row in data:
            run("INSERT INTO people VALUES (%d, '%s', %d, '%s')" % row)
        run("COMMIT")

    timed(results, f"insert {n:,} rows, one INSERT each, one transaction", engine, insert_single)

    run("CREATE TABLE people3 (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, city TEXT)")

    def insert_parameters():
        run("BEGIN")
        for row in data:
            run("INSERT INTO people3 VALUES (?, ?, ?, ?)", row)
        run("COMMIT")

    timed(results, f"insert {n:,} rows, one INSERT each with ? parameters", engine, insert_parameters)

    run("CREATE TABLE people2 (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, city TEXT)")

    def insert_batched():
        run("BEGIN")
        for start in range(0, n, 1000):
            values = ", ".join("(%d, '%s', %d, '%s')" % row for row in data[start:start + 1000])
            run(f"INSERT INTO people2 VALUES {values}")
        run("COMMIT")

    timed(results, f"insert {n:,} rows, 1,000 per INSERT, one transaction", engine, insert_batched)

    run("CREATE TABLE autocommit (id INTEGER PRIMARY KEY, v TEXT)")

    def insert_autocommit():
        for i in range(1000):
            run(f"INSERT INTO autocommit VALUES ({i}, 'value {i}')")

    timed(results, "insert 1,000 rows, autocommit (a commit + fsync each)", engine, insert_autocommit)

    rng = random.Random(2)
    keys = [rng.randint(1, n) for _ in range(10_000)]

    def point_lookups():
        for key in keys:
            assert len(run(f"SELECT name FROM people WHERE id = {key}")) == 1

    timed(results, "10,000 primary key lookups", engine, point_lookups)

    def point_lookups_parameters():
        for key in keys:
            assert len(run("SELECT name FROM people WHERE id = ?", (key,))) == 1

    timed(results, "10,000 primary key lookups with ? parameter", engine, point_lookups_parameters)

    def range_scans():
        for key in keys[:100]:
            run(f"SELECT id, name FROM people WHERE id >= {key} AND id < {key + 1000}")

    timed(results, "100 primary key range scans (1,000 rows each)", engine, range_scans)

    timed(results, "full scan: count(*) WHERE age > 50", engine,
          lambda: run("SELECT count(*) FROM people WHERE age > 50"))
    timed(results, "full scan: SELECT * (all rows)", engine, lambda: run("SELECT * FROM people"))
    timed(results, "GROUP BY city with 3 aggregates", engine,
          lambda: run("SELECT city, count(*), avg(age), max(name) FROM people GROUP BY city"))
    timed(results, "ORDER BY age, name LIMIT 10", engine,
          lambda: run("SELECT * FROM people ORDER BY age, name LIMIT 10"))
    timed(results, "CREATE INDEX on age", engine,
          lambda: run("CREATE INDEX people_age ON people (age)"))

    def indexed_lookups():
        for age in range(18, 91):
            run(f"SELECT count(*) FROM people WHERE age = {age}")

    timed(results, "73 indexed lookups (age = ?, ~1,400 rows each)", engine, indexed_lookups)
    run("CREATE TABLE cities (name TEXT PRIMARY KEY, country TEXT)")
    run("INSERT INTO cities VALUES " + ", ".join(f"('{c}', 'c{i}')" for i, c in enumerate(CITIES)))
    timed(results, f"join {n:,} people with cities (index lookup per row)", engine,
          lambda: run("SELECT count(*) FROM people p JOIN cities c ON c.name = p.city"))
    engine.close()
    timed(results, "reopen and run one lookup", engine,
          lambda: (engine.open(), engine.run("SELECT name FROM people WHERE id = 7")))
    engine.close()
    results.setdefault("database file size (MB)", {})[engine.name] = os.path.getsize(engine.path) / 1e6


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=100_000)
    args = parser.parse_args()
    results = {}
    with tempfile.TemporaryDirectory() as directory:
        for name in ("minidb", "sqlite3"):
            engine = Engine(name, os.path.join(directory, f"{name}.db"))
            benchmark(engine, args.rows, results)
    print(f"| operation ({args.rows:,} rows) | MiniDB | sqlite3 | ratio |")
    print("|---|---:|---:|---:|")
    for label, times in results.items():
        mini, lite = times["minidb"], times["sqlite3"]
        if label.startswith("database file size"):
            print(f"| {label} | {mini:.1f} | {lite:.1f} | {mini / lite:.1f}x |")
        else:
            print(f"| {label} | {mini:.3f} s | {lite:.3f} s | {mini / lite:.0f}x |")


if __name__ == "__main__":
    main()
