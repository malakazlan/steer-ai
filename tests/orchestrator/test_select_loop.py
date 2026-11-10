"""`select` through the loop (W3 v1, its verdict and W3 v2, 2026-11-09): T1 reads the live
IsSelected, an R2 miss stops the task, a done key re-reads the selection (WV-12), a state verb
that navigated away stops the task (W3-01), a state verb outside Chrome is refused before the
gate (W3-04), and twin tabs or radios are never picked (W3-08). W3b (2026-11-10): a listbox
option is admitted before the gate only when it exposes SelectionItem under a single-select List
with no ComboBox, Tree or DataGrid ancestor, and an option scrolled out of view inside its box is
scrolled first."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.loop import _StepFailureError
from steerai.orchestrator.replan import replan_trigger
from steerai.verify.predicates import CheckState, TitleMatches
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import BASE_ROWS, FakeLive, FakeWorld, Row
from tests.orchestrator.test_edge_cases import _rebuild
from tests.orchestrator.test_loop import Harness
from tests.orchestrator.test_state_verbs_loop import Boxes, _flips
from tests.world.test_twins import BOTTOM, TOP, _page

TABLIST: Row = (11, "Tab", "Sections", (42, 50), None)
OVERVIEW: Row = (12, "TabItem", "Overview", (42, 51), 11)
DETAILS: Row = (13, "TabItem", "Details", (42, 52), 11)
TERMS: Row = (14, "Group", "Do you agree to the terms?", (42, 53), None)
AGREE: Row = (15, "RadioButton", "I agree", (42, 54), 14)
DISAGREE: Row = (16, "RadioButton", "I do not agree", (42, 55), 14)
ALERTS: Row = (17, "CheckBox", "Email alerts", (42, 56), None)
ROWS = [TABLIST, OVERVIEW, DETAILS, TERMS, AGREE, DISAGREE, ALERTS]
LISTBOX: Row = (31, "List", "Destinations", (42, 80), None)
ACCRA: Row = (32, "ListItem", "Accra", (42, 81), 31)
KYOTO: Row = (33, "ListItem", "Kyoto", (42, 82), 31)
PLAIN: Row = (34, "ListItem", "Plain", (42, 83), 31)
COMBO: Row = (35, "ComboBox", "Country", (42, 84), None)
COMBO_LIST: Row = (36, "List", "Country", (42, 85), 35)
FRANCE: Row = (37, "ListItem", "France", (42, 86), 36)
TOPPINGS: Row = (38, "List", "Toppings", (42, 87), None)
OLIVES: Row = (39, "ListItem", "Olives", (42, 88), 38)
FOLDERS: Row = (40, "Tree", "Folders", (42, 89), None)
INBOX: Row = (41, "ListItem", "Inbox", (42, 90), 40)
ORDERS: Row = (42, "DataGrid", "Orders", (42, 91), None)
ORDER_ROWS: Row = (43, "List", "Rows", (42, 92), 42)
ORDER: Row = (44, "ListItem", "Order 7", (42, 93), 43)
LOOSE: Row = (45, "ListItem", "Loose", (42, 94), None)
BARE: Row = (46, "List", "Bare", (42, 95), None)
BARE_ITEM: Row = (47, "ListItem", "Bare item", (42, 96), 46)
LIST_ROWS = [
    LISTBOX,
    ACCRA,
    KYOTO,
    PLAIN,
    COMBO,
    COMBO_LIST,
    FRANCE,
    TOPPINGS,
    OLIVES,
    FOLDERS,
    INBOX,
    ORDERS,
    ORDER_ROWS,
    ORDER,
    LOOSE,
    BARE,
    BARE_ITEM,
]
LIST_ITEMS = [ACCRA, KYOTO, PLAIN, FRANCE, OLIVES, INBOX, ORDER, LOOSE, BARE_ITEM]


def _selects(boxes: Boxes) -> Any:
    """Select clears the element's peers under the same parent, then selects it."""

    def on_execute(_ex: Any, action: Any) -> None:
        row = next(r for r in boxes.world.rows if r[0] == action.element_id)
        for peer in boxes.world.rows:
            if peer[4] == row[4]:
                boxes.selected[peer[3]] = False
        boxes.selected[row[3]] = True

    return on_execute


