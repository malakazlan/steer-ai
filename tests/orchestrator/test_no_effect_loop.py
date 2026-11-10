"""An Enter submit that provably did nothing (packet no-effect-submit-2026-11-10; [v2.64] npm1):
while npm's suggestion dropdown is open the form's first submit button is an invisible backdrop,
so Enter only closes the dropdown. After the verify's own deadline, never earlier, the loop reads
five facts: the address bar equals its before read, the top Document keeps its RuntimeId and
name, the composer still holds the approved text (resolved in the deadline snapshot, read through
its handle), nothing with a new RuntimeId appeared outside the composer's container, and the
window list is the step's. All held: the step ends `no_effect`, the ledger closes the record
`no_effect` (never done on a resume), the task ends `stopped`/`no_effect`, and the replan may
submit through the one Button beside the field. Any fact missing: today's `unverified_effect`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import Plan, Step, Subgoal
from steerai.orchestrator.replan import Progress, replan_trigger
from steerai.planner.contracts import FailedStep
from steerai.probes.windows import WindowInfo
from steerai.verify.predicates import Appeared, Within
from tests.orchestrator.fakes import COMPOSER, SEND
from tests.orchestrator.test_edge_cases import FocusLive, _rebuild
from tests.orchestrator.test_loop import HELLO, Harness, _send
from tests.orchestrator.test_url_query_loop import (
    GOAL,
    HOME,
    LOADED,
    REPLACING,
    RESULTS,
    Bar,
    _harness,
    _submit,
)

PAGE = (40, "Document", "Bing", (42, 90), 0)
"""The home page's top Document, as `_harness` lists it."""
TASK_WINDOW = WindowInfo(hwnd=0x10, title="Bing - Google Chrome", pid=777)
DROPDOWN_ROW = (45, "ListItem", "hello there world", (42, 95), 7)
"""A suggestion under the composer's own container (the Composer group)."""

OnPress = Callable[[Harness, list[WindowInfo]], None]


@dataclass
class StaleLive(FocusLive):
    """A live reader that raises on a handle the page no longer holds: a re-rendered input
    keeps its text but not its RuntimeId, so the stale before-of-record handle is unreadable."""

    def value(self, handle: Any) -> str | None:
        if handle not in {r[3] for r in self.world.rows}:
            raise SteerError(ErrorCode.ELEMENT_NOT_FOUND, "stale handle")
        return self.world.composer_value


def _dead(
    bar: Bar | None = None, *, on_press: OnPress | None = None, live: Any | None = None
) -> tuple[Harness, Bar, list[WindowInfo]]:
    """An Enter the page swallows: the bar, the page, the composer and the windows stay as
    they were unless `on_press` changes one of them."""
    bar = bar or Bar(HOME, HOME)
    h = _harness(bar)
    if live is not None:
        _rebuild(h, live=live)
        h.session._goal = GOAL
        h.session._identify = lambda hwnd: {"hwnd": hwnd, "image": "chrome.exe", "title": "Bing"}
        h.session._address_bar = bar
    windows = [TASK_WINDOW]
    h.session._windows = lambda: list(windows)

    def press(_executor: Any, action: Any) -> None:
        if action.verb == "press":
            bar.pressed = True
            if on_press is not None:
                on_press(h, windows)

    h.physical.on_execute = press
    return h, bar, windows


def _plan(step: Step | None = None) -> Plan:
    return Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g1", desc="results", condition=REPLACING),),
        steps=(step or _submit(),),
    )


def _swap_page(h: Harness, row: tuple[Any, ...]) -> None:
    h.world.rows[:] = [r for r in h.world.rows if r[0] != PAGE[0]] + [row]


def _uncertain(h: Harness, _windows: list[WindowInfo]) -> None:
    h.physical.outcomes[COMPOSER[0]] = ActionResult(
        verb="press", delivery=Delivery.DELIVERY_UNCERTAIN, code=ErrorCode.VERIFY_TIMEOUT
    )


# ---- all five facts hold ------------------------------------------------------------------------


