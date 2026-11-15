"""A stopped run never leaves its task running: the harness kills the child it started (by the
handle `start` gave it, its pid's tree) on every exit path, not only at the cap: a Ctrl+C or
any other exception while it waits ends the child before `run_child` returns, then re-raises."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerai.loop2.stage1 import run_child


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

    def kill(pid: int) -> None:
        child.events.append(f"kill:{pid}")

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
