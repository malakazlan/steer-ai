"""The takeover monitor's pure core (TK-01/02/05/19/20/26/27/28/29/31/32/37/43/44): what the
hook thread feeds it and what the loop asks it, with an injected clock and no Win32."""

from __future__ import annotations

import pytest

from steerai.probes.win_input import STEER_EXTRA_INFO
from steerai.session.monitor import (
    DEAD_BAND_PX,
    LLKHF_INJECTED,
    LLKHF_UP,
    LLMHF_INJECTED,
    PROBE_DEADLINE_S,
    PROBE_PERIOD_S,
    PROBE_VK,
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


# ---- [V2.57] LW-02: the monitor recognises the run's own tag, and only it --------------------


def test_the_core_counts_only_its_runs_tag_as_steer() -> None:
    from steerai.session.monitor import MonitorCore  # noqa: PLC0415 - this test only

    core = MonitorCore(started_at=0.0, extra_info=0x1234_5678)
    core.on_keyboard(flags=0, extra=0x1234_5678, now=1.0)
    assert core.last_user_input() == 0.0  # its own key: not the person's
    core.on_keyboard(flags=0, extra=STEER_EXTRA_INFO, now=2.0)  # another instance's key
    assert core.last_user_input() == 2.0


# ---- [V2.58] (2): v1's constant tag in a v2 run is another Steer, not a person ------------------


def test_another_steers_input_stops_the_same_way_but_is_not_a_person() -> None:
    from steerai.session.monitor import MonitorCore  # noqa: PLC0415 - this test only

    core = MonitorCore(started_at=0.0, extra_info=0x1234_5678)
    core.on_keyboard(flags=0, extra=STEER_EXTRA_INFO, now=2.0)
    assert core.last_user_input() == 2.0  # the idle rule and the takeover stop as for a person
    assert core.takeover_token() == "other_steer_input"
    assert core.counts().get("other_steer") == 1
    assert "user" not in core.counts()  # not a person at the keys
    core.on_keyboard(flags=0, extra=0, now=3.0)
    assert core.takeover_token() == "takeover"  # the latest foreign input was a person's


def test_another_steers_click_is_other_steer_input_too() -> None:
    from steerai.session.monitor import WM_LBUTTONDOWN, MonitorCore  # noqa: PLC0415

    core = MonitorCore(started_at=0.0, extra_info=0x1234_5678)
    core.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=STEER_EXTRA_INFO, pt=(5, 5), now=4.0)
    assert (core.last_user_input(), core.takeover_token()) == (4.0, "other_steer_input")


def test_in_v1_its_own_constant_is_still_its_own_input() -> None:
    from steerai.session.monitor import MonitorCore  # noqa: PLC0415 - this test only

    core = MonitorCore(started_at=0.0)
    core.on_keyboard(flags=0, extra=STEER_EXTRA_INFO, now=2.0)
    assert core.last_user_input() == 0.0
    assert core.takeover_token() == "takeover"
    assert "other_steer" not in core.counts()


# ---- [V2.57] LW-01: per-hook proof (v2 only; v1's single proof is unchanged) -------------------


def _per_hook(start: float = 100.0) -> MonitorCore:
    return MonitorCore(started_at=start, per_hook=True)


def _own_key(core: MonitorCore, now: float, *, vk: int = PROBE_VK, up: bool = True) -> None:
    """An own tagged key event; by default the probe's: a key-up of the probe key."""
    flags = LLKHF_INJECTED | (LLKHF_UP if up else 0)
    core.on_keyboard(flags=flags, extra=STEER_EXTRA_INFO, now=now, vk=vk)


def _own_move(core: MonitorCore, now: float) -> None:
    core.on_mouse(
        message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=STEER_EXTRA_INFO, pt=(1, 1), now=now
    )


def test_a_probe_seen_by_one_hook_only_is_a_lost_monitor() -> None:
    """The keyboard hook removed by the OS while the mouse probe still arrives: v1's single
    proof stays healthy; per hook, the unseen keyboard hook is lost past the deadline."""
    for per_hook, healthy in ((False, True), (True, False)):
        core = MonitorCore(started_at=100.0, per_hook=per_hook)
        core.probe_sent(now=101.0)
        _own_move(core, 101.1)
        assert core.healthy(now=101.0 + PROBE_DEADLINE_S + 0.01, thread_alive=True) is healthy
    assert core.lost_hooks(now=101.0 + PROBE_DEADLINE_S + 0.01) == ("keyboard",)


def test_a_probe_seen_by_both_hooks_is_proof() -> None:
    core = _per_hook()
    core.probe_sent(now=101.0)
    assert core.probe_seen() is False
    _own_move(core, 101.1)
    assert core.probe_seen() is False
    _own_key(core, 101.2)
    assert core.probe_seen() is True
    assert core.healthy(now=105.0, thread_alive=True) is True
    assert core.lost_hooks(now=105.0) == ()


