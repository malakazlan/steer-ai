"""Twin fields through the loop (#150, D-150 v2; principal APPROVE-WITH-CHANGES, hunter
TW-01..44). focus/type at R0/R1 take the one twin on screen; an Enter submit takes the focused
twin on its first resolve; a re-resolve after an ask finds the asked twin by RuntimeId; a
verified type pins its twin for conditions; everything else stays `ambiguous_target`."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.ledger import WriteAheadRecord
from steerai.verify.predicates import Appeared, FieldValue
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.twins import pin_key
from tests.orchestrator.test_edge_cases import _rebuild
from tests.orchestrator.test_loop import Harness
from tests.world.test_twins import BOTTOM, TOP, _page

QUERY = "pathlib"
FIELD = Target(role="Edit", name="Quick search")
KEY = pin_key(0x10, "Edit", "Quick search")
TOKENS = {"onscreen", "asked", "focused", "none_focused", "moved"}


@dataclass
class Page:
    """The twin page; `extra` elements appear after an action (a result line)."""

    top: Element = TOP
    bottom: Element | None = BOTTOM
    extra: list[Element] = field(default_factory=list)
    observed: int = 0

    def observe(self, hwnd: int, **_kwargs: Any) -> Snapshot:
        self.observed += 1
        edits = [e for e in (self.top, self.bottom) if e is not None]
        snap = _page(*edits, extra=tuple(self.extra))
        return replace(
            snap,
            id=f"twins-{self.observed}",
            hwnd=hwnd,
            handles=[e.runtime_id for e in snap.elements],
            elements=list(snap.elements),
        )


@dataclass
class Live:
    values: dict[tuple[int, ...], str] = field(default_factory=dict)
    focused: tuple[int, ...] = ()

    def value(self, handle: Any) -> str | None:
        return self.values.get(tuple(handle), "")

    def toggle_state(self, _handle: Any) -> int | None:
        return None

    def is_selected(self, _handle: Any) -> bool | None:
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self.focused


RIDS = {TOP.id: TOP.runtime_id, BOTTOM.id: BOTTOM.runtime_id}


def _harness(page: Page | None = None, live: Live | None = None) -> tuple[Harness, Page, Live]:
    h = Harness()
    page = page or Page()
    live = live or Live()
    _rebuild(h, live=live, observer=page)
    h.session._goal = f"search the docs for {QUERY}"

    def act(_executor: Any, action: Any) -> None:
        rid = RIDS.get(action.element_id)
        if action.verb == "type" and rid is not None:
            live.values[rid] = action.text
        if action.verb in ("focus", "click") and rid is not None:
            live.focused = rid
        if action.verb == "press" and rid is not None:
            live.values[rid] = ""
            page.extra.append(
                Element(
                    id=90,
                    depth=2,
                    role="Text",
                    name=QUERY,
                    box=(0, 300, 99, 320),
                    runtime_id=(90,),
                    parent_id=1,
                )
            )

    h.physical.on_execute = act
    h.semantic.on_execute = act
    return h, page, live


def _type(**changes: Any) -> Step:
    step = Step(id="s2", subgoal="g1", verb="type", target=FIELD, risk="R1", text=QUERY)
    return replace(step, **changes)


def _submit() -> Step:
    return Step(
        id="s3",
        subgoal="g2",
        verb="submit",
        target=FIELD,
        risk="R2",
        params={
            "text": QUERY,
            "composer_role": "Edit",
            "composer_name": "Quick search",
            "scope": "Docs",
        },
    )


def _twin(result: Any) -> str:
    token = result.evidence.get("twin", "")
    assert token in TOKENS | {""}, "only the five tokens reach the trace (TW-36)"
    return str(token)


# ---- focus / type at R0/R1 --------------------------------------------------------------------


def test_type_at_r1_writes_the_one_twin_on_screen_and_pins_it() -> None:
    h, _, live = _harness()
    result = h.session.run_step(_type())
    assert result.ok is True, result.to_dict()
    assert _twin(result) == "onscreen"
    assert live.values == {TOP.runtime_id: QUERY}
    assert h.session._pins == {KEY: TOP.runtime_id}


def test_focus_at_r0_takes_the_one_twin_on_screen() -> None:
    h, _, live = _harness()
    result = h.session.run_step(Step(id="s1", subgoal="g1", verb="focus", target=FIELD, risk="R0"))
    assert result.ok is True, result.to_dict()
    assert live.focused == TOP.runtime_id


@pytest.mark.parametrize(
    ("label", "page", "step"),
    [
        (
            "both on screen",
            Page(bottom=replace(BOTTOM, offscreen=False, box=(989, 400, 1169, 426))),
            _type(),
        ),
        ("both offscreen", Page(top=replace(TOP, offscreen=True)), _type()),
        ("zero area on screen", Page(top=replace(TOP, box=(989, 183, 989, 209))), _type()),
        (
            "a click",
            Page(),
            Step(
                id="c",
                subgoal="g1",
                verb="click",
                target=FIELD,
                risk="R0",
                expect=Appeared(role="Text", name_pattern=QUERY),
            ),
        ),
        ("a type at R2", Page(), _type(risk="R2")),
        ("another aid", Page(bottom=replace(BOTTOM, automation_id="q")), _type()),
        ("a secure twin", Page(bottom=replace(BOTTOM, secure=True)), _type()),
    ],
)
def test_every_other_twin_shape_stays_ambiguous(label: str, page: Page, step: Step) -> None:
    h, _, live = _harness(page)
    result = h.session.run_step(step)
    assert result.ok is False, label
    assert result.evidence.get("detail") == "ambiguous_target", label
    assert live.values == {}, label
    assert h.session._pins == {}, label


def test_an_onscreen_pick_the_act_finds_offscreen_never_scrolls() -> None:
    """Principal blocking 1 / TW-10: the pick's premise broke, so stop, never scroll."""
    h, _, _live = _harness()
    h.physical.outcomes[TOP.id] = ActionResult(
        verb="type",
        delivery=Delivery.NOT_DELIVERED,
        steps=[],
        code=ErrorCode.ELEMENT_NOT_FOUND,
        evidence={"reason": "not_actionable", "detail": "offscreen"},
    )
    result = h.session.run_step(_type())
    assert result.ok is False
    assert result.evidence.get("detail") == "screen_changed_during_act"
    assert all(call[0] != "scroll_into_view" for call in h.semantic.calls)
    assert [call[0] for call in h.physical.calls] == ["type"], "one refused attempt, no retry"


