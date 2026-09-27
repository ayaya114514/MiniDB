import random

import pytest

from minidb.btree import BTree, BTreeError, DuplicateKeyError
from minidb.pager import Pager


def value_for(key, width=0):
    return (b"v%d:" % key).ljust(width, b"x")


def make_tree(capacity=4096):
    pager = Pager()
    return pager, BTree.create(pager, capacity=capacity)


def test_empty_tree():
    _, tree = make_tree()
    assert tree.get(1) is None
    assert list(tree.scan()) == []
    assert len(tree) == 0
    assert tree.check() == 0
    assert tree.last_key() is None


def test_insert_and_get():
    _, tree = make_tree()
    for key in [5, 1, 9, -3]:
        tree.insert(key, value_for(key))
    assert [tree.get(k) for k in [5, 1, 9, -3]] == [value_for(k) for k in [5, 1, 9, -3]]
    assert tree.get(2) is None
    assert 9 in tree and 2 not in tree
    assert tree.keys() == [-3, 1, 5, 9]


def test_duplicate_key():
    _, tree = make_tree()
    tree.insert(1, b"a")
    with pytest.raises(DuplicateKeyError):
        tree.insert(1, b"b")
    tree.insert(1, b"c", replace=True)
    assert tree.get(1) == b"c"
    assert len(tree) == 1


def test_leaf_split_creates_root():
    _, tree = make_tree(capacity=128)
    for key in range(20):
        tree.insert(key, b"")
    assert tree.depth() >= 2
    assert tree.check() == 20
    assert tree.keys() == list(range(20))


@pytest.mark.parametrize("capacity", [128, 256, 4096])
@pytest.mark.parametrize("order", ["ascending", "descending", "random"])
def test_insert_many_keeps_invariants(capacity, order):
    _, tree = make_tree(capacity)
    keys = list(range(10_000))
    if order == "descending":
        keys.reverse()
    elif order == "random":
        random.Random(capacity).shuffle(keys)
    for key in keys:
        tree.insert(key, value_for(key, 6))
    assert tree.check() == 10_000
    assert tree.keys() == list(range(10_000))
    assert all(tree.get(k) == value_for(k, 6) for k in range(0, 10_000, 97))
    if capacity == 128:
        assert tree.depth() >= 5


@pytest.mark.parametrize("capacity", [128, 512, 4096])
def test_random_insert_and_delete(capacity):
    rng = random.Random(42 + capacity)
    _, tree = make_tree(capacity)
    model = {}
    keys = list(range(12_000))
    rng.shuffle(keys)
    for key in keys:
        model[key] = value_for(key, rng.randint(0, 10))
        tree.insert(key, model[key])
    rng.shuffle(keys)
    for i, key in enumerate(keys[:10_000]):
        assert tree.delete(key)
        del model[key]
        if i % 2000 == 0:
            assert tree.check() == len(model)
    assert not tree.delete(keys[0])
    assert tree.check() == len(model)
    assert list(tree.scan()) == sorted(model.items())


def test_delete_everything_shrinks_to_single_leaf():
    rng = random.Random(7)
    pager, tree = make_tree(capacity=128)
    keys = list(range(3000))
    rng.shuffle(keys)
    for key in keys:
        tree.insert(key, b"xy")
    pages_used = pager.page_count - pager.free_page_count()
    rng.shuffle(keys)
    for key in keys:
        tree.delete(key)
    assert tree.check() == 0
    assert tree.depth() == 1
    # Everything except the header and the root page is back on the free list.
    assert pager.page_count - pager.free_page_count() == 2
    assert pages_used > 100


def test_interleaved_operations_against_dict():
    rng = random.Random(1234)
    _, tree = make_tree(capacity=160)
    model = {}
    for step in range(30_000):
        key = rng.randint(-2000, 2000)
        action = rng.random()
        if action < 0.5:
            value = value_for(key, rng.randint(0, 12))
            tree.insert(key, value, replace=True)
            model[key] = value
        elif action < 0.9:
            assert tree.delete(key) == (key in model)
            model.pop(key, None)
        else:
            assert tree.get(key) == model.get(key)
        if step % 5000 == 0:
            assert tree.check() == len(model)
    assert tree.check() == len(model)
    assert list(tree.scan()) == sorted(model.items())