def _harness() -> tuple[Harness, Boxes]:
    h = Harness()
    h.world.rows = [*BASE_ROWS, *ROWS]
    boxes = Boxes(
        h.world,
        state={ALERTS[3]: 0},
        selected={OVERVIEW[3]: True, DETAILS[3]: False, AGREE[3]: False, DISAGREE[3]: False},
    )
    h.session._live = boxes
    h.semantic.on_execute = _selects(boxes)
    return h, boxes


def _listbox_harness() -> tuple[Harness, Boxes]:
    """The W3b shapes as measured (probe `widget_patterns --section disclosure`): a single-select
    List whose items expose SelectionItem; plus the refused ones: an item without SelectionItem,
    a ComboBox's own list, a multi-select List, a Tree, a DataGrid, an item under no List and a
    List without SelectionPattern."""
    h, boxes = _harness()
    h.world.rows = [*h.world.rows, *LIST_ROWS]
    boxes.selected.update({item[3]: False for item in LIST_ITEMS})
    boxes.selected[ACCRA[3]] = True
    boxes.without_selection_item.add(PLAIN[3])
    boxes.containers.update(
        {
            LISTBOX[3]: False,
            COMBO_LIST[3]: False,
            TOPPINGS[3]: True,
            ORDER_ROWS[3]: False,
        }
    )
    return h, boxes


def _select(name: str = "Details", role: str = "TabItem", step_id: str = "s1") -> Step:
    return Step(id=step_id, subgoal="g1", verb="select", target=Target(role=role, name=name))


def _check(step_id: str = "c1") -> Step:
    return Step(
        id=step_id, subgoal="g1", verb="check", target=Target(role="CheckBox", name="Email alerts")
    )


def _unchanged(verb: str = "select") -> ActionResult:
    return ActionResult(
        verb=verb,
        delivery=Delivery.DELIVERED_UNVERIFIED,
        steps=[ActionStep(label=f"semantic.{verb}", mechanism="semantic", outcome="succeeded")],
        code=ErrorCode.VERIFY_TIMEOUT,
        evidence={"detail": "unchanged"},
    )


def _plan(*steps: Step, condition: Any = None) -> Plan:
    return Plan(
        goal="switch to the details tab",
        subgoals=(
            Subgoal(id="g1", desc="d", condition=condition or TitleMatches(pattern="Never")),
        ),
        steps=steps,
    )


# ---- verified by the live selection --------------------------------------------------------------


def test_an_r1_select_is_verified_by_the_live_is_selected_read_unasked() -> None:
    h, boxes = _harness()
    result = h.session.run_step(_select())
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("select", DETAILS[0], None)]
    assert h.confirmer.asked == []
    assert result.label_kind == "t1"
    assert result.evidence["risk"] == "R1"
    assert boxes.selected_reads > 0
    assert boxes.reads == 0, "a selection is never read through the ToggleState"


def test_an_already_selected_tab_is_ok_with_nothing_sent() -> None:
    h, _boxes = _harness()
    h.semantic.outcomes[OVERVIEW[0]] = ActionResult.already_satisfied("select")
    h.semantic.on_execute = None
    result = h.session.run_step(_select("Overview"))
    assert result.ok is True, result.to_dict()
    assert result.action is not None
    assert result.action.skipped is True


def test_a_selected_state_subgoal_ticks_after_its_select() -> None:
    h, _boxes = _harness()
    condition = CheckState(role="TabItem", name_pattern="Details", wanted="selected")
    task = h.session.run_task(_plan(_select(), condition=condition))
    assert task.status == "done", task
    assert task.ticked == {"g1"}


