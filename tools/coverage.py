"""Line coverage of the ``minidb`` package with only the standard library.

    .venv/bin/python tools/coverage.py [pytest arguments...]

Runs pytest under ``trace`` and reports, per module, the share of executable
statements that ran and the line numbers that did not.  Code that only runs
in child processes (the multi-process tests) is not counted.
"""

import ast
import os
import sys
import threading
import trace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "minidb")


def executable_lines(path):
    """Line numbers of the statements in a module (docstrings excluded)."""
    with open(path) as f:
        tree = ast.parse(f.read())
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # docstrings and other bare constants
            lines.add(node.lineno)
    return lines


def ranges(numbers):
    numbers = sorted(numbers)
    parts, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            parts.append(str(start) if start == n else f"{start}-{n}")
            start = None
    return ", ".join(parts)


class OnlyPackage:
    """Replaces ``trace``'s ignore list: trace the package and nothing else.
    (The stock one caches its verdict by bare module name, so after skipping
    some library's ``__init__.py`` it skipped ours as well.)"""

    def names(self, filename, modulename):
        return 0 if os.path.abspath(filename).startswith(PACKAGE + os.sep) else 1


def main():
    import pytest

    sys.path.insert(0, ROOT)
    tracer = trace.Trace(count=True, trace=False)
    tracer.ignore = OnlyPackage()
    threading.settrace(tracer.globaltrace)
    args = sys.argv[1:] or ["tests", "-p", "no:cacheprovider"]
    code = tracer.runfunc(pytest.main, args)
    counts = tracer.results().counts
    ran = {}
    for (filename, line), _ in counts.items():
        ran.setdefault(os.path.abspath(filename), set()).add(line)
    total_lines = total_ran = 0
    print(f"\n{'module':24} {'stmts':>6} {'miss':>6} {'cover':>6}  missing lines")
    for name in sorted(os.listdir(PACKAGE)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(PACKAGE, name)
        lines = executable_lines(path)
        missing = lines - ran.get(path, set())
        total_lines += len(lines)
        total_ran += len(lines) - len(missing)
        share = 100 * (len(lines) - len(missing)) / len(lines) if lines else 100
        print(f"{name:24} {len(lines):6} {len(missing):6} {share:5.1f}%  {ranges(missing)}")
    print(f"{'TOTAL':24} {total_lines:6} {total_lines - total_ran:6} {100 * total_ran / total_lines:5.1f}%")
    return code


if __name__ == "__main__":
    sys.exit(main())