def test_an_enter_that_changed_nothing_ends_no_effect() -> None:
    h, bar, _windows = _dead()
    result = h.session.run_task(_plan())
    assert result.status == "stopped", result
    assert result.evidence == {"detail": "no_effect"}
    assert result.code is ErrorCode.VERIFY_FAILED
    step = result.results[-1]
    assert step.ok is False
    assert step.code is ErrorCode.VERIFY_FAILED
    assert step.evidence["reason"] == "no_effect"
    assert step.evidence["detail"] == "no_effect"
    assert step.evidence["outcome"] == "no_effect"
    assert step.evidence["unchanged_s"] == 10.0, "unchanged at the navigation verify's deadline"
    assert step.label_kind is None
    assert step.action is not None
    assert step.action.delivery is Delivery.DELIVERED_VERIFIED, "the Enter went out"
    assert h.ledger.executions[h.ledger.key_for(_submit())]["outcome"] == "no_effect"
    assert len(bar.reads) == 2, "the before read and the one read after the deadline"


def test_a_no_effect_end_is_a_replan_trigger_with_the_closed_token() -> None:
    h, _bar, _windows = _dead()
    plan = _plan()
    result = h.session.run_task(plan)
    steps = {s.id: s for s in plan.steps}
    assert replan_trigger(result, steps) == "no_effect"
    progress = Progress()
    progress.add(plan, result)
    assert progress.failure() == FailedStep(step_id="s3", code="verify_failed", detail="no_effect")


@pytest.mark.parametrize(
    ("label", "on_press"),
    [
        (
            "the dropdown closed (a removal under the composer's container)",
            lambda h, _w: h.world.rows.remove(DROPDOWN_ROW),
        ),
        (
            "a suggestion appeared under the composer's container",
            lambda h, _w: h.world.rows.append((46, "ListItem", "hello there again", (42, 96), 7)),
        ),
    ],
)
def test_a_change_inside_the_composers_container_is_still_no_effect(
    label: str, on_press: OnPress
) -> None:
    h, _bar, _windows = _dead(on_press=on_press)
    h.world.rows.append(DROPDOWN_ROW)
    result = h.session.run_task(_plan())
    assert result.evidence == {"detail": "no_effect"}, label


def test_a_composer_re_rendered_with_the_same_text_is_still_no_effect() -> None:
    """NE-04: the fact is the text, not the element: the field is resolved in the deadline
    snapshot and read through that snapshot's handle; the before-of-record handle is stale."""

    def re_render(h: Harness, _windows: list[WindowInfo]) -> None:
        fresh = (COMPOSER[0], COMPOSER[1], COMPOSER[2], (42, 141), COMPOSER[4])
        h.world.rows[:] = [fresh if r[0] == COMPOSER[0] else r for r in h.world.rows]

    h, _bar, _windows = _dead(on_press=re_render)
    _rebuild(h, live=StaleLive(h.world, focused=COMPOSER[3]))
    h.session._goal = GOAL
    h.session._identify = lambda hwnd: {"hwnd": hwnd, "image": "chrome.exe", "title": "Bing"}
    h.session._address_bar = _bar
    h.session._windows = lambda: list(_windows)
    result = h.session.run_task(_plan())
    assert result.evidence == {"detail": "no_effect"}, result.results[-1].to_dict()


# ---- any fact missing: today's unverified_effect ------------------------------------------------


@pytest.mark.parametrize(
    ("label", "bar", "on_press"),
    [
        ("NE-09 the URL moved and no page loaded", Bar(HOME, RESULTS), None),
        (
            "the Document renamed",
            None,
            lambda h, _w: _swap_page(h, (40, "Document", "Bing - loading", (42, 90), 0)),
        ),
        (
            "the Document re-created under the same name",
            None,
            lambda h, _w: _swap_page(h, (40, "Document", "Bing", (42, 99), 0)),
        ),
        ("the composer cleared", None, lambda h, _w: setattr(h.world, "composer_value", "")),
        (
            "the composer holds other text",
            None,
            lambda h, _w: setattr(h.world, "composer_value", HELLO + " x"),
        ),
        (
            "NE-01 a new element outside the composer's container",
            None,
            lambda h, _w: h.world.rows.append((50, "Text", "Results for hello", (42, 150), 0)),
        ),
        (
            "a new top-level window",
            None,
            lambda _h, w: w.append(WindowInfo(hwnd=0x20, title="Results", pid=777)),
        ),
        ("the omnibox unreadable before", Bar(None, HOME), None),
        ("the omnibox unreadable after", Bar(HOME, None), None),
        ("NE-05 an uncertain delivery", None, _uncertain),
        ("no window list", None, lambda h, _w: setattr(h.session, "_windows", None)),
    ],
)
def test_any_fact_missing_keeps_unverified_effect(
    label: str, bar: Bar | None, on_press: OnPress | None
) -> None:
    h, _bar, _windows = _dead(bar, on_press=on_press)
    plan = _plan()
    result = h.session.run_task(plan)
    step = result.results[-1]
    assert step.ok is False, label
    assert step.evidence.get("reason") != "no_effect", label
    assert "outcome" not in step.evidence, label
    assert h.ledger.executions[h.ledger.key_for(_submit())]["outcome"] != "no_effect", label
    assert result.evidence == {"detail": "unfinished"}, label
    steps = {s.id: s for s in plan.steps}
    assert replan_trigger(result, steps) == "unverified_effect", label


