"""#375 review P1: every foreign function the helper and its launch call has `argtypes` and
`restype` declared. Without them ctypes converts a handle as a C int, and `GetCurrentProcess()`
(the pseudo-handle -1, 2**64-1 as an unsigned pointer) overflows: the real launch never worked."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from steerai.session import helper_launch, hook_helper

DLLS = {"_k", "_a", "_k32", "_u32", "psapi", "k", "a", "u"}
"""The names the two modules hold a `ctypes.WinDLL` under (attributes of self, or locals)."""


def _dll_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.attr if node.value.id == "self" else None
    if isinstance(node, ast.Name):
        return node.id
    return None


def _called_and_declared(path: Path) -> tuple[set[str], set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    called: set[str] = set()
    argtypes: set[str] = set()
    restype: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _dll_name(node.func.value) in DLLS
        ):
            called.add(node.func.attr)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in {"argtypes", "restype"}
                    and isinstance(target.value, ast.Attribute)
                    and _dll_name(target.value.value) in DLLS
                ):
                    (argtypes if target.attr == "argtypes" else restype).add(target.value.attr)
    return called, argtypes, restype


@pytest.mark.parametrize("module", [helper_launch, hook_helper])
def test_every_foreign_function_called_is_declared(module: object) -> None:
    called, argtypes, restype = _called_and_declared(Path(module.__file__))  # type: ignore[attr-defined]  # a module
    assert called, "the walker found no foreign calls"
    assert not called - argtypes, f"no argtypes: {sorted(called - argtypes)}"
    assert not called - restype, f"no restype: {sorted(called - restype)}"


def test_the_walker_sees_an_undeclared_call(tmp_path: Path) -> None:
    source = tmp_path / "m.py"
    source.write_text(
        "class C:\n"
        "    def f(self):\n"
        "        self._k.SetEvent.argtypes = ()\n"
        "        self._k.SetEvent.restype = None\n"
        "        self._k.SetEvent(1)\n"
        "        self._k.GetCurrentProcess()\n",
        encoding="utf-8",
    )
    called, argtypes, restype = _called_and_declared(source)
    assert called - argtypes == {"GetCurrentProcess"}
    assert called - restype == {"GetCurrentProcess"}
