"""Crash recovery fuzzing for SQLite-format files.

A MiniDB connection runs random transactions on a SQLite-format file (with
the rollback journal, or in WAL mode) and the power fails at a random
fsync or unlink.  Every write to a file since that file's last fsync may
then be lost, kept or torn at 512-byte sectors, in any combination, and a
file's growth or truncation may be lost too (creating and deleting a file
count as done at once).  sqlite3 (in another process) and MiniDB each
recover their own copy of what is left; both must end up with the same,
intact database holding the last acknowledged commit or the one that was
in flight - and must be able to write to it.

    .venv/bin/python tests/crash_fuzz.py --seeds 0-99 [--wal] [--trials 8]
"""

from __future__ import annotations

import argparse
import base64
import os
import pickle
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minidb.database import Database  # noqa: E402

SECTOR = 512
SUFFIXES = ("", "-journal", "-wal")  # the files whose contents must survive (-shm need not)
DUMPS = ("SELECT id, v, w FROM t ORDER BY id", "SELECT k, n FROM w ORDER BY k")
SCHEMA = ("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, w BLOB)", "CREATE INDEX tv ON t (v)",
          "CREATE TABLE w (k TEXT PRIMARY KEY, n INTEGER) WITHOUT ROWID")


class SimulatedCrash(BaseException):
    """The power failed (raised from the fsync or unlink it failed at)."""


class Disk:
    """Keeps what is durable of each database file: its contents at its
    last fsync.  ``crash_after``: the number of fsyncs and unlinks that
    happen before the power fails (None: never)."""

    def __init__(self, path: str, crash_after: int | None = None) -> None:
        self.paths = [path + suffix for suffix in SUFFIXES]
        self.durable = {p: read(p) for p in self.paths}
        self.crash_after = crash_after
        self.operations = 0
        self.at_crash = None  # each file's contents when the power failed

    def _opportunity(self) -> None:
        if self.crash_after is not None and self.operations >= self.crash_after:
            self.at_crash = {p: read(p) for p in self.paths + [self.paths[0] + "-shm"]}
            raise SimulatedCrash(f"after {self.operations} operations")
        self.operations += 1

    def fsync(self, fd: int) -> None:
        self._opportunity()
        st = os.fstat(fd)
        for p in self.paths:
            try:
                other = os.stat(p)
            except FileNotFoundError:
                continue
            if (other.st_dev, other.st_ino) == (st.st_dev, st.st_ino):
                self.durable[p] = read(p)
        # (No real fsync: nothing here needs to survive a real power failure.)

    def unlink(self, path: str, *args: object, **kwargs: object) -> None:
        self._opportunity()
        real_unlink(path, *args, **kwargs)
        if path in self.durable:
            self.durable[path] = None

    def crash(self, rng: random.Random) -> None:
        """Replace each file by something the power failure could have left."""
        keep = rng.choice([0.0, 0.5, 0.5, 1.0])  # how likely an unsynced sector made it to the disk
        for p in self.paths:
            current = self.at_crash.get(p)
            if current is None:
                if os.path.exists(p):
                    real_unlink(p)
                continue
            base = self.durable[p] or b""  # (a file created since: there, but maybe empty)
            size = max(len(base), len(current))
            image = bytearray(base.ljust(size, b"\x00"))
            for start in range(0, size, SECTOR):
                new = current[start:start + SECTOR]
                if new != base[start:start + SECTOR] and rng.random() < keep:
                    image[start:start + len(new)] = new
            length = len(current) if len(current) == len(base) or rng.random() < max(keep, 0.2) else len(base)
            write(p, bytes(image[:length]).ljust(length, b"\x00"))
        shm = self.paths[0] + "-shm"
        if os.path.exists(shm) and rng.random() < 0.5:
            real_unlink(shm)  # (-shm is never durable: it may be gone, or hold what was last written)


def read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


real_fsync, real_unlink, real_remove = os.fsync, os.unlink, os.remove