def test_a_composer_twin_in_the_deadline_snapshot_is_a_missing_fact() -> None:
    """NE-04: exactly one field by role and exact name, else the text cannot be the fact."""
    twin = (COMPOSER[0] + 100, COMPOSER[1], COMPOSER[2], (42, 241), 3)
    h, _bar, _windows = _dead(on_press=lambda h, _w: h.world.rows.append(twin))
    result = h.session.run_task(_plan())
    assert result.evidence == {"detail": "unfinished"}


@pytest.mark.parametrize(
    ("label", "step"),
    [
        ("a send", _send()),
        (
            "a chat composer submit",
            _submit(
                expect=Appeared(role="Text", name_pattern=HELLO, within=Within("Conversation"))
            ),
        ),
    ],
)
def test_a_send_or_a_chat_submit_never_reaches_no_effect(label: str, step: Step) -> None:
    """Scope: `url_query_eligible` only (the bare replacing Document expect naming the goal's
    words). A send button that did nothing, or a chat composer's Enter, is never `no_effect`:
    the message may have gone."""
    h, _bar, _windows = _dead()
    h.semantic.outcomes[SEND[0]] = ActionResult(
        verb="click", delivery=Delivery.DELIVERED_UNVERIFIED, code=ErrorCode.VERIFY_TIMEOUT
    )
    result = h.session.run_step(step)
    assert result.ok is False, label
    assert result.evidence.get("reason") != "no_effect", label
    assert "outcome" not in result.evidence, label


# ---- the slow site: the facts are read after the deadline ---------------------------------------


class LateBar(Bar):
    """The address bar of a page that answers late: the results URL shows only once the
    results Document is there."""

    def __init__(self, loaded: dict[str, bool]) -> None:
        super().__init__(HOME, RESULTS)
        self.loaded = loaded

    def __call__(self, hwnd: int) -> str | None:
        self.reads.append(hwnd)
        return self.after if self.loaded["now"] else self.before


def test_a_page_that_navigates_during_the_verify_is_caught_by_the_verify() -> None:
    """The facts are read at the verify deadline, never earlier: a slow results page that
    lands while the verify polls is proven by the address bar (`url_query`), never judged
    `no_effect` from the first quiet look after the Enter."""
    loaded = {"now": False}
    bar = LateBar(loaded)
    h, _bar, _windows = _dead(bar)
    observe = h.world.observe
    pressed_at: list[int] = []

    def late(hwnd: int, **kwargs: Any) -> Any:
        if bar.pressed and not pressed_at:
            pressed_at.append(h.world.counter)
        if bar.pressed and not loaded["now"] and h.world.counter >= pressed_at[0] + 3:
            loaded["now"] = True
            _swap_page(h, (40, "Document", LOADED, (42, 90), 0))
        return observe(hwnd, **kwargs)

    h.world.observe = late  # type: ignore[method-assign]  # the scripted world is a test double
    result = h.session.run_task(_plan())
    assert result.status == "done", result.results[-1].to_dict()
    assert result.results[-1].evidence["reason"] == "url_query"
    assert loaded["now"], "the page landed during the verify, not before it"


# ---- the record, a resume and the re-read before the replan -------------------------------------


def test_a_resume_after_a_no_effect_close_is_never_done_already() -> None:
    h, _bar, _windows = _dead()
    first = h.session.run_step(_submit())
    assert first.evidence["reason"] == "no_effect"
    key = h.ledger.key_for(_submit())
    assert h.ledger.admit(key) == "fresh"
    again = h.session.run_step(_submit())
    assert again.gate != "done_already"
    assert len(h.confirmer.asked) == 2, "the Enter is asked about again, never assumed done"


