"""An offscreen target is scrolled into view before the gate (offscreen-lines packet, principal
REVISE item 1 and hunter OS-01/02/05/06/07): a semantic Invoke on an offscreen element used to
fire with no scroll, after the person approved it from a screen where it was not visible, with
the before of record taken before any scroll. Now: ScrollIntoView, a quiet look, the same element
by RuntimeId, then the gate, the ask and the write-ahead on that screen; no ScrollItem or still
offscreen is refused with nothing sent. Probe `offscreen_reach` (Chrome 156): a below-fold button
and a scrolled list option expose ScrollItem and come on screen after ScrollIntoView, 5/5 each."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from steerai.verify.predicates import Appeared
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import FakeExecutor, not_delivered
from tests.orchestrator.test_bind_asked import nothing, scripted_waits
from tests.orchestrator.test_edge_cases import _rebuild
from tests.orchestrator.test_loop import Harness

WINDOW = Element(id=0, depth=0, role="Window", name="Feed", box=(0, 0, 1000, 700), runtime_id=(1,))
DOC = Element(
    id=1,
    depth=1,
    role="Document",
    name="Feed",
    box=(0, 0, 1000, 700),
    runtime_id=(2,),
    parent_id=0,
)
BELOW = (60, 3600, 160, 3630)
SCROLL_WAIT = 2
"""The boundary before the pre-gate scroll: after entering the window (1), before the gate."""
SHOWN = (60, 600, 160, 630)


@dataclass
class Page:
    """A button below the fold; ScrollIntoView brings it on screen; the click adds a line."""

    scrolled: bool = False
    clicked: bool = False
    deleted: bool = False
    scroll_lands: bool = True
    deletes: bool = False
    churns: bool = False
    """After the scroll, every look adds a new element (a feed that keeps loading)."""
    remounts: bool = False
    """After the scroll, the button is a new node: same role and name, new RuntimeId."""
    """The button removes itself when clicked: an R3 `delete` with its own template."""
    observed: int = 0
    log: list[str] = field(default_factory=list)

    def button(self) -> Element:
        on = self.scrolled and self.scroll_lands
        runtime_id = (55,) if self.scrolled and self.remounts else (5,)
        return Element(
            id=5,
            depth=2,
            role="Button",
            name="Show more",
            box=SHOWN if on else BELOW,
            offscreen=not on,
            runtime_id=runtime_id,
            parent_id=1,
        )

    def observe(self, hwnd: int, **_kwargs: Any) -> Snapshot:
        self.observed += 1
        elements = [WINDOW, DOC] if self.deleted else [WINDOW, DOC, self.button()]
        if self.churns and self.scrolled:
            elements.append(
                Element(
                    id=100 + self.observed,
                    depth=2,
                    role="ListItem",
                    name="loaded",
                    box=(60, 10, 300, 30),
                    runtime_id=(100 + self.observed,),
                    parent_id=1,
                )
            )
        if self.clicked:
            elements.append(
                Element(
                    id=6,
                    depth=2,
                    role="Text",
                    name="More items",
                    box=(60, 640, 300, 660),
                    runtime_id=(6,),
                    parent_id=1,
                )
            )
        return Snapshot(
            id=f"feed-{self.observed}",
            hwnd=hwnd,
            process="chrome.exe",
            title="Feed",
            taken_at=float(self.observed),
            elements=elements,
            node_count=len(elements),
            stable=True,
            handles=[e.runtime_id for e in elements],
        )


def _harness(page: Page, *, scroll_result: ActionResult | None = None) -> Harness:
    h = Harness()
    _rebuild(h, observer=page)

    def act(_executor: FakeExecutor, action: Any) -> None:
        page.log.append(action.verb)
        if action.verb == "scroll_into_view":
            page.scrolled = True
        if action.verb == "click":
            page.clicked = True
            page.deleted = page.deletes

    h.semantic.on_execute = act
    if scroll_result is not None:
        original = h.semantic.execute

        def execute(action: Any, snapshot: Snapshot) -> ActionResult:
            if action.verb == "scroll_into_view":
                page.log.append("scroll_into_view")
                if scroll_result.skipped:
                    page.scrolled = True  # the live read already saw it on screen
                return scroll_result
            return original(action, snapshot)

        h.semantic.execute = execute  # type: ignore[method-assign]  # the refusing scroll path
    return h


def _click(risk: str = "R0") -> Step:
    return Step(
        id="c1",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="Show more"),
        risk=risk,  # type: ignore[arg-type]  # a Risk literal from the parameter
        expect=Appeared(role="Text", name_pattern="More items"),
    )


def test_an_offscreen_target_is_scrolled_into_view_before_the_click() -> None:
    page = Page()
    h = _harness(page)
    result = h.session.run_step(_click())
    assert result.ok is True, result.to_dict()
    assert page.log == ["scroll_into_view", "click"]
    assert result.evidence["scrolled"] == "delivered_verified"


def _delete() -> Step:
    return Step(
        id="d1",
        subgoal="g1",
        verb="delete",
        target=Target(role="Button", name="Show more"),
        risk="R3",
    )


def test_the_ask_comes_after_the_scroll_so_the_person_sees_the_target() -> None:
    page = Page(deletes=True)
    h = _harness(page)
    seen: list[bool] = []
    h.confirmer.on_ask = lambda _ask: seen.append(page.scrolled)
    result = h.session.run_step(_delete())
    assert result.ok is True, result.to_dict()
    assert seen == [True], "asked only once the target was on screen"


def test_a_target_without_scroll_item_is_refused_with_nothing_sent() -> None:
    page = Page()
    refused = not_delivered(ErrorCode.ELEMENT_NOT_ACTIONABLE, "no_ScrollItemPattern")
    h = _harness(page, scroll_result=refused)
    result = h.session.run_step(_delete())
    assert result.ok is False
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "offscreen_unreachable"
    assert "click" not in page.log
    assert h.confirmer.asked == [], "never asked about an element nobody can see"


def test_a_scroll_that_does_not_land_is_refused_with_nothing_sent() -> None:
    page = Page(scroll_lands=False)
    h = _harness(page)
    result = h.session.run_step(_click())
    assert result.ok is False
    assert result.evidence["detail"] == "offscreen_unreachable"
    assert page.log == ["scroll_into_view"]


def test_an_on_screen_target_is_never_scrolled() -> None:
    page = Page(scrolled=True)
    h = _harness(page)
    result = h.session.run_step(_click())
    assert result.ok is True, result.to_dict()
    assert page.log == ["click"]
    assert "scrolled" not in result.evidence


def test_the_before_of_record_is_the_screen_after_the_scroll() -> None:
    """OS-01/02: content the scroll loads is never the step's effect."""
    page = Page(deletes=True)
    h = _harness(page)
    befores: list[bool] = []
    original = h.ledger.write_ahead

    def recording(step: Step, before: Snapshot, *args: Any, **kwargs: Any) -> Any:
        befores.append(not before.element(5).offscreen)
        return original(step, before, *args, **kwargs)

    h.ledger.write_ahead = recording  # type: ignore[method-assign]  # observe the record
    assert h.session.run_step(_delete()).ok is True
    assert befores == [True]


