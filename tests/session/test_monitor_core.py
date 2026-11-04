"""The takeover monitor's pure core (TK-01/02/05/19/20/26/27/28/29/31/32/37/43/44): what the
hook thread feeds it and what the loop asks it, with an injected clock and no Win32."""

from __future__ import annotations

import pytest

from steerai.probes.win_input import STEER_EXTRA_INFO
from steerai.session.monitor import (
    DEAD_BAND_PX,
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    PROBE_DEADLINE_S,
    PROBE_PERIOD_S,
    WM_LBUTTONDOWN,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    MonitorCore,
)


def _core(start: float = 100.0) -> MonitorCore:
    return MonitorCore(started_at=start)


# ---- classification: only Steer's tag exempts an event ------------------------------------------


def test_our_tag_is_steer_whatever_the_injected_bit_says() -> None:
    core = _core()
    core.on_keyboard(flags=LLKHF_INJECTED, extra=STEER_EXTRA_INFO, now=101.0)
    core.on_keyboard(flags=0, extra=STEER_EXTRA_INFO, now=101.1)
    core.on_mouse(
        message=WM_LBUTTONDOWN, flags=LLMHF_INJECTED, extra=STEER_EXTRA_INFO, pt=(1, 1), now=101.2
    )
    assert core.counts() == {"steer": 3}
    assert core.last_user_input() == 100.0, "the start time: no user input seen yet"


def test_injected_keys_buttons_and_wheel_from_anyone_else_count_as_user_input() -> None:
    core = _core()
    core.on_keyboard(flags=LLKHF_INJECTED, extra=0, now=101.0)
    assert core.last_user_input() == 101.0
    core.on_mouse(message=WM_LBUTTONDOWN, flags=LLMHF_INJECTED, extra=7, pt=(5, 5), now=102.0)
    assert core.last_user_input() == 102.0
    core.on_mouse(message=WM_MOUSEWHEEL, flags=LLMHF_INJECTED, extra=0, pt=(5, 5), now=103.0)
    assert core.last_user_input() == 103.0
    assert core.counts() == {"injected_other": 3}


def test_a_real_key_is_user_input() -> None:
    core = _core()
    core.on_keyboard(flags=0, extra=0, now=105.0)
    assert core.last_user_input() == 105.0
    assert core.counts() == {"user": 1}


def test_an_injected_bare_move_is_counted_but_never_a_takeover() -> None:
    core = _core()
    core.on_mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=0, pt=(10, 10), now=101.0)
    core.on_mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=0, pt=(500, 500), now=101.5)
    assert core.last_user_input() == 100.0
    assert core.counts() == {"injected_other": 2}


# ---- the dead band on real mouse moves ----------------------------------------------------------


def test_the_first_real_move_counts_and_small_jitter_after_it_does_not() -> None:
    core = _core()
    core.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=0, pt=(100, 100), now=101.0)
    assert core.last_user_input() == 101.0
    core.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=0, pt=(100 + DEAD_BAND_PX, 100), now=102.0)
    assert core.last_user_input() == 101.0, "inside the box: a nudged desk, not a takeover"
    core.on_mouse(
        message=WM_MOUSEMOVE, flags=0, extra=0, pt=(100 + DEAD_BAND_PX + 1, 100), now=103.0
    )
    assert core.last_user_input() == 103.0


def test_our_own_move_updates_the_reference_so_a_nudge_after_our_click_is_not_a_takeover() -> None:
    core = _core()
    core.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=0, pt=(0, 0), now=101.0)
    core.on_mouse(
        message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=STEER_EXTRA_INFO, pt=(600, 400), now=102.0
    )
    core.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=0, pt=(601, 401), now=103.0)
    assert core.last_user_input() == 101.0


def test_negative_physical_coordinates_use_absolute_distance() -> None:
    core = _core()
    core.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=0, pt=(-1920, -10), now=101.0)
    core.on_mouse(
        message=WM_MOUSEMOVE, flags=0, extra=0, pt=(-1920 - DEAD_BAND_PX - 1, -10), now=102.0
    )
    assert core.last_user_input() == 102.0


