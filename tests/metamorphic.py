"""Metamorphic testing: find query bugs without a reference database.

    .venv/bin/python tests/metamorphic.py --seeds 0-199 --queries 300

The differential fuzzer (fuzz.py) needs sqlite3 to know the right answer.
These checks, from the SQLancer project (Rigger & Su, 2020), need only
MiniDB: each runs a query in two ways that must agree however the rows are
stored, indexed or planned.

* **TLP** (ternary logic partitioning).  For any predicate ``p`` every row
  makes ``p`` true, false or NULL, so ``SELECT ... FROM f`` returns the same
  multiset as ``... WHERE p UNION ALL ... WHERE NOT p UNION ALL ... WHERE p
  IS NULL``.  Variants: DISTINCT (sets, with UNION), aggregates (MIN, MAX,
  COUNT and integer SUM of the three partitions combine into the whole), and
  HAVING (groups partitioned by a predicate on the group).
* **NoREC** (non-optimizing reference engine construction).  ``SELECT ...
  FROM f WHERE p`` lets the planner use ``p`` (row id and index ranges,
  multi-index OR, join order); ``SELECT ..., CASE WHEN p THEN 1 ELSE 0 END
  FROM f`` evaluates ``p`` on every row, and filtering on that column in
  Python gives what the first query must return.

A query that raises an error is skipped: a planner may legitimately never
evaluate ``p`` on a row that would have made it fail.
"""

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuzz import Generator, parse_range  # noqa: E402
from minidb import Database, Error  # noqa: E402


class LiteralGenerator(Generator):
    """The fuzzer's generator with literals only (no ``?`` parameters)."""

    def literal(self, text_safe=False):
        return self._literal(text_safe)


def typed(row):
    """A row as a hashable key that tells 1 from 1.0 (not a JSON text from
    a plain one, nor SQLite's IntReal from a REAL: which one a query gives
    may depend on its plan, as in SQLite)."""
    return tuple((_kind(v), v) for v in row)


def _kind(value):
    for base in (str, bytes, float):
        if isinstance(value, base):
            return base.__name__
    return type(value).__name__


def loose(row):
    """A row as a hashable key where equal numbers are equal (1 = 1.0):
    which of them DISTINCT keeps depends on the order it meets them in."""
    return tuple(("num", float(v)) if isinstance(v, (int, float)) else (type(v).__name__, v) for v in row)


def folded(row):
    """Like ``loose``, with texts compared as NOCASE and RTRIM would: of texts
    a collation makes equal, which one DISTINCT or a group shows depends on
    the order the rows come in."""
    return tuple(("num", float(v)) if isinstance(v, (int, float))
                 else ("str", v.lower().rstrip(" ")) if isinstance(v, str) else (type(v).__name__, v) for v in row)


class Mismatch(AssertionError):
    pass