def test_a_consent_radio_asks_then_selects() -> None:
    h, boxes = _harness()
    result = h.session.run_step(_select("I agree", "RadioButton"))
    assert result.ok is True, result.to_dict()
    assert [a.risk for a in h.confirmer.asked] == ["R2"]
    assert h.confirmer.asked[0].verb == "select"
    assert boxes.selected[AGREE[3]] is True


def test_a_miss_at_r2_stops_the_task_with_no_second_select() -> None:
    h, _boxes = _harness()
    h.semantic.on_execute = None
    h.semantic.outcomes[AGREE[0]] = _unchanged()
    task = h.session.run_task(_plan(_select("I agree", "RadioButton"), _select(step_id="s2")))
    assert task.status == "stopped", task
    assert task.evidence["detail"] == "state_unverified"
    assert h.semantic.calls == [("select", AGREE[0], None)]


def test_a_miss_at_r1_does_not_stop_the_task() -> None:
    h, _boxes = _harness()
    h.semantic.on_execute = None
    h.semantic.outcomes[DETAILS[0]] = _unchanged()
    task = h.session.run_task(_plan(_select(), _select("Overview", step_id="s2")))
    assert [r.step_id for r in task.results] == ["s1", "s2"]
    assert task.results[0].ok is False
    assert task.results[0].evidence["reason"] == "state_differs"


def test_a_done_key_whose_radio_was_deselected_since_stops_without_a_select() -> None:
    h, boxes = _harness()
    step = _select("I agree", "RadioButton")
    assert h.session.run_step(step).ok is True
    boxes.selected[AGREE[3]] = False  # the page or the person chose another option since
    again = h.session.run_step(replace(step, id="s9"))
    assert again.ok is False
    assert again.action is None
    assert again.evidence["detail"] == "state_changed_since_done"
    assert h.semantic.calls == [("select", AGREE[0], None)]
    assert boxes.reads == 0, "the done re-read is the selection, never the ToggleState"


def test_a_done_key_whose_radio_is_still_selected_is_done_already() -> None:
    h, _boxes = _harness()
    step = _select("I agree", "RadioButton")
    assert h.session.run_step(step).ok is True
    again = h.session.run_step(replace(step, id="s9"))
    assert again.ok is True
    assert again.gate == "done_already"
    assert h.semantic.calls == [("select", AGREE[0], None)]


# ---- a listbox option (W3b) ----------------------------------------------------------------------


def test_a_listbox_option_is_selected_through_its_selection_item_unasked() -> None:
    h, boxes = _listbox_harness()
    result = h.session.run_step(_select("Kyoto", "ListItem"))
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("select", KYOTO[0], None)]
    assert h.confirmer.asked == []
    assert result.evidence["risk"] == "R1"
    assert boxes.selected[KYOTO[3]] is True
    assert boxes.selected[ACCRA[3]] is False, "a single-select List: the peer is cleared"
    assert boxes.admission_reads == 2, "the item's SelectionItem and the List's Selection"


def test_a_selected_listbox_subgoal_ticks_after_its_select() -> None:
    h, _boxes = _listbox_harness()
    condition = CheckState(role="ListItem", name_pattern="Kyoto", wanted="selected")
    task = h.session.run_task(_plan(_select("Kyoto", "ListItem"), condition=condition))
    assert task.status == "done", task
    assert task.ticked == {"g1"}


def test_an_already_selected_listbox_option_is_ok_with_nothing_sent() -> None:
    h, _boxes = _listbox_harness()
    h.semantic.outcomes[ACCRA[0]] = ActionResult.already_satisfied("select")
    h.semantic.on_execute = None
    result = h.session.run_step(_select("Accra", "ListItem"))
    assert result.ok is True, result.to_dict()
    assert result.action is not None
    assert result.action.skipped is True