def test_a_delivered_scroll_that_was_already_on_screen_counts_as_seen() -> None:
    page = Page()
    h = _harness(page, scroll_result=ActionResult.already_satisfied("scroll_into_view"))
    result = h.session.run_step(_click())
    assert result.ok is True, result.to_dict()
    assert result.evidence["scrolled"] == Delivery.NOT_DELIVERED.value


def test_a_page_that_never_goes_quiet_after_the_scroll_is_refused() -> None:
    """#176 review 1: a capped quiet look is a before taken mid-load: unclear means fail."""
    page = Page(churns=True)
    h = _harness(page)
    result = h.session.run_step(_click())
    assert result.ok is False
    assert result.code is ErrorCode.UNSTABLE_UI
    assert result.evidence["detail"] == "scroll_not_quiet"
    assert page.log == ["scroll_into_view"]


def test_a_target_remounted_by_the_scroll_is_a_changed_screen() -> None:
    """#176 review 2: the same role and name on a new node is not the element asked about."""
    page = Page(deletes=True, remounts=True)
    h = _harness(page)
    result = h.session.run_step(_delete())
    assert result.ok is False
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "screen_changed_during_scroll"
    assert h.confirmer.asked == []
    assert page.log == ["scroll_into_view"]


def test_a_wait_before_the_scroll_scrolls_the_freshly_found_target() -> None:
    """#176 review 2: the boundary wait re-observes; the scroll then acts on that look."""
    page = Page()
    h = _harness(page)
    scripted_waits(h, {SCROLL_WAIT: nothing})
    looks_before = page.observed
    result = h.session.run_step(_click())
    assert result.ok is True, result.to_dict()
    assert result.evidence["restarted"] is True
    assert page.log == ["scroll_into_view", "click"]
    assert page.observed > looks_before
