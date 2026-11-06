"""Two rules from the first real-site run (Google, 2026-11-04; design 5.9 [v1.7]):

- `submit` on a text target (Edit, ComboBox, Document) is Enter on the focused composer, a
  physical press at R2 with the step's own postcondition; a button target stays a click.
- a `focus` click refused by the hit test for a transparent overlay (`hit_test_miss`) falls
  back to UIA `SetFocus`, verified by the live focus read like any focus step.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from tests.orchestrator.fakes import COMPOSER, FakeExecutor, not_delivered
from tests.orchestrator.test_edge_cases import FocusLive, _rebuild
from tests.orchestrator.test_loop import HELLO, Harness

PARAMS = {
    "text": HELLO,
    "composer_role": "Edit",
    "composer_name": "Message",
    "scope": "Conversation",
}


def _submit(role: str, name: str) -> Step:
    return Step(
        id="s3",
        subgoal="g1",
        verb="submit",
        target=Target(role=role, name=name),
        risk="R2",
        params=PARAMS,
    )


def _sends_on_enter(h: Harness) -> list[Any]:
    """The composer behaves like a real one: Enter in it sends what was typed."""
    actions: list[Any] = []

    def on_execute(_executor: FakeExecutor, action: Any) -> None:
        actions.append(action)
        if action.verb == "press" and action.chord == "enter":
            text = h.world.composer_value
            h.world.sent.append(text)
            h.world.add_bubble(text)
            h.world.composer_value = ""

    h.physical.on_execute = on_execute
    return actions


def test_submit_on_a_text_target_presses_enter_in_it() -> None:
    h = Harness()
    h.world.composer_value = HELLO
    actions = _sends_on_enter(h)
    result = h.session.run_step(_submit("Edit", "Message"))
    assert result.ok is True, result.to_dict()
    assert result.gate == "confirm"
    assert [(a.verb, a.element_id, a.chord) for a in actions] == [("press", COMPOSER[0], "enter")]
    assert h.semantic.calls == []
    assert h.world.sent == [HELLO]
    assert result.evidence["submit"] == "enter"


def test_submit_on_a_button_is_still_a_click() -> None:
    h = Harness()
    h.world.composer_value = HELLO
    result = h.session.run_step(_submit("Button", "Send"))
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("click", 9, None)]
    assert h.physical.calls == []
    assert "submit" not in result.evidence


def test_submit_enter_is_refused_when_the_composer_is_not_focused() -> None:
    """The executor's refusal (FOCUS_LOST / target_not_focused) reaches the step unchanged."""
    h = Harness()
    h.world.composer_value = HELLO
    h.physical.outcomes[COMPOSER[0]] = not_delivered(ErrorCode.FOCUS_LOST, "target_not_focused")
    result = h.session.run_step(_submit("Edit", "Message"))
    assert result.ok is False
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.action is not None
    assert result.action.evidence["detail"] == "target_not_focused"
    assert h.world.sent == []


def _focus_step() -> Step:
    return Step(
        id="f1", subgoal="g1", verb="focus", target=Target(role="Edit", name="Message"), risk="R0"
    )


def test_a_focus_click_refused_by_the_hit_test_falls_back_to_set_focus() -> None:
    h = Harness()
    live = FocusLive(h.world)
    _rebuild(h, live=live)
    h.physical.outcomes[COMPOSER[0]] = not_delivered(
        ErrorCode.ELEMENT_NOT_ACTIONABLE, "hit_test_miss"
    )

    def takes_focus(_executor: FakeExecutor, action: Any) -> None:
        if action.verb == "focus":
            live.focused = COMPOSER[3]

    h.semantic.on_execute = takes_focus
    result = h.session.run_step(_focus_step())
    assert result.ok is True, result.to_dict()
    assert h.physical.calls == [("click", COMPOSER[0], None)]
    assert h.semantic.calls == [("focus", COMPOSER[0], None)]
    assert result.evidence["focus_fallback"] == "set_focus"
    assert result.label_kind == "t1"


