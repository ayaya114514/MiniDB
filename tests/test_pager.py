import pytest

from minidb.pager import PAGE_SIZE, DatabaseError, Pager, RawPage
from minidb.record import RecordError, decode_record, encode_record


@pytest.mark.parametrize(
    "values",
    [
        [],
        [None],
        [0, 1, -1, 2**63 - 1, -(2**63)],
        [1.5, -0.0, 1e300],
        ["", "hello", "日本語", "a|b'c"],
        [1, "x", None, 2.5],
    ],
)
def test_record_round_trip(values):
    data = encode_record(values)
    decoded, end = decode_record(data)
    assert decoded == values
    assert end == len(data)


def test_record_rejects_unsupported_values():
    with pytest.raises(RecordError):
        encode_record([2**63])
    with pytest.raises(RecordError):
        encode_record([b"bytes"])
    with pytest.raises(RecordError):
        encode_record([True])


def test_new_file_has_header_page(tmp_path):
    path = tmp_path / "t.db"
    pager = Pager(str(path))
    assert pager.page_count == 1
    pager.close()
    assert path.stat().st_size == PAGE_SIZE


def test_pages_survive_close_and_reopen(tmp_path):
    path = str(tmp_path / "t.db")
    pager = Pager(path)
    pages = [pager.allocate(RawPage) for _ in range(5)]
    for i, page in enumerate(pages):
        page.data[:5] = b"page%d" % i
    pager.close()

    pager = Pager(path)
    assert pager.page_count == 6
    for i, page in enumerate(pages):
        assert pager.get(page.pgno, RawPage).data[:5] == b"page%d" % i
    pager.close()


def test_page_cache_returns_same_object(tmp_path):
    pager = Pager(str(tmp_path / "t.db"))
    page = pager.allocate(RawPage)
    pager.close()
    pager = Pager(str(tmp_path / "t.db"))
    assert pager.get(page.pgno, RawPage) is pager.get(page.pgno, RawPage)


def test_free_list_reuses_pages():
    pager = Pager()
    a = pager.allocate(RawPage)
    b = pager.allocate(RawPage)
    pager.free(a.pgno)
    pager.free(b.pgno)
    assert pager.free_page_count() == 2
    assert pager.allocate(RawPage).pgno == b.pgno
    assert pager.allocate(RawPage).pgno == a.pgno
    assert pager.allocate(RawPage).pgno == 3
    assert pager.free_page_count() == 0


def test_free_list_persists(tmp_path):
    path = str(tmp_path / "t.db")
    pager = Pager(path)
    pages = [pager.allocate(RawPage) for _ in range(3)]
    pager.free(pages[1].pgno)
    pager.close()
    pager = Pager(path)
    assert pager.free_page_count() == 1
    assert pager.allocate(RawPage).pgno == pages[1].pgno
    pager.close()


def test_out_of_range_page(tmp_path):
    pager = Pager()
    with pytest.raises(DatabaseError):
        pager.get(1, RawPage)


def test_rejects_non_database_file(tmp_path):
    path = tmp_path / "junk.db"
    path.write_bytes(b"x" * PAGE_SIZE)
    with pytest.raises(DatabaseError):
        Pager(str(path))


def test_rejects_partial_page_file(tmp_path):
    path = tmp_path / "junk.db"
    path.write_bytes(b"x" * 100)
    with pytest.raises(DatabaseError):
        Pager(str(path))


@pytest.mark.parametrize("values", [
    [0, 1, -1, 2, 127, 128, -128, -129, 32767, 32768, -32768, -32769,
     2**31 - 1, 2**31, -(2**31), -(2**31) - 1, 2**63 - 1, -(2**63)],
    ["x" * 239, "y" * 240, "z" * 100_000, "é" * 120, "日本" * 500],
    [None] * 300 + ["wide"],  # the header is longer than 254 bytes
    [0.0, -0.0, 1e308, float("inf"), 5e-324],
])
def test_record_format_edge_cases(values):
    data = encode_record(values)
    decoded, end = decode_record(data)
    assert end == len(data)
    assert [(type(v), v) for v in decoded] == [(type(v), v) for v in values]


def test_record_round_trip_random():
    import random
    rng = random.Random(5)
    for _ in range(3000):
        values = [
            rng.choice([None, rng.randint(-(2**63), 2**63 - 1), rng.randint(-300, 300),
                        rng.random() * 1e6, "".join(rng.choice("aé日") for _ in range(rng.randint(0, 300)))])
            for _ in range(rng.randint(0, 12))
        ]
        data = encode_record(values)
        assert decode_record(data + b"trailing")[0] == values


def test_small_values_take_little_space():
    assert len(encode_record([0, 1, None])) == 4  # header size + three codes
    assert len(encode_record([100, "abc"])) == 1 + 2 + 1 + 3


def test_corrupt_record_is_reported():
    data = encode_record(["hello", 12345678])
    with pytest.raises(RecordError):
        decode_record(data[:-3])
    with pytest.raises(RecordError):
        decode_record(bytes([1, 9]))  # unknown type code


def test_encoded_size_matches_encoding():
    import random
    from minidb.record import encoded_size
    rng = random.Random(9)
    for _ in range(3000):
        values = [
            rng.choice([None, 0, 1, rng.randint(-(2**63), 2**63 - 1), rng.randint(-40000, 40000),
                        rng.random(), "".join(rng.choice("aé日") for _ in range(rng.randint(0, 400)))])
            for _ in range(rng.randint(0, 300 if rng.random() < 0.05 else 8))
        ]
        assert encoded_size(values) == len(encode_record(values))