class Checker:
    def __init__(self, seed, path=None):
        self.generator = LiteralGenerator(seed)
        self.rng = self.generator.rng
        self.db = Database(path)
        self.history = []
        self.checks = collections.Counter()  # oracle -> queries compared
        self.skipped = 0

    def close(self):
        self.db.close()

    def execute(self, sql):
        self.history.append(sql)
        return self.db.execute(sql)

    def try_execute(self, sql):
        try:
            return self.execute(sql)
        except Error:
            return None

    # ---- database -----------------------------------------------------------------

    def populate(self, rows):
        generator = self.generator
        for _ in range(self.rng.randint(2, 3)):
            self.execute(generator.create_table())
        for _ in range(self.rng.randint(1, 4)):
            self.try_execute(generator.create_index())
        for _ in range(rows):
            roll = self.rng.random()
            if roll < 0.85:
                self.try_execute(generator.insert())
            elif roll < 0.93:
                self.try_execute(generator.update())
            else:
                self.try_execute(generator.delete())
        if self.rng.random() < 0.5:
            self.execute("ANALYZE")
        # Values that are in the tables: comparisons with them hit the edges
        # of row id and index ranges, where off-by-one bugs live.
        self.samples = {}
        for table in generator.tables:
            names = table.column_names() + ([] if table.rowid_alias or not table.has_rowid else ["rowid"])
            for name in names:
                found = [row[0] for row in self.execute(f"SELECT {name} FROM {table.name}")]
                self.samples[table.name, name] = [v for v in found if v is not None]

    def from_clause(self):
        """A FROM clause over one or two tables, and its scope."""
        rng = self.rng
        tables = self.generator.tables
        scope = [("a", rng.choice(tables))]
        sql = f"{scope[0][1].name} AS a"
        if rng.random() < 0.35:
            other = rng.choice(tables)
            scope.append(("b", other))
            join = rng.choice(["JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN", ","])
            if join == ",":
                sql += f", {other.name} AS b"
            else:
                on = f"{self.generator.column(scope[1:])} = {self.generator.column(scope[:1])}"
                if rng.random() < 0.3:
                    on = self.generator.expr(scope, 1)
                sql += f" {join} {other.name} AS b ON {on}"
        return sql, scope

    def edge(self, scope):
        """``column op value`` with a value that occurs in the column."""
        rng = self.rng
        alias, table = rng.choice(scope)
        names = table.column_names() + ([] if table.rowid_alias or not table.has_rowid else ["rowid"])
        name = rng.choice(names)
        found = self.samples.get((table.name, name))
        if not found:
            return f"{alias}.{name} IS NULL"
        value = rng.choice(found)
        literal = repr(value) if not isinstance(value, str) else "'" + value.replace("'", "''") + "'"
        return f"{alias}.{name} {rng.choice(['=', '<', '<=', '>', '>=', '!='])} {literal}"

    def predicate(self, scope):
        rng = self.rng
        roll = rng.random()
        if roll < 0.35:
            return self.generator.condition(scope)  # includes planner-friendly comparisons
        if roll < 0.7:
            edges = [self.edge(scope) for _ in range(rng.randint(1, 2))]
            if rng.random() < 0.3:
                edges.append(self.generator.expr(scope, 2))
            return f" {rng.choice(['AND', 'OR'])} ".join(edges)
        return self.generator.expr(scope, 1)

    def items(self, scope, count):
        generator = self.generator
        return [generator.column(scope) if self.rng.random() < 0.6 else generator.expr(scope, 2)
                for _ in range(count)]

    # ---- oracles ------------------------------------------------------------------

    def compare(self, oracle, expected, actual, key, queries):
        self.checks[oracle] += 1
        if collections.Counter(map(key, expected)) != collections.Counter(map(key, actual)):
            missing = collections.Counter(map(key, expected)) - collections.Counter(map(key, actual))
            extra = collections.Counter(map(key, actual)) - collections.Counter(map(key, expected))
            raise Mismatch(
                f"{oracle}: results differ\n" + "\n".join(queries)
                + f"\nmissing: {list(missing.elements())[:10]}\nextra: {list(extra.elements())[:10]}"
            )

    def run_all(self, *queries):
        """The results of ``queries``, or None if any of them fails."""
        results = []
        for sql in queries:
            result = self.try_execute(sql)
            if result is None:
                self.skipped += 1
                return None
            results.append(result)
        return results

    def tlp_where(self):
        from_sql, scope = self.from_clause()
        p = self.predicate(scope)
        items = ", ".join(self.items(scope, self.rng.randint(1, 3)))
        base = f"SELECT {items} FROM {from_sql}"
        distinct = self.rng.random() < 0.25
        if distinct:
            base = base.replace("SELECT ", "SELECT DISTINCT ", 1)
        glue = " UNION " if distinct else " UNION ALL "
        partitioned = glue.join(
            f"{base} WHERE {condition}" for condition in (f"({p})", f"NOT ({p})", f"({p}) IS NULL")
        )
        results = self.run_all(base, partitioned)
        if results:
            oracle = "TLP distinct" if distinct else "TLP where"
            self.compare(oracle, results[0], results[1], folded if distinct else typed, [base, partitioned])

    def tlp_aggregate(self):
        from_sql, scope = self.from_clause()
        p = self.predicate(scope)
        function = self.rng.choice(["min", "max", "count", "sum"])
        argument = "*" if function == "count" and self.rng.random() < 0.4 else self.items(scope, 1)[0]
        whole = f"SELECT {function}({argument}) FROM {from_sql}"
        combine = {"min": "min", "max": "max", "count": "sum", "sum": "sum"}[function]
        conditions = (f"({p})", f"NOT ({p})", f"({p}) IS NULL")
        if function in ("min", "max"):
            # min() / max() over the partitions' rows: a column keeps its
            # collation through the UNION ALL, not through min() itself.
            partitioned = (f"SELECT {function}(x) FROM ("
                           + " UNION ALL ".join(f"SELECT {argument} AS x FROM {from_sql} WHERE {condition}"
                                                for condition in conditions)
                           + ") AS s")
        else:
            partitioned = (f"SELECT {combine}(x) FROM ("
                           + " UNION ALL ".join(
                               f"SELECT {function}({argument}) AS x FROM {from_sql} WHERE {condition}"
                               for condition in conditions)
                           + ") AS s")
        results = self.run_all(whole, partitioned)
        if not results:
            return
        expected, actual = results
        if function == "sum" and any(isinstance(r[0], float) for r in expected + actual):
            return  # REAL sums depend on the order of addition
        self.compare(f"TLP {function}", expected, actual, folded if function in ("min", "max") else loose,
                     [whole, partitioned])

    def tlp_having(self):
        from_sql, scope = self.from_clause()
        generator = self.generator
        groups = [generator.column(scope) for _ in range(self.rng.randint(1, 2))]
        aggregate = f"{self.rng.choice(['count', 'min', 'max'])}({generator.column(scope)})"
        terms = [f"{self.rng.choice(groups)} {self.rng.choice(['=', '<', '>', '<=', '>=', '!='])} "
                 f"{generator._literal()}",
                 f"count(*) {self.rng.choice(['>', '<=', '='])} {self.rng.randint(0, 3)}",
                 f"{aggregate} {self.rng.choice(['=', '<', '>', 'IS'])} {generator._literal()}"]
        p = f" {self.rng.choice(['AND', 'OR'])} ".join(self.rng.sample(terms, self.rng.randint(1, 3)))
        where = f" WHERE {self.predicate(scope)}" if self.rng.random() < 0.4 else ""
        base = f"SELECT {', '.join(groups)}, {aggregate} FROM {from_sql}{where} GROUP BY {', '.join(groups)}"
        partitioned = " UNION ALL ".join(
            f"{base} HAVING {condition}" for condition in (f"({p})", f"NOT ({p})", f"({p}) IS NULL")
        )
        results = self.run_all(base, partitioned)
        if results:
            self.compare("TLP having", results[0], results[1], folded, [base, partitioned])

    def norec(self):
        from_sql, scope = self.from_clause()
        p = self.predicate(scope)
        items = self.items(scope, self.rng.randint(1, 2))
        optimized = f"SELECT {', '.join(items)} FROM {from_sql} WHERE {p}"
        unoptimized = f"SELECT {', '.join(items)}, CASE WHEN {p} THEN 1 ELSE 0 END FROM {from_sql}"
        results = self.run_all(optimized, unoptimized)
        if results:
            kept = [row[:-1] for row in results[1] if row[-1] == 1]
            self.compare("NoREC", results[0], kept, typed, [optimized, unoptimized])

    def check(self):
        roll = self.rng.random()
        if roll < 0.35:
            self.tlp_where()
        elif roll < 0.55:
            self.tlp_aggregate()
        elif roll < 0.70:
            self.tlp_having()
        else:
            self.norec()