class Workload:
    """The transactions of one seed, and the database states they lead to
    (computed with sqlite3 in memory)."""

    def __init__(self, seed: int, wal: bool) -> None:
        rng = random.Random(seed)
        self.wal = wal
        self.page_size = rng.choice([512, 1024, 4096])
        self.auto_vacuum = rng.choice([0, 0, 1, 2])
        self.autocheckpoint = rng.choice([0, 4, 20, 1000])
        self.transactions = []
        for _ in range(rng.randint(8, 20)):
            roll = rng.random()
            if roll < 0.06:
                self.transactions.append([("VACUUM", ())])
            elif roll < 0.09 and not wal:  # (the whole file goes to the journal, in the old page size)
                self.transactions.append([(f"PRAGMA page_size = {rng.choice([512, 1024, 2048, 4096])}", ()),
                                          ("VACUUM", ())])
            elif roll < 0.12 and wal:
                self.transactions.append([(f"PRAGMA wal_checkpoint({rng.choice(['PASSIVE', 'RESTART', 'TRUNCATE'])})", ())])
            elif roll < 0.16 and self.auto_vacuum == 2:
                self.transactions.append([("PRAGMA incremental_vacuum(3)", ())])
            else:
                statements = [self.statement(rng) for _ in range(rng.randint(1, 4))]
                if rng.random() < 0.1:
                    statements.append(("ROLLBACK", ()))
                self.transactions.append(statements)
        self.readers = [rng.random() < 0.3 for _ in self.transactions] if wal else [False] * len(self.transactions)
        reference = sqlite3.connect(":memory:", isolation_level=None)
        for sql in SCHEMA:
            reference.execute(sql)
        self.states = [dump(reference)]  # after each transaction
        for statements in self.transactions:
            if statements[-1][0] != "ROLLBACK":
                for sql, parameters in statements:
                    if not sql.startswith(("VACUUM", "PRAGMA")):
                        reference.execute(sql, parameters)
            self.states.append(dump(reference))
        reference.close()

    @staticmethod
    def statement(rng: random.Random) -> tuple[str, tuple]:
        roll = rng.random()
        if roll < 0.35:
            return ("INSERT INTO t (v, w) VALUES (?, ?)",
                    (f"v{rng.randint(0, 40)}", rng.randbytes(rng.choice([10, 300, 3000]))))
        if roll < 0.5:
            return ("UPDATE t SET w = ?, v = v || 'u' WHERE id % 5 = ?",
                    (rng.randbytes(rng.choice([20, 1500])), rng.randint(0, 4)))
        if roll < 0.62:
            return "DELETE FROM t WHERE id % 7 = ?", (rng.randint(0, 6),)
        if roll < 0.85:
            return "INSERT OR REPLACE INTO w VALUES (?, ?)", (f"k{rng.randint(0, 30)}" * rng.choice([1, 40]),
                                                                 rng.randint(0, 99))
        return "DELETE FROM w WHERE n % 3 = ?", (rng.randint(0, 2),)

    def create(self, path: str) -> None:
        with Database(path, format="sqlite") as db:
            db.execute(f"PRAGMA page_size = {self.page_size}")
            db.execute(f"PRAGMA auto_vacuum = {self.auto_vacuum}")
            for sql in SCHEMA:
                db.execute(sql)
            if self.wal:
                db.execute("PRAGMA journal_mode = WAL")

    def run(self, path: str, disk: Disk) -> tuple[list, list]:
        """Run against ``path`` (power failing as ``disk`` says).  Returns
        the last acknowledged state and the one in flight."""
        writer = Database(path)
        writer.execute(f"PRAGMA wal_autocheckpoint = {self.autocheckpoint}")
        reader = Database(path) if self.wal else None
        reading = False
        acknowledged = in_flight = self.states[0]
        os.fsync, os.unlink, os.remove = disk.fsync, disk.unlink, disk.unlink
        try:
            for i, statements in enumerate(self.transactions):
                in_flight = self.states[i + 1]
                if statements[0][0].startswith(("VACUUM", "PRAGMA")):
                    for sql, parameters in statements:
                        writer.execute(sql, parameters)
                else:
                    writer.execute("BEGIN")
                    for sql, parameters in statements:
                        writer.execute(sql, parameters)
                    if statements[-1][0] != "ROLLBACK":
                        writer.execute("COMMIT")
                acknowledged = in_flight
                if self.readers[i]:  # a reader holds an old snapshot for a while
                    reader.execute("COMMIT" if reading else "BEGIN")
                    if not reading:
                        reader.execute("SELECT count(*) FROM t")
                    reading = not reading
            if reader is not None:
                reader.close()
                reader = None
            writer.close()  # (checkpoints and deletes the log: may fail too)
            writer = None
        except SimulatedCrash:
            pass
        finally:
            os.fsync, os.unlink, os.remove = real_fsync, real_unlink, real_remove
            for db in (writer, reader):
                if db is not None:
                    db.pager.close_files()
        return acknowledged, in_flight


