"""A harsher crash model than test_transactions: a crash (power loss) may
lose any write that was not fsynced, apply writes out of order, and tear a
write apart at 512-byte sector boundaries.  Whatever happens, the reopened
database must be intact and hold the last acknowledged commit, or the one
that was in flight."""

import os
import random

import pytest

from minidb.database import Database
from test_transactions import SimulatedCrash

SECTOR = 512


class Disk:
    """Records the writes to some files since their last fsync, and makes up
    what a crash could leave of them."""

    def __init__(self, crash_after=None):
        self.crash_after = crash_after  # crash when this many operations were done
        self.operations = 0
        self.files = {}  # fd -> [path, durable content, pending operations]

    def track(self, raw):
        with open(raw.name, "rb") as f:
            self.files[raw.fileno()] = [raw.name, f.read(), []]
        return Recorder(raw, self)

    def record(self, raw, operation):
        if self.crash_after is not None and self.operations >= self.crash_after:
            raise SimulatedCrash(f"after {self.operations} operations")
        self.operations += 1
        self.files[raw.fileno()][2].append(operation)

    def fsync(self, fd):
        entry = self.files.get(fd)
        if entry is not None:
            if self.crash_after is not None and self.operations >= self.crash_after:
                raise SimulatedCrash(f"after {self.operations} operations")
            self.operations += 1
            with open(entry[0], "rb") as f:
                entry[1] = f.read()
            entry[2] = []

    def crash(self, rng):
        """Replace every file by one thing a crash could have left."""
        for path, durable, pending in self.files.values():
            content = bytearray(durable)
            for operation in pending:
                roll = rng.random()
                if operation[0] == "truncate":
                    if roll < 0.5:
                        del content[operation[1]:]
                        content.extend(bytes(operation[1] - len(content)))
                    continue
                _, offset, data = operation
                if roll < 0.3:
                    continue  # lost
                for start in range(0, len(data), SECTOR):
                    if roll < 0.6 or rng.random() < 0.5:  # whole, or torn: some sectors
                        chunk = data[start:start + SECTOR]
                        end = offset + start + len(chunk)
                        if len(content) < end:
                            content.extend(bytes(end - len(content)))
                        content[offset + start:end] = chunk
            with open(path, "wb") as f:
                f.write(content)


class Recorder:
    """A file object whose writes and truncations a Disk records."""

    def __init__(self, raw, disk):
        self.raw = raw
        self.disk = disk

    def write(self, data):
        self.disk.record(self.raw, ("write", self.raw.tell(), bytes(data)))
        return self.raw.write(data)

    def truncate(self, size=None):
        size = self.raw.tell() if size is None else size
        self.disk.record(self.raw, ("truncate", size))
        return self.raw.truncate(size)

    def __getattr__(self, name):
        return getattr(self.raw, name)


def transactions(seed):
    rng = random.Random(seed)
    result = []
    for i in range(24):
        if i % 9 == 8:
            result.append([("VACUUM", ())])
            continue
        statements = []
        for _ in range(rng.randint(1, 4)):
            roll = rng.random()
            if roll < 0.5:
                statements.append(("INSERT INTO t (v, w) VALUES (?, ?)",
                                   (f"v{rng.randint(0, 40)}", rng.randbytes(rng.choice([10, 300, 5000])))))
            elif roll < 0.75:
                statements.append(("UPDATE t SET w = ?, v = v || 'u' WHERE id % 5 = ?",
                                   (rng.randbytes(rng.choice([20, 3000])), rng.randint(0, 4))))
            else:
                statements.append(("DELETE FROM t WHERE id % 7 = ?", (rng.randint(0, 6),)))
        result.append(statements)
    return result


def dump(db):
    return db.execute("SELECT id, v, w FROM t ORDER BY id")


def run(path, seed, disk, monkeypatch):
    """Run the workload of ``seed`` against the file at ``path``, recording
    on ``disk``.  Returns the acknowledged state and the one in flight (the
    same if the workload finished)."""
    rng = random.Random(seed * 31 + 7)
    reference = Database()
    reference.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, w BLOB)")
    reference.execute("CREATE INDEX t_v ON t (v)")
    real_fsync = os.fsync

    def fsync(fd):
        disk.fsync(fd)
        real_fsync(fd)

    writer, reader = Database(path), Database(path)
    writer.pager.checkpoint_frames = 6
    writer.pager.restart_wait = 0  # the reader is in this thread
    writer.pager.file = disk.track(writer.pager.file)
    writer.pager.wal = disk.track(writer.pager.wal)
    monkeypatch.setattr(os, "fsync", fsync)
    acknowledged = in_flight = dump(reference)
    reading = False
    try:
        for statements in transactions(seed):
            for sql, parameters in statements:
                reference.execute(sql, parameters)
            in_flight = dump(reference)
            if statements[0][0] == "VACUUM":
                writer.execute("VACUUM")
            else:
                writer.execute("BEGIN")
                for sql, parameters in statements:
                    writer.execute(sql, parameters)
                writer.execute("COMMIT")
            acknowledged = in_flight
            if rng.random() < 0.3:  # a reader holds an old snapshot for a while
                if reading:
                    reader.execute("COMMIT")
                else:
                    reader.execute("BEGIN")
                    reader.execute("SELECT count(*) FROM t")
                reading = not reading
            if rng.random() < 0.2:
                writer.pager.checkpoint()
    except SimulatedCrash:
        pass
    finally:
        monkeypatch.setattr(os, "fsync", real_fsync)
        writer.pager.close_files()
        reader.pager.close_files()
    return acknowledged, in_flight


def setup(path):
    with Database(path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, w BLOB)")
        db.execute("CREATE INDEX t_v ON t (v)")


@pytest.mark.parametrize("seed", range(6))
def test_crash_with_lost_and_torn_writes(tmp_path, monkeypatch, seed):
    path = str(tmp_path / "dry.db")
    setup(path)
    dry = Disk()
    run(path, seed, dry, monkeypatch)
    total = dry.operations
    rng = random.Random(seed)
    for trial in range(12):
        path = str(tmp_path / f"db{trial}")
        setup(path)
        crash_after = rng.randrange(total + 1)
        disk = Disk(crash_after)
        acknowledged, in_flight = run(path, seed, disk, monkeypatch)
        disk.crash(rng)
        with Database(path) as db:
            state = dump(db)
            assert state in (acknowledged, in_flight), f"crash after {crash_after} of {total} operations"
            assert db.integrity_check() == []
            db.execute("INSERT INTO t (v) VALUES ('after the crash')")  # and it still works
        with Database(path) as db:
            assert db.integrity_check() == []
