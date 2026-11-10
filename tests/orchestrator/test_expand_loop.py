"""`expand` / `collapse` through the loop (W5, 2026-11-10): T1 reads the live
ExpandCollapseState (expanded holds on 1 only, collapsed on 0 only), an R2/R3 miss stops the
task, a done key re-reads the state (WV-12), a verb that navigated away stops the task (W3-01),
a verb outside Chrome is refused before the gate (W3-04), and a Button without the pattern or a
LeafNode is refused by the executor with nothing sent and no physical fallback."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.replan import replan_trigger
from steerai.verify.predicates import CheckState, TitleMatches
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import BASE_ROWS, FakeWorld, Row
from tests.orchestrator.test_edge_cases import _rebuild
from tests.orchestrator.test_loop import Harness
from tests.orchestrator.test_state_verbs_loop import Boxes

SHIPPING: Row = (11, "Button", "Shipping questions", (42, 50), None)
HELP: Row = (12, "Button", "Help section", (42, 51), None)
HISTORY: Row = (13, "Button", "Delete history", (42, 52), None)
PLAIN: Row = (14, "Button", "Subscribe", (42, 53), None)
ANSWER: Row = (15, "Text", "Orders ship within two days.", (42, 54), None)
ROWS = [SHIPPING, HELP, HISTORY, PLAIN]
_COLLAPSED, _EXPANDED, _PARTIAL = 0, 1, 2


def _expands(boxes: Boxes) -> Any:
    """Expand sets the Button's state to 1 and Collapse to 0, as the measured page does."""

    def on_execute(_ex: Any, action: Any) -> None:
        row = next(r for r in boxes.world.rows if r[0] == action.element_id)
        boxes.expanded[row[3]] = _EXPANDED if action.verb == "expand" else _COLLAPSED

    return on_execute


def _harness() -> tuple[Harness, Boxes]:
    h = Harness()
    h.world.rows = [*BASE_ROWS, *ROWS]
    boxes = Boxes(
        h.world,
        expanded={SHIPPING[3]: _COLLAPSED, HELP[3]: _EXPANDED, HISTORY[3]: _COLLAPSED},
    )
    h.session._live = boxes
    h.semantic.on_execute = _expands(boxes)
    return h, boxes


def _expand(name: str = "Shipping questions", step_id: str = "s1", verb: str = "expand") -> Step:
    return Step(id=step_id, subgoal="g1", verb=verb, target=Target(role="Button", name=name))


def _collapse(name: str = "Help section", step_id: str = "s1") -> Step:
    return _expand(name, step_id, "collapse")


def _unchanged(verb: str = "expand") -> ActionResult:
    label = "ExpandCollapsePattern.Expand" if verb == "expand" else "ExpandCollapsePattern.Collapse"
    return ActionResult(
        verb=verb,
        delivery=Delivery.DELIVERED_UNVERIFIED,
        steps=[ActionStep(label=label, mechanism="semantic", outcome="succeeded")],
        code=ErrorCode.VERIFY_TIMEOUT,
        evidence={"detail": "unchanged"},
    )


def _refused(verb: str, detail: str) -> ActionResult:
    return ActionResult(
        verb=verb,
        delivery=Delivery.NOT_DELIVERED,
        steps=[],
        code=ErrorCode.ELEMENT_NOT_ACTIONABLE,
        evidence={"detail": detail},
    )


def _plan(*steps: Step, condition: Any = None) -> Plan:
    return Plan(
        goal="show the shipping questions",
        subgoals=(
            Subgoal(id="g1", desc="d", condition=condition or TitleMatches(pattern="Never")),
        ),
        steps=steps,
    )


# ---- verified by the live expand state ---------------------------------------------------------


def test_an_r1_expand_is_verified_by_the_live_expand_state_unasked() -> None:
    h, boxes = _harness()
    result = h.session.run_step(_expand())
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("expand", SHIPPING[0], None)]
    assert h.physical.calls == []
    assert h.confirmer.asked == []
    assert result.label_kind == "t1"
    assert result.evidence["risk"] == "R1"
    assert boxes.expanded[SHIPPING[3]] == _EXPANDED
    assert boxes.expand_reads > 0
    assert boxes.reads == 0, "a disclosure is never read through the ToggleState"
    assert boxes.selected_reads == 0, "nor through IsSelected"


def test_an_r1_collapse_is_verified_by_the_live_expand_state_unasked() -> None:
    h, boxes = _harness()
    result = h.session.run_step(_collapse())
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("collapse", HELP[0], None)]
    assert h.confirmer.asked == []
    assert boxes.expanded[HELP[3]] == _COLLAPSED