def test_the_facts_are_read_again_before_the_replan() -> None:
    """NE-08: the same facts, read once more after the replan's observe; a page that moved in
    between is an uncertain effect and the task ends."""
    h, _bar, _windows = _dead()
    assert h.session.no_effect_holds() is False, "nothing judged yet"
    h.session.run_task(_plan())
    assert h.session.no_effect_holds() is True
    _swap_page(h, (40, "Document", "Bing - loading", (42, 90), 0))
    assert h.session.no_effect_holds() is False


def test_the_facts_of_an_earlier_step_never_outlive_the_next_step() -> None:
    h, _bar, _windows = _dead()
    h.session.run_task(_plan())
    assert h.session.no_effect_holds() is True
    h.session.run_step(_send())
    assert h.session.no_effect_holds() is False


# ---- the hunter's check of the build (NE-01b, NE-07, NE-05b) ------------------------------------


def _off_the_document(h: Harness) -> None:
    """The measured fixture shape: Chrome exposes no element for an unnamed `<form>`, so the
    composer hangs directly off the Document."""
    from tests.orchestrator.test_url_query_loop import _reparented  # noqa: PLC0415

    _reparented(h, COMPOSER[0], PAGE[0])


def test_a_composer_off_the_document_exempts_only_its_own_subtree() -> None:
    """NE-01b: with the composer's parent the Document, exempting the parent's subtree exempted
    the whole page and the fifth fact could never fail. A new element under the Document is an
    effect; a removal there (npm's dropdown closing) still reads `no_effect`."""
    added = (50, "Text", "Results for hello", (42, 150), PAGE[0])
    h, _bar, _windows = _dead(on_press=lambda h, _w: h.world.rows.append(added))
    _off_the_document(h)
    result = h.session.run_task(_plan())
    assert result.evidence == {"detail": "unfinished"}, result.results[-1].to_dict()
    assert result.results[-1].evidence.get("reason") != "no_effect"

    dropdown = (45, "ListItem", "hello there world", (42, 95), PAGE[0])
    h, _bar, _windows = _dead(on_press=lambda h, _w: h.world.rows.remove(dropdown))
    _off_the_document(h)
    h.world.rows.append(dropdown)
    result = h.session.run_task(_plan())
    assert result.evidence == {"detail": "no_effect"}, result.results[-1].to_dict()


def test_a_button_submit_that_changed_nothing_is_never_no_effect() -> None:
    """NE-07: only an Enter on the composer qualifies. A Button submit whose Invoke was
    delivered and changed nothing stays `unverified_effect`, so a decorative Search button
    never loops the ask."""
    from tests.orchestrator.test_url_query_loop import (  # noqa: PLC0415
        SEARCH_BUTTON,
        _button_submit,
    )

    h, _bar, _windows = _dead()
    h.world.rows.append(SEARCH_BUTTON)
    plan = _plan(_button_submit())
    result = h.session.run_task(plan)
    step = result.results[-1]
    assert step.ok is False
    assert step.action is not None
    assert step.action.delivery is not Delivery.NOT_DELIVERED, "the click went out"
    assert step.evidence.get("reason") != "no_effect"
    assert "outcome" not in step.evidence
    assert result.evidence == {"detail": "unfinished"}
    assert replan_trigger(result, {s.id: s for s in plan.steps}) == "unverified_effect"


def test_an_observe_that_fails_after_the_enter_is_never_no_effect() -> None:
    """NE-05b: the facts are read only after a deadline-timeout verdict; a verify that ended
    another way (the world model raising after the Enter) never reaches them."""
    h, _bar, _windows = _dead()
    world = h.world
    pressed = {"yes": False}

    def observe(hwnd: int, **kwargs: Any) -> Any:
        if pressed["yes"]:
            raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "the tree never settled")
        return world.observe(hwnd, **kwargs)

    h.session._observer = type("Observer", (), {"observe": staticmethod(observe)})()
    original = h.physical.on_execute

    def press(executor: Any, action: Any) -> None:
        if original is not None:
            original(executor, action)
        pressed["yes"] = action.verb == "press"

    h.physical.on_execute = press
    result = h.session.run_task(_plan())
    step = result.results[-1]
    assert step.ok is False
    assert step.evidence.get("reason") != "no_effect"
    assert "outcome" not in step.evidence
    assert result.evidence.get("detail") != "no_effect"