def test_other_click_refusals_do_not_fall_back() -> None:
    h = Harness()
    _rebuild(h, live=FocusLive(h.world))
    h.physical.outcomes[COMPOSER[0]] = not_delivered(ErrorCode.ELEMENT_NOT_ACTIONABLE, "covered")
    result = h.session.run_step(_focus_step())
    assert result.ok is False
    assert h.semantic.calls == []
    assert "focus_fallback" not in result.evidence


def test_a_non_focus_click_does_not_fall_back_on_a_hit_test_miss() -> None:
    h = Harness()
    step = Step(
        id="c1",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="Chat options"),
        risk="R0",
        path="physical",
    )
    h.physical.outcomes[10] = not_delivered(ErrorCode.ELEMENT_NOT_ACTIONABLE, "hit_test_miss")
    result = h.session.run_step(step)
    assert result.ok is False
    assert h.semantic.calls == []


@pytest.mark.parametrize("role", ["ComboBox", "Document"])
def test_submit_on_the_other_text_roles_presses_enter_too(role: str) -> None:
    h = Harness()
    h.world.rows = [(r[0], role, *r[2:]) if r == COMPOSER else r for r in h.world.rows]
    h.world.composer_value = HELLO
    actions = _sends_on_enter(h)
    step = Step(
        id="s3",
        subgoal="g1",
        verb="submit",
        target=Target(role=role, name="Message"),
        risk="R2",
        params={**PARAMS, "composer_role": role},
    )
    result = h.session.run_step(step)
    assert result.ok is True, result.to_dict()
    assert [(a.verb, a.chord) for a in actions] == [("press", "enter")]
    assert h.semantic.calls == []


def test_a_set_focus_fallback_that_never_takes_fails_with_the_fallback_recorded() -> None:
    h = Harness()
    _rebuild(h, live=FocusLive(h.world))
    h.physical.outcomes[COMPOSER[0]] = not_delivered(
        ErrorCode.ELEMENT_NOT_ACTIONABLE, "hit_test_miss"
    )
    result = h.session.run_step(_focus_step())
    assert result.ok is False
    assert h.semantic.calls == [("focus", COMPOSER[0], None)]
    assert result.evidence["focus_fallback"] == "set_focus"


def test_submit_enter_is_refused_when_the_composer_shows_other_text() -> None:
    """The approved thing must be the executed thing (OR-31, SE-02): Enter only submits the text
    the person confirmed; the screen's text never enters the evidence."""
    h = Harness()
    h.world.composer_value = "something else entirely"
    result = h.session.run_step(_submit("Edit", "Message"))
    assert result.ok is False
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "composer_differs"
    assert h.physical.calls == []
    assert h.world.sent == []
    assert "something else" not in json.dumps(result.to_dict())


def test_the_set_focus_fallback_re_observes_after_a_wait() -> None:
    """A wait at the fallback's boundary ends after the observe: fresh snapshot first (TK-03)."""
    quiet = Harness()
    _rebuild(quiet, live=FocusLive(quiet.world, focused=COMPOSER[3]))
    quiet.physical.outcomes[COMPOSER[0]] = not_delivered(
        ErrorCode.ELEMENT_NOT_ACTIONABLE, "hit_test_miss"
    )
    assert quiet.session.run_step(_focus_step()).ok is True
    baseline = len(quiet.world.observed)

    waited = Harness()
    _rebuild(waited, live=FocusLive(waited.world, focused=COMPOSER[3]))
    waited.physical.outcomes[COMPOSER[0]] = not_delivered(
        ErrorCode.ELEMENT_NOT_ACTIONABLE, "hit_test_miss"
    )
    waited.session._boundary = lambda evidence: True  # type: ignore[method-assign]  # noqa: ARG005
    result = waited.session.run_step(_focus_step())
    assert result.ok is True, result.to_dict()
    assert result.evidence["restarted"] is True
    assert len(waited.world.observed) >= baseline + 1, "the fallback observed again after the wait"
    assert waited.semantic.calls == [("focus", COMPOSER[0], None)]


