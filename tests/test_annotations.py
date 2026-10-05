"""Every module-level function and method has resolvable type annotations.

MiniDB uses only the standard library, so there is no type checker in the
test suite; this at least guarantees the annotations exist and refer to
real types (``typing.get_type_hints`` evaluates them)."""

import ast
import builtins
import importlib
import inspect
import pkgutil
import typing

import pytest

import minidb

MODULES = [
    importlib.import_module(f"minidb.{info.name}")
    for info in pkgutil.iter_modules(minidb.__path__)
    if info.name != "__main__"
]


def functions_of(module):
    for name, obj in vars(module).items():
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        if inspect.isfunction(obj):
            yield f"{module.__name__}.{name}", obj
        elif inspect.isclass(obj):
            for attr, member in vars(obj).items():
                member = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
                if isinstance(member, property):
                    member = member.fget
                if (
                    inspect.isfunction(member)
                    and member.__module__ == module.__name__
                    # generated methods (dataclasses, Protocol) have no source here
                    and member.__code__.co_filename == module.__file__
                ):
                    yield f"{module.__name__}.{name}.{attr}", member


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_annotations_are_complete_and_resolve(module):
    missing = []
    for qualified, function in functions_of(module):
        hints = typing.get_type_hints(function)  # raises if an annotation is wrong
        parameters = [
            p for p in inspect.signature(function).parameters.values()
            if p.name not in ("self", "cls")
        ]
        if any(p.name not in hints for p in parameters) or "return" not in hints:
            missing.append(qualified)
    assert not missing, "missing annotations: " + ", ".join(missing)


def nested_functions(module):
    """(line, function node) of every def in the module's source, nested
    functions and closures included."""
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.lineno, node


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.__name__)
def test_nested_functions_are_annotated_with_known_names(module):
    # Annotations are strings (from __future__ import annotations) and a
    # closure's are never evaluated: check that every name in them exists.
    problems = []
    for line, node in nested_functions(module):
        arguments = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        arguments += [a for a in (node.args.vararg, node.args.kwarg) if a is not None]
        arguments = [a for a in arguments if a.arg not in ("self", "cls")]
        annotations = [a.annotation for a in arguments]
        if node.name != "__init__" or node.returns is not None:
            annotations.append(node.returns)
        if any(a is None for a in annotations):
            problems.append(f"line {line}: {node.name} is missing annotations")
            continue
        for annotation in annotations:
            quoted = isinstance(annotation, ast.Constant) and isinstance(annotation.value, str)
            expr = ast.parse(annotation.value, mode="eval") if quoted else annotation
            for name in ast.walk(expr):
                if isinstance(name, ast.Name) and name.id not in vars(module) and not hasattr(builtins, name.id):
                    problems.append(f"line {line}: {node.name} names unknown {name.id}")
    assert not problems, "\n".join(problems)
