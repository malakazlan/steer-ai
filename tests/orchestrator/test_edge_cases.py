"""Direct tests for the orchestrator review rows the first slice covered only indirectly
(OR-02/03/04/07/13/14/19/20/21/23/25/27/30/31/33/35/39) and the contract, gate and trace
boundaries: pure against the fakes, on the session's injected clock."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import (
    GateDecision,
    Plan,
    Risk,
    Step,
    StepResult,
    Subgoal,
    Target,
)
from steerai.orchestrator.gate import Ask, Gate, effective_risk, params_for
from steerai.orchestrator.loop import REUSE_FRESHNESS_S, Session, _structural_part
from steerai.orchestrator.trace import JsonLinesTrace, NullTrace
from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    Focused,
    TitleMatches,
    ValueEquals,
    WindowGone,
)
from steerai.verify.t1 import redact
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import (
    COMPOSER,
    FakeConfirmer,
    FakeExecutor,
    FakeLive,
    FakeWorld,
    MemoryLedger,
    Row,
    build,
    not_delivered,
    uncertain,
)
from tests.orchestrator.test_loop import (
    APPEARED,
    CONVERSATION,
    HELLO,
    Harness,
    _click,
    _plan,
    _send,
    _type,
)

TWIN_SEND: Row = (11, "Button", "Send", (42, 43), 7)


def _row_id(h: Harness, name: str) -> int:
    return next(r[0] for r in h.world.rows if r[2] == name)


def _rebuild(h: Harness, *, live: Any | None = None, observer: Any | None = None) -> None:
    """A session over the harness's collaborators with a different live reader or observer."""
    h.session = Session(
        task_id="task-1",
        hwnd=0x10,
        observer=h.world if observer is None else observer,
        semantic=h.semantic,
        physical=h.physical,
        gate=Gate(confirmer=h.confirmer, own_pid=4242, task_id="task-1"),
        ledger=h.ledger,
        live=FakeLive(h.world) if live is None else live,
        trace=h.trace,
        monotonic=lambda: h.clock["t"],
        sleep=lambda s: h.clock.__setitem__("t", h.clock["t"] + s),
        verify_deadline_s=0.5,
    )


def _open_record(h: Harness, step: Step, before: Snapshot, phase: str = "acting") -> str:
    """A write-ahead record with no outcome: the crash happened after dispatch."""
    key = h.ledger.key_for(step)
    h.ledger.write_ahead(
        step, before, GateDecision(decision_id="d-x", action_key=key, outcome="confirm")
    )
    h.ledger.mark(step, phase)
    return key


# ---- contracts ----------------------------------------------------------------------------------


def test_target_identity_normalises_whitespace_case_and_compatibility_forms() -> None:
    base = Target(role="Button", name="Send").identity
    assert base == "Button|send||exact"
    assert Target(role="Button", name="  Send  ").identity == base
    assert Target(role="Button", name="SEND").identity == base
    assert Target(role="Button", name="\N{FULLWIDTH LATIN CAPITAL LETTER S}end").identity == base, (
        "NFKC: full-width S"
    )
    assert Target(role="Button", name="Send", element_id=9).identity == base, "ids are positional"
    assert Target(role="Edit", name="Send").identity != base
    assert Target(role="Button", name="Send", automation_id="send").identity != base
    assert Target(role="Button", name="Send now").identity != base


@pytest.mark.parametrize(
    ("declared", "effective"), [("R0", "R2"), ("R1", "R2"), ("R2", "R2"), ("R3", "R3")]
)
def test_submit_has_the_r2_floor_and_a_higher_plan_tier_is_kept(
    declared: Risk, effective: Risk
) -> None:
    step = Step(
        id="s", subgoal="g1", verb="submit", target=Target(role="Button", name="Go"), risk=declared
    )
    assert step.risk == effective


