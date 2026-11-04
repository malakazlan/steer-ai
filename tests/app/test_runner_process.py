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


def test_the_parser_follows_the_consoles_real_output() -> None:
    """The window parses what `render_ask` and `ask_go` really print (principal item 2): a
    yes ask with a warning line, a which ask whose names contain `)`, and the go line."""
    from steerai.app.runner_process import parse_console_lines  # noqa: PLC0415
    from steerai.orchestrator.console import ConsoleConfirmer, render_ask  # noqa: PLC0415
    from steerai.orchestrator.gate import Ask  # noqa: PLC0415
    from steerai.orchestrator.identity import CandidateLine  # noqa: PLC0415

    yes_ask = Ask(
        decision_id="d-7",
        task_id="t",
        action_key="k",
        verb="send",
        target_role="Button",
        risk="R2",
        expect={"kind": "Appeared"},
        params={"text": "hi"},
        target_name="Send",
        previous_attempt="act",
    )
    pending = parse_console_lines(render_ask(yes_ask).splitlines())
    assert pending is not None
    assert (pending.kind, pending.decision_id, pending.risk) == ("yes", "d-7", "R2")
    assert "WARNING" in pending.text
    assert "type yes:" not in pending.text
    lines = (
        CandidateLine(index=1, role="ListItem", name_part="Amir (work)", preview="a) ok", tags=()),
        CandidateLine(
            index=2, role="ListItem", name_part="Amir)", preview="hi", tags=("lookalike",)
        ),
    )
    which_ask = Ask(
        decision_id="d-8",
        task_id="t",
        action_key="k",
        verb="click",
        target_role="ListItem",
        risk="R1",
        expect={},
        params={},
        target_name="Amir",
        kind="which",
        candidates=lines,
    )
    pending = parse_console_lines(render_ask(which_ask).splitlines())
    assert pending is not None
    assert (pending.kind, pending.decision_id, pending.candidates) == ("which", "d-8", 2)
    import io  # noqa: PLC0415

    err = io.StringIO()
    ConsoleConfirmer(io.StringIO("no\n"), err, interactive=False).ask_go("0123456789ab")
    pending = parse_console_lines(err.getvalue().splitlines())
    assert pending == PendingAsk(kind="go", decision_id="0123456789ab", text="", risk="plan")


def test_the_exit_code_is_visible_right_after_the_child_exits() -> None:
    """Principal item 1: the result is read once the readers finish, not on a timer."""
    proc = RunnerProcess(_child("crash"))
    started = time.perf_counter()
    proc.start()
    assert _wait(lambda: proc.exit_code is not None, timeout_s=10.0)
    elapsed = time.perf_counter() - started
    assert proc.result["detail"] == "internal_error"
    assert elapsed < 1.5, f"took {elapsed:.2f}s; the child itself exits in about 0.2 s"
