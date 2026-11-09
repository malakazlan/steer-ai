"""Person targets through the loop, the hunter's under-tested rows (PT-01/05/06/16/17/18/26/
29/31/36/38): the ask at R0 and R3, the resume on the record's row, one ask per step across
the scroll and retry rungs, the cap boundary, admission before resolve, deny patterns on the
bound row, a released key after `not_delivered`, and a sentinel scan over every path."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

import pytest

from steerai.action.dispositions import Delivery
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, StepResult, Target
from steerai.orchestrator.gate import Ask
from steerai.verify.predicates import Appeared, Within
from tests.orchestrator.fakes import FakeExecutor, Row, not_delivered
from tests.orchestrator.test_loop import Harness, _plan, _type
from tests.orchestrator.test_person_loop import (
    AMIR_KHAN,
    LOOKALIKE,
    MID,
    _answer_which,
    _person_click,
    _person_delete,
)
from tests.orchestrator.test_takeover_loop import FakeMonitor, _wire

CYR_A = "\u0430"
CYR_CAP_A = "\u0410"
CYR_AMIR = "\u0410\u043c\u0438\u0440"  # Amir in Cyrillic letters; "\u0438" has no Latin prototype
KHAN_RID = (42, 17)


def _r2_click(step_id: str = "c2", name: str = "Amir") -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb="click",
        target=Target(role="ListItem", name=name, match="person"),
        risk="R2",
        params={"text": "opened"},
        expect=Appeared(role="Text", name_pattern="opened", within=Within(landmark="Conversation")),
    )


def _r1_click(name: str, step_id: str = "p1") -> Step:
    """An R1 person click whose expectation stays strong if the deny patterns raise the tier."""
    return replace(_r2_click(step_id=step_id, name=name), risk="R1")


def _without_base_amir(h: Harness) -> None:
    h.world.rows = [r for r in h.world.rows if r[3] != (42, 12)]


def _clicked_runtime_ids(h: Harness) -> list[tuple[int, ...]]:
    by_id = {r[0]: r for r in h.world.rows}
    return [by_id[c[1]][3] for c in h.semantic.calls if c[0] == "click" and c[1] in by_id]


def _crash_after_record(h: Harness, step: Step) -> None:
    record = h.ledger.records[step.id]
    h.ledger.executions[h.ledger.key_for(step)]["outcome"] = None
    h.ledger.records[step.id] = replace(record, phase="acting")


# ---- PT-01: a lookalike-only single candidate asks at every tier ---------------------------------


def test_a_lookalike_only_single_candidate_asks_at_r0() -> None:
    h = Harness()
    _without_base_amir(h)
    h.world.rows.append(LOOKALIKE)
    seen = _answer_which(h, 1)
    result = h.session.run_step(_person_click("Amir", risk="R0"))
    assert [a.kind for a in seen] == ["which"], "R0 is not a silent tier for a lookalike"
    assert seen[0].risk == "R0"
    assert "lookalike" in seen[0].candidates[0].tags
    assert result.action is not None, result.to_dict()
    assert _clicked_runtime_ids(h) == [(42, 18)]
    assert result.evidence["candidates"] == 1
    assert result.evidence["lookalikes"] == 1


def test_a_lookalike_only_single_candidate_asks_which_then_yes_at_r3() -> None:
    h = Harness()
    _without_base_amir(h)
    h.world.rows.append(LOOKALIKE)
    seen = _answer_which(h, 1)
    result = h.session.run_step(_person_delete("Amir"))
    assert [a.kind for a in seen] == ["which", "yes"]
    assert seen[1].target_name == LOOKALIKE[2], "the confirm names the resolved lookalike row"
    assert seen[1].risk == "R3"
    assert _clicked_runtime_ids(h) == [(42, 18)]
    assert result.evidence["lookalikes"] == 1
    chosen = h.ledger.records["d1"].chosen
    assert chosen is not None
    assert chosen["name_part"] == "amir", "the comparable form folds the Cyrillic a"


def test_a_lookalike_that_does_not_fold_is_no_match_and_never_asks() -> None:
    h = Harness()
    _without_base_amir(h)
    h.world.rows.append((16, "ListItem", f"{CYR_AMIR} {MID} hey {MID} 1h", (42, 18), 1))
    result = h.session.run_step(_person_click("Amir"))
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "no_match"
    assert result.evidence["candidates"] == 0
    assert h.confirmer.asked == []
    assert h.semantic.calls == []


def test_a_no_to_the_lookalike_ask_at_r0_blocks_none_chosen() -> None:
    h = Harness(default="no")
    _without_base_amir(h)
    h.world.rows.append(LOOKALIKE)
    task = h.session.run_task(_plan(_person_click("Amir", risk="R0"), _type()))
    assert task.status == "blocked"
    assert task.evidence["detail"] == "none_chosen"
    assert task.results[0].gate == "block"
    assert h.semantic.calls == []


# ---- PT-05/09: resume on the record's row --------------------------------------------------------


def test_a_resume_with_the_chosen_row_present_confirms_on_the_records_row_without_asking() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    _answer_which(h, 2)
    step = _r2_click()
    h.session.run_step(step)
    _crash_after_record(h, step)
    # the other Amir is gone and the chosen row's tail moved on: the identity is what survives
    _without_base_amir(h)
    h.world.rows = [
        (r[0], r[1], f"Amir Khan {MID} ok {MID} 4d", r[3], r[4]) if r[3] == KHAN_RID else r
        for r in h.world.rows
    ]
    h.confirmer.asked.clear()
    h.confirmer.on_ask = None  # a which-ask now would be answered "yes" and pause
    before = len(h.semantic.calls)
    result = h.session.run_step(step)
    assert [a.kind for a in h.confirmer.asked] == ["yes"], "the record's choice stands"
    yes = h.confirmer.asked[0]
    assert yes.previous_attempt == "acting"
    current = next(r[2] for r in h.world.rows if r[3] == AMIR_KHAN[3])
    assert yes.target_name == current, "the confirm shows the row as the screen shows it now"
    assert result.gate == "confirm"
    assert _clicked_runtime_ids(h)[before:] == [KHAN_RID]
    assert "candidates" not in result.evidence, "no fresh pick on a resume"


def test_a_resume_whose_chosen_row_is_present_twice_by_name_is_candidate_gone() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    _answer_which(h, 2)
    step = _r2_click()
    h.session.run_step(step)
    _crash_after_record(h, step)
    # the RuntimeId is gone; two rows carry the chosen full name: not unique, nothing dispatched
    h.world.rows = [r for r in h.world.rows if r[3] != KHAN_RID]
    h.world.rows.append((15, "ListItem", AMIR_KHAN[2], (42, 70), 1))
    h.world.rows.append((16, "ListItem", AMIR_KHAN[2], (42, 71), 1))
    h.confirmer.asked.clear()
    before = len(h.semantic.calls)
    result = h.session.run_step(step)
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "candidate_gone"
    assert len(h.semantic.calls) == before
    assert h.confirmer.asked == []


def test_a_resumed_delete_is_not_done_when_only_the_other_candidate_disappeared() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    _answer_which(h, 2)
    h.session.run_step(_person_delete("Amir"))
    _crash_after_record(h, _person_delete("Amir"))
    _without_base_amir(h)  # the row the person did NOT choose is gone
    h.confirmer.asked.clear()
    h.confirmer.on_ask = None
    result = h.session.run_step(_person_delete("Amir"))
    assert result.gate != "done_already", "the resume predicate names the chosen row"
    assert [a.kind for a in h.confirmer.asked] == ["yes"]
    assert h.confirmer.asked[0].previous_attempt == "acting"
    assert h.confirmer.asked[0].target_name == AMIR_KHAN[2]


# ---- PT-06: one ask per step across the scroll and retry rungs -----------------------------------


def test_the_retry_re_resolves_the_bound_row_without_a_second_ask() -> None:
    """The stale-handle retry re-resolves the row the person chose (the R2/R3 scroll rung is
    gone since #184, hunter OS-01r: an offscreen target is scrolled before the gate)."""
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    seen = _answer_which(h, 2)
    clicks = {"n": 0}

    def stale_first(executor: FakeExecutor, action: Any) -> None:
        clicks["n"] += 1
        if clicks["n"] == 1:
            executor.outcomes[action.element_id] = not_delivered(
                ErrorCode.ELEMENT_NOT_FOUND, "stale"
            )
        else:
            executor.outcomes.pop(action.element_id, None)

    h.semantic.on_execute = stale_first
    result = h.session.run_step(_r2_click())
    assert [c[0] for c in h.semantic.calls] == ["click", "click"]
    assert [a.kind for a in seen] == ["which", "yes"], "one identity ask"
    assert result.evidence["retries"] == 1
    assert result.action is not None
    assert result.action.delivery is Delivery.DELIVERED_VERIFIED
    assert set(_clicked_runtime_ids(h)) == {KHAN_RID}
    assert result.evidence["candidates"] == 2
    assert result.evidence["chosen_index"] == 2


def test_an_offscreen_refusal_on_the_bound_row_at_r2_is_never_scrolled() -> None:
    """Hunter OS-01r on #184: no scroll after the write-ahead; the refusal stands."""
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    seen = _answer_which(h, 2)

    def offscreen(executor: FakeExecutor, action: Any) -> None:
        executor.outcomes[action.element_id] = not_delivered(
            ErrorCode.ELEMENT_NOT_ACTIONABLE, "offscreen"
        )

    h.semantic.on_execute = offscreen
    result = h.session.run_step(_r2_click())
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert [c[0] for c in h.semantic.calls] == ["click"]
    assert [a.kind for a in seen] == ["which", "yes"]
    key = h.ledger.key_for(_r2_click())
    assert h.ledger.executions[key]["outcome"] == "not_delivered", "nothing sent: key released"


# ---- PT-16/17: ordinals map to the ask-time list; the choice binds the identity ------------------


def test_the_ordinal_maps_to_the_ask_time_list_in_document_order() -> None:
    for ordinal, runtime_id in ((1, (42, 12)), (2, KHAN_RID), (3, (42, 18))):
        h = Harness()
        h.world.rows.append(AMIR_KHAN)
        h.world.rows.append(LOOKALIKE)
        seen = _answer_which(h, ordinal)
        h.session.run_step(_person_click("Amir"))
        assert [line.index for line in seen[0].candidates] == [1, 2, 3]
        assert _clicked_runtime_ids(h) == [runtime_id], ordinal


def test_a_third_candidate_appearing_before_the_re_observe_does_not_invalidate_the_choice() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    seen = _answer_which(h, 2)
    original = h.confirmer.on_ask

    def reply_then_newcomer(ask: Ask) -> None:
        assert original is not None
        original(ask)
        if ask.kind == "yes":
            h.world.rows.insert(2, (99, "ListItem", f"Amir Zed {MID} yo {MID} now", (42, 99), 1))

    h.confirmer.on_ask = reply_then_newcomer
    result = h.session.run_step(_person_delete("Amir"))
    assert [a.kind for a in seen] == ["which", "yes"], "a third candidate is not a second ask"
    assert _clicked_runtime_ids(h) == [KHAN_RID]
    assert result.evidence["candidates"] == 2, "the ask-time count"
    assert result.evidence["chosen_index"] == 2


@pytest.mark.xfail(strict=True, reason="PT-17 optional: candidates_after is not recorded")
def test_the_evidence_carries_the_candidate_count_after_the_re_observe() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    _answer_which(h, 2)
    original = h.confirmer.on_ask

    def reply_then_newcomer(ask: Ask) -> None:
        assert original is not None
        original(ask)
        if ask.kind == "yes":
            h.world.rows.insert(2, (99, "ListItem", f"Amir Zed {MID} yo {MID} now", (42, 99), 1))

    h.confirmer.on_ask = reply_then_newcomer
    result = h.session.run_step(_person_delete("Amir"))
    assert result.evidence.get("candidates_after") == 3, result.evidence


# ---- PT-26: the cap boundary ---------------------------------------------------------------------


def _amir_rows(count: int) -> list[Row]:
    return [(40 + i, "ListItem", f"Amir {i} {MID} hi", (42, 100 + i), 1) for i in range(count)]


def test_twelve_candidates_ask_and_thirteen_are_ambiguous() -> None:
    h = Harness()
    h.world.rows.extend(_amir_rows(11))  # with the base Amir row: twelve
    seen = _answer_which(h, 12)
    result = h.session.run_step(_person_click("Amir"))
    assert [a.kind for a in seen] == ["which"]
    assert len(seen[0].candidates) == 12
    assert result.evidence["candidates"] == 12
    assert result.evidence["chosen_index"] == 12
    assert _clicked_runtime_ids(h) == [(42, 110)]

    h = Harness()
    h.world.rows.extend(_amir_rows(12))  # thirteen
    result = h.session.run_step(_person_click("Amir"))
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "ambiguous_target"
    assert result.evidence["candidates"] == 13
    assert "chosen_index" not in result.evidence
    assert h.confirmer.asked == []
    assert h.semantic.calls == []


# ---- PT-29: admission before the identity ask ----------------------------------------------------


def test_a_done_key_is_done_already_even_when_the_rows_would_be_ambiguous_or_absent() -> None:
    for rows in (_amir_rows(12), []):
        h = Harness()
        h.world.rows.extend(rows)
        if not rows:
            _without_base_amir(h)
        step = _person_delete("Amir")
        key = h.ledger.key_for(step)
        h.ledger.executions[key] = {"step_id": "d1", "phase": "verified", "outcome": "done_already"}
        result = h.session.run_step(step)
        assert result.gate == "done_already", result.to_dict()
        assert result.ok is True
        assert "candidates" not in result.evidence, "no resolve ran before admission"
        assert h.confirmer.asked == []


# ---- PT-31: deny patterns on the bound name part -------------------------------------------------


def _delete_chat_row() -> Row:
    return (16, "ListItem", f"Delete chat {MID} hi {MID} 2h", (42, 18), 1)


def _amir_delete_row() -> Row:
    return (16, "ListItem", f"Amir delete {MID} hi {MID} 2h", (42, 18), 1)


def test_a_row_named_delete_chat_raises_an_r1_person_click_to_r3_with_a_record() -> None:
    h = Harness()
    h.world.rows.append(_delete_chat_row())
    result = h.session.run_step(_r1_click("Delete"))
    assert result.gate == "confirm", result.evidence
    assert [a.kind for a in h.confirmer.asked] == ["yes"]
    assert h.confirmer.asked[0].risk == "R3"
    assert result.evidence["risk"] == "R3"
    assert "p1" in h.ledger.records, "an R3 action has a write-ahead record"


def test_a_deny_word_only_in_the_bound_name_part_raises_the_tier_for_the_whole_step() -> None:
    h = Harness()
    _without_base_amir(h)
    h.world.rows.append(_amir_delete_row())
    h.clock["t"] = 100.0
    monitor = FakeMonitor(h.clock, last_user=10.0)
    _wire(h, monitor)
    result = h.session.run_step(_r1_click("Amir"))
    assert result.gate == "confirm", result.evidence
    assert [a.kind for a in h.confirmer.asked] == ["yes"]
    assert h.confirmer.asked[0].risk == "R3"
    assert result.evidence["risk"] == "R3", "the step's own tier follows the gate's"
    assert "p1" in h.ledger.records, "an R3 action has a write-ahead record"
    assert monitor.asks == ["open", "closed"], "the confirm ask is bracketed for the monitor"


@pytest.mark.parametrize("mark", [chr(0xA1), chr(0xD7), chr(0x0661)])
def test_a_deny_word_before_a_character_the_fold_makes_a_letter_still_raises(mark: str) -> None:
    """The tier reads the raw name part: the confusable fold turns these marks into letters, so
    a folded "delete" + U+00A1 is one word, "deletei", and would never be raised."""
    h = Harness()
    _without_base_amir(h)
    h.world.rows.append((16, "ListItem", f"Amir delete{mark} {MID} hi {MID} 2h", (42, 18), 1))
    result = h.session.run_step(_r1_click("Amir"))
    assert result.gate == "confirm", result.evidence
    assert [a.risk for a in h.confirmer.asked] == ["R3"]
    assert result.evidence["risk"] == "R3", "the step's own tier follows the gate's"
    assert "p1" in h.ledger.records, "an R3 action has a write-ahead record"


def test_a_lookalike_deny_word_in_a_person_rows_name_part_raises_the_tier() -> None:
    """The gate reads the row's name as shown through the closure matcher (#83 on #97)."""
    h = Harness()
    _without_base_amir(h)
    h.world.rows.append((16, "ListItem", f"Amir D{chr(0x0435)}lete {MID} hi {MID} 2h", (42, 18), 1))
    result = h.session.run_step(_r1_click("Amir"))
    assert result.gate == "confirm", result.evidence
    assert [a.risk for a in h.confirmer.asked] == ["R3"]


def test_the_candidates_ceiling_reads_the_raw_name_part() -> None:
    """Admission's ceiling is never below the bound row's tier: both read the name as shown."""
    h = Harness()
    _without_base_amir(h)
    h.world.rows.append((16, "ListItem", f"Amir delete{chr(0xA1)} {MID} hi {MID} 2h", (42, 18), 1))
    step = _r1_click("Amir")
    before = h.world.observe(0x10)
    assert h.session._ceiling(step, h.ledger.key_for(step), before, "R1") == "R3"


def test_a_deny_word_in_the_preview_never_raises_the_tier() -> None:
    h = Harness()
    h.world.rows.append((16, "ListItem", f"Sara {MID} delete everything {MID} 2h", (42, 18), 1))
    result = h.session.run_step(_person_click("Sara"))
    assert result.gate == "approve"
    assert h.confirmer.asked == []
    assert result.evidence["risk"] == "R1"
    assert _clicked_runtime_ids(h) == [(42, 18)]


def test_the_deny_word_on_the_chosen_row_of_two_raises_the_tier_after_the_choice() -> None:
    h = Harness()
    h.world.rows.append(_amir_delete_row())
    seen = _answer_which(h, 2)
    result = h.session.run_step(_r1_click("Amir"))
    assert [a.kind for a in seen] == ["which", "yes"], result.evidence
    assert seen[0].risk == "R1", "the which-ask runs at the plan's tier: nothing is bound yet"
    assert seen[1].risk == "R3"
    assert result.gate == "confirm"


# ---- PT-36: not_delivered releases the key and the binding ---------------------------------------


def test_not_delivered_on_a_person_step_releases_the_key_and_the_binding() -> None:
    h = Harness()
    h.world.rows.append(AMIR_KHAN)
    seen = _answer_which(h, 2)
    h.semantic.outcomes[15] = not_delivered(ErrorCode.ELEMENT_NOT_ACTIONABLE, "disabled")
    step = _r2_click()
    first = h.session.run_step(step)
    assert first.ok is False
    assert first.evidence["delivery"] == "not_delivered"
    key = h.ledger.key_for(step)
    assert h.ledger.executions[key]["outcome"] == "not_delivered"
    assert h.ledger.admit(key) == "fresh"
    h.semantic.outcomes.clear()
    second = h.session.run_step(step)
    assert [a.kind for a in seen] == ["which", "yes", "which", "yes"], "a fresh key asks again"
    assert seen[3].previous_attempt is None, "nothing landed: no earlier-attempt warning"
    assert second.action is not None
    assert second.action.delivery is Delivery.DELIVERED_VERIFIED
    chosen = h.ledger.records["c2"].chosen
    assert chosen is not None
    assert chosen["runtime_id"] == list(KHAN_RID)


# ---- PT-38: the kill switch during the which-ask -------------------------------------------------


def test_a_kill_during_the_which_ask_blocks_the_task_with_a_block_token() -> None:
    h = Harness(default="killed")
    h.world.rows.append(AMIR_KHAN)
    task = h.session.run_task(_plan(_person_click("Amir"), _type()))
    assert task.status == "blocked"
    assert task.evidence["detail"] == "killed"
    assert h.semantic.calls == []
    assert task.results[0].code is ErrorCode.GATE_BLOCKED
    assert task.results[0].gate == "block", "the person answered the ask: the gate's outcome"


# ---- PT-18: a sentinel scan over every path ------------------------------------------------------

SENTINELS = (
    "Amir",
    "Khan",
    "hi bro",
    "invoice attached",
    CYR_A,
    CYR_CAP_A,
    "9173",
    "Newcomer",
    "Zed",
)


_HEX_ID = re.compile(r"[0-9a-f]{12,}")


def _scan(h: Harness, *results: StepResult) -> None:
    text = "\n".join(h.trace.lines)
    for result in results:
        text += "\n" + json.dumps(result.to_dict(), ensure_ascii=False) + "\n" + repr(result)
    for ask in h.confirmer.asked:
        text += "\n" + repr(ask)
    # generated ids (ask ids, decision ids, keys) are random hex and once held "9173" by
    # chance (CI 2026-11-06): masked, since a leaked RuntimeId part is a bare number
    text = _HEX_ID.sub("<id>", text)
    for sentinel in SENTINELS:
        assert sentinel not in text, (sentinel, text)


def _distinct_khan() -> Row:
    return (15, "ListItem", f"Amir Khan {MID} invoice attached {MID} 3d", (42, 9173), 1)


def test_the_trace_is_clean_on_the_ask_no_unanswered_and_candidate_gone_paths() -> None:
    h = Harness()
    h.world.rows.append(_distinct_khan())
    h.world.rows.append(LOOKALIKE)
    seen = _answer_which(h, 2)
    chosen = h.session.run_step(_person_click("Amir"))
    assert chosen.evidence["chosen_index"] == 2
    h.confirmer.on_ask = None
    h.confirmer.default = "no"
    declined = h.session.run_step(_person_click("Amir", step_id="p2"))
    assert declined.evidence["detail"] == "none_chosen"
    h.confirmer.default = ""
    unanswered = h.session.run_step(_person_click("Amir", step_id="p3"))
    assert unanswered.evidence["detail"] == "unanswered"
    h.confirmer.default = "yes"

    def reply_then_remove(ask: Ask) -> None:
        if ask.kind == "which":
            h.confirmer.answers.append(f"2:{ask.decision_id}")
        if ask.kind == "yes":
            h.world.rows = [r for r in h.world.rows if r[3] != (42, 9173)]

    h.confirmer.on_ask = reply_then_remove
    gone = h.session.run_step(_person_delete("Amir"))
    assert gone.evidence["detail"] == "candidate_gone"
    assert len(seen) == 1
    assert '"mixed_script": 1' in "\n".join(h.trace.lines)
    _scan(h, chosen, declined, unanswered, gone)


def test_the_trace_is_clean_on_the_cap_no_match_and_done_paths() -> None:
    h = Harness()
    h.world.rows.extend(_amir_rows(12))
    capped = h.session.run_step(_person_click("Amir"))
    assert capped.evidence["detail"] == "ambiguous_target"
    h.world.rows = [r for r in h.world.rows if r[1] != "ListItem"]
    h.world.rows.append((17, "Button", "Call Amir Khan", (42, 19), 7))
    missing = h.session.run_step(_person_click("Amir", step_id="p2"))
    assert missing.evidence["detail"] == "no_match"
    assert missing.evidence["other_role_matches"] == 1
    h.world.rows.append(_distinct_khan())
    _answer_which(h, 1)
    h.session.run_step(_person_delete("Amir"))
    _crash_after_record(h, _person_delete("Amir"))
    h.world.rows = [r for r in h.world.rows if r[3] != (42, 9173)]
    done = h.session.run_step(_person_delete("Amir"))
    assert done.gate == "done_already"
    _scan(h, capped, missing, done)


def test_a_run_time_raise_over_max_risk_stops_before_any_ask() -> None:
    """--max-risk is the person's limit for the whole run: a tier only the screen raises (a
    row named "delete") stops the step before the ask, never asks past the limit (#88)."""
    h = Harness()
    _without_base_amir(h)
    h.world.rows.append(_amir_delete_row())
    h.session.max_risk = "R1"
    result = h.session.run_step(_r1_click("Amir"))
    assert result.evidence["detail"] == "over_max_risk", result.to_dict()
    assert h.confirmer.asked == []
    assert _clicked_runtime_ids(h) == []


def test_the_default_limit_changes_nothing() -> None:
    h = Harness()
    h.world.rows.append(_delete_chat_row())
    result = h.session.run_step(_r1_click("Delete"))
    assert result.gate == "confirm", result.evidence
