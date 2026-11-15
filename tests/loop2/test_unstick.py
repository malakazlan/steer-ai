"""[V2.64] the start's nudge, the last resort ([V2.62] diagnosis 2026-11-15, Chrome 157): after
the raise, the start waits a bounded time for the page. Only a window still showing a browser
frame with no Document while in front and uncovered is nudged, once; its placement is put
back. If the page is still missing, the task does not start (`page_hidden`). The gentle nudge (a
one-pixel move and back) is the default for a window in its normal state (normal or snapped:
10 of 10 in the evidence run); any other falls back to minimise and restore. The restore can
change only the placement's minimised position (unset -1,-1 to the parking point, measured
2026-11-15), so the put-back is checked on show state, normal position and rect."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.live_world import (
    DOCUMENT_POLL_S,
    DOCUMENT_WAIT_S,
    WPF_SETMINPOSITION,
    LiveWorld,
    WindowPlacement,
)
from steerai.world.cover import CoverFacts
from tests.action.test_physical import FakeInput
from tests.loop2.test_live_world import Platform, _config
from tests.loop2.world_fakes import BAR, DOC, PAGE, TAB, snapshot

BOUND = 16
FOREVER = 10**9
OTHER = 0x99
CLEAR = CoverFacts(
    minimized=False, cloaked=False, rect=(0, 0, 1200, 900), above=(), screen=(0, 0, 1920, 1080)
)
COVERED = dataclasses.replace(CLEAR, above=((0, 0, 1920, 1040),))
FRAME = snapshot([TAB, BAR])
"""A browser frame with no Document: its address bar is read, its page is not exposed."""
EMPTY_DOCUMENT = snapshot([DOC, TAB, BAR])
"""A browser frame whose Document is there with nothing under it yet (a slow first paint)."""
NORMAL = WindowPlacement(
    flags=0, show_cmd=1, normal=(40, 40, 1240, 940), min_pos=(-1, -1), max_pos=(-1, -1)
)
MAXIMIZED = dataclasses.replace(NORMAL, flags=2, show_cmd=3)
MOVED = (41, 40, 1241, 940)
SAVED = (40, 40, 1240, 940)


def _platform(*, blank_reads: int = 0, page_after_nudge: bool = True) -> Platform:
    """A window in front and uncovered whose first `blank_reads` reads show only the frame; a
    nudge, when it comes, shows the page (or not)."""
    platform = Platform()
    platform.frame_screen = FRAME
    platform.blank_reads_left = blank_reads
    platform.page_after_nudge = page_after_nudge
    platform.address_bar_value = "127.0.0.1/b"
    return platform


def _start(platform: Platform, **kw: object) -> LiveWorld:
    world = LiveWorld(_config(), platform)
    try:
        world.bring_to_front(**kw)  # type: ignore[arg-type]  # a test's nudge choice
    finally:
        world.close()
    return world


def _refused(platform: Platform, **kw: object) -> SteerError:
    world = LiveWorld(_config(), platform)
    try:
        with pytest.raises(SteerError) as refused:
            world.bring_to_front(**kw)  # type: ignore[arg-type]  # a test's nudge choice
    finally:
        world.close()
    return refused.value


# ---- no nudge -----------------------------------------------------------------------------------


def test_a_page_at_once_is_never_nudged() -> None:
    platform = _platform()
    world = _start(platform)
    assert (platform.nudges, world.front_nudged) == ([], None)


def test_a_page_that_appears_within_the_wait_is_never_nudged() -> None:
    platform = _platform(blank_reads=3)
    world = _start(platform)
    assert (platform.nudges, world.front_nudged) == ([], None)


def test_a_page_that_appears_late_in_the_wait_is_never_nudged() -> None:
    """#400 REVISE 2: the page first seen at 1.9 s or later, still inside the wait."""
    platform = _platform(blank_reads=int(DOCUMENT_WAIT_S / DOCUMENT_POLL_S))
    world = _start(platform)
    assert platform.read_times[-1] - platform.read_times[0] >= 1.9
    assert (platform.nudges, world.front_nudged) == ([], None)


def test_an_empty_document_past_the_wait_is_never_nudged() -> None:
    """#400 REVISE 2: only the frame-only shape (no Document) is the stall; a Document with
    nothing under it is a slow first paint, left to the task's own first observation."""
    platform = _platform(blank_reads=FOREVER)
    platform.frame_screen = EMPTY_DOCUMENT
    world = _start(platform)
    assert (platform.nudges, world.front_nudged) == ([], None)


def test_a_native_window_with_no_address_bar_is_never_nudged() -> None:
    """No Document and no address bar is a native window, not a hidden page."""
    platform = _platform(blank_reads=FOREVER)
    platform.address_bar_value = None
    _start(platform)
    assert platform.nudges == []


