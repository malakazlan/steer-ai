"""Every test that starts a real Chrome first takes the once-per-run warm-up (issue #39, hunter
CC-15): a new Chrome test that forgets it and sorts first would be the cold start again."""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _uses_warm_chrome(node: ast.FunctionDef) -> bool:
    for decorator in node.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and getattr(decorator.func, "attr", "") == "usefixtures"
            and any(
                isinstance(a, ast.Constant) and a.value == "warm_chrome" for a in decorator.args
            )
        ):
            return True
    return any(arg.arg == "warm_chrome" for arg in node.args.args)


def _launches_chrome(node: ast.FunctionDef) -> bool:
    return any(
        isinstance(call, ast.Call) and getattr(call.func, "id", "") == "launch_chrome"
        for call in ast.walk(node)
    )


def test_every_real_chrome_test_requests_the_warm_up() -> None:
    missing = []
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            if _launches_chrome(node) and not _uses_warm_chrome(node):
                missing.append(f"{path.relative_to(TESTS)}::{node.name}")
    assert missing == []
