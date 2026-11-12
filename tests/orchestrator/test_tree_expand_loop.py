"""expand / collapse on a tree item through the loop ([v2.81], packet tree-expand-2026-11-12 v2).

The semantic executor refuses `expand_by_click` with nothing sent (Chrome ignores Expand on an
ARIA tree item); the loop clicks the item once, physically and hit-tested, after re-reading its
state on the look the click is sent against (principal item 1, TE-04); T1 reads the live state;
one fallback click per item per task (TE-05)."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import effective_risk
from steerai.verify.predicates import CheckState, TitleMatches
from tests.orchestrator.fakes import BASE_ROWS, Row, not_delivered
from tests.orchestrator.test_loop import Harness
from tests.orchestrator.test_state_verbs_loop import Boxes

PROJECTS: Row = (31, "TreeItem", "Projects", (42, 90), None)
DRAFTS: Row = (32, "TreeItem", "Drafts", (42, 91), None)
TWISTY_ROW: Row = (33, "TreeItem", "Archive", (42, 92), None)
TWISTY: Row = (34, "Button", "Toggle Archive", (42, 93), 33)
_COLLAPSED, _EXPANDED, _PARTIAL, _LEAF = 0, 1, 2, 3


def _harness(*, opens: bool = True) -> tuple[Harness, Boxes]:
    """A tree whose items the physical click opens or closes, as the measured page does; a
    semantic expand / collapse on a tree item is refused `expand_by_click`."""
    h = Harness()
    h.world.rows = [*BASE_ROWS, PROJECTS, DRAFTS, TWISTY_ROW, TWISTY]
    boxes = Boxes(
        h.world,
        expanded={PROJECTS[3]: _COLLAPSED, DRAFTS[3]: _EXPANDED, TWISTY_ROW[3]: _COLLAPSED},
    )
    h.session._live = boxes
    for row in (PROJECTS, DRAFTS, TWISTY_ROW):
        h.semantic.outcomes[row[0]] = not_delivered(
            ErrorCode.ELEMENT_NOT_ACTIONABLE, "expand_by_click"
        )

    def toggles(_executor: Any, action: Any) -> None:
        row = next(r for r in h.world.rows if r[0] == action.element_id)
        if opens and row[3] in boxes.expanded:
            boxes.expanded[row[3]] = 1 - boxes.expanded[row[3]]

    h.physical.on_execute = toggles
    return h, boxes


def _expand(name: str = "Projects", step_id: str = "s1", verb: str = "expand") -> Step:
    return Step(id=step_id, subgoal="g1", verb=verb, target=Target(role="TreeItem", name=name))


def _plan(*steps: Step, condition: Any = None) -> Plan:
    return Plan(
        goal="open the Projects folder",
        subgoals=(
            Subgoal(id="g1", desc="d", condition=condition or TitleMatches(pattern="Never")),
        ),
        steps=steps,
    )


def test_a_closed_tree_item_is_opened_by_one_physical_click_and_read_back() -> None:
    h, boxes = _harness()
    result = h.session.run_step(_expand())
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("expand", PROJECTS[0], None)]
    assert h.physical.calls == [("click", PROJECTS[0], None)]
    assert boxes.expanded[PROJECTS[3]] == _EXPANDED
    assert result.label_kind == "t1"
    assert result.evidence["click_fallback"] == "physical"
    assert result.evidence["semantic_detail"] == "expand_by_click"


def test_an_open_tree_item_is_closed_by_one_click() -> None:
    h, boxes = _harness()
    result = h.session.run_step(_expand("Drafts", verb="collapse"))
    assert result.ok is True, result.to_dict()
    assert h.physical.calls == [("click", DRAFTS[0], None)]
    assert boxes.expanded[DRAFTS[3]] == _COLLAPSED


def test_an_item_opened_before_the_click_is_never_clicked() -> None:
    """Principal item 1, TE-04: a lazy tree or the person opened it after the executor's read;
    the re-read on the look the click is sent against says in state, and nothing is sent."""
    h, boxes = _harness()

    def opened_meanwhile(_executor: Any, _action: Any) -> None:
        boxes.expanded[PROJECTS[3]] = _EXPANDED

    h.semantic.on_execute = opened_meanwhile
    result = h.session.run_step(_expand())
    assert result.ok is True, result.to_dict()
    assert h.physical.calls == [], "a toggle click would have closed it"
    assert result.action is not None
    assert result.action.skipped is True


@pytest.mark.parametrize(("state", "detail"), [(_LEAF, "leaf_node"), (_PARTIAL, "partial_state")])
def test_a_re_read_leaf_or_partial_state_sends_nothing(state: int, detail: str) -> None:
    h, boxes = _harness()

    def changed(_executor: Any, _action: Any) -> None:
        boxes.expanded[PROJECTS[3]] = state

    h.semantic.on_execute = changed
    result = h.session.run_step(_expand())
    assert result.ok is False
    assert h.physical.calls == []
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == detail


def test_a_page_that_ignores_the_click_is_never_clicked_twice() -> None:
    """TE-05: the click landed and the state did not change; a later expand of the same item in
    the same task, a replan's or a later step's, is refused `already_clicked`."""
    h, _boxes = _harness(opens=False)
    first = h.session.run_step(_expand())
    assert first.ok is False
    assert first.evidence["reason"] == "state_differs"
    again = h.session.run_step(_expand(step_id="s2"))
    assert again.ok is False
    assert again.evidence["detail"] == "already_clicked"
    assert h.physical.calls == [("click", PROJECTS[0], None)]


def test_a_continuation_keeps_the_one_click_rule() -> None:
    h, _boxes = _harness(opens=False)
    h.session.run_task(_plan(_expand()))
    task = h.session.run_task(_plan(_expand(step_id="p2.s1")), continuation=True)
    assert task.results[0].evidence["detail"] == "already_clicked"
    assert len(h.physical.calls) == 1


def test_a_new_task_may_click_the_item_again() -> None:
    h, _boxes = _harness(opens=False)
    h.session.run_task(_plan(_expand()))
    h.session.run_task(_plan(_expand(step_id="t2")))
    assert len(h.physical.calls) == 2


def test_an_expanded_state_subgoal_ticks_after_the_click() -> None:
    h, _boxes = _harness()
    condition = CheckState(role="TreeItem", name_pattern="Projects", wanted="expanded")
    task = h.session.run_task(_plan(_expand(), condition=condition))
    assert task.status == "done", task
    assert task.ticked == {"g1"}


def test_an_item_whose_own_row_holds_a_button_is_never_clicked() -> None:
    """TE-13: a twisty Button in the row acts on its own; the refusal stands."""
    h, _boxes = _harness()
    result = h.session.run_step(_expand("Archive"))
    assert result.ok is False
    assert h.physical.calls == []
    assert result.evidence["detail"] == "expand_by_click"


def test_another_refusal_never_falls_back() -> None:
    h, _boxes = _harness()
    h.semantic.outcomes[PROJECTS[0]] = not_delivered(ErrorCode.ELEMENT_NOT_ACTIONABLE, "leaf_node")
    result = h.session.run_step(_expand())
    assert result.ok is False
    assert h.physical.calls == []


def test_a_button_refused_expand_by_click_is_never_clicked() -> None:
    """Only a tree item takes the click: a Button's Expand works and is never replaced."""
    h, _boxes = _harness()
    h.world.rows.append((35, "Button", "Shipping questions", (42, 95), None))
    h.semantic.outcomes[35] = not_delivered(ErrorCode.ELEMENT_NOT_ACTIONABLE, "expand_by_click")
    step = Step(
        id="b1",
        subgoal="g1",
        verb="expand",
        target=Target(role="Button", name="Shipping questions"),
    )
    result = h.session.run_step(step)
    assert result.ok is False
    assert h.physical.calls == []


# ---- the gate (principal item 4, TE-03, TE-23) ---------------------------------------------------

_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}


@pytest.mark.parametrize(
    "name", ["Projects", "Delete history", "Trash", "Deleted Items", "Pay now"]
)
def test_expand_on_a_tree_item_is_never_below_a_click_on_it(name: str) -> None:
    target = Target(role="TreeItem", name=name)
    click = effective_risk(Step(id="c", subgoal="g1", verb="click", target=target, risk="R0"))
    expand = effective_risk(Step(id="e", subgoal="g1", verb="expand", target=target, risk="R1"))
    assert _ORDER[expand] >= _ORDER[click]
    assert _ORDER[expand] >= 1, "the R1 floor"
