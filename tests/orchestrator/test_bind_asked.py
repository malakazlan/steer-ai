"""The approved or observed element is the one acted on (#89, IJ-21; principal REVISE on PR #91):
after a yes, after a wait at each boundary that re-observes, and in the act loop's re-resolves,
an exact or aliased target must still be the same UIA element (RuntimeId and role) as the one
asked about. A label moved onto another element, a twin appearing, or an element that cannot be
proven the same is a changed screen with nothing dispatched."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.loop import _same_element
from steerai.world.elements import Element
from tests.orchestrator.fakes import COMPOSER, SEND, FakeExecutor, Row, not_delivered
from tests.orchestrator.test_edge_cases import FocusLive, _rebuild
from tests.orchestrator.test_loop import HELLO, Harness, _send, _type

SEARCH: Row = (11, "Edit", "Search", (42, 50), 7)
"""A second text field: the page can move the composer's label onto it."""
SEND_TWIN: Row = (12, "Button", "Send", (42, 60), 7)

# the boundaries a step passes, in order: entering the window, after the gate, the act loop's
# start, and the focus fallback's
AFTER_GATE = 2
ACT_LOOP = 3
FOCUS_FALLBACK = 4


def swap(h: Harness, a: str, b: str) -> Callable[[], None]:
    """The page's script: two elements exchange their names."""

    def script() -> None:
        names = {a: b, b: a}
        h.world.rows = [(r[0], r[1], names.get(r[2], r[2]), r[3], r[4]) for r in h.world.rows]

    return script


def nothing() -> None:
    """A wait during which the page does not change."""


def scripted_waits(h: Harness, waits: dict[int, Callable[[], None]]) -> None:
    """The session's boundary as a script: on the n-th boundary (1-based) the page runs its
    script and the boundary reports that it waited; every other boundary waits for nothing."""
    calls = [0]

    def boundary() -> bool:
        calls[0] += 1
        script = waits.get(calls[0])
        if script is None:
            return False
        script()
        return True

    # the session's own method replaced by the script: mypy forbids assigning to a method
    h.session._boundary = lambda evidence: boundary()  # type: ignore[method-assign]  # noqa: ARG005


def _detail(result: Any) -> tuple[ErrorCode | None, Any]:
    return result.code, result.evidence.get("detail")


def _focus_step() -> Step:
    return Step(
        id="f1", subgoal="g1", verb="focus", target=Target(role="Edit", name="Message"), risk="R0"
    )


# ---- the wait after the gate, on an approved step (no ask) --------------------------------------


def test_labels_swapped_during_the_wait_after_an_approve_never_move_the_type() -> None:
    h = Harness()
    h.world.rows.append(SEARCH)
    scripted_waits(h, {AFTER_GATE: swap(h, "Message", "Search")})
    result = h.session.run_step(_type())
    assert result.gate == "approve"
    assert _detail(result) == (ErrorCode.FOCUS_LOST, "screen_changed_during_wait")
    assert h.physical.calls == []


def test_a_wait_after_an_approve_with_no_change_types_into_the_same_field() -> None:
    h = Harness()
    h.world.rows.append(SEARCH)
    scripted_waits(h, {AFTER_GATE: nothing})
    result = h.session.run_step(_type())
    assert result.ok is True, result.to_dict()
    assert result.evidence["restarted"] is True
    assert h.physical.calls == [("type", COMPOSER[0], HELLO)]


# ---- the wait at the act loop's boundary ---------------------------------------------------------


def test_labels_swapped_during_the_act_loop_wait_never_move_the_type() -> None:
    h = Harness()
    h.world.rows.append(SEARCH)
    scripted_waits(h, {ACT_LOOP: swap(h, "Message", "Search")})
    result = h.session.run_step(_type())
    assert _detail(result) == (ErrorCode.FOCUS_LOST, "screen_changed_during_wait")
    assert h.physical.calls == []