@pytest.mark.parametrize(
    ("step", "row"), [(_expand("Help section"), HELP), (_collapse("Shipping questions"), SHIPPING)]
)
def test_an_already_in_state_button_is_ok_with_nothing_sent(step: Step, row: Row) -> None:
    h, _boxes = _harness()
    h.semantic.outcomes[row[0]] = ActionResult.already_satisfied(step.verb)
    h.semantic.on_execute = None
    result = h.session.run_step(step)
    assert result.ok is True, result.to_dict()
    assert result.action is not None
    assert result.action.skipped is True


def test_an_expanded_state_subgoal_ticks_after_its_expand() -> None:
    h, _boxes = _harness()
    condition = CheckState(role="Button", name_pattern="Shipping questions", wanted="expanded")
    task = h.session.run_task(_plan(_expand(), condition=condition))
    assert task.status == "done", task
    assert task.ticked == {"g1"}


def test_a_collapsed_state_subgoal_ticks_after_its_collapse() -> None:
    h, _boxes = _harness()
    condition = CheckState(role="Button", name_pattern="Help section", wanted="collapsed")
    task = h.session.run_task(_plan(_collapse(), condition=condition))
    assert task.status == "done", task
    assert task.ticked == {"g1"}


def test_a_partially_expanded_read_is_a_miss_for_expand() -> None:
    """`expanded` holds on 1 only: a Button that lands on 2 misses `state_differs`."""
    h, boxes = _harness()

    def partial(_ex: Any, _action: Any) -> None:
        boxes.expanded[SHIPPING[3]] = _PARTIAL

    h.semantic.on_execute = partial
    result = h.session.run_step(_expand())
    assert result.ok is False
    assert result.evidence["reason"] == "state_differs"
    assert h.semantic.calls == [("expand", SHIPPING[0], None)]


def test_a_deny_word_on_the_button_asks_at_r3_then_expands() -> None:
    h, boxes = _harness()
    result = h.session.run_step(_expand("Delete history"))
    assert result.ok is True, result.to_dict()
    assert [a.risk for a in h.confirmer.asked] == ["R3"]
    assert h.confirmer.asked[0].verb == "expand"
    assert boxes.expanded[HISTORY[3]] == _EXPANDED


def test_a_miss_at_r3_stops_the_task_with_no_second_expand() -> None:
    h, _boxes = _harness()
    h.semantic.on_execute = None
    h.semantic.outcomes[HISTORY[0]] = _unchanged()
    task = h.session.run_task(_plan(_expand("Delete history"), _expand(step_id="s2")))
    assert task.status == "stopped", task
    assert task.evidence["detail"] == "state_unverified"
    assert h.semantic.calls == [("expand", HISTORY[0], None)]


def test_a_miss_at_r1_does_not_stop_the_task() -> None:
    h, _boxes = _harness()
    h.semantic.on_execute = None
    h.semantic.outcomes[SHIPPING[0]] = _unchanged()
    task = h.session.run_task(_plan(_expand(), _collapse(step_id="s2")))
    assert [r.step_id for r in task.results] == ["s1", "s2"]
    assert task.results[0].ok is False
    assert task.results[0].evidence["reason"] == "state_differs"


# ---- the done re-read (WV-12; a done key is recorded for R2/R3 steps) --------------------------


def test_a_done_key_whose_button_was_collapsed_since_stops_without_an_expand() -> None:
    h, boxes = _harness()
    step = _expand("Delete history")
    assert h.session.run_step(step).ok is True
    boxes.expanded[HISTORY[3]] = _COLLAPSED  # the page or the person closed it since
    again = h.session.run_step(replace(step, id="s9"))
    assert again.ok is False
    assert again.action is None
    assert again.evidence["detail"] == "state_changed_since_done"
    assert h.semantic.calls == [("expand", HISTORY[0], None)]
    assert [a.risk for a in h.confirmer.asked] == ["R3"], "asked once, never for the re-read"
    assert boxes.reads == 0, "the done re-read is the expand state, never the ToggleState"


def test_a_done_key_whose_button_is_still_expanded_is_done_already() -> None:
    h, _boxes = _harness()
    step = _expand("Delete history")
    assert h.session.run_step(step).ok is True
    again = h.session.run_step(replace(step, id="s9"))
    assert again.ok is True
    assert again.gate == "done_already"
    assert h.semantic.calls == [("expand", HISTORY[0], None)]