def test_each_hook_unseen_past_the_deadline_is_named() -> None:
    core = _per_hook()
    core.probe_sent(now=101.0)
    _own_key(core, 101.1)
    assert core.lost_hooks(now=101.0 + PROBE_DEADLINE_S) == ()  # not yet past it
    assert core.lost_hooks(now=101.0 + PROBE_DEADLINE_S + 0.01) == ("mouse",)
    core = _per_hook()
    core.probe_sent(now=101.0)
    assert core.lost_hooks(now=102.0) == ("keyboard", "mouse")


def test_a_probe_is_due_when_either_hook_has_been_quiet() -> None:
    """Our own typing keeps the keyboard hook proven but says nothing about the mouse hook."""
    core = _per_hook()
    _own_key(core, 100.0 + PROBE_PERIOD_S - 0.1)
    assert core.probe_due(now=100.0 + PROBE_PERIOD_S) is True  # the mouse hook went quiet
    v1 = _core()
    _own_key(v1, 100.0 + PROBE_PERIOD_S - 0.1)
    assert v1.probe_due(now=100.0 + PROBE_PERIOD_S) is False  # v1: any own event is proof


def test_one_per_hook_probe_in_flight_at_a_time() -> None:
    core = _per_hook()
    core.probe_sent(now=101.0)
    _own_move(core, 101.1)
    assert core.probe_due(now=103.0) is False  # the keyboard half is still in flight
    _own_key(core, 101.2)
    assert core.probe_due(now=101.2 + PROBE_PERIOD_S) is True


# ---- #364 P1: proof is a delivery after the probe, by the probe's own key ----------------------


def test_an_own_event_in_the_same_tick_as_the_probe_is_not_proof() -> None:
    """time.monotonic is GetTickCount64 here (15.625 ms): the tail of the last act, stamped
    in the probe's own tick, must not prove the hooks."""
    core = _per_hook()
    _own_key(core, 101.0)
    _own_move(core, 101.0)
    core.probe_sent(now=101.0)
    assert core.probe_seen() is False
    _own_key(core, 101.0)
    _own_move(core, 101.0)
    assert core.probe_seen() is True  # delivered after the probe went out, same tick or not


def test_a_tagged_key_other_than_the_probe_key_up_is_not_keyboard_proof() -> None:
    core = _per_hook()
    core.probe_sent(now=101.0)
    _own_move(core, 101.1)
    _own_key(core, 101.1, vk=0x41)  # an act's own letter
    _own_key(core, 101.1, up=False)  # the probe key, but down
    assert core.probe_seen() is False
    assert core.lost_hooks(now=102.0) == ("keyboard",)
    _own_key(core, 101.2)
    assert core.probe_seen() is True


def test_a_late_event_of_an_overwritten_probe_is_not_proof_of_the_next() -> None:
    """maintain() sent probe A at act entry; prove_hooks sends B before A arrived: A's late
    events prove A, never B."""
    core = _per_hook()
    core.probe_sent(now=101.0)  # A
    core.probe_sent(now=101.05)  # B, A still in flight
    _own_key(core, 101.1)
    _own_move(core, 101.1)  # A's own events, late
    assert core.probe_seen() is False
    _own_key(core, 101.2)
    _own_move(core, 101.2)  # B's
    assert core.probe_seen() is True


# ---- [V2.59]: the probe deadline is derived from the system's hook timeout ----------------------


@pytest.mark.parametrize(
    ("raw", "timeout_ms"),
    [(None, None), ("300", 300), (300, 300), ("5000", 1000), ("x", None), (chr(0xB2), None),
     ("0", None), (True, None)],
)  # fmt: skip
def test_the_hook_timeout_is_parsed_as_windows_stores_it_and_capped(
    raw: object, timeout_ms: int | None
) -> None:
    from steerai.session.monitor import parse_hook_timeout  # noqa: PLC0415 - this test only

    assert parse_hook_timeout(raw) == timeout_ms


@pytest.mark.parametrize(("timeout_ms", "seconds"), [(None, 1.0), (300, 0.3), (1000, 1.0)])
def test_the_probe_deadline_is_the_hook_timeout_plus_the_measurer_margin(
    timeout_ms: int | None, seconds: float
) -> None:
    """[V2.59]: unset is the 1000 ms cap; the margin is named, never folded in."""
    from steerai.session.monitor import MEASURER_MARGIN_S, probe_deadline_s  # noqa: PLC0415

    assert probe_deadline_s(timeout_ms) == pytest.approx(seconds + MEASURER_MARGIN_S)


def test_a_per_hook_core_loses_a_hook_only_past_its_own_deadline() -> None:
    core = MonitorCore(started_at=100.0, per_hook=True, deadline_s=1.05)
    core.probe_sent(now=101.0)
    _own_move(core, 101.1)
    assert core.lost_hooks(now=101.0 + 1.05) == ()
    assert core.lost_hooks(now=101.0 + 1.06) == ("keyboard",)
