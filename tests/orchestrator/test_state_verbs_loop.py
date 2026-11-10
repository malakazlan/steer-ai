"""`check` / `uncheck` through the loop (W2 v2 and its verdict, 2026-11-08): T1 judges the live
ToggleState, a consent box asks, a miss at R2/R3 stops the task with no second Toggle, a done key
re-reads the state (WV-12), and a field once seen secure stays secure (WV-09)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.verify.predicates import TitleMatches
from tests.orchestrator.fakes import BASE_ROWS, FakeWorld, Row
from tests.orchestrator.test_loop import Harness

CONSENT: Row = (11, "CheckBox", "I accept the terms", (42, 50), 3)
ALERTS: Row = (12, "CheckBox", "Email alerts", (42, 51), 3)
PASSWORD: Row = (13, "Edit", "Password", (42, 52), 3)


@dataclass
class Boxes:
    """The live ToggleState of each box, the live IsSelected of each tab or radio and the live
    ExpandCollapseState of each disclosure Button, by RuntimeId (the fakes' handles are
    RuntimeIds)."""

    world: FakeWorld
    state: dict[tuple[int, ...], int] = field(default_factory=dict)
    lands_after_reads: dict[tuple[int, ...], int] = field(default_factory=dict)
    """A server-confirmed box: its wanted state lands after this many live reads (WV-03)."""
    wanted: dict[tuple[int, ...], int] = field(default_factory=dict)
    reads: int = 0
    selected: dict[tuple[int, ...], bool] = field(default_factory=dict)
    selected_reads: int = 0
    expanded: dict[tuple[int, ...], int] = field(default_factory=dict)
    """Each disclosure Button's live ExpandCollapseState (W5); a Button not here has no pattern."""
    expand_reads: int = 0
    containers: dict[tuple[int, ...], bool] = field(default_factory=dict)
    """Each List's CanSelectMultiple (W3b); a List not here exposes no SelectionPattern."""
    without_selection_item: set[tuple[int, ...]] = field(default_factory=set)
    """Items that expose no SelectionItemPattern; every other item with a state does."""
    admission_failure: SteerError | None = None
    """A COM failure every admission read raises (W3b)."""
    admission_reads: int = 0

    def value(self, _handle: Any) -> str | None:
        return self.world.composer_value

    def toggle_state(self, handle: Any) -> int | None:
        self.reads += 1
        late = self.lands_after_reads.get(handle)
        if late is not None and self.reads >= late:
            self.state[handle] = self.wanted[handle]
        return self.state.get(handle)

    def is_selected(self, handle: Any) -> bool | None:
        self.selected_reads += 1
        return self.selected.get(handle)

    def expand_state(self, handle: Any) -> int | None:
        self.expand_reads += 1
        return self.expanded.get(handle)

    def focused_runtime_id(self) -> tuple[int, ...]:
        return ()

    def has_selection_item(self, handle: Any) -> bool:
        self._admission_read()
        return handle in self.selected and handle not in self.without_selection_item

    def selection_container(self, handle: Any) -> bool | None:
        self._admission_read()
        return self.containers.get(handle)

    def _admission_read(self) -> None:
        self.admission_reads += 1
        if self.admission_failure is not None:
            raise self.admission_failure


def _harness(**kw: Any) -> tuple[Harness, Boxes]:
    h = Harness(**kw)
    h.world.rows = [*BASE_ROWS, CONSENT, ALERTS]
    boxes = Boxes(h.world, state={CONSENT[3]: 0, ALERTS[3]: 0})
    h.session._live = boxes
    return h, boxes


def _toggled(verb: str, toggles: int = 1, detail: str | None = None) -> ActionResult:
    steps = [
        ActionStep(label="semantic.TogglePattern.Toggle", mechanism="semantic", outcome="succeeded")
    ] * toggles
    if detail is None:
        return ActionResult(verb=verb, delivery=Delivery.DELIVERED_VERIFIED, steps=steps)
    return ActionResult(
        verb=verb,
        delivery=Delivery.DELIVERED_UNVERIFIED,
        steps=steps,
        code=ErrorCode.VERIFY_TIMEOUT,
        evidence={"toggles": toggles, "detail": detail},
    )


def _flips(boxes: Boxes) -> Any:
    def on_execute(_ex: Any, action: Any) -> None:
        rid = next(r[3] for r in boxes.world.rows if r[0] == action.element_id)
        boxes.state[rid] = 1 if action.verb == "check" else 0

    return on_execute


