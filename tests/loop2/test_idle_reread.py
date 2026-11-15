"""#383 review, blockers 1 and 2: HH-09's idle re-read runs on every physical send, armed or not,
timed from the act port's last idle check that passed.

Through `LiveWorld`: the act port, v1's physical executor, the guard and the desk are the real
ones; only the platform (UIA handles, the input side, the monitor) is faked. The person's input
lands after the act port's last idle check and before the click: it must refuse the click."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from steerai.action.execution import RefusalError
from steerai.errors import ErrorCode
from steerai.loop2.act_guards import REREAD_AFTER_S, InputGuard
from steerai.loop2.loop import Dispatch
from tests.action.fakes import TYPE_IDS, UIA_VALUE, FakeHandle
from tests.action.test_physical import FakeInput
from tests.loop2.test_live_world import Platform, _Handle, _world
from tests.loop2.world_fakes import FIELD, GO, PAGE, act, task

LATE = REREAD_AFTER_S * 2
"""Later than one tick after the last idle check."""


class Clock:
    """The world clock (and here the gap clock too); `on_pause` runs at each pause."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.on_pause: Any = None  # Any: a test hook

    def __call__(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.now += seconds
        if self.on_pause is not None:
            self.on_pause()


def _live(*, no_invoke: bool = False) -> tuple[Any, Platform, Clock]:  # Any: the ports
    platform = Platform()
    handles: list[FakeHandle] = [
        _Handle(control_type=TYPE_IDS.get(e.role, TYPE_IDS["Button"]), name=e.name) for e in PAGE
    ]
    if no_invoke:
        for handle, e in zip(handles, PAGE, strict=True):
            if e.name == GO.name:
                handle.patterns = {UIA_VALUE}  # no Invoke: the act port's fallback click
    platform.handles = handles
    platform.fake_input.focused = FIELD.runtime_id
    platform.fake_input.hits = [GO.runtime_id if no_invoke else FIELD.runtime_id]  # the target
    clock = Clock()
    world, _ = _world(platform, clock=clock, pause=clock.pause, gap_clock=clock)
    ports = world.ports(task())
    ports.observe()
    return ports, platform, clock


def _person_after_the_idle_check(platform: Platform, clock: Clock) -> None:
    """The person moves the mouse just as the click's cursor move goes out, later than a tick
    after the act port's last idle check: the click itself must see it."""

    def moved(_x: int, _y: int) -> None:
        clock.now += LATE
        platform.monitor.last = clock.now

    platform.fake_input.after_move = moved


def _dispatch(verb: str, target: Any) -> Dispatch:  # Any: a world_fakes element
    return Dispatch(act(verb, target=target.id), target.runtime_id, target.role, target.name,
                    "R0", None)  # fmt: skip


def test_without_the_persons_input_the_focus_click_goes_out() -> None:
    """The control: the harness's click does reach the input side."""
    ports, platform, _clock = _live()
    ports.act(_dispatch("focus", FIELD))
    assert len(platform.fake_input.clicks) == 1


def test_without_the_persons_input_the_fallback_click_goes_out() -> None:
    ports, platform, _clock = _live(no_invoke=True)
    ports.act(_dispatch("click", GO))
    assert len(platform.fake_input.clicks) == 1


def test_the_focus_click_re_reads_the_idle_rule() -> None:
    ports, platform, clock = _live()
    _person_after_the_idle_check(platform, clock)
    ports.act(_dispatch("focus", FIELD))
    assert platform.fake_input.clicks == []


def test_the_fallback_click_re_reads_the_idle_rule() -> None:
    ports, platform, clock = _live(no_invoke=True)
    _person_after_the_idle_check(platform, clock)
    ports.act(_dispatch("click", GO))
    assert platform.fake_input.clicks == []


def test_a_click_after_the_double_click_wait_re_reads_the_idle_rule() -> None:
    """The second click near the first waits out the double-click time first (AP-02); the
    person's input during that wait refuses the click that follows it."""
    ports, platform, clock = _live()
    ports.act(_dispatch("focus", FIELD))
    assert len(platform.fake_input.clicks) == 1
    clock.on_pause = lambda: setattr(platform.monitor, "last", clock.now)
    ports.act(_dispatch("focus", FIELD))
    assert len(platform.fake_input.clicks) == 1  # the second click never went out


# ---- T2: the re-read on every send path, at the guard ------------------------------------------


def _guard(clock: dict[str, float], last: dict[str, float]) -> tuple[InputGuard, FakeInput]:
    inner = FakeInput()
    inner.focused = (1, 7)
    guard = InputGuard(
        inner,
        interrupted=lambda: None,
        focused=lambda: (inner.focused_runtime_id(), False),
        clock=lambda: clock["t"],
        last_input=lambda: last["t"],
        takeover_token=lambda: "takeover",
        gap_clock=lambda: clock["t"],
    )
    return guard, inner


SENDS = {
    "move_cursor": lambda g: g.move_cursor(5, 5),
    "click_at": lambda g: g.click_at(5, 5),
    "acquire_foreground": lambda g: g.acquire_foreground(16),
    "type_text": lambda g: g.type_text("abc"),
    "press": lambda g: g.press("enter"),
}


@pytest.mark.parametrize("send", list(SENDS))
def test_every_send_path_re_reads_the_idle_rule_when_late(send: str) -> None:
    clock, last = {"t": 100.0}, {"t": 50.0}
    guard, _inner = _guard(clock, last)
    guard.idle_checked()
    clock["t"] += LATE
    last["t"] = clock["t"]  # the person, after the last idle check
    with guard.armed(frozenset({(1, 7)})), pytest.raises(RefusalError) as refused:
        SENDS[send](guard)
    assert refused.value.code == ErrorCode.GATE_PENDING_USER


@pytest.mark.parametrize("send", list(SENDS))
def test_an_unarmed_send_never_stamped_re_reads_too(send: str) -> None:
    clock, last = {"t": 100.0}, {"t": 99.9}
    guard, _inner = _guard(clock, last)
    keyboard = send in {"type_text", "press"}  # keyboard input goes only while armed (AP-10)
    arming = guard.armed(frozenset({(1, 7)})) if keyboard else contextlib.nullcontext()
    with arming, pytest.raises(RefusalError) as refused:
        SENDS[send](guard)
    assert refused.value.code == ErrorCode.GATE_PENDING_USER


# ---- blocker 3: the gap on its own clock, a strict boundary, at most three re-reads ----------


def _counting_guard(gap: list[float], last: dict[str, float], reads: list[int]) -> InputGuard:
    inner = FakeInput()

    def last_input() -> float:
        reads.append(1)
        return last["t"]

    return InputGuard(
        inner,
        interrupted=lambda: None,
        focused=lambda: (inner.focused_runtime_id(), False),
        clock=lambda: 100.0,
        last_input=last_input,
        takeover_token=lambda: "takeover",
        gap_clock=lambda: gap[0],
    )


def test_exactly_one_tick_after_the_idle_check_re_reads() -> None:
    gap, last = [10.0], {"t": 0.0}
    reads: list[int] = []
    guard = _counting_guard(gap, last, reads)
    guard.idle_checked()
    gap[0] = 10.0 + REREAD_AFTER_S
    guard.move_cursor(1, 1)
    assert reads == [1]


def test_just_under_a_tick_does_not_re_read() -> None:
    gap, last = [10.0], {"t": 0.0}
    reads: list[int] = []
    guard = _counting_guard(gap, last, reads)
    guard.idle_checked()
    gap[0] = 10.0 + REREAD_AFTER_S * 0.99
    guard.move_cursor(1, 1)
    assert reads == []


def test_a_gap_clock_going_backwards_forces_a_re_read() -> None:
    """HM-13/HM-14: a negative gap is never "fresh"."""
    gap, last = [10.0], {"t": 0.0}
    reads: list[int] = []
    guard = _counting_guard(gap, last, reads)
    guard.idle_checked()
    gap[0] = 9.0
    guard.move_cursor(1, 1)
    assert reads == [1]


def test_the_gap_is_on_the_gap_clock_not_the_world_clock() -> None:
    """The world clock ticks every 15.625 ms; the gap is measured on QPC (`gap_clock`)."""
    gap, last = [10.0], {"t": 0.0}
    reads: list[int] = []
    guard = _counting_guard(gap, last, reads)
    guard.idle_checked()
    guard.move_cursor(1, 1)  # the gap clock has not moved: no re-read, whatever the world clock
    assert reads == []


def test_after_three_re_reads_that_each_come_too_late_the_send_is_refused() -> None:
    """§5.3: a re-read that is itself late (the thread lost the CPU) is re-read; three times
    at most, then the send is refused."""
    last: dict[str, float] = {"t": 0.0}
    reads: list[int] = []
    gap = [10.0]
    guard = _counting_guard(gap, last, reads)
    guard.idle_checked()

    def slow_last_input() -> float:
        reads.append(1)
        gap[0] += 2 * REREAD_AFTER_S  # each read takes two ticks
        return last["t"]

    guard._last_input = slow_last_input  # a slow read, on the instance
    gap[0] += 2 * REREAD_AFTER_S
    with pytest.raises(RefusalError) as refused:
        guard.move_cursor(1, 1)
    assert refused.value.code == ErrorCode.GATE_PENDING_USER
    assert reads == [1, 1, 1]


def test_an_idle_rule_that_cannot_be_read_refuses_with_a_code() -> None:
    """HM-15: never a raw exception out of the guard."""
    gap: list[float] = [10.0]
    reads: list[int] = []
    guard = _counting_guard(gap, {"t": 0.0}, reads)

    def broken() -> float:
        raise RuntimeError("the monitor's words are gone")

    guard._last_input = broken  # an unreadable idle rule
    with pytest.raises(RefusalError) as refused:
        guard.click_at(1, 1)
    assert (refused.value.code, refused.value.detail) == (ErrorCode.GATE_BLOCKED, "idle_unread")