@pytest.mark.parametrize("composer", ["", "-1", "1.5", " 12", "twelve"])
def test_composer_id_that_is_not_an_element_id_is_refused_at_construction(composer: str) -> None:
    with pytest.raises(ValueError, match="composer_id"):
        Step(
            id="s",
            subgoal="g1",
            verb="send",
            target=Target(role="Button", name="Send"),
            risk="R2",
            params={"text": HELLO, "composer_id": composer},
        )


@pytest.mark.parametrize("composer", ["0", "12"])
def test_composer_id_zero_and_a_plain_integer_are_accepted(composer: str) -> None:
    step = Step(
        id="s",
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        risk="R2",
        params={"text": HELLO, "composer_id": composer, "scope": "Conversation"},
    )
    assert params_for(step).composer_id == int(composer)


def test_a_blocking_decision_can_never_be_consumed() -> None:
    decision = GateDecision(decision_id="d-1", action_key="k", outcome="block")
    assert decision.consume("k") is False
    assert decision.consumed is False
    assert decision.consume("k") is False


def test_step_result_without_an_action_serialises_to_plain_json() -> None:
    result = StepResult(
        step_id="s1",
        ok=False,
        gate="unreached",
        action=None,
        verify_code=None,
        label_kind=None,
        latency_ms={"observe": 1.5},
        evidence={"detail": "no_match"},
        code=ErrorCode.ELEMENT_NOT_FOUND,
    )
    payload = result.to_dict()
    assert payload["action"] is None
    assert payload["verify_code"] is None
    assert payload["code"] == "element_not_found"
    assert payload["gate"] == "unreached"
    assert json.loads(json.dumps(payload)) == payload


# ---- gate ---------------------------------------------------------------------------------------


def _named(name: str, risk: Risk = "R1") -> Step:
    return Step(
        id="c",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name=name),
        risk=risk,
        expect=TitleMatches(pattern="x"),
    )


@pytest.mark.parametrize(
    "name", ["Resend", "Sender", "Forwarded items", "Replying", "Payment history", "Shared with me"]
)
def test_deny_patterns_never_match_inside_longer_words(name: str) -> None:
    # OR-19: whole words only; "resend" is not "send".
    assert effective_risk(_named(name)) == "R1"


def test_deny_patterns_are_case_folded() -> None:
    assert effective_risk(_named("SEND")) == "R2"
    assert effective_risk(_named("Delete Chat")) == "R3"


@pytest.mark.parametrize(
    ("name", "tier"),
    [("Send.", "R2"), ("Delete,", "R3"), ("(Delete)", "R3"), ("Reply…", "R2"), ("“Send”", "R2")],
)
def test_deny_patterns_match_a_word_followed_or_wrapped_by_punctuation(
    name: str, tier: Risk
) -> None:
    # OR-19 / 5.13: the deny word is a whole word of the name even when punctuation touches it.
    assert effective_risk(_named(name)) == tier


def test_a_bound_no_blocks_and_a_no_for_another_decision_is_pending() -> None:
    # OR-13/14: the token must be exact and for the open ask's id, for "no" as for "yes".
    class BoundNo(FakeConfirmer):
        def ask(self, ask: Ask) -> str:
            self.asked.append(ask)
            return f"no:{ask.decision_id}"

    class StaleNo(FakeConfirmer):
        def ask(self, ask: Ask) -> str:
            self.asked.append(ask)
            return "no:d-stale"

    step = _send()
    bound = Gate(confirmer=BoundNo(), own_pid=4242, task_id="t").decide(
        step, action_key="k", expect=APPEARED, target_pid=777
    )
    assert bound.outcome == "block"
    assert bound.code is ErrorCode.GATE_BLOCKED
    assert bound.evidence["detail"] == "user_declined"
    stale = Gate(confirmer=StaleNo(), own_pid=4242, task_id="t").decide(
        step, action_key="k", expect=APPEARED, target_pid=777
    )
    assert stale.outcome == "pending"
    assert stale.code is ErrorCode.GATE_PENDING_USER


