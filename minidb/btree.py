"""B+ tree stored in pager pages.

Every node occupies one page.  Leaves hold (key, value) pairs and are linked
left-to-right through ``next_leaf``; internal nodes hold n separator keys and
n + 1 child page numbers, where child i covers keys k with
``keys[i-1] <= k < keys[i]``.

Nodes are split and merged by their serialized size in bytes (not by key
count) so that keys and values may have variable length.  ``capacity`` is the
number of bytes a node may use; it defaults to the page size and tests shrink
it to build deep trees from few keys.  Values that would make a leaf cell too
large are moved to a chain of overflow pages.

The root node never moves: when it splits, its contents move to a new child
page, and when it shrinks to a single child, that child is copied back up.
So a tree is identified by its root page number for its whole life.

Page layouts (all integers big-endian):

    node header   u8 type (1 leaf, 2 internal) | u16 cell count |
                  u32 next leaf (leaf) or rightmost child (internal)
    leaf cell     key | u16 value length | value bytes
                  (length 0xFFFF: u32 total length | u32 first overflow page)
    internal cell u32 child | key
    overflow page u32 next overflow page | data
"""

import struct
from bisect import bisect_left, bisect_right

from minidb.errors import Error
from minidb.pager import PAGE_SIZE

LEAF, INTERNAL = 1, 2
HEADER_SIZE = 7
OVERFLOW_MARK = 0xFFFF
OVERFLOW_DATA_SIZE = PAGE_SIZE - 4

_header = struct.Struct(">BHI")
_u16 = struct.Struct(">H")
_u32 = struct.Struct(">I")
_i64 = struct.Struct(">q")
_overflow_ref = struct.Struct(">II")


class BTreeError(Error):
    pass


class DuplicateKeyError(BTreeError):
    pass


class IntKey:
    """Codec for 64-bit signed integer keys (table row ids)."""

    @staticmethod
    def encode(key):
        return _i64.pack(key)

    @staticmethod
    def decode(data, pos):
        return _i64.unpack_from(data, pos)[0], pos + 8

    @staticmethod
    def size(key):
        return 8


class OverflowRef:
    """A value stored in overflow pages: its total length and first page."""

    __slots__ = ("length", "pgno")

    def __init__(self, length, pgno):
        self.length = length
        self.pgno = pgno


def value_cell_size(value):
    return 10 if isinstance(value, OverflowRef) else 2 + len(value)


class Leaf:
    is_leaf = True

    def __init__(self, pgno, codec, keys=None, values=None, next_leaf=0):
        self.pgno = pgno
        self.codec = codec
        self.keys = keys if keys is not None else []
        self.values = values if values is not None else []
        self.next_leaf = next_leaf
        self.recompute_size()

    def recompute_size(self):
        size = self.codec.size
        self.size = HEADER_SIZE + sum(size(k) for k in self.keys) + sum(
            value_cell_size(v) for v in self.values
        )

    def copy(self):
        return Leaf(self.pgno, self.codec, list(self.keys), list(self.values), self.next_leaf)

    def to_bytes(self):
        out = bytearray(_header.pack(LEAF, len(self.keys), self.next_leaf))
        encode = self.codec.encode
        for key, value in zip(self.keys, self.values):
            out += encode(key)
            if isinstance(value, OverflowRef):
                out += _u16.pack(OVERFLOW_MARK)
                out += _overflow_ref.pack(value.length, value.pgno)
            else:
                out += _u16.pack(len(value))
                out += value
        return bytes(out.ljust(PAGE_SIZE, b"\x00"))


class Internal:
    is_leaf = False

    def __init__(self, pgno, codec, keys=None, children=None):
        self.pgno = pgno
        self.codec = codec
        self.keys = keys if keys is not None else []
        self.children = children if children is not None else []
        self.recompute_size()

    def recompute_size(self):
        size = self.codec.size
        self.size = HEADER_SIZE + sum(4 + size(k) for k in self.keys)

    def copy(self):
        return Internal(self.pgno, self.codec, list(self.keys), list(self.children))

    def to_bytes(self):
        out = bytearray(_header.pack(INTERNAL, len(self.keys), self.children[-1]))
        encode = self.codec.encode
        for child, key in zip(self.children, self.keys):
            out += _u32.pack(child)
            out += encode(key)
        return bytes(out.ljust(PAGE_SIZE, b"\x00"))