@pytest.mark.parametrize(
    ("item", "detail"),
    [
        (FRANCE, "in_combobox"),
        (OLIVES, "multi_select"),
        (INBOX, "not_a_listbox_item"),
        (ORDER, "not_a_listbox_item"),
        (LOOSE, "not_a_listbox_item"),
        (PLAIN, "not_a_listbox_item"),
        (BARE_ITEM, "not_a_listbox_item"),
    ],
    ids=[
        "combobox",
        "multi_select",
        "tree",
        "datagrid",
        "no_list",
        "no_selection_item",
        "no_selection",
    ],
)
def test_an_item_outside_a_single_select_listbox_is_refused_before_the_gate(
    item: Row, detail: str
) -> None:
    h, _boxes = _listbox_harness()
    task = h.session.run_task(_plan(_select(item[2], "ListItem"), _select(step_id="s2")))
    result = task.results[0]
    assert result.ok is False
    assert result.gate == "unreached"
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == detail
    assert h.semantic.calls == []
    assert h.confirmer.asked == []


def test_a_listbox_option_whose_reader_cannot_read_patterns_is_refused() -> None:
    """A live reader without the admission reads: unclear means fail, nothing sent."""
    h, _boxes = _listbox_harness()
    _rebuild(h, live=FakeLive(h.world))
    result = h.session.run_step(_select("Kyoto", "ListItem"))
    assert result.ok is False
    assert result.gate == "unreached"
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "selection_unreadable"
    assert h.semantic.calls == []


def test_a_com_failure_in_the_admission_read_is_refused() -> None:
    h, boxes = _listbox_harness()
    boxes.admission_failure = SteerError(ErrorCode.PERCEPTION_TIMEOUT, "live_read_failed")
    result = h.session.run_step(_select("Kyoto", "ListItem"))
    assert result.ok is False
    assert result.evidence["detail"] == "selection_unreadable"
    assert h.semantic.calls == []


def test_a_tab_or_a_radio_never_reads_the_listbox_patterns() -> None:
    h, boxes = _listbox_harness()
    assert h.session.run_step(_select()).ok is True
    assert h.session.run_step(_select("I agree", "RadioButton")).ok is True
    assert boxes.admission_reads == 0


def test_a_click_on_a_listbox_option_is_unaffected_by_the_admission() -> None:
    h, boxes = _listbox_harness()
    step = Step(
        id="k1",
        subgoal="g1",
        verb="click",
        target=Target(role="ListItem", name="France"),
        expect=TitleMatches(pattern="Amir"),
    )
    result = h.session.run_step(step)
    assert result.evidence.get("detail") != "in_combobox"
    assert h.semantic.calls == [("click", FRANCE[0], None)]
    assert boxes.admission_reads == 0


@dataclass
class Hidden:
    """Options scrolled out of view inside an on-screen listbox (H3 n7): offscreen until the
    pre-gate ScrollIntoView ([v2.55])."""

    world: FakeWorld
    hidden: set[tuple[int, ...]]

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        snapshot = self.world.observe(hwnd, **kwargs)
        elements = [
            replace(e, offscreen=True) if e.runtime_id in self.hidden else e
            for e in snapshot.elements
        ]
        return replace(snapshot, elements=elements)


def test_an_offscreen_listbox_option_is_scrolled_into_view_then_selected() -> None:
    h, boxes = _listbox_harness()
    page = Hidden(h.world, hidden={KYOTO[3]})
    _rebuild(h, live=boxes, observer=page)
    select = _selects(boxes)

    def on_execute(executor: Any, action: Any) -> None:
        if action.verb == "scroll_into_view":
            page.hidden.clear()
        else:
            select(executor, action)

    h.semantic.on_execute = on_execute
    result = h.session.run_step(_select("Kyoto", "ListItem"))
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("scroll_into_view", KYOTO[0], None), ("select", KYOTO[0], None)]
    assert result.evidence["scrolled"] == "delivered_verified"
    assert boxes.selected[KYOTO[3]] is True


