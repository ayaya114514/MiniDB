"""Build the SQLite that the differential tests compare against.

    eval "$(python tools/reference_sqlite.py)"     # then run pytest / fuzz

MiniDB follows plain SQLite as released by sqlite.org.  The library that a
Python build links can differ from it: older versions (Ubuntu 24.04 ships
3.45) or extra compile options (conda-forge enables ICU, which makes
upper(), lower() and LIKE Unicode-aware).  This script downloads the pinned
amalgamation, checks its SHA3-256, compiles it into ``.reference-sqlite/``
with the default options and prints the environment variable that makes
Python's ``sqlite3`` module load it.  Needs a C compiler; nothing else.
"""

import hashlib
import io
import os
import subprocess
import sys
import urllib.request
import zipfile

VERSION = "3.53.4"
URL = "https://www.sqlite.org/2026/sqlite-amalgamation-3530400.zip"
SHA3_256 = "628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECTORY = os.path.join(ROOT, ".reference-sqlite")

if sys.platform == "darwin":
    NAMES = ["libsqlite3.dylib", "libsqlite3.0.dylib"]
    VARIABLE = "DYLD_LIBRARY_PATH"
    LINK = ["-dynamiclib", "-install_name", "@rpath/libsqlite3.dylib",
            "-compatibility_version", "9.0.0", "-current_version", "9.6.0"]
else:
    NAMES = ["libsqlite3.so.0", "libsqlite3.so"]
    VARIABLE = "LD_LIBRARY_PATH"
    LINK = ["-shared", "-Wl,-soname,libsqlite3.so.0", "-ldl"]


def build():
    library = os.path.join(DIRECTORY, NAMES[0])
    stamp = os.path.join(DIRECTORY, "version")
    if os.path.exists(library) and os.path.exists(stamp):
        with open(stamp) as f:
            if f.read() == VERSION:
                return
    os.makedirs(DIRECTORY, exist_ok=True)
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha3_256(data).hexdigest() != SHA3_256:
        sys.exit(f"checksum mismatch for {URL}")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = next(n for n in archive.namelist() if n.endswith("/sqlite3.c"))
        source = os.path.join(DIRECTORY, "sqlite3.c")
        with open(source, "wb") as f:
            f.write(archive.read(name))
    subprocess.run(
        ["cc", "-O2", "-fPIC", "-DSQLITE_ENABLE_MATH_FUNCTIONS", source, "-o", library,
         *LINK, "-lpthread", "-lm"],
        check=True,
    )
    for alias in NAMES[1:]:
        path = os.path.join(DIRECTORY, alias)
        if not os.path.exists(path):
            os.symlink(NAMES[0], path)
    with open(stamp, "w") as f:
        f.write(VERSION)


def check():
    """Fail unless a fresh interpreter picks the library up."""
    env = dict(os.environ)
    env[VARIABLE] = DIRECTORY
    code = "import sqlite3; print(sqlite3.sqlite_version)"
    found = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                           text=True, check=True).stdout.strip()
    if found != VERSION:
        sys.exit(f"{sys.executable} still loads SQLite {found} with {VARIABLE}={DIRECTORY}")


def main():
    build()
    check()
    print(f"export {VARIABLE}={DIRECTORY}")


if __name__ == "__main__":
    main()
