"""Damaged database files must raise DatabaseError, never crash or return wrong data."""

import random
import shutil

import pytest

from minidb.database import Database
from minidb.errors import DatabaseError
from minidb.pager import MAGIC, PAGE_SIZE


def build(path):
    db = Database(path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)")
    db.execute("CREATE INDEX t_n ON t (n)")
    db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, 'name{i}', {i % 17})" for i in range(3000)))
    db.execute("INSERT INTO big VALUES (1, ?), (2, ?)", ("a" * 30_000, "b" * 9_000))
    db.execute("DELETE FROM t WHERE id % 5 = 0")
    db.execute("CREATE TABLE junk (x TEXT)")
    db.execute("INSERT INTO junk VALUES " + ", ".join(f"('{'j' * 200}')" for _ in range(300)))
    db.execute("DROP TABLE junk")  # leaves free pages behind
    db.close()


def read_everything(path):
    db = Database(path)
    try:
        return (
            db.execute("SELECT * FROM t"),
            db.execute("SELECT id, n FROM t WHERE n = 3"),
            db.execute("SELECT id, length(body) FROM big"),
            db.execute("SELECT body FROM big"),
            db.integrity_check(),
        )
    finally:
        db.close()


@pytest.fixture(scope="module")
def original(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("corruption") / "original.db")
    build(path)
    return path, read_everything(path)


def test_original_is_consistent(original):
    _, contents = original
    assert contents[-1] == []


@pytest.mark.parametrize("seed", range(150))
def test_random_byte_flips(original, tmp_path, seed):
    source, expected = original
    rng = random.Random(seed)
    path = str(tmp_path / "damaged.db")
    shutil.copy(source, path)
    with open(path, "r+b") as f:
        data = bytearray(f.read())
        for _ in range(rng.choice([1, 1, 2, 8])):
            if rng.random() < 0.3:
                # hit the start of a page: node headers, counts, links
                offset = rng.randrange(len(data) // PAGE_SIZE) * PAGE_SIZE + rng.randrange(16)
            else:
                offset = rng.randrange(len(data))
            data[offset] ^= 1 << rng.randrange(8)
        f.seek(0)
        f.write(data)
    try:
        contents = read_everything(path)
    except DatabaseError:
        return
    # No query failed: the flips were in pages the queries never read (free
    # pages).  The data must be intact, and since every byte of the file is
    # covered by a page checksum, integrity_check must report the damage.
    assert contents[:-1] == expected[:-1]
    assert contents[-1] and all("bad checksum" in problem for problem in contents[-1])


@pytest.mark.parametrize("pages", [0.5, 1, 2, 7, 20])
def test_truncated_files(original, tmp_path, pages):
    source, _ = original
    path = str(tmp_path / "short.db")
    shutil.copy(source, path)
    with open(path, "r+b") as f:
        f.seek(0, 2)
        f.truncate(f.tell() - int(pages * PAGE_SIZE))
    with pytest.raises(DatabaseError, match="malformed|multiple of the page size"):
        read_everything(path)


def test_zeroed_page_is_detected(original, tmp_path):
    source, _ = original
    path = str(tmp_path / "zero.db")
    shutil.copy(source, path)
    with open(path, "r+b") as f:
        f.seek(5 * PAGE_SIZE)
        f.write(bytes(PAGE_SIZE))
    with pytest.raises(DatabaseError, match="bad checksum on page 5"):
        read_everything(path)


def test_damage_on_a_free_page_is_reported_by_integrity_check(original, tmp_path):
    source, expected = original
    path = str(tmp_path / "free.db")
    shutil.copy(source, path)
    db = Database(path)
    free_page = db.pager.header.freelist_head
    db.close()
    assert free_page
    with open(path, "r+b") as f:
        f.seek(free_page * PAGE_SIZE + 100)
        f.write(b"garbage")
    db = Database(path)
    assert db.execute("SELECT * FROM t") == expected[0]  # queries do not touch it...
    assert db.integrity_check() == [f"page {free_page}: bad checksum"]  # ...the check does
    db.close()


def test_not_a_database(tmp_path):
    for content in [b"hello" * 1000, bytes(PAGE_SIZE), b"SQLite format 3\x00" + bytes(PAGE_SIZE - 16)]:
        path = tmp_path / "junk.db"
        path.write_bytes(content)
        with pytest.raises(DatabaseError):
            Database(str(path))


def test_old_file_format_is_rejected_clearly(tmp_path):
    path = tmp_path / "old.db"
    path.write_bytes(b"MiniDB format 1\x00" + bytes(PAGE_SIZE - 16))
    with pytest.raises(DatabaseError, match="unsupported MiniDB file format: MiniDB format 1"):
        Database(str(path))
    assert MAGIC.startswith(b"MiniDB format 3")


def test_every_page_carries_a_checksum(original):
    source, _ = original
    with open(source, "rb") as f:
        data = f.read()
    import zlib
    for pgno in range(len(data) // PAGE_SIZE):
        page = data[pgno * PAGE_SIZE:(pgno + 1) * PAGE_SIZE]
        assert int.from_bytes(page[-4:], "big") == zlib.crc32(page[:-4])