def _check(name: str = "Email alerts", verb: str = "check", step_id: str = "c1") -> Step:
    return Step(id=step_id, subgoal="g1", verb=verb, target=Target(role="CheckBox", name=name))


def test_an_r1_check_is_verified_by_the_live_toggle_state_unasked() -> None:
    h, boxes = _harness()
    h.semantic.on_execute = _flips(boxes)
    result = h.session.run_step(_check())
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("check", ALERTS[0], None)]
    assert h.confirmer.asked == []
    assert result.label_kind == "t1"


def test_a_consent_box_asks_then_checks() -> None:
    h, boxes = _harness()
    h.semantic.on_execute = _flips(boxes)
    result = h.session.run_step(_check("I accept the terms"))
    assert result.ok is True, result.to_dict()
    assert len(h.confirmer.asked) == 1
    assert h.confirmer.asked[0].risk == "R2"
    assert h.confirmer.asked[0].expect == {"kind": "State", "target_id": CONSENT[0], "wanted": "on"}


def test_a_slow_confirm_that_lands_within_t1_passes() -> None:
    h, boxes = _harness()
    h.semantic.outcomes[ALERTS[0]] = _toggled("check", detail="unchanged")
    boxes.wanted[ALERTS[3]] = 1
    boxes.lands_after_reads[ALERTS[3]] = 3
    result = h.session.run_step(_check())
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("check", ALERTS[0], None)]


def test_a_miss_at_r2_stops_the_task_with_no_second_toggle() -> None:
    h, _boxes = _harness()
    h.semantic.outcomes[CONSENT[0]] = _toggled("check", detail="unchanged")
    plan = Plan(
        goal="accept the terms and turn on email alerts",
        subgoals=(Subgoal(id="g1", desc="accepted", condition=TitleMatches(pattern="Done")),),
        steps=(_check("I accept the terms"), _check(step_id="c2")),
    )
    task = h.session.run_task(plan)
    assert task.status == "stopped", task
    assert task.evidence["detail"] == "state_unverified"
    assert h.semantic.calls == [("check", CONSENT[0], None)]


def test_a_miss_at_r1_does_not_stop_the_task() -> None:
    h, boxes = _harness()
    h.semantic.outcomes[ALERTS[0]] = _toggled("check", detail="unchanged")
    h.semantic.on_execute = None
    plan = Plan(
        goal="turn on email alerts and accept the terms",
        subgoals=(Subgoal(id="g1", desc="on", condition=TitleMatches(pattern="Done")),),
        steps=(_check(), _check("I accept the terms", step_id="c2")),
    )
    h.session.run_task(plan)
    assert ("check", CONSENT[0], None) in h.semantic.calls
    del boxes


def test_a_done_key_whose_box_was_unticked_since_stops_without_a_toggle() -> None:
    h, boxes = _harness()
    h.semantic.on_execute = _flips(boxes)
    step = _check("I accept the terms")
    assert h.session.run_step(step).ok is True
    boxes.state[CONSENT[3]] = 0  # the page or the person unticked it after the verified check
    again = h.session.run_step(replace(step, id="c9"))
    assert again.ok is False
    assert again.action is None
    assert again.evidence["detail"] == "state_changed_since_done"
    assert h.semantic.calls == [("check", CONSENT[0], None)]


def test_a_done_key_whose_box_still_holds_is_done_already() -> None:
    h, boxes = _harness()
    h.semantic.on_execute = _flips(boxes)
    step = _check("I accept the terms")
    assert h.session.run_step(step).ok is True
    again = h.session.run_step(replace(step, id="c9"))
    assert again.ok is True
    assert again.gate == "done_already"
    assert h.semantic.calls == [("check", CONSENT[0], None)]


def test_a_field_seen_secure_stays_secure_for_the_task() -> None:
    h, _boxes = _harness()
    h.world.rows = [*h.world.rows, PASSWORD]
    inner = h.world

    class _Shows:
        """The password field reads secure once, then "Show password" made it plain."""

        def __init__(self) -> None:
            self.calls = 0

        def observe(self, hwnd: int, **kwargs: Any) -> Any:
            self.calls += 1
            snapshot = inner.observe(hwnd, **kwargs)
            elements = [
                replace(e, secure=self.calls == 1) if e.runtime_id == PASSWORD[3] else e
                for e in snapshot.elements
            ]
            return replace(snapshot, elements=elements)

    h.session._observer = _Shows()
    first = h.session._observe()
    later = h.session._observe()
    assert first.element(PASSWORD[0]).secure is True
    assert later.element(PASSWORD[0]).secure is True
    assert later.element(ALERTS[0]).secure is False
