"""The app runs the real runner as a child process (the proven console path) and talks to it
over its pipes: GO and ASK blocks on stderr become pending asks, button answers become the
exact bound tokens on stdin, the final JSON line is the result, close is terminate. Pure:
the child here is `tests/app/fake_runner.py`."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from steerai.app.runner_process import Answer, PendingAsk, RunnerProcess

FAKE = Path(__file__).with_name("fake_runner.py")


def _child(script: str) -> list[str]:
    return [sys.executable, str(FAKE), script]


def _wait(condition: object, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():  # type: ignore[operator]
            return True
        time.sleep(0.02)
    return False


def _kind(proc: RunnerProcess) -> str | None:
    pending = proc.pending()
    return None if pending is None else pending.kind


def test_a_go_block_becomes_a_pending_ask_and_the_token_runs_the_plan() -> None:
    proc = RunnerProcess(_child("go-then-ask"))
    proc.start()
    assert _wait(lambda: proc.pending() is not None)
    pending = proc.pending()
    assert pending == PendingAsk(kind="go", decision_id="0123456789ab", text="", risk="plan")
    assert any("plan 0123456789ab" in line for line in proc.lines())
    proc.answer(Answer.go("0123456789ab"))
    assert _wait(lambda: _kind(proc) == "yes")
    ask = proc.pending()
    assert ask is not None
    assert ask.decision_id == "d-1"
    assert ask.risk == "R2"
    assert "text: hello there" in ask.text
    assert "type yes:" not in ask.text, "the overlay shows buttons, not tokens to type"
    proc.answer(Answer.yes("d-1"))
    assert _wait(lambda: proc.exit_code is not None)
    assert proc.exit_code == 0
    assert proc.result == {"status": "done", "ticked": ["g1"], "answer": "yes:d-1"}
    assert proc.pending() is None


def test_a_which_block_counts_its_candidates_and_an_ordinal_is_bound() -> None:
    proc = RunnerProcess(_child("which"))
    proc.start()
    assert _wait(lambda: proc.pending() is not None)
    proc.answer(Answer.go("0123456789ab"))
    assert _wait(lambda: _kind(proc) == "which")
    ask = proc.pending()
    assert ask is not None
    assert ask.candidates == 2
    proc.answer(Answer.choose(2, "d-2"))
    assert _wait(lambda: proc.exit_code is not None)
    assert proc.result["answer"] == "2:d-2"


def test_no_declines_and_a_stale_or_wrong_answer_is_dropped() -> None:
    proc = RunnerProcess(_child("go-then-ask"))
    proc.start()
    assert _wait(lambda: proc.pending() is not None)
    proc.answer(Answer.yes("0123456789ab"))  # a yes for a go ask: dropped
    proc.answer(Answer.go("other-id"))  # the wrong id: dropped
    time.sleep(0.2)
    assert proc.pending() is not None, "still waiting on the go"
    proc.answer(Answer.no())
    assert _wait(lambda: proc.exit_code is not None)
    assert proc.exit_code == 2
    assert proc.result["detail"] == "plan_unanswered"


def test_close_terminates_the_child_and_reads_as_killed() -> None:
    proc = RunnerProcess(_child("go-then-ask"))
    proc.start()
    assert _wait(lambda: proc.pending() is not None)
    proc.close()
    assert _wait(lambda: proc.exit_code is not None)
    assert proc.exit_code != 0
    assert proc.pending() is None
    assert proc.killed is True


def test_a_crashed_child_reports_its_lines_and_code() -> None:
    proc = RunnerProcess(_child("crash"))
    proc.start()
    assert _wait(lambda: proc.exit_code is not None)
    assert proc.exit_code == 70
    assert any("internal error" in line for line in proc.lines())
    assert proc.result["detail"] == "internal_error"


def test_answers_before_start_or_after_exit_do_not_raise() -> None:
    proc = RunnerProcess(_child("crash"))
    proc.answer(Answer.no())
    proc.start()
    assert _wait(lambda: proc.exit_code is not None)
    proc.answer(Answer.no())
    assert proc.running() is False


@pytest.mark.parametrize("bad", [0, -1])
def test_choose_rejects_non_positive_ordinals(bad: int) -> None:
    with pytest.raises(ValueError, match="ordinal"):
        Answer.choose(bad, "d-1")
