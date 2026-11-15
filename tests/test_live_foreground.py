"""#205, #439: a test that starts its own Chrome brings that window to the front before it reads
or acts. A Chrome started by a background process opens behind the foreground window, a covered
Chromium window keeps a partial accessibility tree (`layer_b._in_front`, measured 2026-11-08),
and an occluded one has its renderer backgrounded, so input there has no effect (H4 packet,
2026-11-10: 2/5 and 1/5 before, 5/5 after). A native `<select>` whose Expand never opens then
reads Collapsed with no options: `no_options`, as `test_choose_live` failed on CI (#439).

Read from the source: every test function that calls `launch_chrome` also calls
`acquire_foreground`, after the window is found and before anything else reads it."""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _calls(node: ast.AST) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name | ast.Attribute)
    ]


def _name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _launchers() -> dict[str, list[ast.FunctionDef]]:
    found: dict[str, list[ast.FunctionDef]] = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and any(
                _name(c) == "launch_chrome" for c in _calls(fn)
            ):
                found.setdefault(str(path.relative_to(TESTS)), []).append(fn)
    return found


def test_the_check_sees_the_live_tests_that_start_chrome() -> None:
    assert len(_launchers()) >= 10


def test_every_test_that_starts_chrome_brings_it_to_the_front_first() -> None:
    behind: list[str] = []
    for path, functions in _launchers().items():
        for fn in functions:
            calls = sorted(_calls(fn), key=lambda c: (c.lineno, c.col_offset))
            names = [_name(c) for c in calls]
            if "acquire_foreground" not in names:
                behind.append(f"{path}::{fn.name}")
                continue
            front = names.index("acquire_foreground")
            found = names.index("wait_window_for_pid") if "wait_window_for_pid" in names else -1
            reads = [
                i
                for i, n in enumerate(names)
                if n in {"observe", "WorldModel", "UiaClient", "_ready", "run_step"}
            ]
            if found > front or any(i < front for i in reads):
                behind.append(f"{path}::{fn.name} (order)")
    assert not behind, behind
