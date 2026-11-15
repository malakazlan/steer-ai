"""The standing rule after #355's unmarked-test incident: a test that touches a real Windows API
(UIA, the hook monitor, SendInput, the v2 platform) carries the `windows`, `live` or `probe`
marker, so a local `-m "not windows and not live and not probe"` run never reaches the desktop.
A marker moved onto a helper by an edit is caught here, not on the person's machine."""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
LIVE_CALLS = frozenset({"UiaClient", "WindowsPlatform", "LiveInput", "LiveWindows", "HookMonitor"})
"""Constructors of the real Windows side."""
LIVE_MODULES = frozenset({"win_input"})
"""A call through one of these modules (`win_input.click_at(...)`) is live."""
MARKS = frozenset({"windows", "live", "probe"})
FAKED: frozenset[str] = frozenset(
    {
        # v1's keyboard tests: `send_inputs` / `build_keyboard_unicode` over a fake user32
        "action/test_keyboard.py::test_send_inputs_of_an_empty_batch_never_loads_user32",
        "action/test_keyboard.py::test_send_inputs_forwards_every_record_to_one_sendinput_call",
        "action/test_keyboard.py::test_build_keyboard_unicode_at_the_word_boundary",
        # the v2 platform built, never used: no COM, no hook, no input
        "loop2/test_live_world.py::test_the_windows_platform_is_a_platform",
        # v1's foreground ladder and menu-mode reads over a monkeypatched user32
        "probes/test_acquire_foreground.py::test_already_foreground_touches_nothing",
        "probes/test_acquire_foreground.py::test_direct_call_when_windows_allows_it",
        "probes/test_acquire_foreground.py::test_a_nudge_then_the_call_never_a_key",
        "probes/test_acquire_foreground.py::"
        "test_attach_thread_input_is_the_last_rung_and_always_detaches",
        "probes/test_acquire_foreground.py::test_hung_foreground_skips_attach_thread_input",
        "probes/test_acquire_foreground.py::test_missing_foreground_skips_attach_thread_input",
        "probes/test_acquire_foreground.py::test_every_rung_refused_detaches_and_raises",
        "probes/test_acquire_foreground.py::test_menu_mode_reads_the_menu_flags",
        "probes/test_acquire_foreground.py::test_an_unreadable_thread_counts_as_in_a_menu",
        # v1's hook monitor over a fake user32 and a fake hook thread
        "session/test_followups.py::test_a_monitor_whose_close_timed_out_refuses_to_start_again",
        "session/test_hook_callback.py::"
        "test_a_failed_hook_install_is_a_refusal_that_unhooks_what_was_installed",
        "session/test_hook_callback.py::test_a_taken_hot_key_is_a_refusal_that_unhooks_both_hooks",
        "session/test_hook_callback.py::"
        "test_an_exception_in_the_hook_thread_before_ready_is_a_start_refusal",
        "session/test_hook_callback.py::"
        "test_a_self_probe_that_cannot_be_sent_does_not_raise_and_fails_closed",
    }
)
"""Tests that call these names over fakes (a monkeypatched user32, a fake hook thread), read and
pinned here one by one: a new live-looking test is marked, or added here with its reason."""


def _marks(decorators: list[ast.expr]) -> set[str]:
    """`pytest.mark.<name>` (called or not) among the decorators."""
    found: set[str] = set()
    for node in decorators:
        target = node.func if isinstance(node, ast.Call) else node
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "mark"
        ):
            found.add(target.attr)
    return found


def _module_marks(tree: ast.Module) -> set[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            value = node.value
            values = list(value.elts) if isinstance(value, ast.List | ast.Tuple) else [value]
            return _marks(values)
    return set()


def _live_calls(function: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in LIVE_CALLS:
            found.add(func.id)
        elif isinstance(func, ast.Attribute):
            if func.attr in LIVE_CALLS:
                found.add(func.attr)
            elif isinstance(func.value, ast.Name) and func.value.id in LIVE_MODULES:
                found.add(f"{func.value.id}.{func.attr}")
    return found


def _unmarked(path: Path, root: Path = TESTS) -> list[str]:
    """`<path>::<test> calls [...]` for each unmarked test of `path` calling a live API."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if _module_marks(tree) & MARKS:
        return []
    out: list[str] = []

    def visit(body: list[ast.stmt], inherited: set[str]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, inherited | _marks(node.decorator_list))
            elif isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                marked = inherited | _marks(node.decorator_list)
                calls = _live_calls(node)
                if calls and not marked & MARKS:
                    name = path.relative_to(root).as_posix()
                    out.append(f"{name}::{node.name} calls {sorted(calls)}")

    visit(tree.body, set())
    return out


def _offenders() -> set[str]:
    return {
        line.partition(" calls ")[0]
        for path in sorted(TESTS.rglob("test_*.py"))
        for line in _unmarked(path)
    }


def test_every_test_that_touches_real_windows_apis_is_marked() -> None:
    assert sorted(_offenders() - FAKED) == []


def test_every_pinned_faked_test_still_exists_and_still_needs_the_pin() -> None:
    assert sorted(FAKED - _offenders()) == []


def test_the_guard_catches_a_marker_moved_onto_a_helper(tmp_path: Path) -> None:
    """The #355 incident's shape: the decorator on the helper, the live test bare."""
    lines = [
        "import pytest",
        "@pytest.mark.windows",
        "def _helper():",
        "    return 1",
        "def test_live():",
        "    WindowsPlatform().uia()",
    ]
    sample = tmp_path / "test_sample.py"
    sample.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    assert _unmarked(sample, tmp_path) == ["test_sample.py::test_live calls ['WindowsPlatform']"]


def test_the_guard_sees_a_constructor_reached_through_its_module(tmp_path: Path) -> None:
    lines = ["def test_live():", "    uia_snapshot.UiaClient().focused_facts()"]
    sample = tmp_path / "test_sample.py"
    sample.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    assert _unmarked(sample, tmp_path) == ["test_sample.py::test_live calls ['UiaClient']"]


def test_the_runtime_trap_is_armed_in_an_unmarked_test() -> None:
    """The runtime half (tests/conftest.py): here, unmarked, every real-input primitive is the
    trap, so a live call the syntactic guard missed fails before any event leaves the process."""
    from steerai.probes import win_input  # noqa: PLC0415 - this test only
    from steerai.session import monitor  # noqa: PLC0415 - this test only

    primitives = [vars(win_input)["_send"], vars(monitor)["_send"], vars(monitor)["_user32"]]
    assert [p.__name__ for p in primitives] == ["trap"] * 3