def test_submit_enter_without_live_reads_is_refused_unreadable() -> None:
    from steerai.orchestrator.gate import Gate  # noqa: PLC0415
    from steerai.orchestrator.loop import Session  # noqa: PLC0415

    h = Harness()
    h.world.composer_value = HELLO
    h.session = Session(
        task_id="task-1",
        hwnd=0x10,
        observer=h.world,
        semantic=h.semantic,
        physical=h.physical,
        gate=Gate(confirmer=h.confirmer, own_pid=4242, task_id="task-1"),
        ledger=h.ledger,
        live=None,
        trace=h.trace,
        monotonic=lambda: h.clock["t"],
        sleep=lambda s: h.clock.__setitem__("t", h.clock["t"] + s),
        verify_deadline_s=0.5,
    )
    result = h.session.run_step(_submit("Edit", "Message"))
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "composer_unreadable"
    assert h.physical.calls == []


def test_submit_enter_with_a_failing_value_read_is_refused_unreadable() -> None:
    from steerai.errors import SteerError  # noqa: PLC0415

    class BrokenLive(FocusLive):
        def value(self, _handle: Any) -> str | None:
            raise SteerError(ErrorCode.ELEMENT_NOT_FOUND, "handle gone")

    h = Harness()
    h.world.composer_value = HELLO
    _rebuild(h, live=BrokenLive(h.world, focused=COMPOSER[3]))
    result = h.session.run_step(_submit("Edit", "Message"))
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "composer_unreadable"
    assert h.physical.calls == []


def test_submit_enter_without_an_approved_text_is_refused() -> None:
    """Unreachable through the gate (no text means no strong template), kept as defence."""
    from steerai.orchestrator.loop import _StepFailureError  # noqa: PLC0415

    h = Harness()
    step = Step(
        id="s3",
        subgoal="g1",
        verb="submit",
        target=Target(role="Edit", name="Message"),
        risk="R2",
        params={"composer_role": "Edit"},
    )
    with pytest.raises(_StepFailureError) as caught:
        h.session._check_composer(step, h.world.observe(0x10), COMPOSER[0])
    assert caught.value.detail == "submit_text_missing"


def test_input_during_the_fallback_wait_is_not_the_steps_input() -> None:
    """D7: the counts baseline is re-taken after the fallback's re-observe, like the act loop."""
    from steerai.orchestrator.gate import Gate  # noqa: PLC0415
    from steerai.orchestrator.loop import Session  # noqa: PLC0415
    from tests.orchestrator.test_takeover_loop import FakeMonitor  # noqa: PLC0415

    h = Harness()
    monitor = FakeMonitor(clock=h.clock)
    h.session = Session(
        task_id="task-1",
        hwnd=0x10,
        observer=h.world,
        semantic=h.semantic,
        physical=h.physical,
        gate=Gate(confirmer=h.confirmer, own_pid=4242, task_id="task-1"),
        ledger=h.ledger,
        live=FocusLive(h.world, focused=COMPOSER[3]),
        trace=h.trace,
        monitor=monitor,
        monotonic=lambda: h.clock["t"],
        sleep=lambda s: h.clock.__setitem__("t", h.clock["t"] + s),
        verify_deadline_s=0.5,
    )
    h.physical.outcomes[COMPOSER[0]] = not_delivered(
        ErrorCode.ELEMENT_NOT_ACTIONABLE, "hit_test_miss"
    )

    def waited(evidence: dict[str, Any]) -> bool:
        monitor.user_events += 5  # the person typed while the loop waited
        return True

    h.session._boundary = waited  # type: ignore[method-assign]
    result = h.session.run_step(_focus_step())
    assert result.ok is True, result.to_dict()
    assert result.evidence["focus_fallback"] == "set_focus"
    assert "user_input_during_step" not in result.evidence


