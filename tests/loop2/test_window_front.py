"""[V2.62] the bound window in front: Steer never raises it mid-task (the guard refuses a
foreground acquire for a window that is not already in front), and no physical click or key goes
where the bound window is not the root window under the target's point (v1's hit test for a
click and a type, pinned here on the v2 path; the act port's own for a press)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from steerai.action.dispositions import Delivery
from steerai.action.execution import UiaBindings
from steerai.action.physical import PhysicalAction, PhysicalExecutor
from steerai.errors import ErrorCode
from steerai.loop2.act_guards import GuardDesk, InputGuard
from steerai.loop2.loop import WindowCovered, render_reason
from steerai.loop2.runner import run_task
from steerai.loop2.world_ports import WorldPorts, WorldTimes
from steerai.world.effects import Observed
from steerai.world.observe2 import FirstTouches
from tests.action.fakes import TYPE_IDS, FakeHandle, FakeWindows
from tests.action.test_physical import (
    FakeInput,
    _edit,
    _snapshot,
    fast_clock,  # noqa: F401 - the v1 fixture, used by name
)
from tests.loop2.test_act_port import Desk, _dispatch, _nothing_executed, _port, _refused
from tests.loop2.test_world_ports import KEY, Clock, Live, Screen
from tests.loop2.world_fakes import FIELD, World, observed, task

BOUND = 0x10
OTHER = 0x99


def _guard(inner: FakeInput) -> InputGuard:
    return InputGuard(
        inner,
        interrupted=lambda: None,
        focused=lambda: (inner.focused_runtime_id(), False),
        clock=lambda: 0.0,
        last_input=lambda: -100.0,
        takeover_token=lambda: "takeover",
    )


def _physical(inputs: InputGuard) -> PhysicalExecutor:
    return PhysicalExecutor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: 0.0,
        inputs=inputs,
    )


# ---- never re-raise the window mid-task --------------------------------------------------------


def test_a_window_already_in_front_is_not_acquired_again() -> None:
    inner = FakeInput(foreground=BOUND, acquire_procedure="direct")
    assert _guard(inner).acquire_foreground(BOUND) == "already"


def test_a_window_not_in_front_is_never_raised_mid_task() -> None:
    inner = FakeInput(foreground=OTHER)
    with pytest.raises(OSError, match="not_in_front"):
        _guard(inner).acquire_foreground(BOUND)
    assert inner.foreground == OTHER  # the inner acquire never ran


@pytest.mark.usefixtures("fast_clock")
def test_a_type_into_a_window_behind_another_is_refused_and_sends_nothing() -> None:
    inner = FakeInput(foreground=OTHER)
    result = _physical(_guard(inner)).execute(
        PhysicalAction(verb="type", element_id=1, text="hi"), _snapshot(_edit(), role="Edit")
    )
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "foreground_refused"
    assert inner.bursts == []
    assert inner.foreground == OTHER


# ---- v1's hit test on the v2 path: the root window under the point must be the bound one -----


@pytest.mark.usefixtures("fast_clock")
def test_a_click_whose_point_is_covered_is_refused_and_sends_nothing() -> None:
    inner = FakeInput(root_at=OTHER)
    result = _physical(_guard(inner)).execute(
        PhysicalAction(verb="click", element_id=1), _snapshot(FakeHandle())
    )
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "covered"
    assert inner.moves == []
    assert inner.clicks == []


@pytest.mark.usefixtures("fast_clock")
def test_a_type_whose_point_is_covered_is_refused_and_sends_nothing() -> None:
    inner = FakeInput(root_at=OTHER)
    result = _physical(_guard(inner)).execute(
        PhysicalAction(verb="type", element_id=1, text="hi"), _snapshot(_edit(), role="Edit")
    )
    assert result.evidence["detail"] == "covered"
    assert inner.bursts == []


# ---- the act port's hit test before a press ------------------------------------------------------


def test_a_press_whose_target_point_is_covered_is_refused_and_sends_nothing() -> None:
    desk = Desk()
    desk.root = OTHER
    rig = _port(desk=desk)
    result = rig.port.act(_dispatch("press", FIELD, key="Enter"))
    assert _refused(result, ErrorCode.ELEMENT_NOT_ACTIONABLE, "covered")
    assert _nothing_executed(rig)
    cx = (FIELD.box[0] + FIELD.box[2]) // 2
    cy = (FIELD.box[1] + FIELD.box[3]) // 2
    assert desk.hit_points == [(cx, cy)]


def test_a_press_whose_target_has_no_area_is_refused() -> None:
    """No point to test means nothing is known to be under it: never sent."""
    flat = dataclasses.replace(FIELD, box=(5, 5, 5, 5))
    rig = _port()
    last = rig.world.last
    assert last is not None
    shown = [flat if e.id == FIELD.id else e for e in last.snapshot.elements]
    rig.world.last = dataclasses.replace(
        last, snapshot=dataclasses.replace(last.snapshot, elements=shown)
    )
    result = rig.port.act(_dispatch("press", flat, key="Enter"))
    assert _refused(result, ErrorCode.ELEMENT_NOT_ACTIONABLE, "covered")
    assert _nothing_executed(rig)


def test_the_guard_desk_reads_the_root_window_under_a_point() -> None:
    inner = FakeInput(root_at=OTHER)
    desk = GuardDesk(
        _guard(inner), last_input=lambda: 0.0, takeover_token=lambda: "t", prove=lambda: None
    )
    assert desk.root_at(3, 4) == OTHER


def test_an_unreadable_root_window_is_never_the_bound_one() -> None:
    class Broken(FakeInput):
        def root_hwnd_at(self, _x: int, _y: int) -> int:
            raise OSError("no window")

    desk = GuardDesk(
        _guard(Broken()), last_input=lambda: 0.0, takeover_token=lambda: "t", prove=lambda: None
    )
    assert desk.root_at(3, 4) == 0


# ---- [V2.62] the cover check before any wait for a page ----------------------------------------


def _world_ports(screen: Screen, clock: Clock, cover: Callable[[], str | None]) -> WorldPorts:
    traced: list[str] = []
    ports = WorldPorts(
        poll_read=screen.poll_read,
        full_from=screen.full_from,
        interrupted=lambda: None,
        clock=clock,
        pause=clock.pause,
        touches=FirstTouches(),
        times=WorldTimes(),
        live=Live(),
        key=KEY,
        trace=traced.append,
        cover=cover,
    )
    ports.traced = traced  # type: ignore[attr-defined]  # the test reads what was traced
    return ports


@pytest.mark.parametrize("token", ["covered", "minimized", "cloaked", "cover_unread"])
def test_a_covered_window_ends_the_observe_before_any_read(token: str) -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _world_ports(screen, clock, lambda: token)
    with pytest.raises(WindowCovered) as covered:
        ports.observe()
    assert covered.value.token == token
    assert screen.polls == 0
    assert clock.pauses == []  # never waited for a page
    assert ports.traced == [f"cover_{token}"]  # type: ignore[attr-defined]


def test_an_uncovered_window_is_observed_as_before() -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _world_ports(screen, clock, lambda: None)
    assert ports.observe().snapshot.elements == observed().snapshot.elements
    assert screen.polls >= 1


def test_a_covered_window_ends_the_blank_page_hold_before_it_waits() -> None:
    clock = Clock()
    screen = Screen(observed(), clock)
    ports = _world_ports(screen, clock, lambda: "covered")
    with pytest.raises(WindowCovered):
        ports.settle(observed(), 10.0)
    assert screen.polls == 0
    assert clock.pauses == []


def test_the_task_ends_stopped_window_covered_never_page_not_ready() -> None:
    world = World(decisions=[])

    def covered() -> Observed:
        raise WindowCovered("covered")

    ports = dataclasses.replace(world.ports(), observe=covered)
    outcome = run_task(task(), ports)
    assert outcome.status == "stopped"
    assert render_reason(outcome.cause) == "window_covered"
    assert outcome.model_calls == 0


def test_a_cover_found_in_the_act_ports_fresh_look_ends_the_task_not_the_act() -> None:
    """The act port turns any failure into an uncertain act; a cover found in its look after an
    idle wait is the task's end, raised before anything is sent."""
    rig = _port(desk=Desk(last_input=1.0))  # the person was active: the port waits, then looks

    def covered() -> Observed:
        raise WindowCovered("covered")

    rig.world.observe = covered  # type: ignore[method-assign]  # the fresh look finds a cover
    with pytest.raises(WindowCovered):
        rig.port.act(_dispatch("click", FIELD))
    assert _nothing_executed(rig)