def test_range_scan():
    _, tree = make_tree(capacity=128)
    for key in range(0, 1000, 2):
        tree.insert(key, b"")
    assert [k for k, _ in tree.scan(100, 110)] == [100, 102, 104, 106, 108, 110]
    assert [k for k, _ in tree.scan(101, 109)] == [102, 104, 106, 108]
    assert [k for k, _ in tree.scan(100, 110, False, False)] == [102, 104, 106, 108]
    assert [k for k, _ in tree.scan(990)] == [990, 992, 994, 996, 998]
    assert [k for k, _ in tree.scan(end=4)] == [0, 2, 4]
    assert [k for k, _ in tree.scan(end=4, end_inclusive=False)] == [0, 2]
    assert list(tree.scan(2000)) == []
    assert list(tree.scan(5, 3)) == []


def test_range_scan_matches_model_randomly():
    rng = random.Random(99)
    _, tree = make_tree(capacity=200)
    keys = sorted(rng.sample(range(100_000), 5000))
    for key in keys:
        tree.insert(key, b"")
    for _ in range(300):
        lo, hi = sorted(rng.sample(range(-10, 100_010), 2))
        lo_inc, hi_inc = rng.random() < 0.5, rng.random() < 0.5
        expected = [
            k for k in keys
            if (k > lo or (lo_inc and k == lo)) and (k < hi or (hi_inc and k == hi))
        ]
        assert [k for k, _ in tree.scan(lo, hi, lo_inc, hi_inc)] == expected


def test_overflow_values():
    pager, tree = make_tree()
    big = {k: bytes([k % 256]) * (k * 1000) for k in range(1, 30)}
    for key, value in big.items():
        tree.insert(key, value)
    assert all(tree.get(k) == v for k, v in big.items())
    assert tree.check() == len(big)
    used = pager.page_count - pager.free_page_count()
    for key in big:
        tree.delete(key)
    assert pager.page_count - pager.free_page_count() == 2
    assert used > 100


def test_overflow_with_small_capacity_and_replace():
    _, tree = make_tree(capacity=128)
    for key in range(500):
        tree.insert(key, b"z" * (key % 40))
    for key in range(0, 500, 3):
        tree.insert(key, b"q" * (key % 17), replace=True)
    assert tree.check() == 500
    for key in range(500):
        expected = b"q" * (key % 17) if key % 3 == 0 else b"z" * (key % 40)
        assert tree.get(key) == expected


def test_persistence(tmp_path):
    path = str(tmp_path / "tree.db")
    pager = Pager(path)
    tree = BTree.create(pager, capacity=256)
    rng = random.Random(5)
    keys = rng.sample(range(1_000_000), 5000)
    for key in keys:
        tree.insert(key, value_for(key, 30))
    root = tree.root
    pager.close()

    pager = Pager(path)
    tree = BTree(pager, root, capacity=256)
    assert tree.check() == 5000
    assert tree.keys() == sorted(keys)
    assert tree.get(keys[0]) == value_for(keys[0], 30)
    pager.close()


def test_key_too_large_for_capacity():
    from minidb.record import encode_record

    class RecordKey:
        encode = staticmethod(lambda key: encode_record(list(key)))
        size = staticmethod(lambda key: len(encode_record(list(key))))

    _, tree = make_tree()
    tree.codec = RecordKey
    with pytest.raises(BTreeError):
        tree.insert(("x" * 1000,), b"")


def test_destroy_and_clear_free_pages():
    pager, tree = make_tree(capacity=128)
    for key in range(2000):
        tree.insert(key, b"v" * (key % 50))
    tree.clear()
    assert tree.check() == 0
    assert pager.page_count - pager.free_page_count() == 2
    for key in range(2000):
        tree.insert(key, b"")
    tree.destroy()
    assert pager.page_count - pager.free_page_count() == 1


def test_dump():
    _, tree = make_tree(capacity=128)
    for key in range(30):
        tree.insert(key, b"")
    lines = tree.dump()
    assert lines[0].startswith("- internal (page 1")
    assert any(line.startswith("  - leaf") for line in lines)
