"""[V2.62] addendum (diagnosis 2026-11-15): a Chrome that starts behind another window stays
hidden to its own occlusion tracker after the start's raise, even with the window in front and
uncovered; only a minimise and restore of our own window brings its page back (measured once per
nudge: raise alone, HWND_TOP and a second raise left it hidden for 10 s; minimise and restore
showed the page at once). So `bring_to_front` nudges, but only a window that was behind or not
the foreground before the raise, never one already in front; a maximised window comes back
maximised; a minimised or cloaked one is still refused, never restored."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.live_world import NUDGE_PAUSE_S, LiveWorld
from steerai.world.cover import CoverFacts
from tests.loop2.test_live_world import Platform, _config

BOUND = 16
OTHER = 0x99
CLEAR = CoverFacts(
    minimized=False, cloaked=False, rect=(0, 0, 1200, 900), above=(), screen=(0, 0, 1920, 1080)
)
COVERED = dataclasses.replace(CLEAR, above=((0, 0, 1920, 1040),))


def _platform(*, in_front: bool, covers: list[CoverFacts] | None = None) -> Platform:
    platform = Platform()
    platform.fake_input.foreground = BOUND if in_front else OTHER
    platform.covers = list(covers or [])
    return platform


def _start(platform: Platform) -> LiveWorld:
    world = LiveWorld(_config(), platform)
    world.bring_to_front()
    return world


def test_a_window_already_in_front_and_uncovered_is_never_nudged() -> None:
    platform = _platform(in_front=True)
    _start(platform).close()
    assert platform.shown == []


def test_a_window_behind_another_is_nudged_after_the_raise() -> None:
    platform = _platform(in_front=False, covers=[COVERED, CLEAR])
    world = _start(platform)
    world.close()
    assert platform.shown == ["minimize", "restore"]
    assert world.front_nudged is True
    assert platform.fake_input.foreground == BOUND


def test_a_window_not_the_foreground_but_uncovered_is_nudged() -> None:
    platform = _platform(in_front=False)
    _start(platform).close()
    assert platform.shown == ["minimize", "restore"]


def test_a_window_in_front_but_covered_is_nudged() -> None:
    """A topmost window over the foreground one: covered although in front."""
    platform = _platform(in_front=True, covers=[COVERED, CLEAR])
    _start(platform).close()
    assert platform.shown == ["minimize", "restore"]


def test_the_nudge_pauses_between_minimise_and_restore() -> None:
    config = _config()
    platform = _platform(in_front=False)
    before = config.clock()
    world = LiveWorld(config, platform)
    world.bring_to_front()
    world.close()
    assert platform.show_times[1] - platform.show_times[0] >= NUDGE_PAUSE_S - 1e-9
    assert config.clock() > before


def test_a_maximised_window_comes_back_maximised() -> None:
    platform = _platform(in_front=False)
    platform.maximized_now = True
    platform.restore_keeps_maximized = False  # a restore that lands it normal
    _start(platform).close()
    assert platform.shown == ["minimize", "restore", "maximize"]
    assert platform.maximized_now is True


def test_a_maximised_window_restored_maximised_is_left_alone() -> None:
    platform = _platform(in_front=False)
    platform.maximized_now = True
    platform.restore_keeps_maximized = True
    _start(platform).close()
    assert platform.shown == ["minimize", "restore"]


def test_a_normal_window_is_never_maximised() -> None:
    platform = _platform(in_front=False)
    _start(platform).close()
    assert "maximize" not in platform.shown
    assert platform.maximized_now is False


@pytest.mark.parametrize(
    "facts",
    [dataclasses.replace(CLEAR, minimized=True), dataclasses.replace(CLEAR, cloaked=True)],
    ids=["minimized", "cloaked"],
)
def test_a_minimised_or_cloaked_window_is_refused_never_restored(facts: CoverFacts) -> None:
    platform = _platform(in_front=False)
    platform.cover = facts
    world = LiveWorld(_config(), platform)
    try:
        with pytest.raises(SteerError) as refused:
            world.bring_to_front()
    finally:
        world.close()
    assert (refused.value.code, refused.value.message) == (ErrorCode.FOCUS_LOST, "window_covered")
    assert platform.shown == []


def test_a_window_still_covered_after_the_nudge_is_refused() -> None:
    platform = _platform(in_front=False, covers=[COVERED, COVERED])
    platform.cover = COVERED
    world = LiveWorld(_config(), platform)
    try:
        with pytest.raises(SteerError) as refused:
            world.bring_to_front()
    finally:
        world.close()
    assert refused.value.message == "window_covered"
    assert platform.shown == ["minimize", "restore"]


def test_a_nudge_that_fails_refuses_the_start() -> None:
    platform = _platform(in_front=False)
    platform.show_error = OSError("ShowWindow failed")
    world = LiveWorld(_config(), platform)
    try:
        with pytest.raises(SteerError) as refused:
            world.bring_to_front()
    finally:
        world.close()
    assert (refused.value.code, refused.value.message) == (ErrorCode.FOCUS_LOST, "nudge_refused")


def test_a_kill_during_the_nudge_is_left_to_the_task_and_nothing_more_is_done() -> None:
    platform = _platform(in_front=False)

    def killed_after_minimise(command: str) -> None:
        if command == "minimize":
            platform.monitor.killed = True

    platform.on_show = killed_after_minimise
    world = LiveWorld(_config(), platform)
    world.bring_to_front()  # no refusal: the switch token is the task's own
    world.close()
    assert platform.shown == ["minimize", "restore"]  # never left minimised
