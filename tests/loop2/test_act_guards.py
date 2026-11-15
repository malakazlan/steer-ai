"""Adapter slice 6: the guards the act port puts around v1's executors (packet rev 3.9).

`InputGuard` wraps v1's `InputPort` (AP-01, AP-02, AP-10): every input-sending method reads the
switch first, keyboard input goes only to the armed focus set and never to a password field, and
the double-click wait is kill-aware. `ToggleGuard` wraps v1's TogglePattern ([V2.51] (4)): the
second Toggle of one actuation goes only when the state re-read just before it is
indeterminate. Both run against v1's real executors over the v1 test fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, get_args

import pytest

import steerai.action.physical as physical_mod
import steerai.action.readback as readback_mod
from steerai.action.dispositions import Delivery
from steerai.action.execution import RefusalError, UiaBindings
from steerai.action.executor import Action, Executor
from steerai.action.keyboard import NAMED_KEYS
from steerai.action.physical import PhysicalAction, PhysicalExecutor
from steerai.errors import ErrorCode
from steerai.loop2.act_guards import KEY_CHORDS, GuardDesk, InputGuard, ToggleGuard
from steerai.loop2.decision import Key
from tests.action.fakes import TYPE_IDS, UIA_INVOKE, UIA_TOGGLE, FakeHandle, FakeWindows
from tests.action.test_executor import _snapshot as _state_snapshot
from tests.action.test_physical import (
    CHILD_ID,
    TARGET_ID,
    FakeInput,
    _edit,
    _snapshot,
    fast_clock,  # noqa: F401 - the v1 fixture, used by name
)

CLICK = PhysicalAction(verb="click", element_id=1)
TEXT = "abcdefghijkl"  # 12 units: two bursts of v1's 8-unit chunks


class Switch:
    """The kill switch: None for the first `after` reads, then the token, latched."""

    def __init__(self, after: int = 10**9) -> None:
        self.after = after
        self.latched = "killed"
        self.reads = 0

    def __call__(self) -> str | None:
        self.reads += 1
        return self.latched if self.reads > self.after else None


def _guard(
    inner: FakeInput,
    switch: Callable[[], str | None] | None = None,
    *,
    secure: Callable[[], bool | None] = lambda: False,
    now: dict[str, float] | None = None,
    pauses: list[float] | None = None,
) -> InputGuard:
    clock = {"t": 0.0} if now is None else now
    paused = [] if pauses is None else pauses

    def pause(seconds: float) -> None:
        paused.append(seconds)
        clock["t"] += seconds

    return InputGuard(
        inner,
        interrupted=(lambda: None) if switch is None else switch,
        focused=lambda: (inner.focused_runtime_id(), secure()),
        clock=lambda: clock["t"],
        pause=pause,
    )


def _physical(inputs: InputGuard) -> PhysicalExecutor:
    """v1's physical executor with no blocker of its own: the guard is the only defence."""
    return PhysicalExecutor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: 0.0,
        inputs=inputs,
    )


# ---- AP-01: the switch before every input-sending call -----------------------------------------

SENDS: dict[str, Callable[[InputGuard], object]] = {
    "move_cursor": lambda g: g.move_cursor(1, 2),
    "click_at": lambda g: g.click_at(1, 2),
    "type_text": lambda g: g.type_text("a"),
    "press": lambda g: g.press("enter"),
    "acquire_foreground": lambda g: g.acquire_foreground(0x10),
}


@pytest.mark.parametrize("name", sorted(SENDS))
def test_a_latched_switch_refuses_every_sending_call(name: str) -> None:
    inner = FakeInput()
    guard = _guard(inner, Switch(after=0))
    with guard.armed(frozenset({TARGET_ID})), pytest.raises(RefusalError) as refused:
        SENDS[name](guard)
    assert refused.value.code is ErrorCode.GATE_BLOCKED
    assert refused.value.detail == "killed"
    assert inner.moves == []
    assert inner.clicks == []
    assert inner.bursts == []
    assert inner.foreground == 0x10  # never acquired: the fake's own start value


def test_reads_pass_through_without_the_switch() -> None:
    inner = FakeInput(focused=CHILD_ID)
    switch = Switch(after=0)
    guard = _guard(inner, switch)
    assert guard.focused_runtime_id() == CHILD_ID
    assert guard.foreground_hwnd() == 0x10
    assert switch.reads == 0