class NodeReader:
    """Decodes pages of one tree (whose keys use ``codec``) into node objects."""

    def __init__(self, codec):
        self.codec = codec

    def from_bytes(self, pgno, data):
        node_type, count, link = _header.unpack_from(data)
        decode = self.codec.decode
        pos = HEADER_SIZE
        keys = []
        if node_type == LEAF:
            values = []
            for _ in range(count):
                key, pos = decode(data, pos)
                (length,) = _u16.unpack_from(data, pos)
                pos += 2
                if length == OVERFLOW_MARK:
                    values.append(OverflowRef(*_overflow_ref.unpack_from(data, pos)))
                    pos += 8
                else:
                    values.append(bytes(data[pos:pos + length]))
                    pos += length
                keys.append(key)
            return Leaf(pgno, self.codec, keys, values, link)
        if node_type == INTERNAL:
            children = []
            for _ in range(count):
                children.append(_u32.unpack_from(data, pos)[0])
                key, pos = decode(data, pos + 4)
                keys.append(key)
            children.append(link)
            return Internal(pgno, self.codec, keys, children)
        raise BTreeError(f"page {pgno} is not a B+ tree node")


class OverflowPage:
    def __init__(self, pgno, next_page=0, data=b""):
        self.pgno = pgno
        self.next_page = next_page
        self.data = data

    @classmethod
    def from_bytes(cls, pgno, data):
        return cls(pgno, _u32.unpack_from(data)[0], bytes(data[4:]))

    def to_bytes(self):
        return (_u32.pack(self.next_page) + self.data).ljust(PAGE_SIZE, b"\x00")

    def copy(self):
        return OverflowPage(self.pgno, self.next_page, self.data)


def _tail_split(sizes, needed, lo, hi):
    """Largest m in [lo, hi] with sum(sizes[m:]) >= needed, else lo."""
    total = 0
    for m in range(len(sizes) - 1, lo - 1, -1):
        total += sizes[m]
        if total >= needed and m <= hi:
            return m
    return lo


def _balanced_split(sizes, lo, hi):
    """Index m in [lo, hi] where sum(sizes[:m]) is closest to half the total."""
    half = sum(sizes) / 2
    best, best_diff, prefix = lo, None, sum(sizes[:lo])
    for m in range(lo, hi + 1):
        diff = abs(prefix - half)
        if best_diff is None or diff < best_diff:
            best, best_diff = m, diff
        if m < len(sizes):
            prefix += sizes[m]
    return best


