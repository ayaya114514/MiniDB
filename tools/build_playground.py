"""Build the browser playground into ``site/`` (served by GitHub Pages).

    python tools/build_playground.py && python -m http.server -d site

The page runs MiniDB in Pyodide: ``site/minidb.zip`` holds the ``minidb``
package and ``playground/bridge.py``; nothing is sent anywhere.  ``__BUILD__``
in the page's files becomes a hash of the content, so browsers do not keep
stale copies.
"""

import hashlib
import io
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "playground")
TEXT_FILES = ["index.html", "app.js", "worker.js", "style.css"]


def build(target: str) -> str:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        package = os.path.join(ROOT, "minidb")
        for name in sorted(os.listdir(package)):
            if name.endswith(".py"):
                zf.write(os.path.join(package, name), f"minidb/{name}")
        zf.write(os.path.join(SOURCE, "bridge.py"), "bridge.py")
    digest = hashlib.sha256(archive.getvalue())
    for name in TEXT_FILES:
        with open(os.path.join(SOURCE, name), "rb") as f:
            digest.update(f.read())
    version = digest.hexdigest()[:12]
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "minidb.zip"), "wb") as f:
        f.write(archive.getvalue())
    for name in TEXT_FILES:
        with open(os.path.join(SOURCE, name), encoding="utf-8") as f:
            text = f.read().replace("__BUILD__", version)
        with open(os.path.join(target, name), "w", encoding="utf-8") as f:
            f.write(text)
    shutil.copy(os.path.join(SOURCE, "favicon.svg"), os.path.join(target, "favicon.svg"))
    return version


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "site")
    print(f"built {target} (version {build(target)})")