@pytest.mark.parametrize("after", range(4))
def test_a_kill_latched_anywhere_in_a_click_sends_no_click(
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
    after: int,
) -> None:
    """The switch is read before the move and before the click; latched at any read, the
    button never goes down. Inside v1's `Call.send` (the click) the guard alone cannot tell v1
    nothing went, so v1 records it uncertain: fail closed, never a retry."""
    inner = FakeInput()
    switch = Switch(after=after)
    result = _physical(_guard(inner, switch)).execute(CLICK, _snapshot(FakeHandle()))
    if inner.clicks:
        assert switch.reads <= after  # it only clicked while the switch read None
    else:
        assert result.delivery in {Delivery.NOT_DELIVERED, Delivery.DELIVERY_UNCERTAIN}
        assert result.evidence["detail"] == "killed"
    assert after >= 2 or not inner.clicks  # the move's read and the click's read both refuse


def test_with_v1s_own_blocker_too_a_kill_at_the_click_is_not_delivered(
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    """Slice 7 also passes the switch as v1's blocker: `Call.send` reads it before marking the
    click sent, so a kill there is a clean `not_delivered`."""
    inner = FakeInput()
    switch = Switch(after=1)
    executor = PhysicalExecutor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: 0.0,
        inputs=_guard(inner, switch),
        blocker=switch,
    )
    result = executor.execute(CLICK, _snapshot(FakeHandle()))
    assert inner.clicks == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.GATE_BLOCKED