# ---- the Enter submit ---------------------------------------------------------------------------


def test_the_submit_takes_the_focused_twin_and_re_finds_it_after_the_ask() -> None:
    page = Page()
    live = Live(values={TOP.runtime_id: QUERY}, focused=TOP.runtime_id)
    h, _, _live = _harness(page, live)
    h.session._pins[KEY] = TOP.runtime_id

    def console_takes_focus(_ask: Any) -> None:
        live.focused = ()  # the person answers in the console (TW-09)

    h.confirmer.on_ask = console_takes_focus
    result = h.session.run_step(_submit())
    assert result.ok is True, result.to_dict()
    assert _twin(result) in ("focused", "asked")
    assert ("press", TOP.id, None) in [(c[0], c[1], None) for c in h.physical.calls]


def test_a_submit_with_focus_on_neither_twin_is_ambiguous() -> None:
    h, _, _live = _harness(live=Live(values={TOP.runtime_id: QUERY}))
    result = h.session.run_step(_submit())
    assert result.ok is False
    assert result.evidence.get("detail") == "ambiguous_target"
    assert _twin(result) == "none_focused"
    assert h.physical.calls == []


def test_a_submit_focused_on_the_twin_the_type_did_not_write_fails_closed() -> None:
    """TW-12: the pin may only refuse, never pick."""
    live = Live(values={TOP.runtime_id: QUERY, BOTTOM.runtime_id: QUERY}, focused=BOTTOM.runtime_id)
    h, _, _live = _harness(live=live)
    h.session._pins[KEY] = TOP.runtime_id
    result = h.session.run_step(_submit())
    assert result.ok is False
    assert _twin(result) == "moved"
    assert h.physical.calls == []