def test_not_in_front_after_the_raise_refuses_before_any_nudge() -> None:
    class Stubborn(FakeInput):
        def acquire_foreground(self, _hwnd: int) -> str:
            return "direct"  # says it worked; the foreground stayed elsewhere

    platform = _platform(blank_reads=FOREVER)
    platform.fake_input = Stubborn(foreground=OTHER)
    refused = _refused(platform)
    assert refused.message == "not_in_front"
    assert platform.nudges == []


def test_covered_after_the_raise_refuses_before_any_nudge() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.cover = COVERED
    assert _refused(platform).message == "window_covered"
    assert platform.nudges == []


# ---- one nudge, the last resort -----------------------------------------------------------------


def test_a_frame_only_window_in_front_is_nudged_once_after_the_wait() -> None:
    platform = _platform(blank_reads=FOREVER)
    world = _start(platform)
    assert platform.nudges == ["move:+1", "move:-1"]
    assert world.front_nudged == "move1px"
    waited = platform.read_times[platform.nudge_read_index - 1] - platform.read_times[0]
    assert waited >= DOCUMENT_WAIT_S - 0.5  # the bounded wait came first


def test_the_exact_placement_is_put_back_with_its_minimised_position() -> None:
    """#400 REVISE 3: WPF_SETMINPOSITION, so the minimised position is set back too."""
    platform = _platform(blank_reads=FOREVER)
    platform.placement = MAXIMIZED
    _start(platform)
    assert platform.placements_set == [
        dataclasses.replace(MAXIMIZED, flags=MAXIMIZED.flags | WPF_SETMINPOSITION)
    ]


def test_a_normal_window_moved_by_the_restore_goes_back_to_its_exact_rect() -> None:
    """A snapped window's placement holds its pre-snap rect: the rect read before is put back."""
    platform = _platform(blank_reads=FOREVER)
    platform.rect_now = (0, 0, 960, 1040)
    platform.rect_after_placement = (40, 40, 1240, 940)
    _start(platform, nudge="minrestore")
    assert platform.rects_set[-1] == (0, 0, 960, 1040)


def test_the_page_still_missing_after_the_one_nudge_refuses_the_start() -> None:
    platform = _platform(blank_reads=FOREVER, page_after_nudge=False)
    refused = _refused(platform)
    assert (refused.code, refused.message) == (ErrorCode.FOCUS_LOST, "page_hidden")
    assert platform.nudges == ["move:+1", "move:-1"]  # exactly one nudge, no loop


# ---- the gentle nudge -----------------------------------------------------------------------


def test_the_gentle_nudge_moves_a_normal_window_one_pixel_and_back() -> None:
    platform = _platform(blank_reads=FOREVER)
    world = _start(platform, nudge="move1px")
    assert platform.nudges == ["move:+1", "move:-1"]
    assert world.front_nudged == "move1px"


def test_the_gentle_nudge_falls_back_to_minimise_and_restore_on_a_maximised_window() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.placement = MAXIMIZED
    world = _start(platform, nudge="move1px")
    assert platform.nudges == ["minimize", "placement"]
    assert world.front_nudged == "minrestore"


def test_the_default_nudge_is_the_gentle_one_on_a_normal_window() -> None:
    platform = _platform(blank_reads=FOREVER)
    world = _start(platform)
    assert platform.nudges == ["move:+1", "move:-1"]
    assert world.front_nudged == "move1px"


def test_the_default_nudge_on_a_maximised_window_is_minimise_and_restore() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.placement = MAXIMIZED
    world = _start(platform)
    assert platform.nudges == ["minimize", "placement"]
    assert world.front_nudged == "minrestore"


def test_a_kill_during_the_gentle_nudge_still_moves_the_window_back() -> None:
    platform = _platform(blank_reads=FOREVER)

    def killed_after_the_move(dx: int) -> None:
        if dx > 0:
            platform.monitor.killed = True

    platform.on_move = killed_after_the_move
    world = LiveWorld(_config(), platform)
    world.bring_to_front()
    world.close()
    assert platform.nudges == ["move:+1", "move:-1"]


def test_a_gentle_nudge_move_that_fails_refuses_the_start() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.rect_errors = [OSError("SetWindowPos failed")]
    refused = _refused(platform)
    assert (refused.code, refused.message) == (ErrorCode.FOCUS_LOST, "nudge_refused")
    assert platform.rects_set == []


