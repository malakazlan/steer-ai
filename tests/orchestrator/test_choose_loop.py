"""`choose` through the loop (widget-verbs W4 v1-v3): the executor gets the option, T1 reads the
live Value with the choose's comparator, a refusal after Expand closes as a refusal with no T1
(W4-06), a box left expanded stops the task (W4-02, W4-08), an R2 miss stops, a done key re-reads
the Value (W4-07), a choose that navigated stops (W4-01, W4-12), the provider gate covers it,
its twin is picked and pinned like a type's (W4-11), and `type` / `focus` never alias to a
ComboBox (W4-10)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.loop import ALIAS_ROLES, _StepFailureError
from steerai.verify.predicates import FieldValue, TitleMatches
from steerai.world.twins import pin_key
from tests.orchestrator.fakes import BASE_ROWS, COMPOSER, FakeWorld, Row
from tests.orchestrator.test_edge_cases import _rebuild
from tests.orchestrator.test_loop import Harness, _type
from tests.orchestrator.test_select_loop import Process, Retitled
from tests.world.test_twins import BOTTOM, TOP, _page

NBSP = chr(0xA0)
SIZE: Row = (11, "ComboBox", "Size", (42, 50), None)
PLAN: Row = (12, "ComboBox", "Plan", (42, 51), None)
ROWS = [SIZE, PLAN]
PREMIUM = "Premium ($20/mo)"
EXPAND, SELECT, COLLAPSE = (
    "ExpandCollapsePattern.Expand",
    "SelectionItemPattern.Select",
    "ExpandCollapsePattern.Collapse",
)


@dataclass
class Values:
    """The live Value of each ComboBox by RuntimeId (the fakes' handles are RuntimeIds); the
    composer reads the world's text as in the other loop tests."""

    world: FakeWorld
    values: dict[tuple[int, ...], str] = field(default_factory=dict)
    reads: int = 0

    def value(self, handle: Any) -> str | None:
        self.reads += 1
        if handle == COMPOSER[3]:
            return self.world.composer_value
        return self.values.get(handle)

    def toggle_state(self, _handle: Any) -> int | None:
        return None

    def is_selected(self, _handle: Any) -> bool | None:
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        return ()


def _chooses(values: Values, landed: str | None = None) -> Any:
    """The fake executor's effect: the chosen ComboBox reads the option (or `landed`)."""

    def on_execute(_ex: Any, action: Any) -> None:
        row = next(r for r in values.world.rows if r[0] == action.element_id)
        values.values[row[3]] = action.option if landed is None else landed

    return on_execute


def _harness() -> tuple[Harness, Values]:
    h = Harness()
    h.world.rows = [*BASE_ROWS, *ROWS]
    values = Values(h.world, values={SIZE[3]: "Small", PLAN[3]: "Free"})
    h.session._live = values
    h.semantic.on_execute = _chooses(values)
    # the real executor's call log: a Select went out and the value read back (W4-06)
    chosen = _result(Delivery.DELIVERED_VERIFIED, [EXPAND, SELECT, COLLAPSE], None, {})
    h.semantic.outcomes.update({SIZE[0]: chosen, PLAN[0]: chosen})
    return h, values


def _choose(option: str = "Large", name: str = "Size", step_id: str = "s1", **kw: Any) -> Step:
    return Step(
        id=step_id,
        subgoal=kw.pop("subgoal", "g1"),
        verb="choose",
        target=Target(role="ComboBox", name=name),
        params={"option": option},
        **kw,
    )


def _plan(*steps: Step, condition: Any = None, goal: str = "set the size to Large") -> Plan:
    return Plan(
        goal=goal,
        subgoals=(
            Subgoal(id="g1", desc="d", condition=condition or TitleMatches(pattern="Never")),
        ),
        steps=steps,
    )


def _result(
    delivery: Delivery,
    labels: list[str],
    code: ErrorCode | None,
    evidence: dict[str, Any],
) -> ActionResult:
    steps = [ActionStep(label=label, mechanism="semantic", outcome="succeeded") for label in labels]
    return ActionResult(verb="choose", delivery=delivery, steps=steps, code=code, evidence=evidence)


def _refused(detail: str = "no_match") -> ActionResult:
    return _result(
        Delivery.NOT_DELIVERED,
        [EXPAND, COLLAPSE],
        ErrorCode.ELEMENT_NOT_FOUND,
        {"detail": detail, "expanded": True},
    )


def _scripted(h: Harness, element_id: int, outcome: ActionResult) -> list[Any]:
    """The executor returns `outcome` for the element; the actions it was given are kept."""
    seen: list[Any] = []
    h.semantic.on_execute = lambda _ex, action: seen.append(action)
    h.semantic.outcomes[element_id] = outcome
    return seen


# ---- verified by the live Value ------------------------------------------------------------------


def test_an_r1_choose_gives_the_executor_its_option_and_t1_reads_the_value() -> None:
    h, values = _harness()
    seen: list[Any] = []
    effect = _chooses(values)

    def on_execute(executor: Any, action: Any) -> None:
        seen.append(action)
        effect(executor, action)

    h.semantic.on_execute = on_execute
    result = h.session.run_step(_choose())
    assert result.ok is True, result.to_dict()
    assert result.label_kind == "t1"
    assert h.confirmer.asked == []
    assert [(a.verb, a.element_id, a.option) for a in seen] == [("choose", SIZE[0], "Large")]
    assert "Large" not in repr(seen[0])


def test_a_value_landing_with_a_no_break_space_is_verified_by_the_folding_comparator() -> None:
    h, values = _harness()
    h.semantic.on_execute = _chooses(values, landed=f"New{NBSP}York")
    result = h.session.run_step(_choose("New York"))
    assert result.ok is True, result.to_dict()


def test_a_choose_value_subgoal_ticks_after_its_choose() -> None:
    h, _values = _harness()
    condition = FieldValue(role="ComboBox", name_pattern="Size", text="Large", folded=True)
    task = h.session.run_task(_plan(_choose(), condition=condition))
    assert task.status == "done", task
    assert task.ticked == {"g1"}


def test_a_choose_value_subgoal_ticks_after_an_r2_choose() -> None:
    """W4 v1 verdict item 4: a value proven by a choose may tick after R2/R3, as a state does."""
    h, _values = _harness()
    condition = FieldValue(role="ComboBox", name_pattern="Plan", text=PREMIUM, folded=True)
    plan = _plan(_choose(PREMIUM, "Plan"), condition=condition, goal=f"pick {PREMIUM}")
    task = h.session.run_task(plan)
    assert [a.risk for a in h.confirmer.asked] == ["R2"]
    assert task.status == "done", task


def test_an_already_chosen_option_is_ok_with_nothing_sent() -> None:
    h, _values = _harness()
    h.semantic.outcomes[SIZE[0]] = ActionResult.already_satisfied("choose")
    h.semantic.on_execute = None
    result = h.session.run_step(_choose("Small"))
    assert result.ok is True, result.to_dict()
    assert result.action is not None
    assert result.action.skipped is True


# ---- refusals after Expand close as refusals (W4-06) ---------------------------------------------


@pytest.mark.parametrize("detail", ["no_match", "ambiguous_option", "option_disabled"])
def test_a_refusal_after_expand_closes_with_its_detail_and_no_t1(detail: str) -> None:
    h, values = _harness()
    _scripted(h, SIZE[0], _refused(detail))
    reads = values.reads
    result = h.session.run_step(_choose())
    assert result.ok is False
    assert result.evidence["detail"] == detail
    assert "verify" not in result.latency_ms, "no T1 for a choose that sent no Select"
    assert values.reads == reads
    assert len(h.semantic.calls) == 1, "never a local retry once Expand was sent"


def test_an_r2_refusal_after_a_confirmed_collapse_releases_the_attempt() -> None:
    h, _values = _harness()
    _scripted(h, PLAN[0], _refused())
    step = _choose(PREMIUM, "Plan")
    result = h.session.run_step(step)
    assert result.evidence["detail"] == "no_match"
    assert h.ledger.outcome_of(h.ledger.key_for(step)) == "not_delivered"


def test_no_options_after_expand_is_uncertain_and_keeps_the_attempt_open() -> None:
    h, _values = _harness()
    uncertain = _result(
        Delivery.DELIVERY_UNCERTAIN,
        [EXPAND],
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        {"detail": "no_options"},
    )
    _scripted(h, PLAN[0], uncertain)
    step = _choose(PREMIUM, "Plan")
    result = h.session.run_step(step)
    assert result.ok is False
    assert result.evidence["detail"] == "no_options"
    assert "verify" not in result.latency_ms
    assert h.ledger.outcome_of(h.ledger.key_for(step)) == "delivery_uncertain"


# ---- a box left expanded stops the task (W4-02, W4-08) -------------------------------------------


def test_a_collapse_that_never_confirmed_stops_the_task_left_expanded() -> None:
    h, _values = _harness()
    left = _result(
        Delivery.DELIVERY_UNCERTAIN,
        [EXPAND, SELECT, COLLAPSE],
        ErrorCode.VERIFY_FAILED,
        {"detail": "left_expanded"},
    )
    _scripted(h, SIZE[0], left)
    task = h.session.run_task(_plan(_choose(), _choose("Free", "Plan", step_id="s2")))
    assert task.status == "stopped", task
    assert task.evidence["detail"] == "left_expanded"
    assert [r.step_id for r in task.results] == ["s1"], "no further input in the window"
    assert "verify" not in task.results[0].latency_ms


def test_the_blocker_after_expand_stops_the_task_with_nothing_more_sent() -> None:
    h, _values = _harness()
    blocked = _result(
        Delivery.DELIVERY_UNCERTAIN,
        [EXPAND],
        ErrorCode.GATE_BLOCKED,
        {"detail": "left_expanded", "blocker": "killed"},
    )
    _scripted(h, SIZE[0], blocked)
    task = h.session.run_task(_plan(_choose(), _choose("Free", "Plan", step_id="s2")))
    assert task.status in ("stopped", "blocked"), task
    assert task.evidence["detail"] == "left_expanded"
    assert [r.step_id for r in task.results] == ["s1"]


# ---- R2 miss, done re-read, navigation, provider (W4-01, W4-07, W4-12) ---------------------------


def _unchanged() -> ActionResult:
    return _result(
        Delivery.DELIVERED_UNVERIFIED,
        [EXPAND, SELECT, COLLAPSE],
        ErrorCode.VERIFY_TIMEOUT,
        {"detail": "unchanged"},
    )


def test_a_miss_at_r2_stops_the_task_with_no_second_choose() -> None:
    h, _values = _harness()
    _scripted(h, PLAN[0], _unchanged())
    task = h.session.run_task(
        _plan(_choose(PREMIUM, "Plan"), _choose(step_id="s2"), goal=f"pick {PREMIUM}")
    )
    assert task.status == "stopped", task
    assert task.evidence["detail"] == "state_unverified"
    assert [c[0] for c in h.semantic.calls] == ["choose"]


def test_a_miss_at_r1_does_not_stop_the_task() -> None:
    h, _values = _harness()
    _scripted(h, SIZE[0], _unchanged())
    task = h.session.run_task(_plan(_choose(), _choose("Free", "Plan", step_id="s2")))
    assert [r.step_id for r in task.results] == ["s1", "s2"]
    assert task.results[0].evidence["reason"] == "value_differs"


def test_a_done_key_whose_value_changed_since_stops_without_a_choose() -> None:
    h, values = _harness()
    step = _choose(PREMIUM, "Plan")
    assert h.session.run_step(step).ok is True
    values.values[PLAN[3]] = "Free"  # the page or the person chose another option since
    again = h.session.run_step(replace(step, id="s9"))
    assert again.ok is False
    assert again.action is None
    assert again.evidence["detail"] == "state_changed_since_done"
    assert len(h.semantic.calls) == 1


def test_a_done_key_whose_value_still_holds_is_done_already() -> None:
    h, values = _harness()
    step = _choose(PREMIUM, "Plan")
    assert h.session.run_step(step).ok is True
    values.values[PLAN[3]] = f"Premium{NBSP}($20/mo)"  # the comparator folds it (W4-05)
    again = h.session.run_step(replace(step, id="s9"))
    assert again.ok is True
    assert again.gate == "done_already"
    assert len(h.semantic.calls) == 1


def test_a_choose_whose_combobox_vanished_stops_the_task_navigated() -> None:
    h, values = _harness()
    effect = _chooses(values)

    def submits(executor: Any, action: Any) -> None:
        effect(executor, action)
        h.world.rows = [r for r in h.world.rows if r[1] != "ComboBox"]

    h.semantic.on_execute = submits
    task = h.session.run_task(_plan(_choose(), _choose("Free", "Plan", step_id="s2")))
    assert task.results[0].evidence["reason"] == "target_not_found"
    assert task.status == "stopped", task
    assert task.evidence["detail"] == "navigated"
    assert [r.step_id for r in task.results] == ["s1"]


def test_a_choose_missed_under_a_new_title_stops_the_task_navigated() -> None:
    h, values = _harness()
    observer = Retitled(h.world)
    _rebuild(h, live=values, observer=observer)

    def retitles(_executor: Any, _action: Any) -> None:
        observer.title = "Another page"

    h.semantic.on_execute = retitles
    h.semantic.outcomes[SIZE[0]] = _unchanged()
    task = h.session.run_task(_plan(_choose(), _choose("Free", "Plan", step_id="s2")))
    assert task.evidence["detail"] == "navigated"
    assert [r.step_id for r in task.results] == ["s1"]


@pytest.mark.parametrize("process", ["notepad.exe", "msedge.exe", ""])
def test_a_choose_outside_chrome_is_refused_before_the_gate(process: str) -> None:
    h, values = _harness()
    _rebuild(h, live=values, observer=Process(h.world, process))
    result = h.session.run_step(_choose())
    assert result.gate == "unreached"
    assert result.evidence["detail"] == "unmeasured_provider"
    assert h.semantic.calls == []


# ---- twins (W4-11) and aliases (W4-10) -----------------------------------------------------------


def test_the_session_picks_the_on_screen_twin_for_a_choose_as_for_a_type() -> None:
    h, _values = _harness()
    top = replace(TOP, role="ComboBox", name="Size")
    bottom = replace(BOTTOM, role="ComboBox", name="Size")
    picked = h.session._twin(_choose(), _page(top, bottom), [top, bottom], focused=None, asked=None)
    assert picked == top.id
    assert h.session._twin_pick == "onscreen"


def test_a_twin_choose_at_r2_is_ambiguous() -> None:
    h, _values = _harness()
    top = replace(TOP, role="ComboBox", name="Plan")
    bottom = replace(BOTTOM, role="ComboBox", name="Plan")
    step = _choose(PREMIUM, "Plan")
    with pytest.raises(_StepFailureError) as caught:
        h.session._twin(step, _page(top, bottom), [top, bottom], focused=None, asked=None)
    assert caught.value.detail == "ambiguous_target"


def test_a_verified_choose_of_a_twin_pins_it_for_its_value_condition() -> None:
    h, _values = _harness()

    def resolve(*_args: Any, **_kw: Any) -> int:
        h.session._twin_pick = "onscreen"
        h.session._twin_rid = SIZE[3]
        return SIZE[0]

    h.session._resolve = resolve  # type: ignore[method-assign]  # the twin pick is unit-tested above
    assert h.session.run_step(_choose()).ok is True
    assert h.session._pins[pin_key(0x10, "ComboBox", "Size")] == SIZE[3]


def test_type_and_focus_never_alias_to_a_combobox() -> None:
    assert "ComboBox" not in ALIAS_ROLES
    h, _values = _harness()
    h.world.rows = [(r[0], "ComboBox", *r[2:]) if r[0] == COMPOSER[0] else r for r in h.world.rows]
    result = h.session.run_step(_type())
    assert result.ok is False
    assert result.evidence["detail"] == "no_match"
    assert h.physical.calls == []


def test_the_executor_action_for_another_verb_carries_no_option() -> None:
    h, _values = _harness()
    seen: list[Any] = []
    h.physical.on_execute = lambda _ex, action: seen.append(action)
    h.session.run_step(_type())
    assert seen
    assert getattr(seen[0], "option", None) is None
