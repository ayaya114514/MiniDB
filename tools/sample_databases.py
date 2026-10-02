"""Public SQLite sample databases for MiniDB's tests, downloaded on demand.

    python tools/sample_databases.py            # download (and check) all of them
    python tools/sample_databases.py chinook    # one

The files are data with their own licenses, so they are not stored in this
repository: they go into ``.sample-databases/`` (ignored by git), each pinned
to one release and checked against its SHA3-256 hash before use.

- Chinook 1.4.5 (https://github.com/lerocha/chinook-database, MIT): a music
  store - 11 tables, foreign keys and their indexes; 1024-byte pages.
- Northwind (https://github.com/jpwhite3/northwind-SQLite3, MIT): the classic
  trading company, enlarged to 600 000 order lines; AUTOINCREMENT, CHECK
  constraints, foreign keys and 16 views.
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECTORY = os.path.join(ROOT, ".sample-databases")

SAMPLES = {
    "chinook": (
        "https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_Sqlite.sqlite",
        "41c2e03ae0573bc11ff016d55670129baa3781e3a30f8ef857d51a4300eac137",
    ),
    "northwind": (
        "https://raw.githubusercontent.com/jpwhite3/northwind-SQLite3/"
        "4f56e7f5906dfd23b25244c5bfe8fb5da6402efd/dist/northwind.db",
        "0ee34124d7105ed6abc892c71e74a98240ca8b42f75a948e7758088a18052a3a",
    ),
}


def digest(path: str) -> str:
    sha3 = hashlib.sha3_256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            sha3.update(block)
    return sha3.hexdigest()


def fetch(name: str) -> str:
    """The path of sample ``name``, downloading it first if needed; raises
    OSError (no network ...) or ValueError (the content does not match)."""
    url, expected = SAMPLES[name]
    path = os.path.join(DIRECTORY, name + ".sqlite")
    if os.path.exists(path) and digest(path) == expected:
        return path
    os.makedirs(DIRECTORY, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "MiniDB-samples"})
    with urllib.request.urlopen(request, timeout=300) as response:
        data = response.read()
    actual = hashlib.sha3_256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"{url}: SHA3-256 {actual}, expected {expected}")
    partial = path + ".part"
    with open(partial, "wb") as f:
        f.write(data)
    os.replace(partial, path)
    return path


def main(argv: list[str]) -> None:
    for name in argv or sorted(SAMPLES):
        print(fetch(name))


if __name__ == "__main__":
    main(sys.argv[1:])