def test_a_navigation_effect_gets_the_longer_verify_deadline() -> None:
    """A page replacing itself loads slower than a message appears: a replacing `appeared` is
    verified under `NAVIGATION_VERIFY_S`, not the step deadline (Wikipedia run, 2026-11-04)."""
    from steerai.orchestrator.loop import NAVIGATION_VERIFY_S  # noqa: PLC0415
    from steerai.verify.predicates import Appeared  # noqa: PLC0415

    h = Harness()  # verify_deadline_s is 0.5 in the harness
    h.world.rows.append((40, "Document", "Home", (42, 90), 0))
    h.world.composer_value = HELLO
    _rebuild(h, live=FocusLive(h.world, focused=COMPOSER[3]))
    started = h.clock["t"]

    class SlowPage:
        def observe(self, hwnd: int, **kwargs: Any) -> Any:
            if (
                h.clock["t"] - started >= 2.0
                and (40, "Document", "Home", (42, 90), 0) in h.world.rows
            ):
                h.world.rows.remove((40, "Document", "Home", (42, 90), 0))
                h.world.rows.append((40, "Document", f"{HELLO} - Google Search", (42, 91), 0))
            return h.world.observe(hwnd, **kwargs)

    h.session._observer = SlowPage()
    step = Step(
        id="s3",
        subgoal="g1",
        verb="submit",
        target=Target(role="Edit", name="Message"),
        risk="R2",
        params=PARAMS,
        expect=Appeared(
            role="Document", name_pattern=HELLO, decoration="google_search", replaces=True
        ),
    )
    result = h.session.run_step(step)
    assert result.ok is True, result.to_dict()
    assert result.evidence["replaced"] == 1
    assert NAVIGATION_VERIFY_S > 2.0


def _renaming_page(h: Harness) -> None:
    """The page renames its own Document after the action (a single-page app, PR #29 review)."""
    h.world.rows.append((40, "Document", "Home", (42, 90), 0))
    started = h.clock["t"]
    world_observe = h.world.observe

    class RenamingPage:
        def observe(self, hwnd: int, **kwargs: Any) -> Any:
            old = (40, "Document", "Home", (42, 90), 0)
            if h.clock["t"] - started >= 0.2 and old in h.world.rows:
                h.world.rows.remove(old)
                h.world.rows.append((40, "Document", f"{HELLO} - Google Search", (42, 90), 0))
            return world_observe(hwnd, **kwargs)

    h.session._observer = RenamingPage()


def _replacing() -> Any:
    from steerai.verify.predicates import Appeared  # noqa: PLC0415

    return Appeared(role="Document", name_pattern=HELLO, decoration="google_search", replaces=True)


def test_an_in_place_rename_never_verifies_an_r2_step() -> None:
    """Principal review of PR #29, item 2: a rename keeps its RuntimeId, so at R2/R3 it is no
    new element (5.10 [v1.2]); the step stays unverified with no label."""
    h = Harness()
    h.world.composer_value = HELLO
    _rebuild(h, live=FocusLive(h.world, focused=COMPOSER[3]))
    _renaming_page(h)
    step = Step(
        id="s3",
        subgoal="g1",
        verb="submit",
        target=Target(role="Edit", name="Message"),
        risk="R2",
        params=PARAMS,
        expect=_replacing(),
    )
    result = h.session.run_step(step)
    assert result.ok is False, result.to_dict()
    assert result.evidence["reason"] == "renamed_at_risk"
    assert result.label_kind is None
    assert result.code is ErrorCode.VERIFY_TIMEOUT


def test_an_in_place_rename_verifies_an_r1_click() -> None:
    h = Harness()
    _renaming_page(h)
    step = Step(
        id="c1",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="Chat options"),
        risk="R1",
        expect=_replacing(),
    )
    result = h.session.run_step(step)
    assert result.ok is True, result.to_dict()
    assert result.evidence["renamed"] == 1
