"""Items from the principal's implementation review of the orchestrator loop."""

from __future__ import annotations

import time
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.ledger import LedgerError
from steerai.verify.predicates import All, Appeared, CountDelta, TitleMatches, WindowExists, Within
from tests.orchestrator.fakes import FakeConfirmer
from tests.orchestrator.test_loop import HELLO, Harness, _click, _plan, _send, _type

CONVERSATION = Within(landmark="Conversation")


# ---- 1. a failure before the gate is not a block ----------


def test_unstable_world_is_reported_as_unreached_not_blocked() -> None:
    h = Harness()
    h.world.stable = False
    result = h.session.run_step(_type())
    assert result.gate == "unreached"
    assert result.code is ErrorCode.UNSTABLE_UI
    task = h.session.run_task(_plan(_type(), _send()))
    assert task.status == "stopped"
    assert task.code is ErrorCode.UNSTABLE_UI
    assert task.evidence["detail"] == "unstable"


def test_missing_target_stops_the_task_with_element_not_found() -> None:
    h = Harness()
    step = Step(
        id="x",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="Nowhere"),
        risk="R0",
        expect=TitleMatches(pattern="Nothing"),
    )
    task = h.session.run_task(_plan(step))
    assert task.status == "stopped"
    assert task.code is ErrorCode.ELEMENT_NOT_FOUND
    assert task.results[0].gate == "unreached"


def test_a_real_gate_block_still_reads_as_blocked() -> None:
    h = Harness(default="no")
    task = h.session.run_task(_plan(_type(), _send()))
    assert task.status == "blocked"
    assert task.code is ErrorCode.GATE_BLOCKED


# ---- 2. done already ticks its subgoal ----------


def test_a_resumed_task_with_the_send_already_done_finishes() -> None:
    h = Harness()
    first = h.session.run_task(_plan(_type(), _send()))
    assert first.status == "done"
    again = h.session.run_task(_plan(_type(), _send()))
    assert again.status == "done", [r.to_dict() for r in again.results]
    assert again.ticked == {"g1"}
    assert [r.gate for r in again.results] == ["approve", "done_already"]
    assert h.world.sent == [HELLO]


# ---- 3. the verifier runs on the session's clock ----------


def test_verify_inside_the_loop_uses_the_injected_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any) -> None:
        raise AssertionError("the loop must not sleep on the real clock")

    monkeypatch.setattr(time, "sleep", forbidden)
    h = Harness()
    h.world.composer_value = ""
    step = Step(
        id="t",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="Chat options"),
        risk="R0",
        expect=TitleMatches(pattern="Nothing - Chat"),
    )
    result = h.session.run_step(step)
    assert result.ok is False
    assert result.verify_code is ErrorCode.VERIFY_TIMEOUT
    assert h.clock["t"] >= 0.5, "the fake clock advanced through the verify polls"


# ---- 4. a ledger error after dispatch is a step result, not an exception ----------


def test_ledger_error_after_dispatch_is_reported_as_wal_unavailable() -> None:
    h = Harness()
    h.session.run_step(_type())
    original = h.ledger.record_outcome

    def broken(step: Step, outcome: str) -> None:
        raise LedgerError("ledger state could not be written: disk full")

    h.ledger.record_outcome = broken  # type: ignore[method-assign]
    result = h.session.run_step(_send())
    h.ledger.record_outcome = original  # type: ignore[method-assign]
    assert result.ok is False
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "wal_unavailable"
    assert h.world.sent == [HELLO], "the action had already run"
    assert h.trace.lines[-1].find('"step_id": "s3"') >= 0
    assert h.ledger.outcome_of(h.ledger.key_for(_send())) is None, "the attempt stays open"


# ---- 5. plan text is validated at construction ----------


def test_non_numeric_composer_id_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="composer_id"):
        Step(
            id="s",
            subgoal="g1",
            verb="send",
            target=Target(role="Button", name="Send"),
            risk="R2",
            params={"text": "x", "composer_id": "twelve", "scope": "Conversation"},
        )


# ---- 6. the before window list is captured at before time ----------


def test_window_exists_needs_a_window_new_since_the_step_began() -> None:
    windows_now: list[Any] = [type("W", (), {"title": "Save As", "hwnd": 0x100, "pid": 50})()]
    h = Harness()
    h.session._windows = lambda: list(windows_now)
    step = Step(
        id="o",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="Chat options"),
        risk="R0",
        expect=WindowExists(name_pattern="Save As"),
    )
    pre_existing = h.session.run_step(step)
    assert pre_existing.ok is False
    assert pre_existing.evidence["reason"] == "not_new"
    windows_now.append(type("W", (), {"title": "Save As", "hwnd": 0x200, "pid": 50})())
    h.session._windows = lambda: list(windows_now)
    # the window list is captured when the step begins; a window appearing after is new
    new_window = h.session.run_step(step)
    assert new_window.ok is False, "already there when the second step began"


# ---- non-blocking: structural part order, scroll evidence ----------------------------------------


def test_structural_part_prefers_appeared_over_count() -> None:
    from steerai.orchestrator.loop import _structural_part  # noqa: PLC0415

    both = All(
        (
            CountDelta(role="Text", delta=1, within=CONVERSATION),
            Appeared(role="Text", name_pattern="hello", within=CONVERSATION),
        )
    )
    assert isinstance(_structural_part(both), Appeared)


def test_done_already_in_the_middle_of_a_plan_keeps_going() -> None:
    h = Harness()
    h.session.run_task(_plan(_type(), _send()))
    plan = Plan(
        goal="g",
        subgoals=(
            Subgoal(id="g1", desc="d", condition=_plan(_type()).subgoals[0].condition),
            Subgoal(id="g2", desc="d2", condition=TitleMatches(pattern="Amir Khan - Chat")),
        ),
        steps=(_type(), _send()),
    )
    result = h.session.run_task(plan)
    assert result.status == "done"
    assert result.ticked == {"g1", "g2"}
    assert isinstance(h.confirmer, FakeConfirmer)


# ---- final pass: a post-gate refusal that dispatched nothing stops the task ----------------


def test_a_post_gate_failure_that_dispatched_nothing_stops_the_task() -> None:
    h = Harness()
    h.ledger.fail_writes = True
    task = h.session.run_task(_plan(_type(), _send(), _click(step_id="s9")))
    assert task.status == "stopped"
    assert task.code is ErrorCode.GATE_BLOCKED
    assert task.evidence["detail"] == "wal_unavailable"
    assert [r.step_id for r in task.results] == ["s2", "s3"], "the step after was never run"
    assert task.results[1].gate == "confirm"
    assert h.world.sent == []