def test_ask_repr_names_every_token_and_no_content() -> None:
    ask = Ask(
        decision_id="d-1",
        task_id="task-1",
        action_key="k" * 64,
        verb="send",
        target_role="Button",
        risk="R2",
        expect=redact(APPEARED),
        params={
            "text": HELLO,
            "composer_name": "Message",
            "scope": "Conversation",
            "recipient": "Amir",
        },
        previous_attempt="acting",
    )
    text = repr(ask)
    for token in (
        "decision_id='d-1'",
        "task_id='task-1'",
        "verb='send'",
        "target_role='Button'",
        "risk='R2'",
        "previous_attempt='acting'",
        "'kind': 'Appeared'",
        "'within': {'kind': 'landmark'}",
    ):
        assert token in text, token
    for sentinel in (HELLO, "Message", "Conversation", "Amir", "params"):
        assert sentinel not in text, sentinel
    assert ask.params["recipient"] == "Amir", "the confirmer still renders the parameters"


# ---- traces -------------------------------------------------------------------------------------


def test_json_lines_trace_appends_across_instances_and_never_rewrites(tmp_path: Path) -> None:
    path = tmp_path / "traces" / "task-1.jsonl"
    first = JsonLinesTrace(path)
    first.write({"kind": "task", "b": 1, "a": "é"})
    first.write({"kind": "step"})
    JsonLinesTrace(path).write({"kind": "task_end"})
    assert path.read_text(encoding="utf-8").splitlines() == [
        '{"a": "é", "b": 1, "kind": "task"}',
        '{"kind": "step"}',
        '{"kind": "task_end"}',
    ]
    assert path.read_bytes().endswith(b"\n")
    NullTrace().write({"kind": "task"})


def test_trace_stays_redacted_on_failed_and_declined_paths() -> None:
    # OR-20 over the other exits: a verify timeout, a twin target and a declined ask.
    h = Harness(default="no")
    h.world.rows.append((12, "Button", "Chat options", (42, 24), 3))
    plan = _plan(
        _type(),
        _click(step_id="c1", expect=TitleMatches(pattern="Nothing - Chat")),
        _send(),
    )
    result = h.session.run_task(plan)
    assert result.status == "stopped"
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    declined = Harness(default="no")
    assert declined.session.run_task(_plan(_type(), _send())).status == "blocked"
    joined = "\n".join(h.trace.lines + declined.trace.lines)
    for sentinel in ("Amir", HELLO, "Message", "Conversation", "Nothing", "Chat options", "Send"):
        assert sentinel not in joined, sentinel
    assert '"detail": "ambiguous_target"' in joined
    assert '"detail": "user_declined"' in joined


# ---- resume guard (OR-02/03/04) ----------------------------------------------------------


def test_structural_part_skips_live_and_window_predicates() -> None:
    assert _structural_part(ValueEquals(target_id=1, text="x")) is None
    assert _structural_part(Focused(target_id=1)) is None
    assert _structural_part(TitleMatches(pattern="x")) is None
    assert _structural_part(All((ValueEquals(target_id=1, text="x"), APPEARED))) == APPEARED
    gone = Disappeared(role="Edit", name_pattern="Message")
    assert _structural_part(AnyOf((Focused(target_id=1), gone))) == gone
    nested = All((AnyOf((Focused(target_id=1), ValueEquals(target_id=1, text=""))), APPEARED))
    assert _structural_part(nested) == APPEARED
    count = CountDelta(role="Text", delta=1, within=CONVERSATION)
    assert _structural_part(All((count, ValueEquals(target_id=1, text="")))) == count
    assert _structural_part(WindowGone(name_pattern="Compose")) == WindowGone(
        name_pattern="Compose"
    )


def test_resume_ignores_the_live_parts_of_the_template_on_the_persisted_before() -> None:
    # OR-02: the composer is not empty any more; the structural part alone decides.
    h = Harness()
    h.session.run_step(_type())
    _open_record(h, _send(), h.world.observe(0x10))
    h.world.add_bubble(HELLO)
    h.world.composer_value = "a new draft"
    result = h.session.run_step(_send())
    assert result.gate == "done_already", result.to_dict()
    assert h.confirmer.asked == []
    assert h.semantic.calls == []


