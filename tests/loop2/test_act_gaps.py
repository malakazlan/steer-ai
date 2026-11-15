"""#356: the tests the slice 6 tester found missing (#352, advisory review). Each pins a rule of the
act port, its guards or the runner that a surviving mutant could break without a failing test.
Tests only: no code changes."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.execution import RefusalError
from steerai.action.physical import PhysicalAction
from steerai.errors import ErrorCode, SteerError
from steerai.loop2.act_guards import GuardDesk, InputGuard
from steerai.loop2.loop import ActResult, PortInterrupted
from steerai.loop2.runner import run_task
from steerai.loop2.task import Budgets
from steerai.orchestrator.loop import IDLE_S
from tests.action.test_physical import (
    TARGET_ID,
    FakeInput,
    _edit,
    _snapshot,
    fast_clock,  # noqa: F401 - the v1 fixture, used by name
)
from tests.loop2.test_act_guards import (
    ON_INDET_OFF,
    THIRD,
    Switch,
    _check,
    _Cycle,
    _guard,
    _physical,
    fast_poll,  # noqa: F401 - a fixture, used by name
)
from tests.loop2.test_act_port import (
    BOX,
    ROW,
    TOGGLED,
    Desk,
    Executor,
    _dispatch,
    _port,
    nothing_sent,
    ok,
)
from tests.loop2.test_rt_deferred import GOOD
from tests.loop2.world_fakes import (
    FIELD,
    QUERY_PAGE,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

TYPE: dict[str, Any] = {"text": "red shoes", "param": "text"}
TOK_FIRST, TOK_IDLE, TOK_WAIT = "tok_first", "tok_idle", "tok_wait"
"""Distinct switch tokens: a test that always latches `killed` would pass a constant."""


# ---- 1: the tap refusal before a press -------------------------------------------------------


def _desk(guard: InputGuard) -> GuardDesk:
    return GuardDesk(guard, last_input=lambda: 0.0, takeover_token=lambda: "takeover")


def test_a_tap_refusal_stops_a_press() -> None:
    rig = _port(physical=Executor(ok("press")), desk=Desk(tap="hands_on"))
    result = rig.port.act(_dispatch("press", FIELD, key="Enter"))
    assert result.action.evidence["detail"] == "hands_on"
    assert rig.physical.calls == []
    assert rig.desk.taps == 1


# ---- 2: the idle boundary is inclusive (v1: `now - last >= IDLE_S`) ------------------------------


def test_a_person_idle_for_exactly_idle_s_is_idle() -> None:
    rig = _port(physical=Executor(ok("type")), desk=Desk(last_input=-IDLE_S))
    rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert rig.clock.pauses == []
    assert rig.world.looks == 0


def test_a_person_idle_for_just_under_idle_s_is_waited_for() -> None:
    rig = _port(physical=Executor(ok("type")), desk=Desk(last_input=-IDLE_S + 0.01))
    rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert rig.clock.pauses != []


# ---- 3, 4: the switch's token is passed on as it was read ----------------------------------------


def test_the_token_read_first_is_the_one_raised() -> None:
    rig = _port(interrupted=lambda: TOK_FIRST)
    with pytest.raises(PortInterrupted) as stop:
        rig.port.act(_dispatch("click", BOX))
    assert stop.value.token == TOK_FIRST


def test_the_token_read_at_the_top_of_the_idle_wait_is_the_one_raised() -> None:
    reads: list[int] = []

    def interrupted() -> str | None:
        reads.append(1)
        return TOK_IDLE if len(reads) >= 2 else None

    rig = _port(physical=Executor(ok("type")), desk=Desk(last_input=0.0), interrupted=interrupted)
    with pytest.raises(PortInterrupted) as stop:
        rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert stop.value.token == TOK_IDLE
    assert rig.physical.calls == []


def test_the_token_read_inside_the_idle_wait_is_the_one_raised() -> None:
    latched: list[str] = []
    rig = _port(
        physical=Executor(ok("type")),
        desk=Desk(last_input=0.0),
        interrupted=lambda: latched[0] if latched else None,
    )
    rig.clock.on_pause = lambda: latched.append(TOK_WAIT)
    with pytest.raises(PortInterrupted) as stop:
        rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert stop.value.token == TOK_WAIT


class _Token(Switch):
    """The switch, latching a named token instead of `killed`."""

    def __init__(self, token: str, after: int = 0) -> None:
        super().__init__(after=after)
        self.latched = token


def test_a_guard_refusal_carries_the_switchs_token() -> None:
    guard = _guard(FakeInput(), _Token("tok_guard"))
    with pytest.raises(RefusalError) as refused:
        guard.click_at(1, 2)
    assert refused.value.detail == "tok_guard"


def test_a_double_click_wait_refusal_carries_the_switchs_token() -> None:
    now = {"t": 0.0}
    switch = _Token("tok_dbl", after=10**9)
    guard = _guard(FakeInput(double_click_s=0.5), switch, now=now)
    guard.click_at(1, 2)
    switch.after = switch.reads + 1
    with pytest.raises(RefusalError) as refused:
        guard.double_click_time_s()
    assert refused.value.detail == "tok_dbl"


# ---- 5: arming ends when the armed block raises --------------------------------------------------


def test_arming_ends_when_the_armed_block_raises() -> None:
    guard = _guard(FakeInput())
    with pytest.raises(ValueError, match="boom"), guard.armed(frozenset({TARGET_ID})):
        raise ValueError("boom")
    with pytest.raises(RefusalError) as refused:
        guard.press("enter")
    assert refused.value.detail == "not_armed"


# ---- 6: a taxonomy error from the desk's checks fails closed -------------------------------------


class _TaxonomyFailure(FakeInput):
    def input_desktop_name(self) -> str:
        raise SteerError(ErrorCode.VERIFY_TIMEOUT, "x")

    def focused_runtime_id(self) -> tuple[int, ...]:
        raise SteerError(ErrorCode.VERIFY_TIMEOUT, "x")


def test_a_taxonomy_error_in_the_tap_check_refuses() -> None:
    desk = GuardDesk(
        _guard(_TaxonomyFailure()), last_input=lambda: 0.0, takeover_token=lambda: "takeover"
    )
    assert desk.tap_refusal() == "tap_unreadable"


def test_a_taxonomy_error_in_the_focus_read_is_unread() -> None:
    assert (
        GuardDesk(
            _guard(_TaxonomyFailure()), last_input=lambda: 0.0, takeover_token=lambda: "takeover"
        ).focused()
        is None
    )


# ---- 7: the double-click wait is the remainder, not the whole time -------------------------------


def test_the_double_click_wait_is_the_remainder() -> None:
    now = {"t": 0.0}
    pauses: list[float] = []
    guard = _guard(FakeInput(double_click_s=0.5), now=now, pauses=pauses)
    guard.click_at(1, 2)
    now["t"] += 0.3
    guard.double_click_time_s()
    assert sum(pauses) == pytest.approx(0.2, abs=0.05)


# ---- 8, 9, 13: the toggle re-read ----------------------------------------------------------------


@pytest.mark.parametrize("garbage", [7, None, "x"], ids=["out_of_range", "none", "text"])
def test_a_garbage_re_read_is_refused_never_a_success_or_a_crash(
    fast_poll: None,  # noqa: F811 - the fixture
    garbage: Any,
) -> None:
    """An out-of-range or non-int re-read before the second Toggle is unread: no second Toggle,
    `second_toggle_refused`; never `settled_on_reread`, never a raw TypeError."""
    handle = _Cycle(1, ON_INDET_OFF, reads=[1, *THIRD, garbage])
    result = _check(handle, "uncheck")
    assert handle.calls == ["toggle"]
    assert result.evidence["detail"] == "second_toggle_refused"


def test_the_third_state_is_the_one_v1_settled_on(fast_poll: None) -> None:  # noqa: F811
    """The first read after the Toggle still shows the initial state; v1 polls on and settles on
    the mixed state; the re-read shows mixed: the second Toggle goes."""
    handle = _Cycle(1, ON_INDET_OFF, reads=[1, 1, *THIRD, 2])
    result = _check(handle, "uncheck")
    assert handle.calls == ["toggle", "toggle"]
    assert result.evidence == {"toggles": 2}


def test_a_second_toggle_raising_a_non_oserror_may_have_landed(fast_poll: None) -> None:  # noqa: F811
    """A COM error is no OSError: the guard still names it uncertain."""

    class _Raising(_Cycle):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802 - COM naming
            pattern = super().GetCurrentPattern(pattern_id)
            handle = self
            first = pattern.Toggle

            def toggle() -> None:
                if handle.calls:
                    handle.calls.append("toggle")
                    raise RuntimeError("a COM error stand-in")
                first()

            pattern.Toggle = toggle
            return pattern

    handle = _Raising(1, ON_INDET_OFF)
    result = _check(handle, "uncheck")
    assert handle.calls == ["toggle", "toggle"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence["detail"] == "second_toggle_uncertain"


# ---- 10: the act port's wall cap pauses the task on the budget, with no strike -------------------


class _SlowWorld(World):
    def _act(self, sent: Any) -> ActResult:
        self.now += 100.0
        return super()._act(sent)


def test_the_act_ports_wall_cap_pauses_on_the_budget_with_no_strike() -> None:
    refused = ActionResult(
        "type",
        Delivery.NOT_DELIVERED,
        code=ErrorCode.GATE_PENDING_USER,
        evidence={"detail": "wall_budget"},
    )
    world = _SlowWorld(
        [decision(act("type", target=FIELD.id, param="text"), first=True)],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        results=[ActResult(refused, None)],
    )
    outcome = run_task(task(Budgets(**(GOOD | {"max_wall_s": 3.0}))), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "max_wall")
    assert outcome.strikes == ()


# ---- 11: only a read-back-phase uncertainty is the refused-second-toggle case ------------------


def test_an_uncertain_toggle_from_another_phase_stays_uncertain() -> None:
    v1 = ActionResult(
        verb="check",
        delivery=Delivery.DELIVERY_UNCERTAIN,
        steps=[TOGGLED],
        code=ErrorCode.VERIFY_FAILED,
        evidence={"reason": "verify_failed", "phase": "call"},
    )
    rig = _port(semantic=Executor(v1))
    assert rig.port.act(_dispatch("check", BOX)).action is v1


# ---- 12: Backspace needs the pending text of THIS field ------------------------------------------


def test_backspace_on_another_filled_field_than_steers_is_refused() -> None:
    """Steer typed into FIELD; the person's text is in NOTES. A Backspace at NOTES (focused, the
    last acted) is refused `not_own_text` although FIELD holds pending text."""
    notes = element(60, "Edit", "Notes", (9, 60), 1)
    shown = [*QUERY_PAGE, notes]
    world = World(
        [
            decision(act("type", target=FIELD.id, param="text"), first=True),
            decision(act("click", target=notes.id)),
            decision(act("press", key="Backspace")),
        ],
        screen=observed(shown, focus=FIELD.runtime_id),
        effects=[report("own_value"), report("focus")],
        timeline=[(2, observed(shown, focus=notes.runtime_id))],
    )
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "click"]
    assert outcome.not_own_text == 1


# ---- N04, N07 --------------------------------------------------------------------------------


def test_one_acts_steps_never_reach_the_next_acts_port_error() -> None:
    sent = ActionResult("check", Delivery.DELIVERED_VERIFIED, steps=[TOGGLED])
    rig = _port(semantic=Executor(sent), physical=Executor(ok("type")))
    rig.port.act(_dispatch("check", BOX))

    def broken(_element: Any) -> bool:
        raise RuntimeError("boom")

    setattr(rig.world, "read_secure", broken)  # noqa: B010 - a broken read, swapped in
    result = rig.port.act(_dispatch("type", FIELD, **TYPE))
    assert result.action.evidence["detail"] == "port_error"
    assert result.action.steps == []


def test_a_tree_item_collapse_by_click_is_deferred_too() -> None:
    rig = _port(
        semantic=Executor(nothing_sent("collapse", detail="expand_by_click")),
        physical=Executor(ok("click")),
    )
    result = rig.port.act(_dispatch("collapse", ROW))
    assert result.action.evidence["detail"] == "expand_needs_click"
    assert rig.physical.calls == []


# ---- the kill switch at every input call, swept over v1's real PhysicalExecutor ------------------


class _Logged(FakeInput):
    """Counts every input-sending call."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.sent: list[str] = []

    def acquire_foreground(self, hwnd: int) -> str:
        self.sent.append("acquire")
        return super().acquire_foreground(hwnd)

    def move_cursor(self, x: int, y: int) -> None:
        self.sent.append("move")
        super().move_cursor(x, y)

    def click_at(self, x: int, y: int) -> None:
        self.sent.append("click")
        super().click_at(x, y)

    def type_text(self, text: str) -> None:
        self.sent.append("type")
        super().type_text(text)

    def press(self, chord: str, approval: Any = None) -> None:
        self.sent.append("press")
        super().press(chord, approval)


CASES = {
    "click": (PhysicalAction(verb="click", element_id=1), "Button"),
    "type": (PhysicalAction(verb="type", element_id=1, text="abcdefghijkl"), "Edit"),
    "press": (PhysicalAction(verb="press", element_id=1, chord="enter"), "Edit"),
}


@pytest.mark.parametrize("latch_after", range(7))
@pytest.mark.parametrize("verb", sorted(CASES))
def test_nothing_is_sent_once_the_switch_latches(
    verb: str,
    latch_after: int,
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    """The switch latches once `latch_after` input calls went out: no further call may go."""
    action, role = CASES[verb]
    handle = _edit()
    inner = _Logged(sink=handle)
    guard = _guard(inner, lambda: "killed" if len(inner.sent) >= latch_after else None)
    with guard.armed(frozenset({TARGET_ID})):
        _physical(guard).execute(action, _snapshot(handle, role=role))
    assert len(inner.sent) <= latch_after