def test_an_offscreen_item_outside_a_listbox_is_refused_before_any_scroll() -> None:
    h, boxes = _listbox_harness()
    page = Hidden(h.world, hidden={FRANCE[3]})
    _rebuild(h, live=boxes, observer=page)
    result = h.session.run_step(_select("France", "ListItem"))
    assert result.ok is False
    assert result.evidence["detail"] == "in_combobox"
    assert h.semantic.calls == []


# ---- navigation (W3-01) --------------------------------------------------------------------------


@dataclass
class Retitled:
    """The page's title after the dispatch: a state verb that navigated retitles the window."""

    world: FakeWorld
    title: str | None = None

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        snapshot = self.world.observe(hwnd, **kwargs)
        return snapshot if self.title is None else replace(snapshot, title=self.title)


def _retitles(h: Harness, boxes: Boxes, *, lands: bool) -> Retitled:
    observer = Retitled(h.world)
    _rebuild(h, live=boxes, observer=observer)
    select = _selects(boxes)

    def on_execute(executor: Any, action: Any) -> None:
        observer.title = "Another page"
        if lands:
            select(executor, action)

    h.semantic.on_execute = on_execute
    return observer


def _assert_navigated(task: Any, h: Harness, sent: int = 1) -> None:
    assert task.status == "stopped", task
    assert task.code is ErrorCode.VERIFY_FAILED
    assert task.evidence["detail"] == "navigated"
    assert [r.step_id for r in task.results] == ["s1"], "no later step runs"
    assert len(h.semantic.calls) == sent
    steps = {s.id: s for s in (_select(), _select("Overview", step_id="s2"))}
    assert replan_trigger(task, steps) is None, "no replan resends it"


def test_a_select_whose_target_vanished_stops_the_task_navigated() -> None:
    h, _boxes = _harness()

    def navigates(_executor: Any, _action: Any) -> None:
        h.world.rows = [r for r in h.world.rows if r[1] not in ("TabItem", "Tab")]

    h.semantic.on_execute = navigates
    task = h.session.run_task(_plan(_select(), _select("Overview", step_id="s2")))
    assert task.results[0].evidence["reason"] == "target_not_found"
    _assert_navigated(task, h)


def test_a_select_missed_under_a_new_title_stops_the_task_navigated() -> None:
    h, boxes = _harness()
    _retitles(h, boxes, lands=False)
    task = h.session.run_task(_plan(_select(), _select("Overview", step_id="s2")))
    _assert_navigated(task, h)


def test_a_verified_select_whose_title_changed_runs_on() -> None:
    """Single-page-app tabs retitle on a correct select: a verified State never stops."""
    h, boxes = _harness()
    _retitles(h, boxes, lands=True)
    task = h.session.run_task(_plan(_select(), _select("Overview", step_id="s2")))
    assert [r.step_id for r in task.results] == ["s1", "s2"]
    assert task.results[0].ok is True
    assert task.evidence.get("detail") != "navigated"


def test_a_check_missed_under_a_new_title_stops_the_task_navigated() -> None:
    """W3-01 closes WV-02 for check and uncheck too."""
    h, boxes = _harness()
    observer = _retitles(h, boxes, lands=False)
    del observer
    task = h.session.run_task(
        Plan(
            goal="turn on email alerts",
            subgoals=(Subgoal(id="g1", desc="d", condition=TitleMatches(pattern="Never")),),
            steps=(_check("s1"), _check("s2")),
        )
    )
    assert task.status == "stopped", task
    assert task.evidence["detail"] == "navigated"
    assert [r.step_id for r in task.results] == ["s1"]


def test_a_verified_check_whose_title_changed_runs_on() -> None:
    h, boxes = _harness()
    observer = _retitles(h, boxes, lands=False)
    flip = _flips(boxes)

    def on_execute(executor: Any, action: Any) -> None:
        observer.title = "Another page"
        flip(executor, action)

    h.semantic.on_execute = on_execute
    task = h.session.run_task(_plan(_check("s1"), _select(step_id="s2")))
    assert [r.step_id for r in task.results] == ["s1", "s2"]