def test_resume_does_not_tick_done_on_a_count_rise_from_another_author() -> None:
    h = Harness(default="no")
    h.session.run_step(_type())
    step = Step(
        id="s3",
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        risk="R2",
        params={"text": HELLO},
        expect=All(
            (
                CountDelta(role="Text", delta=1, within=CONVERSATION),
                Appeared(role="Text", name_pattern=HELLO, within=CONVERSATION),
            )
        ),
    )
    _open_record(h, step, h.world.observe(0x10))
    h.world.add_bubble("are you there?")
    result = h.session.run_step(step)
    assert result.gate != "done_already", "a count rise from another author is not our send"
    assert h.confirmer.asked[0].previous_attempt == "acting"
    assert result.evidence["detail"] == "user_declined"
    assert h.world.sent == []


@pytest.mark.parametrize("mismatch", ["pid", "hwnd"])
def test_resume_refuses_a_persisted_before_of_another_window_or_process(mismatch: str) -> None:
    # OR-03: the effect is there relative to the record, but the record is not of this window.
    h = Harness(default="no")
    h.session.run_step(_type())
    before = h.world.observe(0x10)
    before = replace(before, pid=778) if mismatch == "pid" else replace(before, hwnd=0x11)
    _open_record(h, _send(), before)
    h.world.add_bubble(HELLO)
    result = h.session.run_step(_send())
    assert result.gate != "done_already"
    assert len(h.confirmer.asked) == 1
    assert h.confirmer.asked[0].previous_attempt == "acting"
    assert result.evidence["detail"] == "user_declined"
    assert h.semantic.calls == []


def test_resume_never_ticks_at_the_window_wide_scope() -> None:
    # OR-04: a window-wide `appeared` (level 3) is never enough to call a send done.
    h = Harness(default="no")
    h.session.run_step(_type())
    step = Step(
        id="s3",
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        risk="R2",
        params={"text": HELLO},
        expect=Appeared(role="Text", name_pattern=HELLO),
    )
    _open_record(h, step, h.world.observe(0x10))
    h.world.add_bubble(HELLO)
    result = h.session.run_step(step)
    assert result.gate != "done_already"
    assert h.confirmer.asked[0].previous_attempt == "acting"


def test_resume_resolves_the_composer_in_the_persisted_before_not_the_fresh_one() -> None:
    # The template's anchor is a snapshot id; the fresh snapshot's composer id names another
    # element in the persisted before when the tree grew in between, and the effect is hidden.
    persisted_rows: list[Row] = [
        (0, "Pane", "Amir Khan - Chat", (42, 1), None),
        (1, "Group", "Chats", (42, 10), 0),
        (2, "ListItem", "Amir · hi bro · 2h", (42, 12), 1),
        (3, "ListItem", "Sara · ok · 3h", (42, 13), 1),
        (4, "ListItem", "Bob · yes · 4h", (42, 14), 1),
        (5, "ListItem", "Dan · hm · 5h", (42, 15), 1),
        (6, "ListItem", "Eve · hi · 6h", (42, 16), 1),
        (7, "Group", "Header", (42, 20), 0),
        (8, "Button", "Delete chat", (42, 22), 7),
        (9, "Group", "Conversation", (42, 30), 0),
        (10, "Text", "ok", (42, 31), 9),
        (11, "Group", "Composer", (42, 40), 0),
        (12, "Edit", "Message", (42, 41), 11),
        (13, "Button", "Send", (42, 42), 11),
        (14, "Button", "Chat options", (42, 23), 7),
    ]
    h = Harness(default="no")
    h.session.run_step(_type())
    persisted = build(persisted_rows, snapshot_id="persisted", taken_at=50.0)
    _open_record(h, _send(), persisted)
    h.world.add_bubble(HELLO)
    assert (
        _row_id(h, "Message") == 9 == next(r[0] for r in persisted_rows if r[2] == "Conversation")
    )
    result = h.session.run_step(_send())
    assert result.gate == "done_already", result.to_dict()
    assert h.confirmer.asked == [], "a wrong 'not done' plus a yes would send twice"


