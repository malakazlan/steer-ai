"""W4-04 with the principal's corrections on W4 v3: before `done`, and before any R2/R3 step, the
ticked `value` and `state` conditions of the current window are read again on the snapshot the
loop already holds (the step's before, or the last pair's after): live reads only, no extra
observe. Only a positive contradiction un-ticks (exactly one element, readable, not secure, and
reading otherwise); a reset ends the plan `stopped` / `reset` with nothing of the pending R2/R3
step dispatched or asked, and `reset` is a replan trigger. A dependent select that resets an
earlier choice is the case this covers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.replan import replan_trigger
from steerai.verify.predicates import AnyOf, CheckState, Disappeared, FieldValue, TitleMatches
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import FakeWorld, Row
from tests.orchestrator.test_choose_loop import PLAN, SIZE, _choose, _chooses, _harness
from tests.orchestrator.test_edge_cases import _rebuild

NEXT: Row = (13, "Button", "Next", (42, 52), None)
SAVE: Row = (14, "Button", "Save", (42, 53), None)
ALERTS: Row = (15, "CheckBox", "Email alerts", (42, 54), None)
GOAL = "set the size to Large and the plan to Pro, then save"
SIZE_LARGE = FieldValue(role="ComboBox", name_pattern="Size", text="Large", folded=True)
PLAN_PRO = FieldValue(role="ComboBox", name_pattern="Plan", text="Pro", folded=True)


def _submit() -> Step:
    return Step(
        id="s3", subgoal="g3", verb="submit", target=Target(role="Button", name="Save"), risk="R2"
    )


def _next() -> Step:
    return Step(
        id="s3",
        subgoal="g3",
        verb="click",
        target=Target(role="Button", name="Next"),
        expect=Disappeared(role="ComboBox", name_pattern="Size"),
    )


def _plan(*steps: Step) -> Plan:
    """The size and the plan; a third subgoal (saved) only when a step serves it."""
    saved = Subgoal(id="g3", desc="saved", condition=TitleMatches(pattern="Never"))
    third = (saved,) if any(s.subgoal == "g3" for s in steps) else ()
    return Plan(
        goal=GOAL,
        subgoals=(
            Subgoal(id="g1", desc="size", condition=SIZE_LARGE),
            Subgoal(id="g2", desc="plan", condition=PLAN_PRO),
            *third,
        ),
        steps=steps,
    )


def _dependent(h: Any, values: Any) -> None:
    """Choosing the plan resets the size to its default (a dependent select)."""
    effect = _chooses(values)

    def on_execute(executor: Any, action: Any) -> None:
        effect(executor, action)
        if action.element_id == PLAN[0]:
            values.values[SIZE[3]] = "Small"

    h.semantic.on_execute = on_execute


def _setup() -> tuple[Any, Any]:
    h, values = _harness()
    h.world.rows = [*h.world.rows, NEXT, SAVE, ALERTS]
    return h, values


def _untick_records(h: Any) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in h.trace.lines]
    return [r for r in records if r.get("kind") == "untick"]


# ---- before an R2/R3 step --------------------------------------------------------------------


def test_a_dependent_select_reset_before_a_submit_stops_reset_and_replans() -> None:
    h, values = _setup()
    _dependent(h, values)
    plan = _plan(
        _choose("Large", step_id="s1"), _choose("Pro", "Plan", "s2", subgoal="g2"), _submit()
    )
    task = h.session.run_task(plan)
    assert task.status == "stopped", task
    assert task.code is ErrorCode.VERIFY_FAILED
    assert task.evidence["detail"] == "reset"
    assert task.ticked == {"g2"}, "the size's tick is withdrawn, the plan's stays"
    assert [c[0] for c in h.semantic.calls] == ["choose", "choose"], "the submit never went out"
    assert h.confirmer.asked == [], "nothing of the R2 step is asked"
    assert task.results[-1].step_id == "s3"
    assert task.results[-1].gate == "unreached"
    assert [r["subgoal"] for r in _untick_records(h)] == ["g1"]
    assert replan_trigger(task, {s.id: s for s in plan.steps}) == "reset"


def test_an_unverified_r2_effect_still_wins_over_a_reset() -> None:
    """Principal correction 2: `_maybe_landed` is checked after the trigger, as for every
    trigger: an earlier R2/R3 dispatch nobody verified stops `unverified_effect`."""
    from steerai.action.dispositions import ActionResult, Delivery  # noqa: PLC0415
    from steerai.action.dispositions import Step as ActionStep  # noqa: PLC0415
    from steerai.orchestrator.contracts import StepResult  # noqa: PLC0415
    from steerai.orchestrator.loop import TaskResult  # noqa: PLC0415

    landed = StepResult(
        step_id="s3",
        ok=False,
        gate="confirm",
        action=ActionResult(
            verb="submit",
            delivery=Delivery.DELIVERY_UNCERTAIN,
            steps=[
                ActionStep(label="InvokePattern.Invoke", mechanism="semantic", outcome="failed")
            ],
            code=ErrorCode.VERIFY_FAILED,
        ),
        verify_code=None,
        label_kind=None,
        latency_ms={},
        evidence={"risk": "R2"},
        code=ErrorCode.VERIFY_FAILED,
    )
    task = TaskResult(
        status="stopped",
        results=[landed],
        ticked=set(),
        code=ErrorCode.VERIFY_FAILED,
        evidence={"detail": "reset"},
    )
    assert replan_trigger(task, {"s3": _submit()}) == "unverified_effect"


def test_a_held_value_lets_the_r2_step_go_on_to_the_gate() -> None:
    h, _values = _setup()
    plan = _plan(
        _choose("Large", step_id="s1"), _choose("Pro", "Plan", "s2", subgoal="g2"), _submit()
    )
    task = h.session.run_task(plan)
    assert task.results[-1].step_id == "s3"
    assert task.results[-1].evidence.get("detail") != "reset"
    assert task.results[-1].gate != "unreached"
    assert _untick_records(h) == []


# ---- before done ------------------------------------------------------------------------------


def test_a_reset_found_before_done_withdraws_the_tick_and_stops() -> None:
    h, values = _setup()
    _dependent(h, values)
    plan = _plan(_choose("Large", step_id="s1"), _choose("Pro", "Plan", "s2", subgoal="g2"))
    task = h.session.run_task(plan)
    assert task.status == "stopped", task
    assert task.evidence["detail"] == "reset"
    assert task.ticked == {"g2"}
    assert [r["subgoal"] for r in _untick_records(h)] == ["g1"]
    assert replan_trigger(task, {s.id: s for s in plan.steps}) == "reset"


def test_the_reread_before_done_takes_no_extra_observe() -> None:
    """The last pair's after is the snapshot read again: live reads only (correction 2)."""

    def observes(dependent: bool) -> int:
        h, values = _setup()
        if dependent:
            _dependent(h, values)
        h.session.run_task(
            _plan(
                _choose("Large", step_id="s1"),
                _choose("Pro", "Plan", "s2", subgoal="g2"),
            )
        )
        return len(h.world.observed)

    assert observes(dependent=True) == observes(dependent=False)