# ---- the provider (W3-04) ------------------------------------------------------------------------


@dataclass
class Process:
    world: FakeWorld
    name: str

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        return replace(self.world.observe(hwnd, **kwargs), process=self.name)


@pytest.mark.parametrize(
    "step",
    [_select(), _select("I agree", "RadioButton"), _check(), replace(_check(), verb="uncheck")],
)
@pytest.mark.parametrize("process", ["notepad.exe", "msedge.exe", "", "chrome.exe.evil"])
def test_a_state_verb_outside_chrome_is_refused_before_the_gate(step: Step, process: str) -> None:
    h, boxes = _harness()
    _rebuild(h, live=boxes, observer=Process(h.world, process))
    task = h.session.run_task(_plan(step, _select("Overview", step_id="s2")))
    result = task.results[0]
    assert result.ok is False
    assert result.gate == "unreached"
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "unmeasured_provider"
    assert h.semantic.calls == []
    assert h.confirmer.asked == []
    assert task.status == "stopped"
    assert [r.step_id for r in task.results] == [step.id]


def test_chrome_is_matched_case_insensitively() -> None:
    h, boxes = _harness()
    _rebuild(h, live=boxes, observer=Process(h.world, "Chrome.EXE"))
    h.semantic.on_execute = _selects(boxes)
    assert h.session.run_step(_select()).ok is True


def test_a_click_outside_chrome_is_unaffected() -> None:
    h, boxes = _harness()
    _rebuild(h, live=boxes, observer=Process(h.world, "notepad.exe"))
    step = Step(
        id="k1",
        subgoal="g1",
        verb="click",
        target=Target(role="TabItem", name="Details"),
        expect=TitleMatches(pattern="Amir"),
    )
    result = h.session.run_step(step)
    assert result.evidence.get("detail") != "unmeasured_provider"
    assert h.semantic.calls == [("click", DETAILS[0], None)]


# ---- twins (W3-08) -------------------------------------------------------------------------------

TWIN_TAB: Row = (21, "TabItem", "details", (42, 61), None)
TWIN_RADIO: Row = (22, "RadioButton", "I AGREE", (42, 62), None)


@pytest.mark.parametrize(
    ("twin", "step"),
    [(TWIN_TAB, _select()), (TWIN_RADIO, _select("I agree", "RadioButton"))],
)
def test_twin_tabs_or_radios_named_alike_are_ambiguous(twin: Row, step: Step) -> None:
    h, _boxes = _harness()
    h.world.rows = [*h.world.rows, twin]
    result = h.session.run_step(step)
    assert result.ok is False
    assert result.evidence["detail"] == "ambiguous_target"
    assert h.semantic.calls == []


@pytest.mark.parametrize("role", ["TabItem", "RadioButton"])
def test_the_session_never_picks_a_twin_for_select(role: str) -> None:
    """A page-level twin set (shared automation id, same chain) whose one copy is on screen
    and focused: focus and type would pick it, select never does (verdict ruling (e))."""
    h, _boxes = _harness()
    top = replace(TOP, role=role, name="Details")
    bottom = replace(BOTTOM, role=role, name="Details")
    snapshot = _page(top, bottom)
    step = Step(id="s1", subgoal="g1", verb="select", target=Target(role=role, name="Details"))
    for focused in (None, lambda: top.runtime_id):
        with pytest.raises(_StepFailureError) as caught:
            h.session._twin(step, snapshot, [top, bottom], focused=focused, asked=None)
        assert caught.value.detail == "ambiguous_target"
        assert h.session._twin_pick is None
    focus = Step(id="f1", subgoal="g1", verb="focus", target=Target(role="Edit", name="Details"))
    edits = [replace(top, role="Edit"), replace(bottom, role="Edit")]
    assert h.session._twin(focus, _page(*edits), edits, focused=None, asked=None) == top.id