# ---- act: scroll, retries, twins (OR-07/33/35) ---------------------------------------------------


def test_an_offscreen_target_is_scrolled_into_view_once_then_the_click_is_retried() -> None:
    h = Harness()
    h.session.run_step(_type())
    clicks = {"n": 0}

    def offscreen_first(executor: FakeExecutor, action: Any) -> None:
        if action.verb == "scroll_into_view":
            executor.outcomes.pop(action.element_id, None)  # the scroll brings it on screen
            return
        clicks["n"] += 1
        if clicks["n"] == 1:
            executor.outcomes[action.element_id] = not_delivered(
                ErrorCode.ELEMENT_NOT_ACTIONABLE, "offscreen"
            )

    h.semantic.on_execute = offscreen_first
    observed = len(h.world.observed)
    result = h.session.run_step(_send())
    assert result.ok is True, result.to_dict()
    assert [c[0] for c in h.semantic.calls] == ["click", "scroll_into_view", "click"]
    assert result.evidence["retries"] == 0, "a scroll is not the local retry"
    assert result.evidence["scrolled"] == "delivered_verified"
    assert len(h.confirmer.asked) == 1
    assert h.world.sent == [HELLO]
    assert len(h.world.observed) == observed + 4, "before, post-ask, post-scroll, after"


def test_nothing_delivered_after_the_scroll_releases_the_key_for_a_fresh_attempt() -> None:
    h = Harness()
    h.session.run_step(_type())
    h.semantic.outcomes[_row_id(h, "Send")] = not_delivered(
        ErrorCode.ELEMENT_NOT_ACTIONABLE, "clipped"
    )
    result = h.session.run_step(_send())
    assert [c[0] for c in h.semantic.calls] == ["click", "scroll_into_view", "click"], "one scroll"
    assert result.ok is False
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["delivery"] == "not_delivered"
    assert "scrolled" in result.evidence
    key = h.ledger.key_for(_send())
    assert h.ledger.executions[key]["outcome"] == "not_delivered"
    assert h.ledger.admit(key) == "fresh"
    assert h.world.sent == []
    h.semantic.outcomes.clear()
    again = h.session.run_step(_send())
    assert again.ok is True, again.to_dict()
    assert again.gate == "confirm"
    assert h.confirmer.asked[1].previous_attempt is None, "nothing was sent: a fresh attempt"
    assert h.world.sent == [HELLO]


def test_twin_targets_are_ambiguous_and_nothing_is_asked_written_or_sent() -> None:
    # OR-35: two "Send" buttons are ambiguity, never a pick.
    h = Harness()
    h.world.rows.append(TWIN_SEND)
    h.session.run_step(_type())
    result = h.session.run_step(_send())
    assert result.gate == "unreached"
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "ambiguous_target"
    assert h.confirmer.asked == []
    assert h.semantic.calls == []
    assert "s3" not in h.ledger.records


def test_a_retry_whose_re_resolve_is_ambiguous_fails_element_not_found_after_one_dispatch() -> None:
    h = Harness()
    h.session.run_step(_type())

    def twin_appears(executor: FakeExecutor, action: Any) -> None:
        if action.verb != "click":
            return
        executor.outcomes[action.element_id] = not_delivered(
            ErrorCode.ELEMENT_NOT_FOUND, "identity_changed"
        )
        executor.world.rows.append(TWIN_SEND)

    h.semantic.on_execute = twin_appears
    result = h.session.run_step(_send())
    assert result.ok is False
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "ambiguous_target"
    assert [c[0] for c in h.semantic.calls] == ["click"]
    assert h.world.sent == []
    key = h.ledger.key_for(_send())
    assert h.ledger.admit(key) == "fresh", "nothing was sent: the key is released (OR-07)"
    assert h.ledger.outcome_of(key) == "not_delivered"
    assert result.gate == "confirm", "the gate ran and a decision was consumed: not 'unreached'"