def run_seed(seed, queries, rows=120, path=None, verbose=False):
    """Check ``queries`` queries on a random database; returns None or a failure report,
    and the Checker's counts."""
    checker = Checker(seed, path)
    try:
        checker.populate(rows)
        for _ in range(queries):
            checker.check()
            if verbose:
                print(checker.history[-1])
    except Mismatch as exc:
        setup = [sql for sql in checker.history if not sql.startswith("SELECT")]
        return f"seed {seed}:\n{exc}\n--- setup ---\n" + ";\n".join(setup), checker
    finally:
        checker.close()
    return None, checker


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", default="0-99")
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--rows", type=int, default=120, help="data-changing statements per seed")
    parser.add_argument("--file", action="store_true", help="use database files instead of memory")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    import tempfile

    failures = 0
    checks = collections.Counter()
    skipped = 0
    seeds = parse_range(args.seeds)
    with tempfile.TemporaryDirectory() as directory:
        for seed in seeds:
            path = os.path.join(directory, f"meta{seed}.db") if args.file else None
            failure, checker = run_seed(seed, args.queries, args.rows, path, args.verbose)
            checks.update(checker.checks)
            skipped += checker.skipped
            if failure:
                failures += 1
                print(failure[:6000])
                print("=" * 70)
    print(", ".join(f"{name}: {count}" for name, count in sorted(checks.items())), f"(skipped {skipped})")
    print(f"{len(seeds)} seeds x {args.queries} queries: {failures} failing seeds")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
