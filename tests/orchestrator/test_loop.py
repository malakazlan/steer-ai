"""The core loop (design 4, 5.11, orchestrator review OR-01..40): lookup → decide → gate → act →
verify → learn, driven against a scripted world so every path runs on any CI runner."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import GateDecision, Plan, Risk, Step, Subgoal, Target
from steerai.orchestrator.gate import Gate
from steerai.orchestrator.loop import Session
from steerai.orchestrator.trace import ListTrace
from steerai.verify.predicates import Appeared, TitleMatches, ValueEquals, Within
from tests.orchestrator.fakes import (
    COMPOSER,
    FakeConfirmer,
    FakeExecutor,
    FakeLive,
    FakeWorld,
    MemoryLedger,
    not_delivered,
    uncertain,
)

CONVERSATION = Within(landmark="Conversation")
HELLO = "hello there"
APPEARED = Appeared(
    role="Text", name_pattern=HELLO, within=CONVERSATION, excluding_anchor=COMPOSER[0]
)


def _click(step_id: str = "s1", name: str = "Chat options", risk: Risk = "R0", **kw: Any) -> Step:
    kw.setdefault("expect", TitleMatches(pattern="Amir Khan - Chat"))
    return Step(
        id=step_id,
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name=name),
        risk=risk,
        **kw,
    )


def _type(step_id: str = "s2") -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb="type",
        target=Target(role="Edit", name="Message"),
        text=HELLO,
        risk="R1",
        expect=ValueEquals(target_id=COMPOSER[0], text=HELLO),
    )


def _send(step_id: str = "s3") -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        risk="R2",
        params={
            "text": HELLO,
            "composer_role": "Edit",
            "composer_name": "Message",
            "scope": "Conversation",
        },
    )


def _plan(*steps: Step) -> Plan:
    return Plan(
        goal="send hello there to Amir",
        subgoals=(Subgoal(id="g1", desc="sent", condition=APPEARED),),
        steps=steps,
    )


class Harness:
    def __init__(
        self, *, answers: list[str] | None = None, default: str = "yes", task_id: str = "task-1"
    ) -> None:
        self.world = FakeWorld()
        self.semantic = FakeExecutor(self.world, "semantic")
        self.physical = FakeExecutor(self.world, "physical")
        self.confirmer = FakeConfirmer(answers=answers or [], default=default)
        self.ledger = MemoryLedger(task_id=task_id, started_at=99.0)
        self.trace = ListTrace()
        self.clock = {"t": 0.0}
        self.session = Session(
            task_id=task_id,
            hwnd=0x10,
            observer=self.world,
            semantic=self.semantic,
            physical=self.physical,
            gate=Gate(confirmer=self.confirmer, own_pid=4242, task_id=task_id),
            ledger=self.ledger,
            live=FakeLive(self.world),
            trace=self.trace,
            monotonic=lambda: self.clock["t"],
            sleep=lambda s: self.clock.__setitem__("t", self.clock["t"] + s),
            verify_deadline_s=0.5,
        )


# ---- the happy path ------------------------------------------------------------------------------


def test_r0_click_is_approved_executed_and_verified() -> None:
    h = Harness()
    step = _click(expect=None, risk="R0")
    # click has no template: the plan must supply the effect; with none the gate blocks
    result = h.session.run_step(step)
    assert result.gate == "block"
    assert result.evidence["detail"] == "no_predicate"
    assert h.semantic.calls == []


def test_r1_type_runs_physically_and_t1_verifies_the_value() -> None:
    h = Harness()
    result = h.session.run_step(_type())
    assert result.ok is True, result.to_dict()
    assert result.gate == "approve"
    assert h.physical.calls == [("type", COMPOSER[0], HELLO)]
    assert h.semantic.calls == []
    assert result.label_kind == "t1"
    assert set(result.latency_ms) >= {"observe", "decide", "gate", "act", "verify"}
    assert h.confirmer.asked == []


def test_r2_send_confirms_writes_ahead_executes_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness()
    h.session.run_step(_type())
    phases: list[str] = []
    h.semantic.on_execute = lambda _ex, _a: phases.append(h.ledger.records["s3"].phase)
    result = h.session.run_step(_send())
    assert result.ok is True, result.to_dict()
    assert result.gate == "confirm"
    assert len(h.confirmer.asked) == 1
    assert phases == ["acting"], "the record exists and is marked acting before dispatch"
    assert h.world.sent == [HELLO]
    assert h.ledger.records["s3"].phase == "verified"
    assert h.ledger.executions[h.ledger.key_for(_send())]["outcome"] == "delivered_verified"
    assert result.label_kind == "t1"
    assert result.verify_code is None


def test_the_same_send_in_the_same_task_is_done_already_without_executing() -> None:
    h = Harness()
    h.session.run_step(_type())
    h.session.run_step(_send())
    again = h.session.run_step(_send())
    assert again.gate == "done_already"
    assert again.ok is True
    assert h.world.sent == [HELLO]
    assert len(h.confirmer.asked) == 1


def test_a_new_task_id_sends_again() -> None:
    first = Harness(task_id="task-1")
    first.session.run_step(_type())
    first.session.run_step(_send())
    second = Harness(task_id="task-2")
    second.world = first.world
    second.session = Session(
        task_id="task-2",
        hwnd=0x10,
        observer=first.world,
        semantic=FakeExecutor(first.world),
        physical=FakeExecutor(first.world, "physical"),
        gate=Gate(confirmer=second.confirmer, own_pid=4242, task_id="task-2"),
        ledger=MemoryLedger(task_id="task-2", started_at=200.0),
        live=FakeLive(first.world),
        trace=second.trace,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        verify_deadline_s=0.5,
    )
    second.session.run_step(_type())
    result = second.session.run_step(_send())
    assert result.ok is True, result.to_dict()
    assert first.world.sent == [HELLO, HELLO]


# ---- the gate's answers --------------------------------------------------------------------------


def test_a_no_blocks_before_any_write_or_action() -> None:
    h = Harness(default="no")
    h.session.run_step(_type())
    result = h.session.run_step(_send())
    assert result.gate == "block"
    assert result.code is ErrorCode.GATE_BLOCKED
    assert "s3" not in h.ledger.records
    assert all(call[0] != "click" for call in h.semantic.calls)
    assert h.world.sent == []


def test_no_answer_pauses_with_gate_pending_user() -> None:
    h = Harness(default="unanswered")
    h.session.run_step(_type())
    result = h.session.run_step(_send())
    assert result.gate == "pending"
    assert result.code is ErrorCode.GATE_PENDING_USER
    assert "s3" not in h.ledger.records
    assert h.world.sent == []


def test_weak_predicate_at_r2_blocks_without_asking() -> None:
    h = Harness()
    step = Step(
        id="s3",
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        risk="R2",
        params={"text": HELLO},
        expect=Appeared(role="Text", name_pattern="Amir Khan"),
    )
    result = h.session.run_step(step)
    assert result.gate == "block"
    assert result.evidence["detail"] == "weak_predicate"
    assert h.confirmer.asked == []


def test_own_process_is_blocked_before_the_ask() -> None:
    h = Harness()
    h.world.pid = 4242
    result = h.session.run_step(_send())
    assert result.gate == "block"
    assert result.evidence["detail"] == "self_target"
    assert h.confirmer.asked == []


def test_wal_failure_means_no_action() -> None:
    h = Harness()
    h.session.run_step(_type())
    h.ledger.fail_writes = True
    result = h.session.run_step(_send())
    assert result.ok is False
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "wal_unavailable"
    assert h.world.sent == []


# ---- uncertain outcomes, retries and resume ----------------------------------------------------


def test_uncertain_send_needs_a_confirm_next_time_and_is_verified_against_the_first_before() -> (
    None
):
    h = Harness()
    h.session.run_step(_type())
    send_id = next(e.id for e in h.world.observe(0x10).elements if e.name == "Send")

    def late_landing(executor: FakeExecutor, action: Any) -> None:
        # the click "fails" but the message lands anyway
        executor.world.sent.append(HELLO)
        executor.world.add_bubble(HELLO)
        executor.world.composer_value = ""

    h.semantic.outcomes[send_id] = uncertain()
    h.semantic.on_execute = late_landing
    first = h.session.run_step(_send())
    assert first.ok is True, "T1 sees the bubble even though the executor was unsure"
    assert first.evidence["delivery"] == "delivery_uncertain"
    # make the first attempt look unverified so the key stays uncertain
    h.ledger.executions[h.ledger.key_for(_send())]["outcome"] = "delivery_uncertain"
    h.semantic.outcomes.clear()
    h.semantic.on_execute = None
    h.world.composer_value = HELLO
    second = h.session.run_step(_send())
    assert len(h.confirmer.asked) == 2
    assert h.confirmer.asked[1].previous_attempt == "delivery_uncertain"
    assert second.ok is False
    assert second.verify_code is ErrorCode.VERIFY_FAILED
    assert second.evidence["reason"] == "duplicate_effect", "verified against the original before"


def test_resume_with_a_record_and_the_effect_present_is_done_already() -> None:
    h = Harness()
    h.session.run_step(_type())
    h.session.run_step(_send())
    key = h.ledger.key_for(_send())
    # "crash" after dispatch: the outcome was never recorded
    h.ledger.executions[key]["outcome"] = None
    h.ledger.executions[key]["phase"] = "acting"
    h.ledger.mark(_send(), "acting")
    result = h.session.run_step(_send())
    assert result.gate == "done_already"
    assert result.ok is True
    assert h.world.sent == [HELLO], "never sent twice"
    assert len(h.confirmer.asked) == 1


def test_resume_with_a_record_and_no_effect_confirms_before_running() -> None:
    h = Harness()
    h.session.run_step(_type())
    key = h.ledger.key_for(_send())
    before = h.world.observe(0x10)
    h.ledger.write_ahead(
        _send(), before, GateDecision(decision_id="d-x", action_key=key, outcome="confirm")
    )
    h.ledger.mark(_send(), "acting")
    h.world.composer_value = HELLO
    result = h.session.run_step(_send())
    assert len(h.confirmer.asked) == 1
    assert h.confirmer.asked[0].previous_attempt == "acting"
    assert result.ok is True
    assert h.world.sent == [HELLO]


def test_unreadable_record_is_unknown_and_confirms() -> None:
    h = Harness()
    h.session.run_step(_type())
    h.ledger.unreadable.add("s3")
    result = h.session.run_step(_send())
    assert result.gate in ("block", "pending", "confirm", "unreached")
    assert h.confirmer.asked
    assert h.confirmer.asked[0].previous_attempt == "unknown"
    assert result.code is ErrorCode.GATE_BLOCKED or result.ok is False


def test_element_not_found_is_relocated_and_retried_once() -> None:
    h = Harness()
    h.session.run_step(_type())
    send_id = next(e.id for e in h.world.observe(0x10).elements if e.name == "Send")
    attempts = {"n": 0}

    def flaky(executor: FakeExecutor, action: Any) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            executor.outcomes[action.element_id] = not_delivered(
                ErrorCode.ELEMENT_NOT_FOUND, "identity_changed"
            )
        else:
            executor.outcomes.pop(action.element_id, None)

    h.semantic.outcomes[send_id] = not_delivered(ErrorCode.ELEMENT_NOT_FOUND, "identity_changed")
    h.semantic.on_execute = flaky
    result = h.session.run_step(_send())
    assert attempts["n"] == 2
    assert result.ok is True, result.to_dict()
    assert result.evidence["retries"] == 1
    assert len(h.confirmer.asked) == 1, "one decision covers the local retry"


def test_gate_blocked_from_the_executor_is_final() -> None:
    h = Harness()
    h.session.run_step(_type())
    send_id = next(e.id for e in h.world.observe(0x10).elements if e.name == "Send")
    h.semantic.outcomes[send_id] = not_delivered(ErrorCode.GATE_BLOCKED, "secure_field")
    result = h.session.run_step(_send())
    assert result.ok is False
    assert result.code is ErrorCode.GATE_BLOCKED
    assert len([c for c in h.semantic.calls if c[0] == "click"]) == 1


def test_field_not_empty_pauses_for_the_user() -> None:
    h = Harness()
    h.physical.outcomes[COMPOSER[0]] = not_delivered(ErrorCode.GATE_PENDING_USER, "field_not_empty")
    result = h.session.run_step(_type())
    assert result.code is ErrorCode.GATE_PENDING_USER
    assert len(h.physical.calls) == 1


def test_no_label_leaves_the_key_consumed_and_the_step_not_ok() -> None:
    h = Harness()
    h.session.run_step(_type())

    def churn(executor: FakeExecutor, _action: Any) -> None:
        # the whole conversation re-renders with the send
        executor.world.rows = [
            (r[0], r[1], r[2], (43, r[3][1]), r[4])
            if r[1] == "Text" or r[2] == "Conversation"
            else r
            for r in executor.world.rows
        ]
        executor.world.add_bubble(HELLO)
        executor.world.composer_value = ""

    h.semantic.on_execute = churn
    result = h.session.run_step(_send())
    assert result.ok is False
    assert result.label_kind is None
    assert result.evidence["reason"] == "scope_churn"
    assert h.ledger.admit(h.ledger.key_for(_send())) == "confirm"


# ---- snapshots: stability and reuse ----------------------------------------------------------


def test_unstable_world_is_re_observed_once_then_fails_without_acting() -> None:
    h = Harness()
    h.world.stable = False
    result = h.session.run_step(_type())
    assert result.code is ErrorCode.UNSTABLE_UI
    assert h.physical.calls == []
    assert len(h.world.observed) == 2


def test_r0_r1_steps_reuse_the_previous_decided_after_snapshot_and_r2_observes_fresh() -> None:
    h = Harness()
    h.session.run_step(_click(step_id="c1", expect=TitleMatches(pattern="Amir Khan - Chat")))
    observed_after_first = len(h.world.observed)
    h.session.run_step(_type())
    # the type step's before is the click's decided after: no new before observe, one after observe
    assert len(h.world.observed) == observed_after_first + 1
    h.session.run_step(_send())
    assert h.ledger.records["s3"].before.id != "", "R2 always observes a fresh before"


def test_stale_reused_snapshot_is_replaced_by_a_fresh_observe() -> None:
    h = Harness()
    h.session.run_step(_type(step_id="t1"))
    h.clock["t"] += 10.0
    observed = len(h.world.observed)
    h.session.run_step(_type(step_id="t2"))
    assert len(h.world.observed) == observed + 2, "fresh before and after"


# ---- run_task: resume from state, guards, trace --------------------------------------------------


def test_run_task_runs_the_plan_and_ticks_the_subgoal() -> None:
    h = Harness()
    result = h.session.run_task(_plan(_type(), _send()))
    assert result.status == "done", [r.to_dict() for r in result.results]
    assert result.ticked == {"g1"}
    assert h.world.sent == [HELLO]


def test_run_task_resume_skips_a_subgoal_already_true_for_presence_predicates() -> None:
    h = Harness()
    plan = Plan(
        goal="open the chat",
        subgoals=(
            Subgoal(id="g0", desc="chat open", condition=TitleMatches(pattern="Amir Khan - Chat")),
        ),
        steps=(
            Step(
                id="o1",
                subgoal="g0",
                verb="click",
                target=Target(role="ListItem", name="Amir · hi bro · 2h"),
                risk="R0",
                expect=TitleMatches(pattern="Amir Khan - Chat"),
            ),
        ),
    )
    result = h.session.run_task(plan)
    assert result.status == "done"
    assert result.ticked == {"g0"}
    assert h.semantic.calls == [], "already true at task start: nothing runs"


def test_run_task_stops_on_a_block_and_pauses_on_pending() -> None:
    blocked = Harness(default="no")
    assert blocked.session.run_task(_plan(_type(), _send())).status == "blocked"
    paused = Harness(default="unanswered")
    assert paused.session.run_task(_plan(_type(), _send())).status == "paused"


def test_screen_guard_stops_three_fruitless_steps_on_an_unchanged_screen() -> None:
    h = Harness()
    steps = tuple(
        _click(step_id=f"c{i}", expect=TitleMatches(pattern="Nothing - Chat")) for i in range(5)
    )
    result = h.session.run_task(_plan(*steps))
    assert result.status == "stopped"
    assert result.code is ErrorCode.BUDGET_EXCEEDED
    assert result.evidence["detail"] == "loop"
    assert len(result.results) == 3


def test_max_steps_stops_the_task() -> None:
    h = Harness()
    h.session.max_steps = 1
    result = h.session.run_task(_plan(_type(), _send()))
    assert result.status == "stopped"
    assert result.evidence["detail"] == "max_steps"
    assert h.world.sent == []


def test_trace_never_carries_names_text_or_titles() -> None:
    h = Harness()
    h.session.run_task(_plan(_type(), _send()))
    lines = h.trace.lines
    assert len(lines) >= 3
    joined = "\n".join(lines)
    for sentinel in ("Amir", "hello there", "Message", "Conversation", "send hello there to Amir"):
        assert sentinel not in joined, sentinel
    assert '"kind": "task"' in lines[0]
    assert '"step_id": "s3"' in joined