# ---- gate through the loop: focus, type template, composer by name -------------------------


@dataclass
class FocusLive:
    world: FakeWorld
    focused: tuple[int, ...] = ()

    def value(self, _handle: Any) -> str | None:
        return self.world.composer_value

    def toggle_state(self, _handle: Any) -> int | None:
        return None

    def is_selected(self, _handle: Any) -> bool | None:
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self.focused


def test_a_focus_step_is_verified_by_focused_at_r0_and_refused_at_r1() -> None:
    h = Harness()
    live = FocusLive(h.world)
    _rebuild(h, live=live)

    def takes_focus(_executor: FakeExecutor, _action: Any) -> None:
        live.focused = COMPOSER[3]

    h.physical.on_execute = takes_focus
    focus = Step(
        id="f1", subgoal="g1", verb="focus", target=Target(role="Edit", name="Message"), risk="R0"
    )
    result = h.session.run_step(focus)
    assert result.ok is True, result.to_dict()
    assert result.gate == "approve"
    assert result.label_kind == "t1"
    assert h.physical.calls == [("click", COMPOSER[0], None)]
    assert h.semantic.calls == []
    h.physical.calls.clear()
    refused = h.session.run_step(replace(focus, id="f2", risk="R1"))
    assert refused.gate == "block"
    assert refused.evidence["detail"] == "weak_predicate"
    assert refused.evidence["reason"] == "focused_alone"
    assert h.physical.calls == []


def test_a_focus_step_whose_target_never_takes_focus_fails_verify() -> None:
    h = Harness()
    _rebuild(h, live=FocusLive(h.world, focused=(42, 42)))
    focus = Step(
        id="f1", subgoal="g1", verb="focus", target=Target(role="Edit", name="Message"), risk="R0"
    )
    result = h.session.run_step(focus)
    assert result.ok is False
    assert result.verify_code is ErrorCode.VERIFY_FAILED
    assert result.evidence["reason"] == "not_focused"


def test_a_type_step_without_an_expect_gets_the_template_from_the_resolved_target() -> None:
    h = Harness()
    step = Step(
        id="s2",
        subgoal="g1",
        verb="type",
        target=Target(role="Edit", name="Message"),
        text=HELLO,
        risk="R1",
    )
    result = h.session.run_step(step)
    assert result.ok is True, result.to_dict()
    assert result.label_kind == "t1"
    assert h.physical.calls == [("type", COMPOSER[0], HELLO)]


def test_a_send_whose_named_composer_is_absent_stops_before_any_ask() -> None:
    h = Harness()
    h.world.rows = [r for r in h.world.rows if r[2] != "Message"]
    result = h.session.run_step(_send())
    assert result.ok is False
    assert h.confirmer.asked == []
    assert h.semantic.calls == []
    assert "s3" not in h.ledger.records
    # The composer is resolved while the gate's arguments are built, before `decide` runs: the
    # step is `unreached` with the element token; the result cannot say which element was absent.
    assert (result.gate, result.code, result.evidence["detail"]) == (
        "unreached",
        ErrorCode.ELEMENT_NOT_FOUND,
        "no_match",
    )


def test_a_send_whose_named_composer_is_a_twin_stops_before_any_ask() -> None:
    h = Harness()
    h.world.rows.append((11, "Edit", "Message", (42, 44), 7))
    result = h.session.run_step(_send())
    assert result.ok is False
    assert h.confirmer.asked == []
    assert h.semantic.calls == []
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "ambiguous_target"


# ---- snapshots: reuse boundaries (OR-21/30/39) --------------------------------------------------


