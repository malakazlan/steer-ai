"""A stopped run never leaves its task running: the harness kills the child it started (by the
handle `start` gave it, its pid's tree) on every exit path, not only at the cap: a Ctrl+C or
any other exception while it waits ends the child before `run_child` returns, then re-raises."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2.stage1 import classify, run_child
from steerai.probes import suite_chunks
from tests.loop2.test_stage1 import DONE, _record


class _Interrupted:
    """A child whose first wait is interrupted by `error`; later waits return at once."""

    def __init__(self, error: BaseException) -> None:
        self.pid, self.error, self.waits = 4321, error, 0
        self.events: list[str] = []

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        self.events.append(f"wait:{timeout}")
        if self.waits == 1:
            raise self.error
        return 1


@pytest.mark.parametrize("error", [KeyboardInterrupt(), OSError("wait failed"), SystemExit(2)])
def test_an_interrupt_while_waiting_kills_the_child_before_returning(
    tmp_path: Path, error: BaseException
) -> None:
    child = _Interrupted(error)

    def kill(pid: int) -> str | None:
        child.events.append(f"kill:{pid}")
        return None

    with pytest.raises(type(error)):
        run_child(["steer"], cap_s=60.0, out=tmp_path / "out.jsonl",
                  start=lambda _argv, _out: child, kill=kill)  # fmt: skip
    assert child.events[:2] == ["wait:60.0", "kill:4321"]
    assert child.events[2].startswith("wait:")  # the kill is waited for, bounded


def test_a_child_that_finished_is_never_killed(tmp_path: Path) -> None:
    class Done:
        pid = 4321

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - the Child protocol
            return 0

    killed: list[int] = []
    run_child(["steer"], cap_s=60.0, out=tmp_path / "out.jsonl",
              start=lambda _argv, _out: Done(), kill=killed.append)  # fmt: skip
    assert killed == []


# ---- #431 REVISE: the kill is bounded and its failure is reported, never a clean stop ------------


class _Stubborn:
    """A child that never ends: every wait times out."""

    pid = 4321

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("steer", timeout or 0.0)


def test_a_child_that_survives_its_kill_is_reported(tmp_path: Path) -> None:
    ran = run_child(["steer"], cap_s=1.0, out=tmp_path / "out.jsonl",
                    start=lambda _argv, _out: _Stubborn(), kill=lambda _pid: None)  # fmt: skip
    assert (ran.killed, ran.kill_failure) == (True, "child_survived")


def test_a_kill_that_reports_failure_is_kept_and_scored(tmp_path: Path) -> None:
    class Ends:
        pid, waits = 4321, 0

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("steer", timeout or 0.0)
            return 1

    ran = run_child(["steer"], cap_s=1.0, out=tmp_path / "out.jsonl",
                    start=lambda _argv, _out: Ends(),
                    kill=lambda _pid: "kill_failed:1")  # fmt: skip
    assert ran.kill_failure == "kill_failed:1"
    record = _record(None, killed=True, kill_failure=ran.kill_failure)
    assert classify(DONE, record).reason == "cap_killed:kill_failed:1"


def test_a_kill_that_raises_never_masks_the_interrupt(tmp_path: Path) -> None:
    child = _Interrupted(KeyboardInterrupt())

    def kill(_pid: int) -> str | None:
        raise OSError("taskkill missing")

    with pytest.raises(KeyboardInterrupt) as stopped:
        run_child(["steer"], cap_s=60.0, out=tmp_path / "out.jsonl",
                  start=lambda _argv, _out: child, kill=kill)  # fmt: skip
    assert any("kill_raised:OSError" in note for note in stopped.value.__notes__)


def test_a_failed_kill_on_an_interrupt_is_noted_on_the_interrupt(tmp_path: Path) -> None:
    with pytest.raises(KeyboardInterrupt) as stopped:
        run_child(["steer"], cap_s=60.0, out=tmp_path / "out.jsonl",
                  start=lambda _argv, _out: _Interrupted(KeyboardInterrupt()),
                  kill=lambda _pid: "kill_timeout")  # fmt: skip
    assert any("kill_timeout" in note for note in stopped.value.__notes__)


def test_a_start_that_raises_kills_nothing(tmp_path: Path) -> None:
    killed: list[int] = []

    def start(_argv: list[str], _out: Path) -> Any:  # Any: never returns
        raise OSError("no python")

    with pytest.raises(OSError, match="no python"):
        run_child(["steer"], cap_s=60.0, out=tmp_path / "out.jsonl", start=start,
                  kill=killed.append)  # fmt: skip
    assert killed == []


# ---- kill_tree: bounded, its exit code read ---------------------------------------------------


def _ran(code: int) -> Any:  # Any: a CompletedProcess
    return subprocess.CompletedProcess(["taskkill"], code, b"", b"")


@pytest.mark.parametrize(("code", "result"), [(0, None), (128, None), (1, "kill_failed:1")])
def test_kill_tree_reads_taskkills_exit_code(code: int, result: str | None) -> None:
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kw: Any) -> Any:  # Any: subprocess.run's keywords
        seen.update(kw, argv=argv)
        return _ran(code)

    assert suite_chunks.kill_tree(4321, run=run) == result
    assert seen["timeout"] == suite_chunks.KILL_TIMEOUT_S
    assert seen["argv"][:3] == ["taskkill", "/PID", "4321"]


def test_kill_tree_that_times_out_says_so() -> None:
    def run(argv: list[str], **kw: Any) -> Any:  # Any: subprocess.run's keywords
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    assert suite_chunks.kill_tree(4321, run=run) == "kill_timeout"