def test_a_done_collapse_key_re_reads_collapsed() -> None:
    h, boxes = _harness()
    boxes.expanded[HISTORY[3]] = _EXPANDED
    step = _collapse("Delete history")
    assert h.session.run_step(step).ok is True
    boxes.expanded[HISTORY[3]] = _EXPANDED  # opened again since
    again = h.session.run_step(replace(step, id="s9"))
    assert again.ok is False
    assert again.evidence["detail"] == "state_changed_since_done"
    assert h.semantic.calls == [("collapse", HISTORY[0], None)]


# ---- refusals from the executor: nothing sent, no physical fallback ---------------------------


@pytest.mark.parametrize("verb", ["expand", "collapse"])
@pytest.mark.parametrize("detail", ["no_ExpandCollapsePattern", "leaf_node"])
def test_a_button_the_executor_refuses_fails_with_nothing_else_sent(verb: str, detail: str) -> None:
    """A plain Button (Invoke only) or a LeafNode: the refusal's detail is the step's, no
    physical click follows (the [v2.50] fallback is a click's only), and the task replans."""
    h, _boxes = _harness()
    h.semantic.on_execute = None
    h.semantic.outcomes[PLAIN[0]] = _refused(verb, detail)
    task = h.session.run_task(_plan(_expand("Subscribe", verb=verb), _expand(step_id="s2")))
    result = task.results[0]
    assert result.ok is False
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == detail
    assert result.label_kind is None, "nothing went out: no T1"
    assert h.semantic.calls[0] == (verb, PLAIN[0], None)
    assert h.physical.calls == []
    assert task.evidence.get("detail") != "navigated"


def test_a_subscribe_button_asks_at_r2_before_the_executor_refuses_it() -> None:
    """A Button is a control: "Subscribe" asks at R2 as a click would; the refusal comes after."""
    h, _boxes = _harness()
    h.semantic.on_execute = None
    h.semantic.outcomes[PLAIN[0]] = _refused("expand", "no_ExpandCollapsePattern")
    result = h.session.run_step(_expand("Subscribe"))
    assert result.ok is False
    assert [a.risk for a in h.confirmer.asked] == ["R2"]
    assert result.evidence["detail"] == "no_ExpandCollapsePattern"


# ---- navigation (W3-01) --------------------------------------------------------------------------


@dataclass
class Retitled:
    world: FakeWorld
    title: str | None = None

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        snapshot = self.world.observe(hwnd, **kwargs)
        return snapshot if self.title is None else replace(snapshot, title=self.title)


def _retitles(h: Harness, boxes: Boxes, *, lands: bool) -> Retitled:
    observer = Retitled(h.world)
    _rebuild(h, live=boxes, observer=observer)
    expand = _expands(boxes)

    def on_execute(executor: Any, action: Any) -> None:
        observer.title = "Another page"
        if lands:
            expand(executor, action)

    h.semantic.on_execute = on_execute
    return observer


def _assert_navigated(task: Any, h: Harness) -> None:
    assert task.status == "stopped", task
    assert task.code is ErrorCode.VERIFY_FAILED
    assert task.evidence["detail"] == "navigated"
    assert [r.step_id for r in task.results] == ["s1"], "no later step runs"
    assert len(h.semantic.calls) == 1
    steps = {s.id: s for s in (_expand(), _collapse(step_id="s2"))}
    assert replan_trigger(task, steps) is None, "no replan resends it"


def test_an_expand_whose_target_vanished_stops_the_task_navigated() -> None:
    h, _boxes = _harness()

    def navigates(_executor: Any, _action: Any) -> None:
        h.world.rows = [r for r in h.world.rows if r[1] != "Button"]

    h.semantic.on_execute = navigates
    task = h.session.run_task(_plan(_expand(), _collapse(step_id="s2")))
    assert task.results[0].evidence["reason"] == "target_not_found"
    _assert_navigated(task, h)


def test_an_expand_missed_under_a_new_title_stops_the_task_navigated() -> None:
    h, boxes = _harness()
    _retitles(h, boxes, lands=False)
    task = h.session.run_task(_plan(_expand(), _collapse(step_id="s2")))
    _assert_navigated(task, h)


def test_a_verified_expand_whose_title_changed_runs_on() -> None:
    h, boxes = _harness()
    _retitles(h, boxes, lands=True)
    task = h.session.run_task(_plan(_expand(), _collapse(step_id="s2")))
    assert [r.step_id for r in task.results] == ["s1", "s2"]
    assert task.results[0].ok is True
    assert task.evidence.get("detail") != "navigated"


# ---- the provider (W3-04) ------------------------------------------------------------------------