def test_a_field_gone_after_next_keeps_its_tick_and_the_task_is_done() -> None:
    """Principal correction 1: a multi-page form; the size is not on the next page."""
    h, _values = _setup()

    def next_page(_executor: Any, action: Any) -> None:
        if action.element_id == NEXT[0]:
            h.world.rows = [r for r in h.world.rows if r[1] != "ComboBox"]

    effect = h.semantic.on_execute

    def on_execute(executor: Any, action: Any) -> None:
        effect(executor, action)
        next_page(executor, action)

    h.semantic.on_execute = on_execute
    task = h.session.run_task(
        Plan(
            goal=GOAL,
            subgoals=(
                Subgoal(id="g1", desc="size", condition=SIZE_LARGE),
                Subgoal(id="g3", desc="next", condition=Disappeared("ComboBox", "Size")),
            ),
            steps=(_choose("Large", step_id="s1"), _next()),
        )
    )
    assert task.status == "done", task
    assert task.ticked == {"g1", "g3"}
    assert _untick_records(h) == []


# ---- only a positive contradiction un-ticks ---------------------------------------------------


@dataclass
class Secured:
    """The observer marks elements secure, as a field turned into a password field would be."""

    world: FakeWorld
    secure: set[int] = field(default_factory=set)

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        snapshot = self.world.observe(hwnd, **kwargs)
        elements = [
            replace(e, secure=True) if e.id in self.secure else e for e in snapshot.elements
        ]
        return replace(snapshot, elements=elements)