def test_reuse_freshness_is_inclusive_at_the_limit_and_fresh_just_past_it() -> None:
    h = Harness()
    h.session.run_step(_type("t1"))
    observed = len(h.world.observed)
    h.clock["t"] += REUSE_FRESHNESS_S
    at_limit = h.session.run_step(_type("t2"))
    assert at_limit.evidence["observe_reused"] is True
    assert len(h.world.observed) == observed + 1, "the after only"
    h.clock["t"] += REUSE_FRESHNESS_S + 0.001
    past = h.session.run_step(_type("t3"))
    assert past.evidence["observe_reused"] is False
    assert len(h.world.observed) == observed + 3, "a fresh before and the after"
    assert set(past.latency_ms) >= {"observe", "decide", "gate", "act", "verify"}


def test_an_unstable_after_is_never_remembered_for_reuse() -> None:
    h = Harness()

    def destabilise(executor: FakeExecutor, _action: Any) -> None:
        executor.world.stable = False

    h.physical.on_execute = destabilise
    first = h.session.run_step(_type("t1"))
    assert first.ok is False
    assert first.evidence["reason"] == "unstable"
    h.physical.on_execute = None
    h.world.stable = True
    second = h.session.run_step(_type("t2"))
    assert second.evidence["observe_reused"] is False
    assert second.ok is True


def test_a_decided_after_of_another_window_is_not_reused() -> None:
    # Design 4 [v1.2]: reuse only "of the same window".
    world = FakeWorld()

    class Windowed:
        def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
            return replace(world.observe(hwnd, **kwargs), hwnd=hwnd)

    h = Harness()
    h.world = world
    h.semantic = FakeExecutor(world, "semantic")
    h.physical = FakeExecutor(world, "physical")
    _rebuild(h, observer=Windowed())
    first = h.session.run_step(_type("t1"))
    assert first.ok is True, first.to_dict()
    h.session.hwnd = 0x20
    second = h.session.run_step(_type("t2"))
    assert second.evidence["observe_reused"] is False
    assert second.ok is True, second.to_dict()


def test_one_unstable_observe_followed_by_a_stable_one_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OR-30 boundary: exactly one bounded re-observe.
    h = Harness()
    h.world.stable = False
    original = h.world.observe

    def settles(hwnd: int, **kwargs: Any) -> Snapshot:
        snapshot = original(hwnd, **kwargs)
        h.world.stable = True
        return snapshot

    monkeypatch.setattr(h.world, "observe", settles)
    result = h.session.run_step(_type())
    assert result.ok is True, result.to_dict()
    assert len(h.world.observed) == 3, "two befores, one after"


# ---- after the ask (OR-23/31) -------------------------------------------------------------------


def test_after_a_confirm_the_loop_observes_fresh_and_re_resolves_the_target() -> None:
    h = Harness()
    h.session.run_step(_type())
    send_before = _row_id(h, "Send")
    h.confirmer.on_ask = lambda _ask: h.world.add_bubble("incoming while you decide")
    observed = len(h.world.observed)
    result = h.session.run_step(_send())
    assert result.ok is True, result.to_dict()
    send_after = send_before + 1
    assert [c for c in h.semantic.calls if c[0] == "click"] == [("click", send_after, None)]
    assert len(h.world.observed) == observed + 3, "before, post-ask before, after"
    assert h.ledger.records["s3"].before.id == f"snap-{observed + 2}", (
        "the record holds the post-ask before"
    )


# ---- run_task: subgoals, max steps, screen guard (OR-25/27) --------------------------------


def test_run_task_skips_steps_of_subgoals_ticked_at_start_or_by_an_earlier_step() -> None:
    h = Harness()
    plan = Plan(
        goal="open the chat and send hello there",
        subgoals=(
            Subgoal(id="g0", desc="chat open", condition=TitleMatches(pattern="Amir Khan - Chat")),
            Subgoal(id="g1", desc="sent", condition=APPEARED),
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
            _type(),
            _send(),
            _send("s4"),
        ),
    )
    result = h.session.run_task(plan)
    assert result.status == "done", [r.to_dict() for r in result.results]
    assert result.ticked == {"g0", "g1"}
    assert [r.step_id for r in result.results] == ["s2", "s3"]
    assert h.world.sent == [HELLO]
    assert len(h.confirmer.asked) == 1