def test_a_send_through_twin_composers_is_blocked() -> None:
    """Principal blocking 3 / TW-06: only the Enter submit pins its composer to its target."""
    page = Page(
        extra=[
            Element(
                id=80,
                depth=2,
                role="Button",
                name="Search",
                box=(0, 200, 99, 220),
                runtime_id=(80,),
                parent_id=1,
            )
        ]
    )
    live = Live(values={TOP.runtime_id: QUERY}, focused=TOP.runtime_id)
    h, _, _live = _harness(page, live)
    step = replace(_submit(), target=Target(role="Button", name="Search"))
    result = h.session.run_step(step)
    assert result.ok is False
    assert all(call[0] != "click" for call in h.semantic.calls + h.physical.calls)


def test_a_resume_on_twins_returns_the_phase_never_done_already() -> None:
    """TW-08: the persisted snapshot has no focus; the composer is ambiguous there."""
    live = Live(values={TOP.runtime_id: ""}, focused=TOP.runtime_id)
    h, page, _live = _harness(live=live)
    page.extra.append(
        Element(
            id=90,
            depth=2,
            role="Text",
            name=QUERY,
            box=(0, 300, 99, 320),
            runtime_id=(90,),
            parent_id=1,
        )
    )
    step = _submit()
    key = h.ledger.key_for(step)
    before = Page().observe(0x10)
    h.ledger.records[step.id] = WriteAheadRecord(
        task_id="task-1",
        step_id=step.id,
        action_key=key,
        decision_id="d-x",
        phase="acting",
        before=replace(before, handles=[]),
        written_at=1.0,
    )
    h.ledger.executions[key] = {"step_id": step.id, "phase": "acting", "outcome": None}
    assert h.session._admission(step, key, "R2", page.observe(0x10)) == "acting"


# ---- conditions read the pinned twin -----------------------------------------------------------


def test_a_value_condition_reads_the_twin_the_type_wrote() -> None:
    h, page, live = _harness()
    assert h.session.run_step(_type()).ok is True
    condition = FieldValue(role="Edit", name_pattern="Quick search", text=QUERY)
    assert h.session._holds_now(condition, page.observe(0x10)) is True
    h.session._pins.clear()
    assert h.session._holds_now(condition, page.observe(0x10)) is False
    assert live.values == {TOP.runtime_id: QUERY}


# ---- principal review of #164 ------------------------------------------------------------------


def test_a_twin_that_appears_during_the_ask_of_a_unique_target_stops_the_step() -> None:
    """Blocking 1: (A) only re-finds a step whose first pick was a twin pick."""
    page = Page(bottom=None)
    live = Live(values={TOP.runtime_id: QUERY}, focused=TOP.runtime_id)
    h, _, _live = _harness(page, live)

    def copy_appears(_ask: Any) -> None:
        page.bottom = BOTTOM

    h.confirmer.on_ask = copy_appears
    result = h.session.run_step(_submit())
    assert result.ok is False
    assert result.evidence.get("detail") == "screen_changed_during_ask"
    assert h.physical.calls == []


def test_a_resume_never_uses_the_live_twin_identity() -> None:
    """Blocking 2: on the raised-tier path the admission runs after the resolve, with the
    step's twin RuntimeId set; the persisted snapshot must still give the phase."""
    live = Live(values={TOP.runtime_id: ""}, focused=TOP.runtime_id)
    h, page, _live = _harness(live=live)
    step = _submit()
    key = h.ledger.key_for(step)
    record = WriteAheadRecord(
        task_id="task-1",
        step_id=step.id,
        action_key=key,
        decision_id="d-x",
        phase="acting",
        before=replace(Page().observe(0x10), handles=[]),
        written_at=1.0,
    )
    page.extra.append(
        Element(
            id=90,
            depth=2,
            role="Text",
            name=QUERY,
            box=(0, 300, 99, 320),
            runtime_id=(90,),
            parent_id=1,
        )
    )
    h.ledger.records[step.id] = record
    h.ledger.executions[key] = {"step_id": step.id, "phase": "acting", "outcome": None}
    h.session._twin_rid = TOP.runtime_id  # the step already resolved its twin
    assert h.session._resume(step, record, page.observe(0x10)) == "acting"