def test_an_act_loop_wait_with_no_change_types_into_the_same_field() -> None:
    h = Harness()
    h.world.rows.append(SEARCH)
    scripted_waits(h, {ACT_LOOP: nothing})
    result = h.session.run_step(_type())
    assert result.ok is True, result.to_dict()
    assert result.evidence["restarted"] is True
    assert h.physical.calls == [("type", COMPOSER[0], HELLO)]


# ---- the wait before the SetFocus fallback -------------------------------------------------------


def _hit_test_miss(h: Harness) -> FocusLive:
    live = FocusLive(h.world)
    _rebuild(h, live=live)
    h.physical.outcomes[COMPOSER[0]] = not_delivered(
        ErrorCode.ELEMENT_NOT_ACTIONABLE, "hit_test_miss"
    )

    def takes_focus(_executor: FakeExecutor, action: Any) -> None:
        if action.verb == "focus":
            live.focused = next(r[3] for r in h.world.rows if r[0] == action.element_id)

    h.semantic.on_execute = takes_focus
    return live


def test_labels_swapped_during_the_fallback_wait_never_move_the_focus() -> None:
    h = Harness()
    h.world.rows.append(SEARCH)
    _hit_test_miss(h)
    scripted_waits(h, {FOCUS_FALLBACK: swap(h, "Message", "Search")})
    result = h.session.run_step(_focus_step())
    assert _detail(result) == (ErrorCode.FOCUS_LOST, "screen_changed_during_wait")
    assert h.semantic.calls == []


def test_a_fallback_wait_with_no_change_focuses_the_same_field() -> None:
    h = Harness()
    h.world.rows.append(SEARCH)
    _hit_test_miss(h)
    scripted_waits(h, {FOCUS_FALLBACK: nothing})
    result = h.session.run_step(_focus_step())
    assert result.ok is True, result.to_dict()
    assert result.evidence["restarted"] is True
    assert h.semantic.calls == [("focus", COMPOSER[0], None)]


# ---- what cannot be proven the same --------------------------------------------------------------


def test_a_twin_appearing_during_the_ask_is_a_changed_screen() -> None:
    h = Harness()
    h.session.run_step(_type())

    def twin(_ask: Any) -> None:
        h.world.rows = [*h.world.rows, SEND_TWIN]

    h.confirmer.on_ask = twin
    result = h.session.run_step(_send())
    assert result.gate == "confirm"
    assert _detail(result) == (ErrorCode.FOCUS_LOST, "screen_changed_during_ask")
    assert h.semantic.calls == []
    assert h.world.sent == []


def test_an_asked_element_without_a_runtime_id_is_a_changed_screen() -> None:
    h = Harness()
    h.world.rows = [(*r[:3], (), r[4]) if r[0] == SEND[0] else r for r in h.world.rows]
    h.session.run_step(_type())
    result = h.session.run_step(_send())
    assert result.gate == "confirm"
    assert _detail(result) == (ErrorCode.FOCUS_LOST, "screen_changed_during_ask")
    assert h.semantic.calls == []
    assert h.world.sent == []


SEND_ELEMENT = Element(id=SEND[0], depth=2, role="Button", name="Send", box=(0, 0, 9, 9))


def test_the_same_runtime_id_under_another_role_is_not_the_same_element() -> None:
    asked = replace(SEND_ELEMENT, runtime_id=SEND[3])
    assert _same_element(asked, asked) is True
    assert _same_element(asked, replace(asked, role="Link")) is False


def test_another_runtime_id_is_not_the_same_element() -> None:
    asked = replace(SEND_ELEMENT, runtime_id=SEND[3])
    assert _same_element(asked, replace(asked, runtime_id=(42, 22))) is False


def test_two_elements_without_runtime_ids_are_never_the_same() -> None:
    assert _same_element(SEND_ELEMENT, SEND_ELEMENT) is False