def test_an_ambiguous_field_keeps_its_tick() -> None:
    h, values = _setup()
    effect = _chooses(values)
    twin: Row = (16, "ComboBox", "size", (42, 55), None)

    def on_execute(executor: Any, action: Any) -> None:
        effect(executor, action)
        if action.element_id == PLAN[0]:
            values.values[SIZE[3]] = "Small"
            h.world.rows = [*h.world.rows, twin]

    h.semantic.on_execute = on_execute
    task = h.session.run_task(
        _plan(
            _choose("Large", step_id="s1"),
            _choose("Pro", "Plan", "s2", subgoal="g2"),
        )
    )
    assert task.status == "done", task
    assert _untick_records(h) == []


def test_a_secure_field_keeps_its_tick() -> None:
    h, values = _setup()
    observer = Secured(h.world)
    _rebuild(h, live=values, observer=observer)
    effect = _chooses(values)

    def on_execute(executor: Any, action: Any) -> None:
        effect(executor, action)
        if action.element_id == PLAN[0]:
            values.values[SIZE[3]] = "Small"
            observer.secure.add(SIZE[0])

    h.semantic.on_execute = on_execute
    task = h.session.run_task(
        _plan(
            _choose("Large", step_id="s1"),
            _choose("Pro", "Plan", "s2", subgoal="g2"),
        )
    )
    assert task.status == "done", task


def test_an_unreadable_value_keeps_its_tick() -> None:
    h, values = _setup()
    effect = _chooses(values)

    def on_execute(executor: Any, action: Any) -> None:
        effect(executor, action)
        if action.element_id == PLAN[0]:
            del values.values[SIZE[3]]  # the live read returns None

    h.semantic.on_execute = on_execute
    task = h.session.run_task(
        _plan(
            _choose("Large", step_id="s1"),
            _choose("Pro", "Plan", "s2", subgoal="g2"),
        )
    )
    assert task.status == "done", task


def test_a_state_without_its_pattern_keeps_its_tick_and_one_that_reads_off_resets() -> None:
    from tests.orchestrator.test_state_verbs_loop import Boxes, _flips  # noqa: PLC0415

    def run(reads_off: bool) -> Any:
        h, values = _setup()
        boxes = Boxes(h.world, state={ALERTS[3]: 0})
        boxes.value = values.value  # type: ignore[method-assign]  # one live reader for both
        h.session._live = boxes
        flip = _flips(boxes)
        effect = _chooses(values)

        def on_execute(executor: Any, action: Any) -> None:
            if action.verb == "check":
                flip(executor, action)
            else:
                effect(executor, action)
                if reads_off:
                    boxes.state[ALERTS[3]] = 0
                else:
                    del boxes.state[ALERTS[3]]

        h.semantic.on_execute = on_execute
        check = Step(id="s1", subgoal="g1", verb="check", target=Target("CheckBox", "Email alerts"))
        return h.session.run_task(
            Plan(
                goal=GOAL,
                subgoals=(
                    Subgoal(
                        id="g1",
                        desc="alerts",
                        condition=CheckState("CheckBox", "Email alerts", "on"),
                    ),
                    Subgoal(id="g2", desc="plan", condition=PLAN_PRO),
                ),
                steps=(check, _choose("Pro", "Plan", "s2", subgoal="g2")),
            )
        )

    kept = run(reads_off=False)
    assert kept.status == "done", kept
    reset = run(reads_off=True)
    assert reset.evidence["detail"] == "reset"
    assert "g1" not in reset.ticked


def test_a_value_under_any_of_is_never_withdrawn() -> None:
    h, values = _setup()
    _dependent(h, values)
    either = AnyOf((SIZE_LARGE, FieldValue("ComboBox", "Size", "Small", folded=True)))
    task = h.session.run_task(
        Plan(
            goal=GOAL,
            subgoals=(
                Subgoal(id="g1", desc="size", condition=either),
                Subgoal(id="g2", desc="plan", condition=PLAN_PRO),
            ),
            steps=(_choose("Large", step_id="s1"), _choose("Pro", "Plan", "s2", subgoal="g2")),
        )
    )
    assert task.status == "done", task