@pytest.mark.parametrize("max_steps", [0, -1])
def test_max_steps_at_or_below_zero_runs_nothing(max_steps: int) -> None:
    h = Harness()
    h.session.max_steps = max_steps
    result = h.session.run_task(_plan(_type(), _send()))
    assert result.status == "stopped"
    assert result.code is ErrorCode.BUDGET_EXCEEDED
    assert result.evidence["detail"] == "max_steps"
    assert result.results == []
    assert h.physical.calls == []
    assert len(h.world.observed) == 1, "the task-start observe only"


def test_max_steps_equal_to_the_plan_length_finishes() -> None:
    h = Harness()
    h.session.max_steps = 2
    result = h.session.run_task(_plan(_type(), _send()))
    assert result.status == "done", [r.to_dict() for r in result.results]
    assert len(result.results) == 2


def test_screen_guard_strikes_reset_after_an_ok_step_in_between() -> None:
    # Design 4 [v1.2]: "three in a row"; an ok step on the way breaks the row.
    h = Harness()

    def fruitless(i: int) -> Step:
        return _click(step_id=f"c{i}", expect=TitleMatches(pattern="Nothing - Chat"))

    plan = _plan(fruitless(1), fruitless(2), _type("t1"), fruitless(3), fruitless(4), fruitless(5))
    result = h.session.run_task(plan)
    assert result.status == "stopped"
    assert result.evidence["detail"] == "loop"
    assert [r.step_id for r in result.results] == ["c1", "c2", "t1", "c3", "c4", "c5"]


def test_screen_guard_does_not_count_a_fruitless_step_on_a_changed_screen() -> None:
    h = Harness()
    count = {"n": 0}

    def churn(executor: FakeExecutor, _action: Any) -> None:
        count["n"] += 1
        for i in range(3):
            executor.world.rows.append(
                (
                    20 + 3 * count["n"] + i,
                    "Group",
                    f"Panel {count['n']}-{i}",
                    (42, 500 + 3 * count["n"] + i),
                    0,
                )
            )

    h.semantic.on_execute = churn
    steps = tuple(
        _click(step_id=f"c{i}", expect=TitleMatches(pattern="Nothing - Chat")) for i in range(4)
    )
    result = h.session.run_task(_plan(*steps))
    assert all(r.ok is False for r in result.results)
    assert result.status == "stopped"
    assert result.evidence["detail"] == "unfinished", (
        "four fruitless steps, none on the same screen"
    )
    assert len(result.results) == 4


# ---- outcomes (OR-07/40) ------------------------------------------------------------------------


def test_outcome_tokens_release_the_key_only_when_nothing_was_delivered() -> None:
    outcome = Session._outcome
    verified = ActionResult(verb="click", delivery=Delivery.DELIVERED_VERIFIED)
    unverified = ActionResult(
        verb="click", delivery=Delivery.DELIVERED_UNVERIFIED, code=ErrorCode.VERIFY_TIMEOUT
    )
    nothing = not_delivered(ErrorCode.ELEMENT_NOT_FOUND, "no_match")
    assert outcome(True, unverified, None) == "delivered_verified"
    assert outcome(True, verified, "x") == "delivered_verified"
    assert outcome(False, nothing, "count_unchanged") == "not_delivered"
    for reason in ("unstable", "scope_churn", "scope_shifted", "anchor_vanished", "observe_failed"):
        assert outcome(False, unverified, reason) == "no_label", reason
    assert outcome(False, uncertain(), "count_unchanged") == "delivery_uncertain"
    assert outcome(False, unverified, "count_unchanged") == "count_unchanged"
    ledger = MemoryLedger(task_id="t", started_at=0.0)
    for token in (
        outcome(False, unverified, None),
        outcome(False, unverified, "evaluate_failed"),
        outcome(False, unverified, "siblings_changed"),
        outcome(False, uncertain(), None),
    ):
        ledger.executions["k"] = {"step_id": "s", "phase": "verified", "outcome": token}
        assert ledger.admit("k") == "confirm", token