def test_wheel_and_buttons_count_even_with_the_cursor_still() -> None:
    core = _core()
    core.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=0, pt=(50, 50), now=101.0)
    core.on_mouse(message=WM_MOUSEWHEEL, flags=0, extra=0, pt=(50, 50), now=102.0)
    assert core.last_user_input() == 102.0
    core.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=0, pt=(50, 50), now=103.0)
    assert core.last_user_input() == 103.0


# ---- the ask bracket: the person's answer is not a takeover ---------------------------------


def test_input_while_an_ask_is_open_is_an_answer_not_user_input() -> None:
    core = _core()
    core.mark_ask_open()
    core.on_keyboard(flags=0, extra=0, now=101.0)
    core.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=0, pt=(1, 1), now=101.5)
    core.mark_ask_closed()
    assert core.last_user_input() == 100.0
    assert core.counts() == {"answer": 2}
    core.on_keyboard(flags=0, extra=0, now=102.0)
    assert core.last_user_input() == 102.0


# ---- the kill flag ----------------------------------------------------------------------------


def test_the_kill_flag_is_sticky() -> None:
    core = _core()
    assert core.kill_requested() is False
    core.on_hotkey()
    core.on_hotkey()
    assert core.kill_requested() is True
    core.on_keyboard(flags=0, extra=0, now=200.0)
    assert core.kill_requested() is True


# ---- health: our own injections prove the hook; when idle a self-probe is due ---------------


def test_healthy_while_our_own_events_keep_arriving() -> None:
    core = _core()
    core.on_keyboard(flags=LLKHF_INJECTED, extra=STEER_EXTRA_INFO, now=100.5)
    assert core.probe_due(now=100.9) is False
    assert core.healthy(now=100.9, thread_alive=True) is True


def test_a_probe_is_due_after_a_quiet_period_and_a_missed_probe_is_unhealthy() -> None:
    core = _core()
    assert core.probe_due(now=100.0 + PROBE_PERIOD_S - 0.01) is False
    assert core.probe_due(now=100.0 + PROBE_PERIOD_S) is True
    core.probe_sent(now=101.0)
    assert core.probe_due(now=101.5) is False, "one probe in flight"
    assert core.healthy(now=101.0 + PROBE_DEADLINE_S, thread_alive=True) is True
    assert core.healthy(now=101.0 + PROBE_DEADLINE_S + 0.01, thread_alive=True) is False
    assert core.detail() == "monitor_lost"


def test_a_probe_seen_in_time_keeps_the_monitor_healthy_and_schedules_the_next() -> None:
    core = _core()
    core.probe_sent(now=101.0)
    core.on_mouse(
        message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=STEER_EXTRA_INFO, pt=(1, 1), now=101.1
    )
    assert core.healthy(now=102.0, thread_alive=True) is True
    assert core.probe_due(now=101.1 + PROBE_PERIOD_S - 0.01) is False
    assert core.probe_due(now=101.1 + PROBE_PERIOD_S) is True


def test_a_dead_thread_a_callback_error_or_a_loop_error_is_unhealthy() -> None:
    core = _core()
    assert core.healthy(now=100.1, thread_alive=False) is False
    core = _core()
    core.on_callback_error()
    assert core.healthy(now=100.1, thread_alive=True) is False
    assert core.counts().get("callback_errors") == 1
    core = _core()
    core.on_loop_error()
    assert core.healthy(now=100.1, thread_alive=True) is False


# ---- what reaches a trace -------------------------------------------------------------------


def test_counts_carry_kinds_only_never_codes_positions_or_times() -> None:
    core = _core()
    core.on_keyboard(flags=0, extra=0, now=101.0)
    core.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=0, pt=(123, 456), now=102.0)
    counts = core.counts()
    assert set(counts) <= {"user", "injected_other", "steer", "answer", "callback_errors"}
    assert all(isinstance(v, int) for v in counts.values())
    with pytest.raises(AttributeError):
        _ = core.events  # type: ignore[attr-defined]  # there is no event list to leak