@dataclass
class Process:
    world: FakeWorld
    name: str

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        return replace(self.world.observe(hwnd, **kwargs), process=self.name)


@pytest.mark.parametrize("step", [_expand(), _collapse()])
@pytest.mark.parametrize("process", ["notepad.exe", "msedge.exe", "", "chrome.exe.evil"])
def test_a_verb_outside_chrome_is_refused_before_the_gate(step: Step, process: str) -> None:
    h, boxes = _harness()
    _rebuild(h, live=boxes, observer=Process(h.world, process))
    task = h.session.run_task(_plan(step, _expand(step_id="s2")))
    result = task.results[0]
    assert result.ok is False
    assert result.gate == "unreached"
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "unmeasured_provider"
    assert h.semantic.calls == []
    assert h.confirmer.asked == []
    assert task.status == "stopped"


def test_a_click_on_a_button_outside_chrome_is_unaffected() -> None:
    h, boxes = _harness()
    _rebuild(h, live=boxes, observer=Process(h.world, "notepad.exe"))
    step = Step(
        id="k1",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="Shipping questions"),
        expect=TitleMatches(pattern="Amir"),
    )
    result = h.session.run_step(step)
    assert result.evidence.get("detail") != "unmeasured_provider"
    assert h.semantic.calls == [("click", SHIPPING[0], None)]


# ---- twins ---------------------------------------------------------------------------------------


def test_twin_buttons_named_alike_are_ambiguous() -> None:
    h, _boxes = _harness()
    h.world.rows = [*h.world.rows, (21, "Button", "shipping questions", (42, 61), None)]
    result = h.session.run_step(_expand())
    assert result.ok is False
    assert result.evidence["detail"] == "ambiguous_target"
    assert h.semantic.calls == []


# ---- open it, read it, close it (#210 review) ---------------------------------------------------


def _untick_records(h: Harness) -> list[dict[str, Any]]:
    import json  # noqa: PLC0415 - the trace lines

    return [r for r in (json.loads(line) for line in h.trace.lines) if r.get("kind") == "untick"]


def test_an_expand_then_a_collapse_of_the_same_button_ends_done() -> None:
    """#210 review: the W4-04 re-read before `done` found the Help section collapsed, withdrew
    the expand's tick with `reset` and replanned until MAX_REPLANS. A later widget verb on the
    same Button, whose subgoal ticked, supersedes the earlier state: the task ends `done`."""
    h, boxes = _harness()
    boxes.expanded[HELP[3]] = _COLLAPSED
    plan = Plan(
        goal="open the help section, read it, then close it",
        subgoals=(
            Subgoal(
                id="g1", desc="opened", condition=CheckState("Button", "Help section", "expanded")
            ),
            Subgoal(
                id="g2", desc="closed", condition=CheckState("Button", "Help section", "collapsed")
            ),
        ),
        steps=(
            _expand("Help section", "s1"),
            replace(_collapse("Help section", "s2"), subgoal="g2"),
        ),
    )
    result = h.session.run_task(plan)
    assert result.status == "done", result
    assert result.ticked == {"g1", "g2"}
    assert _untick_records(h) == []


def test_a_different_button_that_collapses_the_first_still_resets() -> None:
    """An exclusive accordion: expanding Billing collapses Shipping. The supersede rule needs the
    same exact name, so the earlier tick is withdrawn and the task cannot end `done`."""
    h, boxes = _harness()
    billing: Row = (16, "Button", "Billing questions", (42, 55), None)
    h.world.rows.append(billing)
    boxes.expanded[billing[3]] = _COLLAPSED
    expands = _expands(boxes)

    def exclusive(executor: Any, action: Any) -> None:
        expands(executor, action)
        if action.element_id == billing[0]:
            boxes.expanded[SHIPPING[3]] = _COLLAPSED

    h.semantic.on_execute = exclusive
    plan = Plan(
        goal="open the shipping questions and the billing questions",
        subgoals=(
            Subgoal(
                id="g1",
                desc="shipping",
                condition=CheckState("Button", "Shipping questions", "expanded"),
            ),
            Subgoal(
                id="g2",
                desc="billing",
                condition=CheckState("Button", "Billing questions", "expanded"),
            ),
        ),
        steps=(
            _expand("Shipping questions", "s1"),
            replace(_expand("Billing questions", "s2"), subgoal="g2"),
        ),
    )
    result = h.session.run_task(plan)
    assert result.status != "done", result
    assert [r["subgoal"] for r in _untick_records(h)] == ["g1"]
