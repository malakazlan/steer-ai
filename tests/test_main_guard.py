"""Every module run as `python -m` keeps its `__main__` guard as the last top-level statement: a
function defined after it does not exist yet when `main()` runs (principal review of #61:
`_closed` after the guard made every runner exit 1 with a NameError, unseen by imports)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "steerai"
MODULES = sorted(p for p in PACKAGE.rglob("*.py") if "__name__" in p.read_text(encoding="utf-8"))


def _is_main_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(PACKAGE).as_posix())
def test_the_main_guard_is_the_last_statement(path: Path) -> None:
    body = ast.parse(path.read_text(encoding="utf-8")).body
    guards = [i for i, node in enumerate(body) if _is_main_guard(node)]
    if guards:
        assert guards == [len(body) - 1], "nothing may follow the __main__ guard"
