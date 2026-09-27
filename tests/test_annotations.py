"""Every module-level function and method has resolvable type annotations.

MiniDB uses only the standard library, so there is no type checker in the
test suite; this at least guarantees the annotations exist and refer to
real types (``typing.get_type_hints`` evaluates them)."""

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
