"""`--desktop` (packet open-multiwindow-2026-11-06, scope item 6; schema gate non-blocking notes;
hunter OW-18/31): the task starts on the shell's desktop window, the runner never taps the
foreground onto it, and the shell itself is never part of Steer's own process tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerai.probes.windows import WindowInfo
from steerai.run import EXIT_DONE, UsageError, parse_args
from tests.orchestrator.test_loop import _click
from tests.orchestrator.test_run import FakeSession, World, _done, _plan, _run

DESKTOP = WindowInfo(hwnd=0x50, title="Program Manager", pid=900)


def _options(tmp_path: Path) -> object:
    plan = _plan(_click())
    plan_path = tmp_path / "plan.json"
    from steerai.orchestrator.plan_io import plan_to_json  # noqa: PLC0415

    plan_path.write_text(plan_to_json(plan), encoding="utf-8")
    return parse_args(["--plan", str(plan_path), "--desktop", "--ledger-dir", str(tmp_path / "l")])


def test_desktop_is_exclusive_with_the_other_window_options(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        parse_args(["--plan", "p.json", "--desktop", "--window", "chrome"])
    options = _options(tmp_path)
    assert options.desktop is True  # type: ignore[attr-defined]


def test_a_desktop_task_binds_the_shell_window_and_never_taps_the_foreground(
    tmp_path: Path,
) -> None:
    plan = _plan(_click())
    world = World(session=FakeSession(_done(plan)))
    deps = world.deps().replace(desktop=lambda: DESKTOP)
    options = _options(tmp_path)
    import io  # noqa: PLC0415
    import json  # noqa: PLC0415

    from steerai.run import run  # noqa: PLC0415

    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(options, deps, stdin=io.StringIO("no\n"), stdout=stdout, stderr=stderr)  # type: ignore[arg-type]
    assert code == EXIT_DONE, stderr.getvalue()
    assert json.loads(stdout.getvalue())["status"] == "done"
    assert world.bound[0].hwnd == DESKTOP.hwnd
    assert "foreground" not in world.calls
    assert world.ledger is not None
    assert world.ledger.windows == [(DESKTOP.pid, DESKTOP.title)]


def test_desktop_without_a_desktop_source_is_a_usage_error(tmp_path: Path) -> None:
    world = World(session=FakeSession(_done(_plan(_click()))))
    code, out, _err = _run(_options(tmp_path), world)  # type: ignore[arg-type]
    assert code != EXIT_DONE
    assert out["status"] == "error"


@pytest.mark.windows
def test_the_shell_is_never_part_of_steers_own_tree() -> None:
    """OW-18: started from Explorer, the parent chain reaches explorer.exe; the desktop and
    every File Explorer window must stay targetable."""
    from steerai.probes.windows import console_exclusions, desktop_window  # noqa: PLC0415

    _hwnds, pids = console_exclusions()
    assert desktop_window().pid not in pids


# ---- per-step windows from the runner (step-window SW-05, gate item 3, SW-09) ---------------


def _run_plan(
    tmp_path: Path, plan: object, *extra: str, **deps_changes: object
) -> tuple[int, dict, str]:  # type: ignore[type-arg]
    import io  # noqa: PLC0415
    import json  # noqa: PLC0415

    from steerai.orchestrator.plan_io import plan_to_json  # noqa: PLC0415
    from steerai.run import run  # noqa: PLC0415

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan_to_json(plan), encoding="utf-8")  # type: ignore[arg-type]
    options = parse_args(["--plan", str(plan_path), "--ledger-dir", str(tmp_path / "l"), *extra])
    world = World(session=FakeSession(_done(plan)))  # type: ignore[arg-type]
    deps = world.deps().replace(desktop=lambda: DESKTOP, **deps_changes)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(options, deps, stdin=io.StringIO(""), stdout=stdout, stderr=stderr)
    return code, json.loads(stdout.getvalue()), stderr.getvalue()


def test_window_start_on_a_desktop_task_is_refused_before_anything_runs(tmp_path: Path) -> None:
    from dataclasses import replace  # noqa: PLC0415

    plan = _plan(replace(_click(), window="start"))
    code, out, err = _run_plan(tmp_path, plan, "--desktop")
    assert out["detail"] == "invalid_plan"
    assert "steps[0].window" in err
    assert code != EXIT_DONE


def test_the_start_windows_identity_is_bound_with_the_window(tmp_path: Path) -> None:
    identity = {"hwnd": 0x10, "pid": 500, "start": 3, "image": "chrome.exe", "title": "t"}
    seen: list[int] = []

    def identify(hwnd: int) -> dict[str, object]:
        seen.append(hwnd)
        return identity

    world_plan = _plan(_click())
    code, _out, err = _run_plan(tmp_path, world_plan, "--window", "chrome", identify=identify)
    assert code == EXIT_DONE, err
    assert seen == [0x10]