class BTree:
    def __init__(self, pager, root, codec=IntKey, capacity=PAGE_SIZE):
        self.pager = pager
        self.root = root
        self.codec = codec
        self.reader = NodeReader(codec)
        self.capacity = capacity
        self.min_fill = capacity // 4
        self.max_key_size = capacity // 8
        self.max_leaf_cell = capacity // 5

    @classmethod
    def create(cls, pager, codec=IntKey, capacity=PAGE_SIZE):
        """Allocate an empty tree and return it."""
        root = pager.allocate(Leaf, codec)
        return cls(pager, root.pgno, codec, capacity)

    def node(self, pgno):
        return self.pager.get(pgno, self.reader)

    # ---- lookups -------------------------------------------------------

    def _find_leaf(self, key):
        """Descend to the leaf that may hold ``key``; returns (path, leaf).

        ``path`` lists (internal node, child index) pairs from the root down.
        """
        path = []
        node = self.node(self.root)
        while not node.is_leaf:
            i = bisect_right(node.keys, key)
            path.append((node, i))
            node = self.node(node.children[i])
        return path, node

    def _leftmost_leaf(self):
        node = self.node(self.root)
        while not node.is_leaf:
            node = self.node(node.children[0])
        return node

    def get(self, key, default=None):
        _, leaf = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i < len(leaf.keys) and leaf.keys[i] == key:
            return self._load_value(leaf.values[i])
        return default

    def __contains__(self, key):
        _, leaf = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        return i < len(leaf.keys) and leaf.keys[i] == key

    def scan(self, start=None, end=None, start_inclusive=True, end_inclusive=True):
        """Yield (key, value) pairs in key order within the given bounds.

        The tree must not be modified while the generator is in use.
        """
        if start is None:
            leaf, i = self._leftmost_leaf(), 0
        else:
            _, leaf = self._find_leaf(start)
            find = bisect_left if start_inclusive else bisect_right
            i = find(leaf.keys, start)
        while True:
            keys = leaf.keys
            while i < len(keys):
                key = keys[i]
                if end is not None and (key > end or (key == end and not end_inclusive)):
                    return
                yield key, self._load_value(leaf.values[i])
                i += 1
            if not leaf.next_leaf:
                return
            leaf, i = self.node(leaf.next_leaf), 0

    def keys(self):
        return [key for key, _ in self.scan()]

    def __len__(self):
        count = 0
        leaf = self._leftmost_leaf()
        while True:
            count += len(leaf.keys)
            if not leaf.next_leaf:
                return count
            leaf = self.node(leaf.next_leaf)

    def last_key(self):
        """The largest key in the tree, or None when it is empty."""
        node = self.node(self.root)
        while not node.is_leaf:
            node = self.node(node.children[-1])
        return node.keys[-1] if node.keys else None

    # ---- values and overflow pages ------------------------------------

    def _store_value(self, value, key_size):
        if key_size + 2 + len(value) <= self.max_leaf_cell:
            return bytes(value)
        chunks = [value[i:i + OVERFLOW_DATA_SIZE] for i in range(0, len(value), OVERFLOW_DATA_SIZE)]
        pages = [self.pager.allocate(OverflowPage) for _ in chunks]
        for page, next_page, chunk in zip(pages, pages[1:] + [None], chunks):
            page.next_page = next_page.pgno if next_page else 0
            page.data = bytes(chunk)
        return OverflowRef(len(value), pages[0].pgno)

    def _load_value(self, value):
        if not isinstance(value, OverflowRef):
            return value
        parts = []
        pgno = value.pgno
        while pgno:
            page = self.pager.get(pgno, OverflowPage)
            parts.append(page.data)
            pgno = page.next_page
        return b"".join(parts)[:value.length]

    def _free_value(self, value):
        if isinstance(value, OverflowRef):
            pgno = value.pgno
            while pgno:
                next_page = self.pager.get(pgno, OverflowPage).next_page
                self.pager.free(pgno)
                pgno = next_page

    # ---- insertion -----------------------------------------------------

    def insert(self, key, value, replace=False):
        """Insert ``key`` -> ``value`` (bytes).

        Raises DuplicateKeyError if the key exists, unless ``replace`` is true.
        """
        key_size = self.codec.size(key)
        if key_size > self.max_key_size:
            raise BTreeError(f"key too large ({key_size} bytes)")
        path, leaf = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i < len(leaf.keys) and leaf.keys[i] == key:
            if not replace:
                raise DuplicateKeyError(key)
            self.delete(key)
            self.insert(key, value)
            return
        stored = self._store_value(value, key_size)
        self.pager.write(leaf)
        leaf.keys.insert(i, key)
        leaf.values.insert(i, stored)
        leaf.size += key_size + value_cell_size(stored)
        if leaf.size > self.capacity:
            # A new largest key (e.g. an auto-increment row id) is an append.
            append = leaf.next_leaf == 0 and i == len(leaf.keys) - 1
            self._split(path, leaf, append)

    def _split(self, path, node, append=False):
        """Split ``node`` (and then its ancestors) while it is over capacity.

        For appends every node on the path is the rightmost of its level;
        those are split unevenly (see ``_split_node``).
        """
        while node.size > self.capacity:
            if not path:
                node = self._move_root_down(node)
                path = [(self.node(self.root), 0)]
            parent, index = path.pop()
            separator, right = self._split_node(node, append)
            self.pager.write(parent)
            parent.keys.insert(index, separator)
            parent.children.insert(index + 1, right.pgno)
            parent.size += 4 + self.codec.size(separator)
            node = parent

    def _move_root_down(self, root):
        """Move the root's contents to a new page below a fresh one-child root."""
        if root.is_leaf:
            child = self.pager.allocate(Leaf, self.codec, root.keys, root.values, root.next_leaf)
        else:
            child = self.pager.allocate(Internal, self.codec, root.keys, root.children)
        self.pager.write(Internal(self.root, self.codec, [], [child.pgno]))
        return child

    def _split_node(self, node, append=False):
        """Split ``node`` by size; returns (separator key, new right node).

        Normally both halves get about the same number of bytes.  For an
        append the new right node gets just enough to reach ``min_fill``, so
        sequential inserts leave nodes about 75% full instead of 50%.
        """
        self.pager.write(node)
        key_size = self.codec.size
        needed = self.min_fill - HEADER_SIZE
        if node.is_leaf:
            sizes = [key_size(k) + value_cell_size(v) for k, v in zip(node.keys, node.values)]
            if append:
                m = _tail_split(sizes, needed, 1, len(sizes) - 1)
            else:
                m = _balanced_split(sizes, 1, len(sizes) - 1)
            right = self.pager.allocate(
                Leaf, self.codec, node.keys[m:], node.values[m:], node.next_leaf
            )
            del node.keys[m:], node.values[m:]
            node.next_leaf = right.pgno
            node.recompute_size()
            return right.keys[0], right
        sizes = [4 + key_size(k) for k in node.keys]
        if append:
            m = _tail_split(sizes, needed, 2, len(sizes) - 1) - 1
        else:
            m = _balanced_split(sizes, 1, len(sizes) - 2)
        separator = node.keys[m]
        right = self.pager.allocate(
            Internal, self.codec, node.keys[m + 1:], node.children[m + 1:]
        )
        del node.keys[m:], node.children[m + 1:]
        node.recompute_size()
        return separator, right

    # ---- deletion ------------------------------------------------------

    def delete(self, key):
        """Remove ``key``; returns False if it was not present."""
        path, leaf = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i == len(leaf.keys) or leaf.keys[i] != key:
            return False
        self.pager.write(leaf)
        value = leaf.values[i]
        self._free_value(value)
        del leaf.keys[i], leaf.values[i]
        leaf.size -= self.codec.size(key) + value_cell_size(value)
        self._rebalance(path, leaf)
        return True

    def _rebalance(self, path, node):
        """Fix underflow of ``node`` by merging with or borrowing from a sibling."""
        while path and node.size < self.min_fill:
            parent, index = path[-1]
            left_index = index - 1 if index > 0 else index
            left = self.node(parent.children[left_index])
            right = self.node(parent.children[left_index + 1])
            self.pager.write(parent)
            self.pager.write(left)
            self.pager.write(right)
            if self._try_merge(parent, left_index, left, right):
                path.pop()
                node = parent
                continue
            self._redistribute(parent, left_index, left, right)
            if parent.size > self.capacity:
                # The new separator may be longer than the old one.
                path.pop()
                self._split(path, parent)
            return
        if not path and not node.is_leaf and not node.keys:
            self._collapse_root(node)

    def _try_merge(self, parent, left_index, left, right):
        separator = parent.keys[left_index]
        separator_size = 4 + self.codec.size(separator)
        if left.is_leaf:
            if left.size + right.size - HEADER_SIZE > self.capacity:
                return False
            left.keys += right.keys
            left.values += right.values
            left.next_leaf = right.next_leaf
            left.size += right.size - HEADER_SIZE
        else:
            if left.size + right.size - HEADER_SIZE + separator_size > self.capacity:
                return False
            left.keys += [separator] + right.keys
            left.children += right.children
            left.size += right.size - HEADER_SIZE + separator_size
        self.pager.free(right.pgno)
        del parent.keys[left_index], parent.children[left_index + 1]
        parent.size -= separator_size
        return True

    def _redistribute(self, parent, left_index, left, right):
        key_size = self.codec.size
        old_separator = parent.keys[left_index]
        if left.is_leaf:
            keys = left.keys + right.keys
            values = left.values + right.values
            sizes = [key_size(k) + value_cell_size(v) for k, v in zip(keys, values)]
            m = _balanced_split(sizes, 1, len(keys) - 1)
            left.keys, left.values = keys[:m], values[:m]
            right.keys, right.values = keys[m:], values[m:]
            separator = right.keys[0]
        else:
            keys = left.keys + [old_separator] + right.keys
            children = left.children + right.children
            sizes = [4 + key_size(k) for k in keys]
            m = _balanced_split(sizes, 1, len(keys) - 2)
            left.keys, left.children = keys[:m], children[:m + 1]
            right.keys, right.children = keys[m + 1:], children[m + 1:]
            separator = keys[m]
        left.recompute_size()
        right.recompute_size()
        parent.keys[left_index] = separator
        parent.size += key_size(separator) - key_size(old_separator)

    def _collapse_root(self, root):
        """The root has a single child: copy that child into the root page."""
        child = self.node(root.children[0])
        if child.is_leaf:
            new_root = Leaf(self.root, self.codec, child.keys, child.values, child.next_leaf)
        else:
            new_root = Internal(self.root, self.codec, child.keys, child.children)
        self.pager.write(new_root)
        self.pager.free(child.pgno)

    # ---- whole-tree operations ------------------------------------------

    def destroy(self):
        """Free every page of the tree, including the root."""
        stack = [self.root]
        while stack:
            node = self.node(stack.pop())
            if node.is_leaf:
                for value in node.values:
                    self._free_value(value)
            else:
                stack.extend(node.children)
            self.pager.free(node.pgno)

    def clear(self):
        """Remove every key, leaving an empty root leaf."""
        root = self.node(self.root)
        if root.is_leaf:
            for value in root.values:
                self._free_value(value)
        else:
            for child in root.children:
                BTree(self.pager, child, self.codec, self.capacity).destroy()
        self.pager.write(Leaf(self.root, self.codec))

    def depth(self):
        depth, node = 1, self.node(self.root)
        while not node.is_leaf:
            depth += 1
            node = self.node(node.children[0])
        return depth

    def dump(self, max_keys=8):
        """Return the tree structure as indented text lines (the ``.btree`` command)."""
        lines = []

        def show(keys):
            text = ", ".join(repr(k) for k in keys[:max_keys])
            return text + (f", ... ({len(keys)} keys)" if len(keys) > max_keys else "")

        def visit(pgno, indent):
            node = self.node(pgno)
            pad = "  " * indent
            if node.is_leaf:
                lines.append(f"{pad}- leaf (page {pgno}, {len(node.keys)} keys): {show(node.keys)}")
                return
            lines.append(f"{pad}- internal (page {pgno}, {len(node.keys)} keys): {show(node.keys)}")
            for child in node.children:
                visit(child, indent + 1)

        visit(self.root, 0)
        return lines

    def check(self):
        """Verify the structural invariants; returns the number of keys.

        Checks key order and separator bounds, equal leaf depth, node sizes
        (at most ``capacity``, at least ``min_fill`` except the root), that
        internal nodes have one more child than keys, and that the leaf chain
        visits exactly the leaves in key order.
        """
        leaves = []
        leaf_depths = set()

        def visit(pgno, low, high, depth, is_root):
            node = self.node(pgno)
            node_size = node.size
            node.recompute_size()
            assert node.size == node_size, f"page {pgno}: size bookkeeping is off"
            assert node.size <= self.capacity, f"page {pgno}: over capacity"
            if not is_root:
                assert node.size >= self.min_fill, f"page {pgno}: underfull ({node.size})"
            keys = node.keys
            assert all(a < b for a, b in zip(keys, keys[1:])), f"page {pgno}: keys out of order"
            if keys:
                assert low is None or keys[0] >= low, f"page {pgno}: key below lower bound"
                assert high is None or keys[-1] < high, f"page {pgno}: key above upper bound"
            if node.is_leaf:
                assert len(node.values) == len(keys)
                leaf_depths.add(depth)
                leaves.append(node)
                return
            assert len(node.children) == len(keys) + 1, f"page {pgno}: child count"
            assert keys or is_root, f"page {pgno}: internal node without keys"
            assert not is_root or keys, "internal root must have at least one key"
            bounds = [low] + keys + [high]
            for i, child in enumerate(node.children):
                visit(child, bounds[i], bounds[i + 1], depth + 1, False)

        visit(self.root, None, None, 1, True)
        assert len(leaf_depths) == 1, "leaves at different depths"
        for leaf, following in zip(leaves, leaves[1:] + [None]):
            expected = following.pgno if following else 0
            assert leaf.next_leaf == expected, f"page {leaf.pgno}: broken sibling pointer"
        return sum(len(leaf.keys) for leaf in leaves)
