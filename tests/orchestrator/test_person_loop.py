"""Person targets through the loop (PT-01/04/05/06/08/09/15/17/18/26/29/30/36): the identity
ask at every tier, the binding carried through the step and the record, the resolved element
in the confirm ask and the templates, counts-only evidence."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from steerai.action.dispositions import Delivery
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.identity import Binding
from steerai.orchestrator.loop import Session
from steerai.verify.predicates import All, Appeared, Disappeared, TitleMatches, Within
from tests.orchestrator.fakes import FakeLive, Row, build
from tests.orchestrator.test_loop import Harness, _plan, _type

MID = chr(0xB7)
CYR_A = chr(0x0430)
AMIR_KHAN: Row = (15, "ListItem", f"Amir Khan {MID} ok {MID} 3d", (42, 17), 1)
LOOKALIKE: Row = (16, "ListItem", f"{CYR_A}mir {MID} hey {MID} 1h", (42, 18), 1)


def _person_click(name: str = "Amir", risk: str = "R1", step_id: str = "p1") -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb="click",
        target=Target(role="ListItem", name=name, match="person"),
        risk=risk,  # type: ignore[arg-type]
        expect=TitleMatches(pattern="Nothing - Chat"),  # the dispatch is what these tests check
    )


def _person_delete(name: str = "Amir") -> Step:
    return Step(
        id="d1",
        subgoal="g1",
        verb="delete",
        target=Target(role="ListItem", name=name, match="person"),
        risk="R3",
        params={"scope": "Chats"},
    )


def _answer_which(h: Harness, ordinal: int) -> list[Ask]:
    seen: list[Ask] = []

    def reply(ask: Ask) -> None:
        seen.append(ask)
        if ask.kind == "which":
            h.confirmer.answers.append(f"{ordinal}:{ask.decision_id}")

    h.confirmer.on_ask = reply
    return seen


# ---- one candidate ---------------------------------------------------------------------------


def test_a_single_candidate_resolves_without_an_ask() -> None:
    h = Harness()
    h.world.rows.append((18, "ListItem", f"Sara {MID} ok {MID} 3h", (42, 20), 1))
    result = h.session.run_step(_person_click("Sara"))
    assert result.action is not None, result.to_dict()
    assert result.action.delivery is Delivery.DELIVERED_VERIFIED
    assert h.confirmer.asked == []
    assert h.semantic.calls[0][1] == 18, "the Sara row"
    assert result.evidence["candidates"] == 1


def test_no_candidate_is_no_match_with_the_other_role_count() -> None:
    h = Harness()
    h.world.rows.append((17, "Button", "Call Amir Khan", (42, 19), 7))
    result = h.session.run_step(_person_click("Zed"))
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "no_match"
    result = h.session.run_step(_person_click("Call Amir"))
    assert result.evidence["detail"] == "no_match"
    assert result.evidence["other_role_matches"] == 1


# ---- the identity ask -------------------------------------------------------------------------


def test_two_candidates_ask_which_at_r1_and_the_answer_picks_the_row() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    seen = _answer_which(h, 2)
    result = h.session.run_step(_person_click("Amir"))
    assert result.action is not None, result.to_dict()
    assert [a.kind for a in seen] == ["which"]
    assert len(seen[0].candidates) == 2
    assert seen[0].candidates[1].name_part == "Amir Khan"
    assert seen[0].candidates[1].preview == f"ok {MID} 3d"
    assert h.semantic.calls[0][1] == 15, "the chosen row, not the first"
    assert result.evidence["candidates"] == 2
    assert result.evidence["chosen_index"] == 2
    assert "Amir" not in repr(seen[0])


def test_a_lookalike_only_candidate_always_asks_and_is_tagged() -> None:
    h = Harness()
    h.world.rows = [r for r in h.world.rows if not (r[1] == "ListItem" and r[2].startswith("Amir"))]
    h.world.rows.append(LOOKALIKE)
    seen = _answer_which(h, 1)
    result = h.session.run_step(_person_click("Amir"))
    assert result.action is not None, result.to_dict()
    assert len(seen) == 1
    assert "lookalike" in seen[0].candidates[0].tags
    assert "mixed script" in seen[0].candidates[0].tags, "one Cyrillic letter among Latin"
    assert result.evidence["lookalikes"] == 1


def test_no_to_the_which_ask_blocks_as_none_chosen() -> None:
    h = Harness(default="no")
    h.world.rows.append(AMIR_KHAN)
    task = h.session.run_task(_plan(_person_click("Amir"), _type()))
    assert task.status == "blocked"
    assert task.evidence["detail"] == "none_chosen"
    assert h.semantic.calls == []


def test_an_unanswered_which_ask_pauses() -> None:
    h = Harness(default="")
    h.world.rows.append(AMIR_KHAN)
    task = h.session.run_task(_plan(_person_click("Amir"), _type()))
    assert task.status == "paused"
    assert task.code is ErrorCode.GATE_PENDING_USER
    assert h.semantic.calls == []


def test_more_candidates_than_the_cap_is_ambiguous_with_the_count() -> None:
    h = Harness()
    for i in range(13):
        h.world.rows.append((40 + i, "ListItem", f"Amir {i} {MID} hi", (42, 100 + i), 1))
    result = h.session.run_step(_person_click("Amir"))
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "ambiguous_target"
    assert result.evidence["candidates"] == 14
    assert h.confirmer.asked == []


# ---- the binding --------------------------------------------------------------------------------


def test_the_choice_binds_the_identity_so_a_reordered_list_still_hits_the_chosen_row() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    seen = _answer_which(h, 2)
    original = h.confirmer.on_ask

    def reply_then_reorder(ask: Ask) -> None:
        assert original is not None
        original(ask)
        if ask.kind == "yes":
            # a new row lands on top between the answer and the re-observe: ids shift
            h.world.rows.insert(1, (99, "ListItem", f"Newcomer {MID} hi {MID} now", (42, 99), 1))

    h.confirmer.on_ask = reply_then_reorder
    result = h.session.run_step(_person_delete("Amir"))
    assert result.ok is False, "the fake does not remove the row; the dispatch is what matters"
    clicked = h.semantic.calls[0]
    assert clicked[0] == "click"
    row = next(r for r in h.world.rows if r[0] == clicked[1])
    assert row[3] == (42, 17), "the chosen RuntimeId, whatever its id now"
    assert [a.kind for a in seen] == ["which", "yes"]


def test_the_chosen_row_gone_after_the_re_observe_is_candidate_gone() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    _answer_which(h, 2)
    original = h.confirmer.on_ask

    def reply_then_remove(ask: Ask) -> None:
        assert original is not None
        original(ask)
        if ask.kind == "yes":
            h.world.rows = [r for r in h.world.rows if r[3] != (42, 17)]

    h.confirmer.on_ask = reply_then_remove
    result = h.session.run_step(_person_delete("Amir"))
    assert result.ok is False
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "candidate_gone"
    assert h.semantic.calls == []


def test_the_confirm_ask_shows_the_resolved_row_and_the_template_uses_its_full_name() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    seen = _answer_which(h, 2)
    h.session.run_step(_person_delete("Amir"))
    yes = next(a for a in seen if a.kind == "yes")
    assert yes.target_name == f"Amir Khan {MID} ok {MID} 3d"
    assert yes.expect["kind"] == "All", "the delete template: Disappeared and CountDelta"
    expect = h.ledger.records["d1"].chosen
    assert expect is not None
    assert expect["name_part"] == "amir khan", "the comparable name part"
    assert expect["full_name"] == f"Amir Khan {MID} ok {MID} 3d"
    assert expect["runtime_id"] == [42, 17]
    assert "Amir" not in repr(Binding.from_record(expect)), "the binding's repr hides names"


def test_the_confirm_template_names_the_bound_row() -> None:
    from steerai.orchestrator.gate import Gate  # noqa: PLC0415

    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    captured: list[Any] = []
    gate = Gate(confirmer=h.confirmer, own_pid=4242, task_id="task-1")
    original_decide = gate.decide

    def spy(step: Step, **kwargs: Any) -> Any:
        result = original_decide(step, **kwargs)
        captured.append(result.expect)
        return result

    gate.decide = spy  # type: ignore[method-assign]  # a spy on the gate's predicate
    h.session = Session(
        task_id="task-1",
        hwnd=0x10,
        observer=h.world,
        semantic=h.semantic,
        physical=h.physical,
        gate=gate,
        ledger=h.ledger,
        live=FakeLive(h.world),
        trace=h.trace,
        monotonic=lambda: h.clock["t"],
        sleep=lambda s: h.clock.__setitem__("t", h.clock["t"] + s),
        verify_deadline_s=0.5,
    )
    _answer_which(h, 2)
    h.session.run_step(_person_delete("Amir"))
    predicate = captured[-1]
    assert isinstance(predicate, All)
    gone = predicate.parts[0]
    assert isinstance(gone, Disappeared)
    assert gone.name_pattern == f"Amir Khan {MID} ok {MID} 3d"


# ---- resume and done keys ---------------------------------------------------------------------


def test_a_done_key_never_asks_which() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    _answer_which(h, 2)
    first = h.session.run_step(_person_delete("Amir"))
    assert first.ok is False  # the fake does not remove rows; the key is consumed by the attempt
    key = h.ledger.key_for(_person_delete("Amir"))
    h.ledger.executions[key]["outcome"] = "delivered_verified"
    h.confirmer.asked.clear()
    again = h.session.run_step(_person_delete("Amir"))
    assert again.gate == "done_already"
    assert h.confirmer.asked == []


def test_a_resume_with_the_chosen_row_gone_never_dispatches_to_another() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    _answer_which(h, 2)
    step = Step(
        id="c2",
        subgoal="g1",
        verb="click",
        target=Target(role="ListItem", name="Amir", match="person"),
        risk="R2",
        params={"text": "opened"},  # an R2 pattern must be one of the step's parameters
        expect=Appeared(role="Text", name_pattern="opened", within=Within(landmark="Conversation")),
    )
    h.session.run_step(step)
    record = h.ledger.records["c2"]
    h.ledger.executions[h.ledger.key_for(step)]["outcome"] = None
    h.ledger.records["c2"] = replace(record, phase="acting")
    h.world.rows = [r for r in h.world.rows if r[3] != (42, 17)]
    h.confirmer.asked.clear()
    dispatched_before = len(h.semantic.calls)
    result = h.session.run_step(step)
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "candidate_gone"
    assert len(h.semantic.calls) == dispatched_before, "never the other Amir"
    assert h.confirmer.asked == [], "the record's choice stands; no second which-ask"


def test_a_resumed_delete_whose_chosen_row_is_gone_is_done_already() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    _answer_which(h, 2)
    h.session.run_step(_person_delete("Amir"))
    record = h.ledger.records["d1"]
    h.ledger.executions[h.ledger.key_for(_person_delete("Amir"))]["outcome"] = None
    h.ledger.records["d1"] = replace(record, phase="acting")
    h.world.rows = [r for r in h.world.rows if r[3] != (42, 17)]
    result = h.session.run_step(_person_delete("Amir"))
    assert result.gate == "done_already", "the structural effect holds against the persisted before"


# ---- the trace ---------------------------------------------------------------------------------


def test_the_trace_never_carries_candidate_names_previews_or_the_chosen_row() -> None:
    h = Harness(default="no")
    h.world.rows.append(AMIR_KHAN)
    h.world.rows.append(LOOKALIKE)
    h.session.run_task(_plan(_person_click("Amir"), _type()))
    _answer_which(h, 2)
    h.confirmer.default = "yes"
    h.session.run_step(_person_delete("Amir"))
    text = "\n".join(h.trace.lines)
    for sentinel in ("Amir", "Khan", "hi bro", "3d", CYR_A, "(42, 17)", "[42, 17]"):
        assert sentinel not in text, sentinel
    assert '"candidates": 3' in text


def test_the_which_ask_is_bound_to_its_id_and_rejects_yes_and_out_of_range() -> None:
    from steerai.orchestrator.gate import Gate  # noqa: PLC0415
    from tests.orchestrator.fakes import FakeConfirmer  # noqa: PLC0415

    for answer, outcome in (
        ("yes", "pending"),
        ("3:ID", "pending"),
        ("0:ID", "pending"),
        ("no", "block"),
    ):
        confirmer = FakeConfirmer()

        def reply(ask: Ask, a: str = answer, c: FakeConfirmer = confirmer) -> None:
            c.answers.append(a.replace("ID", ask.decision_id))

        confirmer.on_ask = reply
        gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
        snapshot = build(
            [
                (0, "Pane", "x", (42, 1), None),
                (1, "ListItem", "Amir", (42, 2), 0),
                (2, "ListItem", "Amir K", (42, 3), 0),
            ],
            snapshot_id="s",
            taken_at=1.0,
        )
        result = gate.which(_person_click("Amir"), [snapshot.element(1), snapshot.element(2)])
        assert result.outcome == outcome, answer
    confirmer = FakeConfirmer()
    confirmer.on_ask = lambda ask: confirmer.answers.append(f"2:{ask.decision_id}")
    result = Gate(confirmer=confirmer, own_pid=1, task_id="t").which(
        _person_click("Amir"), [snapshot.element(1), snapshot.element(2)]
    )
    assert result.outcome == "chosen"
    assert result.index == 2


# ---- the principal's implementation review ---------------------------------------------------


def test_the_binding_refreshes_its_text_after_every_rebind() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    seen = _answer_which(h, 2)
    original = h.confirmer.on_ask

    def answer_then_tick(ask: Ask) -> None:
        assert original is not None
        original(ask)
        if ask.kind == "which":
            # the age ticks between the which-ask and the confirm's re-observe
            h.world.rows = [
                (15, "ListItem", f"Amir Khan {MID} ok {MID} 4d", (42, 17), 1) if r[0] == 15 else r
                for r in h.world.rows
            ]

    h.confirmer.on_ask = answer_then_tick

    def remove_on_click(executor: Any, action: Any) -> None:
        if action.verb == "click":
            executor.world.rows = [r for r in executor.world.rows if r[0] != action.element_id]

    h.semantic.on_execute = remove_on_click
    result = h.session.run_step(_person_delete("Amir"))
    assert result.ok is True, ("the template followed the renamed row", result.to_dict())
    yes = next(a for a in seen if a.kind == "yes")
    assert yes.target_name == f"Amir Khan {MID} ok {MID} 3d", (
        "the ask-time text: no observe between"
    )
    chosen = h.ledger.records["d1"].chosen
    assert chosen is not None
    assert chosen["full_name"] == f"Amir Khan {MID} ok {MID} 4d"


def test_two_rows_with_the_same_full_name_never_rebind_by_name() -> None:
    h = Harness()
    twin_a: Row = (21, "ListItem", f"Amir {MID} Sent a photo {MID} 2h", (42, 31), 1)
    twin_b: Row = (22, "ListItem", f"Amir {MID} Sent a photo {MID} 2h", (42, 32), 1)
    h.world.rows = [r for r in h.world.rows if not (r[1] == "ListItem" and r[2].startswith("Amir"))]
    h.world.rows.extend([twin_a, twin_b])
    _answer_which(h, 1)
    original = h.confirmer.on_ask

    def answer_then_rerender(ask: Ask) -> None:
        assert original is not None
        original(ask)
        if ask.kind == "yes":
            h.world.rows = [r for r in h.world.rows if r[3] != (42, 31)]  # the chosen node is gone

    h.confirmer.on_ask = answer_then_rerender
    result = h.session.run_step(_person_delete("Amir"))
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "candidate_gone"
    assert h.semantic.calls == [], "never the twin by name"


def _strong_person_click(step_id: str = "p1") -> Step:
    """A person click whose expectation is strong enough for R3 (a parameter-backed Appeared)."""
    return Step(
        id=step_id,
        subgoal="g1",
        verb="click",
        target=Target(role="ListItem", name="Amir", match="person"),
        risk="R1",
        params={"text": "opened"},
        expect=Appeared(role="Text", name_pattern="opened", within=Within(landmark="Conversation")),
    )


def test_a_raised_record_is_honoured_whatever_row_the_person_picks_next() -> None:
    """An earlier attempt bound a row whose own name raised the step to R3 and left an open
    record; the plan says R1. The next attempt must consult the ledger before any ask."""
    h = Harness()
    raised: Row = (23, "ListItem", f"Amir delete {MID} hi {MID} 1h", (42, 41), 1)
    h.world.rows.append(raised)
    seen = _answer_which(h, 2)  # the raised row is second in document order
    first = h.session.run_step(_strong_person_click())
    assert first.evidence["risk"] == "R3"
    assert [a.kind for a in seen] == ["which", "yes"]
    assert "p1" in h.ledger.records, "an R3 step writes a record"
    key = h.ledger.key_for(_strong_person_click())
    h.ledger.executions[key]["outcome"] = None
    h.ledger.records["p1"] = replace(h.ledger.records["p1"], phase="acting")
    h.confirmer.asked.clear()
    seen = _answer_which(h, 1)  # the person would now pick the plain row
    second = h.session.run_step(_strong_person_click())
    assert [a.kind for a in seen] == ["yes"], "the open record re-binds; no second which-ask"
    assert seen[0].previous_attempt == "acting"
    assert second.evidence["risk"] == "R3"
    assert h.semantic.calls[-1][1] == 23, "the record's row, not the plain one"


def test_a_done_raised_key_never_asks_which() -> None:
    h = Harness()
    raised: Row = (23, "ListItem", f"Amir delete {MID} hi {MID} 1h", (42, 41), 1)
    h.world.rows.append(raised)
    _answer_which(h, 2)
    h.session.run_step(_strong_person_click())
    key = h.ledger.key_for(_strong_person_click())
    h.ledger.executions[key]["outcome"] = "delivered_verified"
    h.confirmer.asked.clear()
    again = h.session.run_step(_strong_person_click())
    assert again.gate == "done_already"
    assert h.confirmer.asked == []


def test_an_open_record_is_found_even_when_the_raising_row_has_left_the_screen() -> None:
    h = Harness()
    raised: Row = (23, "ListItem", f"Amir delete {MID} hi {MID} 1h", (42, 41), 1)
    h.world.rows.append(raised)
    _answer_which(h, 2)
    first = h.session.run_step(_strong_person_click())
    assert first.evidence["risk"] == "R3"
    key = h.ledger.key_for(_strong_person_click())
    h.ledger.executions[key]["outcome"] = None
    h.ledger.records["p1"] = replace(h.ledger.records["p1"], phase="acting")
    h.world.rows = [r for r in h.world.rows if r[3] != (42, 41)]  # the raising row is gone
    h.confirmer.asked.clear()
    dispatched_before = len(h.semantic.calls)
    second = h.session.run_step(_strong_person_click())
    assert h.confirmer.asked == [], "no which-ask: the open record was found first"
    assert second.evidence["observe_reused"] is False, "a lifted ceiling discards the reused before"
    assert second.code is ErrorCode.ELEMENT_NOT_FOUND
    assert second.evidence["detail"] == "candidate_gone"
    assert len(h.semantic.calls) == dispatched_before, "the plain row is never clicked"


def test_the_rebuilt_expectation_goes_through_the_gates_strength_rung() -> None:
    from steerai.orchestrator.gate import Gate, GateResult  # noqa: PLC0415

    class Weakening(Gate):
        def postcondition(self, _step: Step, _expect: Any, risk: Any, _params: Any) -> Any:
            return GateResult(
                outcome="block",
                decision=None,
                code=ErrorCode.GATE_BLOCKED,
                evidence={"risk": risk, "detail": "weak_predicate"},
            )

    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    h.session = Session(
        task_id="task-1",
        hwnd=0x10,
        observer=h.world,
        semantic=h.semantic,
        physical=h.physical,
        gate=Weakening(confirmer=h.confirmer, own_pid=4242, task_id="task-1"),
        ledger=h.ledger,
        live=FakeLive(h.world),
        trace=h.trace,
        monotonic=lambda: h.clock["t"],
        sleep=lambda s: h.clock.__setitem__("t", h.clock["t"] + s),
        verify_deadline_s=0.5,
    )
    _answer_which(h, 2)
    result = h.session.run_step(_person_delete("Amir"))
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "weak_predicate"
    assert h.world.sent == []
    assert h.semantic.calls == [], "nothing dispatched on a weaker check"