def test_the_gentle_nudge_puts_back_the_saved_rect_not_a_shifted_reread() -> None:
    """#400 REVISE 4: a window moved during the pause goes back to the rect saved before the
    nudge, never to the moved rect less one pixel."""
    platform = _platform(blank_reads=FOREVER)

    def moved_by_someone(dx: int) -> None:
        if dx == 1:
            platform.rect_now = (300, 300, 1500, 1200)

    platform.on_move = moved_by_someone
    _start(platform)
    assert platform.rects_set == [MOVED, SAVED]


# ---- a failed restore (#400 REVISE 1) -----------------------------------------------------------


def test_a_gentle_restore_that_fails_once_is_retried_once() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.rect_errors = [None, OSError("SetWindowPos failed")]
    world = _start(platform)
    assert platform.rect_attempts == [MOVED, SAVED, SAVED]
    assert platform.rects_set == [MOVED, SAVED]
    assert world.front_nudged == "move1px"


def test_a_gentle_restore_that_fails_twice_refuses_with_restore_failed() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.rect_errors = [None, OSError("SetWindowPos failed"), OSError("SetWindowPos failed")]
    refused = _refused(platform)
    assert (refused.code, refused.message) == (ErrorCode.FOCUS_LOST, "restore_failed")
    assert platform.rect_attempts == [MOVED, SAVED, SAVED]


def test_a_placement_restore_that_fails_once_is_retried_once() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.placement = MAXIMIZED
    platform.placement_errors = [OSError("SetWindowPlacement failed")]
    world = _start(platform)
    assert platform.placement_attempts == 2
    assert len(platform.placements_set) == 1
    assert world.front_nudged == "minrestore"


def test_a_placement_restore_that_fails_twice_refuses_with_restore_failed() -> None:
    """Never a silent `nudge_refused` with the window left minimised."""
    platform = _platform(blank_reads=FOREVER)
    platform.placement = MAXIMIZED
    platform.placement_errors = [OSError("failed"), OSError("failed")]
    refused = _refused(platform)
    assert (refused.code, refused.message) == (ErrorCode.FOCUS_LOST, "restore_failed")
    assert platform.placement_attempts == 2


def test_a_snapped_rect_restore_that_fails_twice_refuses_with_restore_failed() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.rect_now = (0, 0, 960, 1040)
    platform.rect_after_placement = (40, 40, 1240, 940)
    platform.rect_errors = [OSError("failed"), OSError("failed")]
    refused = _refused(platform, nudge="minrestore")
    assert (refused.code, refused.message) == (ErrorCode.FOCUS_LOST, "restore_failed")


# ---- a second monitor, unmeasured (#400 REVISE 3) -----------------------------------------------


def test_a_window_off_the_primary_monitor_is_never_nudged() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.primary = False
    refused = _refused(platform)
    assert (refused.code, refused.message) == (ErrorCode.FOCUS_LOST, "page_hidden")
    assert platform.nudges == []


def test_a_monitor_read_that_fails_is_not_the_primary_monitor() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.primary_error = OSError("GetMonitorInfo failed")
    refused = _refused(platform)
    assert refused.message == "page_hidden"
    assert platform.nudges == []


def test_a_window_off_the_primary_monitor_with_its_page_is_left_alone() -> None:
    platform = _platform()
    platform.primary = False
    world = _start(platform)
    assert (platform.nudges, world.front_nudged) == ([], None)


# ---- the switch and failures ----------------------------------------------------------------


def test_a_kill_during_the_wait_stops_the_start_without_a_nudge() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.kill_after_reads = 2
    world = LiveWorld(_config(), platform)
    world.bring_to_front()
    world.close()
    assert platform.nudges == []


def test_a_kill_during_the_nudge_still_puts_the_placement_back() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.placement = MAXIMIZED

    def killed_after_minimise(command: str) -> None:
        if command == "minimize":
            platform.monitor.killed = True

    platform.on_show = killed_after_minimise
    world = LiveWorld(_config(), platform)
    world.bring_to_front()
    world.close()
    assert platform.nudges == ["minimize", "placement"]


def test_a_nudge_call_that_fails_refuses_the_start() -> None:
    platform = _platform(blank_reads=FOREVER)
    platform.placement = MAXIMIZED
    platform.show_error = OSError("ShowWindow failed")
    refused = _refused(platform)
    assert (refused.code, refused.message) == (ErrorCode.FOCUS_LOST, "nudge_refused")


def test_the_trigger_names_no_application() -> None:
    """[V2.64] (4): the trigger is a shape (a frame with no Document, in front, uncovered), not
    a name: the module holds no browser or site name."""
    import inspect  # noqa: PLC0415 - this test only

    from steerai.loop2 import live_world  # noqa: PLC0415

    source = inspect.getsource(live_world.LiveWorld._needs_nudge)
    assert "chrome" not in source.lower()
    assert PAGE  # the fake's page has a Document with children
