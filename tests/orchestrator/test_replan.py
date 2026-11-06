"""Replanning's pure parts (packet replan-2026-11-06, schema gate REVISE items 1, 2, 4, 5, 6 and
decisions Q2, Q3; hunter RP-05/10/13/24/26/31/39/53): which runs replan, what the planner is
told, which continuation plans are refused, and the task's results merged over its plans."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, StepResult, Subgoal, Target
from steerai.orchestrator.loop import TaskResult
from steerai.orchestrator.replan import (
    MAX_REPLANS,
    Progress,
    continuation_refusal,
    replan_trigger,
)
from steerai.planner.contracts import DoneStep, FailedStep, OpenSubgoal
from steerai.verify.predicates import TitleMatches, WindowExists

GOAL = "search YouTube for drake latest video and play the first video"
YT = "drake latest video - YouTube - Google Chrome"
SENTINEL = "sentinel-words-7f3a"


def _open(step_id: str = "s1", site: str = "youtube_search") -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb="open",
        target=Target(role="App", name="chrome"),
        risk="R1",
        params={"site": site, "text": SENTINEL},
    )


def _click(step_id: str, subgoal: str = "g2", *, risk: str = "R0", **kw: Any) -> Step:
    return Step(
        id=step_id,
        subgoal=subgoal,
        verb="click",
        target=kw.pop("target", Target(role="Link", name="Drake - Video")),
        risk=risk,  # type: ignore[arg-type]
        expect=TitleMatches(pattern="Drake"),
        **kw,
    )


def _first_plan() -> Plan:
    return Plan(
        goal=GOAL,
        subgoals=(
            Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT)),
            Subgoal(id="g2", desc="the first video plays", condition=TitleMatches(pattern="x")),
        ),
        steps=(_open(),),
    )


def _second_plan(*steps: Step, subgoals: tuple[Subgoal, ...] | None = None) -> Plan:
    return Plan(
        goal=GOAL,
        subgoals=subgoals
        or (Subgoal(id="g2", desc="playing", condition=TitleMatches(pattern="Drake")),),
        steps=steps or (_click("p2.s1"),),
    )


def _one_click_plan(*steps: Step) -> Plan:
    return Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g2", desc="d", condition=TitleMatches(pattern="x")),),
        steps=steps or (_click("c1"),),
    )


def _result(
    step_id: str,
    *,
    ok: bool = True,
    gate: str = "approve",
    delivery: Delivery | None = Delivery.DELIVERED_UNVERIFIED,
    code: ErrorCode | None = None,
    risk: str = "R0",
    detail: str | None = None,
    label: str | None = "t1",
) -> StepResult:
    action = (
        None
        if delivery is None
        else ActionResult(verb="click", delivery=delivery, code=ErrorCode.VERIFY_TIMEOUT)
    )
    evidence: dict[str, Any] = {"risk": risk, "text": SENTINEL}
    if detail is not None:
        evidence["detail"] = detail
    return StepResult(
        step_id=step_id,
        ok=ok,
        gate=gate,  # type: ignore[arg-type]
        action=action,
        verify_code=None if ok else code,
        label_kind=label if ok else None,  # type: ignore[arg-type]
        latency_ms={},
        evidence=evidence,
        code=None if ok else code,
    )


def _unfinished(*results: StepResult, ticked: set[str] | None = None) -> TaskResult:
    return TaskResult(
        status="stopped",
        results=list(results),
        ticked={"g1"} if ticked is None else ticked,
        code=ErrorCode.VERIFY_FAILED,
        evidence={"detail": "unfinished"},
    )


def _missing(step_id: str = "c1", detail: str = "no_match") -> TaskResult:
    failed = _result(
        step_id,
        ok=False,
        gate="unreached",
        delivery=None,
        code=ErrorCode.ELEMENT_NOT_FOUND,
        detail=detail,
    )
    return TaskResult(
        status="stopped",
        results=[failed],
        ticked=set(),
        code=ErrorCode.ELEMENT_NOT_FOUND,
        evidence={"detail": detail},
    )


def _progress(plan: Plan | None = None, result: TaskResult | None = None) -> Progress:
    progress = Progress()
    progress.add(plan or _first_plan(), result or _unfinished(_result("s1")))
    return progress


def _trigger(progress: Progress) -> str | None:
    return replan_trigger(progress.last, progress.steps)


# ---- triggers: an allow-list (item 1, Q2) ----------------------------------------------------


def test_an_unfinished_run_replans() -> None:
    assert _trigger(_progress()) == "unfinished"


def test_a_missing_target_before_the_gate_replans() -> None:
    assert _trigger(_progress(_one_click_plan(), _missing())) == "element_not_found"


@pytest.mark.parametrize(
    ("status", "code", "detail", "gate"),
    [
        ("stopped", ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target", "unreached"),
        ("stopped", ErrorCode.ELEMENT_NOT_FOUND, "window_gone", "unreached"),
        ("stopped", ErrorCode.ELEMENT_NOT_FOUND, "candidate_gone", "unreached"),
        ("stopped", ErrorCode.BUDGET_EXCEEDED, "max_steps", "approve"),
        ("stopped", ErrorCode.BUDGET_EXCEEDED, "loop", "approve"),
        ("stopped", ErrorCode.INTERRUPT_MODAL, "modal", "approve"),
        ("stopped", ErrorCode.PERCEPTION_DEGRADED, "degraded", "approve"),
        ("stopped", ErrorCode.APP_CRASHED, "crashed", "approve"),
        ("stopped", ErrorCode.SECURE_DESKTOP, "secure_desktop", "approve"),
        ("stopped", ErrorCode.WINDOW_OTHER_DESKTOP, "other_desktop", "approve"),
        ("stopped", ErrorCode.UNSTABLE_UI, "unstable", "unreached"),
        ("stopped", ErrorCode.FOCUS_LOST, "focus", "approve"),
        ("stopped", ErrorCode.GATE_BLOCKED, "wal_unavailable", "approve"),
        ("stopped", ErrorCode.VERIFY_TIMEOUT, "open_unverified", "approve"),
        ("blocked", ErrorCode.GATE_BLOCKED, "block", "block"),
        ("blocked", ErrorCode.GATE_BLOCKED, "killed", "approve"),
        ("paused", ErrorCode.GATE_PENDING_USER, "takeover", "pending"),
        ("done", None, None, "approve"),
    ],
)
def test_everything_else_never_replans(
    status: str, code: ErrorCode | None, detail: str | None, gate: str
) -> None:
    last = _result("c1", ok=code is None, gate=gate, delivery=None, code=code, detail=detail)
    ended = TaskResult(
        status=status,  # type: ignore[arg-type]
        results=[last],
        ticked=set(),
        code=code,
        evidence={} if detail is None else {"detail": detail},
    )
    assert _trigger(_progress(_one_click_plan(), ended)) is None


def test_a_missing_target_after_the_gate_ran_never_replans() -> None:
    ended = _missing()
    ended.results[0] = _result(
        "c1",
        ok=False,
        gate="confirm",
        delivery=None,
        code=ErrorCode.ELEMENT_NOT_FOUND,
        detail="no_match",
    )
    assert _trigger(_progress(_one_click_plan(), ended)) is None


def test_a_person_targets_missing_row_never_replans() -> None:
    person = _click("c1", target=Target(role="ListItem", name="Amir", match="person"))
    assert _trigger(_progress(_one_click_plan(person), _missing())) is None


def test_a_failed_person_step_in_an_unfinished_run_never_replans() -> None:
    person = _click("c1", target=Target(role="ListItem", name="Amir", match="person"))
    failed = _result("c1", ok=False, delivery=None, code=ErrorCode.VERIFY_FAILED)
    assert _trigger(_progress(_one_click_plan(person), _unfinished(failed))) is None


@pytest.mark.parametrize("risk", ["R2", "R3"])
def test_an_unverified_dispatched_r2_or_r3_step_stops_unverified_effect(risk: str) -> None:
    plan = _one_click_plan(_click("c1"), _click("c2"))
    run = _unfinished(
        _result("c1"), _result("c2", ok=False, code=ErrorCode.VERIFY_TIMEOUT, risk=risk)
    )
    assert _trigger(_progress(plan, run)) == "unverified_effect"


def test_the_tier_the_run_used_counts_not_the_declared_one() -> None:
    """RP-10: a row or a deny word raised the R0 step to R2 at run time."""
    run = _unfinished(_result("c1", ok=False, code=ErrorCode.VERIFY_FAILED, risk="R2"))
    assert _trigger(_progress(_one_click_plan(), run)) == "unverified_effect"


def test_an_unverified_effect_beats_a_missing_target() -> None:
    plan = _one_click_plan(_click("c1"), _click("c2"))
    ended = _missing("c2")
    ended.results.insert(0, _result("c1", ok=False, code=ErrorCode.VERIFY_FAILED, risk="R2"))
    assert _trigger(_progress(plan, ended)) == "unverified_effect"


def test_a_failed_r0_or_r1_step_in_an_unfinished_run_replans() -> None:
    run = _unfinished(_result("c1", ok=False, code=ErrorCode.VERIFY_FAILED, risk="R1"))
    assert _trigger(_progress(_one_click_plan(), run)) == "unfinished"


@pytest.mark.parametrize("delivery", [None, Delivery.NOT_DELIVERED])
def test_an_r2_step_that_sent_nothing_is_no_uncertainty(delivery: Delivery | None) -> None:
    failed = _result("c1", ok=False, delivery=delivery, code=ErrorCode.VERIFY_FAILED, risk="R2")
    assert _trigger(_progress(_one_click_plan(), _unfinished(failed))) == "unfinished"


def test_the_replan_limit_is_two() -> None:
    assert MAX_REPLANS == 2


# ---- what the planner is told (items 5, 6) ---------------------------------------------------


def test_progress_lists_done_steps_by_verb_role_and_name_only() -> None:
    progress = _progress()
    assert progress.done_steps() == [DoneStep(verb="open", role="App", name="chrome")]
    assert progress.remaining() == [OpenSubgoal(id="g2", desc="the first video plays")]
    assert SENTINEL not in repr(progress.done_steps())


def test_only_t1_and_done_already_steps_count_as_done() -> None:
    plan = _one_click_plan(_click("c1"), _click("c2"), _click("c3"), _click("c4"))
    run = _unfinished(
        _result("c1", label="t1"),
        _result("c2", gate="done_already", label=None),
        _result("c3", label="user"),
        _result("c4", ok=False, code=ErrorCode.VERIFY_FAILED),
    )
    progress = _progress(plan, run)
    assert len(progress.done_steps()) == 2
    assert progress.failure() == FailedStep(step_id="c4", code="verify_failed")


def test_the_failure_is_the_last_failed_step() -> None:
    plan = _one_click_plan(_click("c1"), _click("c2"))
    run = _unfinished(
        _result("c1", ok=False, code=ErrorCode.VERIFY_TIMEOUT),
        _result("c2", ok=False, code=ErrorCode.VERIFY_FAILED),
    )
    assert _progress(plan, run).failure() == FailedStep(step_id="c2", code="verify_failed")
    assert _progress(_one_click_plan(), _missing()).failure() == FailedStep(
        step_id="c1", code="element_not_found"
    )
    assert _progress().failure() is None


def test_ticked_is_the_union_and_done_needs_every_plans_subgoals() -> None:
    progress = _progress()
    progress.add(
        _second_plan(), TaskResult(status="done", results=[_result("p2.s1")], ticked={"g2"})
    )
    merged = progress.merged()
    assert merged.status == "done"
    assert merged.ticked == {"g1", "g2"}
    assert [r.step_id for r in merged.results] == ["s1", "p2.s1"]


def test_a_done_last_plan_with_an_old_subgoal_left_is_unfinished() -> None:
    progress = _progress(result=_unfinished(ticked=set()))
    progress.add(_second_plan(), TaskResult(status="done", results=[], ticked={"g2"}))
    merged = progress.merged()
    assert merged.status == "stopped"
    assert merged.code is ErrorCode.VERIFY_FAILED
    assert merged.evidence["detail"] == "unfinished"


def test_the_merged_stop_is_the_last_runs() -> None:
    progress = _progress()
    progress.add(_one_click_plan(_click("p2.c1")), _missing("p2.c1"))
    merged = progress.merged()
    assert (merged.status, merged.code, merged.evidence["detail"]) == (
        "stopped",
        ErrorCode.ELEMENT_NOT_FOUND,
        "no_match",
    )


# ---- which continuation plans are refused (items 2, 4, Q3; RP-26) ----------------------------


def test_a_continuation_keeping_the_remaining_ids_passes() -> None:
    assert continuation_refusal(_second_plan(), _progress()) is None


def test_a_continuation_may_add_subgoals() -> None:
    plan = _second_plan(
        _click("p2.s1"),
        _click("p2.s2", subgoal="g3"),
        subgoals=(
            Subgoal(id="g2", desc="playing", condition=TitleMatches(pattern="Drake")),
            Subgoal(id="g3", desc="fullscreen", condition=TitleMatches(pattern="x")),
        ),
    )
    assert continuation_refusal(plan, _progress()) is None


def test_a_continuation_dropping_a_remaining_id_is_refused() -> None:
    plan = _second_plan(
        _click("p2.s1", subgoal="g9"),
        subgoals=(Subgoal(id="g9", desc="other", condition=TitleMatches(pattern="x")),),
    )
    assert continuation_refusal(plan, _progress()) == "subgoals"


def test_a_continuation_reusing_a_ticked_id_is_refused() -> None:
    plan = _second_plan(
        _click("p2.s1"),
        _click("p2.s2", subgoal="g1"),
        subgoals=(
            Subgoal(id="g1", desc="again", condition=TitleMatches(pattern="x")),
            Subgoal(id="g2", desc="playing", condition=TitleMatches(pattern="Drake")),
        ),
    )
    assert continuation_refusal(plan, _progress()) == "subgoals"


def test_a_continuation_reusing_a_step_id_is_refused_at_its_path() -> None:
    plan = _second_plan(_click("p2.s1"), _click("s1"))
    assert continuation_refusal(plan, _progress()) == "steps[1].id"


def test_a_continuation_with_another_goal_is_refused() -> None:
    plan = Plan(goal="pay the bill", subgoals=_second_plan().subgoals, steps=(_click("p2.s1"),))
    assert continuation_refusal(plan, _progress()) == "goal"


def test_a_continuation_opening_the_same_site_again_is_refused() -> None:
    reopen = Step(
        id="p2.o1",
        subgoal="g2",
        verb="open",
        target=Target(role="App", name="chrome"),
        risk="R1",
        params={"site": "youtube_search", "text": "other words"},
    )
    assert continuation_refusal(_second_plan(reopen), _progress()) == "steps[0]"


def test_a_continuation_opening_another_app_passes() -> None:
    notepad = Step(
        id="p2.o1", subgoal="g2", verb="open", target=Target(role="App", name="notepad"), risk="R1"
    )
    assert continuation_refusal(_second_plan(notepad), _progress()) is None


def test_rewritten_conditions_are_named() -> None:
    progress = _progress()
    assert progress.rewritten(_second_plan()) == {"g2"}
    same = _second_plan(
        subgoals=(
            Subgoal(id="g2", desc="the first video plays", condition=TitleMatches(pattern="x")),
        )
    )
    assert progress.rewritten(same) == set()