def test_a_kill_between_bursts_stops_the_typing(
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    """v1's executor without its own blocker: the guard alone stops the second burst."""
    handle = _edit()
    inner = FakeInput(sink=handle)
    switch = Switch()
    guard = _guard(inner, switch)

    def latch(_inputs: FakeInput, index: int) -> None:
        if index == 1:
            switch.after = switch.reads

    inner.after_burst = latch
    with guard.armed(frozenset({TARGET_ID})):
        result = _physical(guard).execute(
            PhysicalAction(verb="type", element_id=1, text=TEXT), _snapshot(handle, role="Edit")
        )
    assert inner.bursts == ["text:8"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN


# ---- AP-02: the double-click wait is the guard's, kill-aware ---------------------------------


def _no_v1_sleep(monkeypatch: pytest.MonkeyPatch, now: dict[str, float]) -> list[float]:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    monkeypatch.setattr(
        physical_mod, "time", SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep)
    )
    return slept


def test_the_guard_waits_out_the_double_click_time_and_v1_never_sleeps(
    monkeypatch: pytest.MonkeyPatch,
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    slept = _no_v1_sleep(monkeypatch, fast_clock)
    pauses: list[float] = []
    inner = FakeInput(double_click_s=0.5)
    executor = _physical(_guard(inner, now=fast_clock, pauses=pauses))
    snapshot = _snapshot(FakeHandle())
    first = fast_clock["t"]
    executor.execute(CLICK, snapshot)
    executor.execute(CLICK, snapshot)
    assert len(inner.clicks) == 2
    assert slept == []
    assert pauses
    assert max(pauses) <= 0.05  # kill_sleep's steps
    assert fast_clock["t"] - first >= 0.5


def test_a_kill_during_the_double_click_wait_sends_no_second_click(
    monkeypatch: pytest.MonkeyPatch,
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    _no_v1_sleep(monkeypatch, fast_clock)
    inner = FakeInput(double_click_s=0.5)
    switch = Switch()
    executor = _physical(_guard(inner, switch, now=fast_clock))
    snapshot = _snapshot(FakeHandle())
    executor.execute(CLICK, snapshot)
    switch.after = switch.reads + 2  # latches inside the wait
    result = executor.execute(CLICK, snapshot)
    assert len(inner.clicks) == 1
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"


def test_no_wait_before_the_first_click() -> None:
    pauses: list[float] = []
    guard = _guard(FakeInput(double_click_s=0.5), pauses=pauses)
    assert guard.double_click_time_s() == 0.0
    assert pauses == []


# ---- AP-10, AP-08: keyboard input only to the armed focus, never to a password ---------------


def test_keyboard_input_outside_an_armed_act_is_refused() -> None:
    inner = FakeInput()
    guard = _guard(inner)
    for name in ("type_text", "press"):
        with pytest.raises(RefusalError) as refused:
            SENDS[name](guard)
        assert refused.value.detail == "not_armed"
    assert inner.bursts == []


def test_keyboard_input_to_a_focus_outside_the_armed_set_is_refused() -> None:
    """Never "any descendant" (AP-10): the exact RuntimeIds the port armed."""
    inner = FakeInput(focused=CHILD_ID)
    guard = _guard(inner)
    with guard.armed(frozenset({TARGET_ID})), pytest.raises(RefusalError) as refused:
        guard.type_text("a")
    assert refused.value.code is ErrorCode.FOCUS_LOST
    assert refused.value.detail == "focus_moved"
    assert inner.bursts == []


@pytest.mark.parametrize(("secure", "detail"), [(True, "secure_field"), (None, "secure_unread")])
def test_keyboard_input_to_a_password_or_unread_field_is_refused(
    secure: bool | None, detail: str
) -> None:
    inner = FakeInput()
    guard = _guard(inner, secure=lambda: secure)
    for name in ("type_text", "press"):
        with guard.armed(frozenset({TARGET_ID})), pytest.raises(RefusalError) as refused:
            SENDS[name](guard)
        assert refused.value.code is ErrorCode.GATE_BLOCKED
        assert refused.value.detail == detail
    assert inner.bursts == []


def test_a_field_that_becomes_a_password_mid_typing_gets_no_more_bursts(
    fast_clock: dict[str, float],  # noqa: F811 - the v1 fixture
) -> None:
    """AP-08: IsPassword is read before every burst, not once."""
    handle = _edit()
    inner = FakeInput(sink=handle)
    secure = {"now": False}
    guard = _guard(inner, secure=lambda: secure["now"])

    def flip(_inputs: FakeInput, index: int) -> None:
        if index == 1:
            secure["now"] = True

    inner.after_burst = flip
    with guard.armed(frozenset({TARGET_ID})):
        result = _physical(guard).execute(
            PhysicalAction(verb="type", element_id=1, text=TEXT), _snapshot(handle, role="Edit")
        )
    assert inner.bursts == ["text:8"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN


def test_arming_ends_with_the_act() -> None:
    inner = FakeInput()
    guard = _guard(inner)
    with guard.armed(frozenset({TARGET_ID})):
        guard.press("enter")
    with pytest.raises(RefusalError):
        guard.press("enter")
    assert inner.chords == ["enter"]


# ---- AP-12: the closed key map ----------------------------------------------------------------


def test_every_key_maps_to_one_named_key_with_no_modifier() -> None:
    assert set(KEY_CHORDS) == set(get_args(Key))
    for chord in KEY_CHORDS.values():
        assert "+" not in chord
        assert chord in NAMED_KEYS


# ---- the desk: v1's tap refusal and the live focus, fail closed -------------------------------


def test_the_desk_reads_v1s_tap_refusal() -> None:
    desk = GuardDesk(_guard(FakeInput(modifiers=["shift"])), last_input=lambda: 0.0)
    assert desk.tap_refusal() == "hands_on"
    assert GuardDesk(_guard(FakeInput()), last_input=lambda: 0.0).tap_refusal() is None


def test_an_unreadable_tap_check_refuses() -> None:
    class Broken(FakeInput):
        def input_desktop_name(self) -> str:
            raise OSError("unreadable")

    desk = GuardDesk(_guard(Broken()), last_input=lambda: 0.0)
    assert desk.tap_refusal() == "tap_unreadable"


def test_the_desk_reads_no_focus_as_unread() -> None:
    class Broken(FakeInput):
        def focused_runtime_id(self) -> tuple[int, ...]:
            raise OSError("unreadable")

    assert GuardDesk(_guard(Broken()), last_input=lambda: 0.0).focused() is None
    assert GuardDesk(_guard(FakeInput(focused=())), last_input=lambda: 0.0).focused() is None
    assert GuardDesk(_guard(FakeInput()), last_input=lambda: 0.0).focused() == TARGET_ID


# ---- [V2.51] (4): the second Toggle only on an indeterminate state, re-read before it ---------


class _Cycle(FakeHandle):
    """A tri-state checkbox with a scripted cycle and every read and Toggle recorded."""

    def __init__(self, state: int, cycle: dict[int, int], reads: list[int] | None = None) -> None:
        super().__init__(toggled=state, patterns={UIA_TOGGLE, UIA_INVOKE})
        self.cycle = cycle
        self.scripted = reads

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802 - COM naming
        if pattern_id != UIA_TOGGLE:
            return super().GetCurrentPattern(pattern_id)
        handle = self

        class _Pattern:
            def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802 - COM naming
                return self

            def Toggle(self) -> None:  # noqa: N802 - COM naming
                handle.toggled = handle.cycle[handle.toggled]
                handle.calls.append("toggle")

            @property
            def CurrentToggleState(self) -> int:  # noqa: N802 - COM naming
                if handle.scripted:
                    return handle.scripted.pop(0)
                return handle.toggled

        return _Pattern()


def _semantic(guarded: bool = True) -> Executor:
    names = {v: k for k, v in TYPE_IDS.items()}
    uia = (
        ToggleGuard(control_types=names, interfaces={})
        if guarded
        else UiaBindings(control_types=names, interfaces={})
    )
    return Executor(uia=uia, windows=FakeWindows(), clock=lambda: 0.0)


ON_INDET_OFF = {1: 2, 2: 0, 0: 1}


@pytest.fixture
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    monkeypatch.setattr(
        readback_mod, "time", SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep)
    )


def _check(handle: _Cycle, verb: str = "check") -> Any:  # Any: v1's ActionResult
    return _semantic().execute(Action(verb, 1), _state_snapshot(handle, role="CheckBox"))


def test_off_to_on_through_mixed_takes_two_toggles(fast_poll: None) -> None:
    """[V2.52] (a): the first Toggle lands on a third state (mixed): one more, then stop."""
    handle = _Cycle(0, {0: 2, 2: 1, 1: 0})
    result = _check(handle)
    assert handle.calls == ["toggle", "toggle"]
    assert handle.toggled == 1
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.evidence == {"toggles": 2}


def test_mixed_to_on_through_off_takes_two_toggles(fast_poll: None) -> None:
    """[V2.52] (a): starting indeterminate, the first Toggle lands on off (a third state, not the
    initial one): one more reaches on. [V2.51] (4)'s indeterminate-only rule failed here."""
    handle = _Cycle(2, {2: 0, 0: 1, 1: 2})
    result = _check(handle)
    assert handle.calls == ["toggle", "toggle"]
    assert handle.toggled == 1
    assert result.delivery is Delivery.DELIVERED_VERIFIED


def test_on_to_off_through_mixed_takes_two_toggles(fast_poll: None) -> None:
    handle = _Cycle(1, ON_INDET_OFF)
    result = _check(handle, "uncheck")
    assert handle.calls == ["toggle", "toggle"]
    assert handle.toggled == 0
    assert result.delivery is Delivery.DELIVERED_VERIFIED


def test_a_control_that_ignores_the_first_toggle_gets_no_second(fast_poll: None) -> None:
    handle = _Cycle(0, {0: 0, 1: 1, 2: 2})
    result = _check(handle)
    assert handle.calls == ["toggle"]
    assert result.evidence["detail"] == "unchanged"  # v1's own stop; the port names it


THIRD = [2] * (readback_mod.MAX_STEPS + 1)
"""What v1's poll reads after the first Toggle of an uncheck from on: mixed, every time."""


def test_the_second_toggle_goes_when_the_re_read_still_shows_the_third_state(
    fast_poll: None,
) -> None:
    handle = _Cycle(1, ON_INDET_OFF, reads=[1, *THIRD, 2])
    result = _check(handle, "uncheck")
    assert handle.calls == ["toggle", "toggle"]
    assert result.evidence == {"toggles": 2}


def test_no_second_toggle_when_the_re_read_is_back_at_the_initial_state(fast_poll: None) -> None:
    """The poll saw mixed on every read; the read just before the second Toggle shows the
    initial state again (the first Toggle undone): no second Toggle."""
    handle = _Cycle(1, ON_INDET_OFF, reads=[1, *THIRD, 1])
    result = _check(handle, "uncheck")
    assert handle.calls == ["toggle"]
    assert result.evidence["detail"] == "second_toggle_refused"


def test_a_re_read_that_shows_the_wanted_state_settles_with_no_second_toggle(
    fast_poll: None,
) -> None:
    """Coordinator on [V2.52] (a): off, then mixed by v1's poll, then on by the re-read: the
    wanted state arrived late. No second Toggle (it would move it off); a success, not a
    refusal: the port reports it verified."""
    handle = _Cycle(0, {0: 2, 2: 1, 1: 0}, reads=[0, *THIRD, 1])
    result = _check(handle)
    assert handle.calls == ["toggle"]
    assert result.evidence["detail"] == "settled_on_reread"


def test_no_second_toggle_when_the_re_read_fails(fast_poll: None) -> None:
    class _Unreadable(_Cycle):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802 - COM naming
            pattern = super().GetCurrentPattern(pattern_id)
            reads = iter([1, *THIRD])

            class _Pattern:
                def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802 - COM naming
                    return self

                def Toggle(self) -> None:  # noqa: N802 - COM naming
                    pattern.Toggle()

                @property
                def CurrentToggleState(self) -> int:  # noqa: N802 - COM naming
                    value = next(reads, None)
                    if value is None:
                        raise OSError("unreadable")
                    return value

            return _Pattern()

    handle = _Unreadable(1, ON_INDET_OFF)
    result = _check(handle, "uncheck")
    assert handle.calls == ["toggle"]
    assert result.evidence["detail"] == "second_toggle_refused"


def test_two_toggles_is_the_hard_limit() -> None:
    """A control that ignores the second Toggle still shows the third state: a third call is
    refused by the limit, not by the re-read."""
    handle = _Cycle(0, {0: 2, 2: 2, 1: 1})
    guard = ToggleGuard(control_types={}, interfaces={})
    pattern = guard.pattern(handle, UIA_TOGGLE, "TogglePattern")
    assert pattern.CurrentToggleState == 0
    pattern.Toggle()
    assert pattern.CurrentToggleState == 2
    pattern.Toggle()
    assert pattern.CurrentToggleState == 2
    with pytest.raises(RefusalError) as refused:
        pattern.Toggle()
    assert refused.value.detail == "toggle_limit"
    assert handle.calls == ["toggle", "toggle"]


def test_other_patterns_pass_through_the_toggle_guard(fast_poll: None) -> None:
    handle = FakeHandle(patterns={UIA_INVOKE})
    result = _semantic().execute(Action("click", 1), _state_snapshot(handle))
    assert handle.invoked == 1
    assert result.delivery is not Delivery.NOT_DELIVERED


# ---- #352 review round --------------------------------------------------------------------------


def test_one_read_of_the_focused_element_per_burst() -> None:
    """Principal contract (a): the RuntimeId and IsPassword come from one read, so the focus
    cannot move between them: the guard never reads the focus a second way."""
    inner = FakeInput(focused=CHILD_ID)  # another read would see the focus elsewhere
    reads: list[int] = []

    def focused() -> tuple[tuple[int, ...], bool | None]:
        reads.append(1)
        return TARGET_ID, False

    guard = InputGuard(
        inner, interrupted=lambda: None, focused=focused, clock=lambda: 0.0, pause=lambda _s: None
    )
    with guard.armed(frozenset({TARGET_ID})):
        guard.type_text("a")
        guard.press("enter")
    assert len(reads) == 2
    assert inner.bursts == ["text:1", "chord:enter"]


def test_a_second_toggle_that_raises_may_have_landed(fast_poll: None) -> None:
    """Principal item 1: the second Toggle has the first's label, so v1 cannot tell a raise
    from it apart from a read-back failure; the guard names it uncertain."""

    class _Raising(_Cycle):
        def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802 - COM naming
            pattern = super().GetCurrentPattern(pattern_id)
            handle = self
            first = pattern.Toggle

            def toggle() -> None:
                if handle.calls:
                    handle.calls.append("toggle")
                    raise OSError("RPC_E_DISCONNECTED")
                first()

            pattern.Toggle = toggle
            return pattern

    handle = _Raising(1, ON_INDET_OFF)
    result = _check(handle, "uncheck")
    assert handle.calls == ["toggle", "toggle"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence["detail"] == "second_toggle_uncertain"