def dump(connection: object) -> list:
    rows = []
    for sql in DUMPS:
        result = connection.execute(sql)
        rows.append([tuple(r) for r in (result.fetchall() if hasattr(result, "fetchall") else result)])
    return rows


CHILD = textwrap.dedent("""
    import base64, pickle, sqlite3, sys
    connection = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=10)
    out = {}
    try:
        out["check"] = connection.execute("PRAGMA integrity_check").fetchall()
        out["rows"] = [connection.execute(sql).fetchall() for sql in %r]
        connection.execute("INSERT INTO t (v) VALUES ('after the crash')")
        out["after"] = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        out["error"] = repr(exc)
    connection.close()
    print(base64.b64encode(pickle.dumps(out)).decode())
""" % (DUMPS,))


def recover_with_sqlite(path: str) -> dict:
    done = subprocess.run([sys.executable, "-c", CHILD, path], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        return {"error": done.stderr[-2000:]}
    return pickle.loads(base64.b64decode(done.stdout))


def recover_with_minidb(path: str) -> dict:
    out = {}
    try:
        with Database(path) as db:
            out["check"] = db.integrity_check()
            out["rows"] = dump(db)
            db.execute("INSERT INTO t (v) VALUES ('after the crash')")
            out["after"] = db.integrity_check()
    except Exception as exc:  # noqa: BLE001 - reported as the failure
        out["error"] = repr(exc)
    return out


def copy_files(source: str, target: str) -> None:
    for suffix in SUFFIXES + ("-shm",):
        if os.path.exists(source + suffix):
            shutil.copyfile(source + suffix, target + suffix)


def run_seed(seed: int, wal: bool, trials: int, directory: str) -> str | None:
    """Crash the workload of ``seed`` at ``trials`` random points; returns a
    description of the first failure, or None."""
    workload = Workload(seed, wal)
    path = os.path.join(directory, f"dry{seed}.db")
    workload.create(path)
    dry = Disk(path)
    workload.run(path, dry)
    total = dry.operations
    rng = random.Random(seed * 7919 + wal)
    for trial in range(trials):
        path = os.path.join(directory, f"s{seed}t{trial}.db")
        workload.create(path)
        crash_after = rng.randrange(total + 1)
        disk = Disk(path, crash_after)
        acknowledged, in_flight = workload.run(path, disk)
        if disk.at_crash is None:
            continue  # (the workload took fewer steps this time)
        disk.crash(rng)
        lite_path, mini_path = path + ".lite", path + ".mini"
        copy_files(path, lite_path)
        copy_files(path, mini_path)
        lite = recover_with_sqlite(lite_path)
        mini = recover_with_minidb(mini_path)
        where = f"seed {seed}{' (WAL)' if wal else ''}, trial {trial}: power failed after {crash_after} of {total}"
        for name, result in (("sqlite3", lite), ("MiniDB", mini)):
            if "error" in result:
                return f"{where}\n  {name} failed: {result['error']}"
            if result["check"] not in ([("ok",)], []) or result["after"] not in ([("ok",)], []):
                return f"{where}\n  {name} integrity: {result['check']} / after a write: {result['after']}"
        if lite["rows"] != mini["rows"]:
            return f"{where}\n  sqlite3 and MiniDB recovered different states"
        if lite["rows"] not in (acknowledged, in_flight):
            return f"{where}\n  the recovered state is neither the acknowledged nor the in-flight one"
    return None


def parse_range(text: str) -> list[int]:
    seeds = []
    for part in text.split(","):
        start, _, end = part.partition("-")
        seeds.extend(range(int(start), int(end or start) + 1))
    return seeds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", default="0-19")
    parser.add_argument("--wal", action="store_true", help="the file is in WAL mode")
    parser.add_argument("--trials", type=int, default=8, help="power failures per seed")
    args = parser.parse_args()
    failures = 0
    seeds = parse_range(args.seeds)
    for seed in seeds:
        with tempfile.TemporaryDirectory() as directory:
            failure = run_seed(seed, args.wal, args.trials, directory)
        if failure is not None:
            failures += 1
            print(failure, flush=True)
    print(f"{len(seeds)} seeds x {args.trials} power failures{' (WAL)' if args.wal else ''}: "
          f"{failures} failing seeds")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
