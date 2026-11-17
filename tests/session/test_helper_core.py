"""[V2.60] hook helper, PR 2 (packet rev 2b §2.1, §3.2, §5.2, §5.5, §5.6, K15): the helper's pure
core. It is what the helper's hook callbacks and loop call. It writes only counters, QPC times,
enums, a nonce and a pid into the status words, and reads `ask_depth` directly each callback."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.session.helper_core import (
    KILL_ACK_S,
    KILL_EXIT_CODE,
    KILL_TERMINATE_S,
    MAX_ASK_DEPTH,
    SLEEP_DIVERGENCE_MS,
    HelperCore,
    config_error,
    write_config,
)
from steerai.session.hook_classifier import (
    LLKHF_INJECTED,
    LLKHF_UP,
    LLMHF_INJECTED,
    V1_TAG,
    WM_LBUTTONDOWN,
    WM_MOUSEMOVE,
)
from steerai.session.hook_layout import (
    ERROR_LAYOUT,
    KIND_OTHER_STEER,
    KIND_TAKEOVER,
    STATE_CLOSED,
    STATE_FAILED,
    STATE_READY,
    STATE_STARTING,
    Words,
    config_size,
    config_words,
    status_size,
    status_words,
    unpack_last_user,
)

FREQ = 10_000_000  # QPC ticks per second (10 MHz, as on this machine's hypervisor-backed QPC)
MS = FREQ // 1000
RUN_TAG = 0x1234_5678
PT = 0x3333_0001
"""A probe's tag, as Steer writes it into the `probe_tag` word before the probe goes out."""
PROBE = 0x0E
NONCE = (0x1111_2222_3333_4444, 0x5555_6666_7777_8888)


def _config() -> Words:
    words = config_words(memoryview(bytearray(config_size())))
    write_config(
        words,
        run_tag=RUN_TAG,
        probe_vk=PROBE,
        kill_mods=0x7,
        kill_vk=0x53,
        dead_band_px=4,
        heartbeat_ms=100,
        hook_timeout_ms=1000,
        nonce=NONCE,
    )
    return words


def _core(config: Words | None = None) -> tuple[HelperCore, Words, Words]:
    cfg = _config() if config is None else config
    status = status_words(memoryview(bytearray(status_size())))
    core = HelperCore(config=cfg, status=status, freq=FREQ, started_qpc=1_000 * MS, pid=4242)
    return core, cfg, status


def _key(
    core: HelperCore,
    *,
    extra: int,
    vk: int = 0x41,
    up: bool = False,
    at_ms: int,
    injected: bool = False,
) -> bool:
    return core.keyboard(
        flags=(LLKHF_UP if up else 0) | (LLKHF_INJECTED if injected else 0),
        extra=extra,
        vk=vk,
        event_ms=at_ms,
        entry_ms=at_ms,
        entry_qpc=at_ms * MS,
    )


# ---- the config ------------------------------------------------------------------------------


def test_a_written_config_is_valid() -> None:
    assert config_error(_config()) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("magic", 1),
        ("layout_version", 99),
        ("layout_version", 2),  # the helper of the version before [V2.80]
        ("run_tag", 0),
        ("run_tag", V1_TAG),
        ("run_tag", 1 << 32 | RUN_TAG),  # wider than the 32-bit tag a probe's number sits above
        ("probe_vk", 0x41),
    ],
)
def test_a_config_that_was_changed_or_is_unusable_is_refused(field: str, value: int) -> None:
    """F15: the helper checks magic, version and the CRC before it hooks anything."""
    cfg = _config()
    cfg.set(field, value)
    assert config_error(cfg) == ERROR_LAYOUT


def test_steers_own_mutable_words_are_outside_the_crc() -> None:
    cfg = _config()
    cfg.set("ask_depth", 3)
    cfg.set("kill_ack", 1)
    assert config_error(cfg) == 0


# ---- lifecycle ---------------------------------------------------------------------------------


def test_the_helper_starts_then_reports_ready_with_its_pid_and_the_nonce() -> None:
    core, _cfg, status = _core()
    assert status.get("state") == STATE_STARTING
    core.ready(qpc=1_000 * MS, unbiased_100ns=1_000 * 10_000)
    assert status.get("state") == STATE_READY
    assert status.get("pid") == 4242
    assert (status.get("nonce_echo_lo"), status.get("nonce_echo_hi")) == NONCE


def test_a_failure_and_a_close_are_states_with_a_code() -> None:
    core, _cfg, status = _core()
    core.failed(ERROR_LAYOUT)
    assert (status.get("state"), status.get("error")) == (STATE_FAILED, ERROR_LAYOUT)
    core.closed()
    assert status.get("state") == STATE_CLOSED


# ---- K1: per-hook delivery, §5.2: per-hook order --------------------------------------------


def test_the_probe_keys_key_up_is_a_keyboard_delivery_and_wakes_steer() -> None:
    core, cfg, status = _core()
    cfg.set("probe_tag", PT)  # Steer writes it before it injects the probe
    assert not _key(core, extra=RUN_TAG, vk=PROBE, up=False, at_ms=2_000)
    assert _key(core, extra=PT, vk=PROBE, up=True, at_ms=2_001, injected=True)
    assert status.get("proof_keyboard") == PT
    assert status.get("last_own_qpc_keyboard") == 2_001 * MS


def test_a_probe_move_is_a_mouse_delivery_by_its_tag() -> None:
    core, cfg, status = _core()
    cfg.set("probe_tag", PT)
    assert core.mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=PT, pt=(1, 1),
                      event_ms=2_000, entry_ms=2_000, entry_qpc=2_000 * MS)  # fmt: skip
    assert status.get("proof_mouse") == PT
    assert status.get("proof_keyboard") == 0


def test_an_own_action_mouse_event_is_no_mouse_delivery_and_wakes_nothing() -> None:
    """#492, [V2.81]: only the probe's tag is proof; the helper's word stays put."""
    core, cfg, status = _core()
    cfg.set("probe_tag", PT)
    for message in (WM_MOUSEMOVE, WM_LBUTTONDOWN):
        assert not core.mouse(message=message, flags=0, extra=RUN_TAG, pt=(1, 1), event_ms=2_000,
                              entry_ms=2_000, entry_qpc=2_000 * MS)  # fmt: skip
    assert status.get("proof_mouse") == 0
    assert status.get("last_own_qpc_mouse") == 2_000 * MS


def test_a_new_probe_tag_retires_the_old_one_which_is_then_own_and_stale() -> None:
    """The helper reads the word before every event: the old probe's late event proves nothing
    and is never the person's."""
    core, cfg, status = _core()
    cfg.set("probe_tag", PT)
    assert core.mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=PT, pt=(1, 1),
                      event_ms=2_000, entry_ms=2_000, entry_qpc=2_000 * MS)  # fmt: skip
    cfg.set("probe_tag", PT + 1)
    assert not core.mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=PT, pt=(1, 1),
                          event_ms=2_001, entry_ms=2_001, entry_qpc=2_001 * MS)  # fmt: skip
    assert status.get("proof_mouse") == PT  # unchanged: the new probe has not arrived
    assert status.get("last_user") == 0
    assert core.mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=PT + 1, pt=(1, 1),
                      event_ms=2_002, entry_ms=2_002, entry_qpc=2_002 * MS)  # fmt: skip
    assert status.get("proof_mouse") == PT + 1


@pytest.mark.parametrize("hook", ["keyboard", "mouse"])
def test_a_persons_event_is_published_before_that_hooks_later_probe(hook: str) -> None:
    """§5.2 (rev 2 P1), per hook: the person's event's time is written before the delivery
    count of that hook's later probe moves, so a reader that sees the count sees the time."""
    core, cfg, status = _core()
    cfg.set("probe_tag", PT)
    seen: list[tuple[int, int]] = []

    class Spy:
        def __init__(self, inner: Words) -> None:
            self.inner = inner

        def get(self, name: str) -> int:
            return self.inner.get(name)

        def set(self, name: str, value: int) -> None:
            self.inner.set(name, value)
            seen.append((status.get(f"proof_{hook}"), status.get(f"last_user_qpc_{hook}")))

        def bump_bucket(self, histogram: str, bucket: int) -> None:
            self.inner.bump_bucket(histogram, bucket)

    core._status = Spy(status)  # type: ignore[assignment]  # a spy on every store, in order
    if hook == "keyboard":
        _key(core, extra=0, at_ms=3_000)
        _key(core, extra=PT, vk=PROBE, up=True, at_ms=3_001, injected=True)
    else:
        core.mouse(message=WM_LBUTTONDOWN, flags=0, extra=0, pt=(9, 9), event_ms=3_000,
                   entry_ms=3_000, entry_qpc=3_000 * MS)  # fmt: skip
        core.mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=PT, pt=(9, 9),
                   event_ms=3_001, entry_ms=3_001, entry_qpc=3_001 * MS)  # fmt: skip
    assert all(user == 3_000 * MS for delivered, user in seen if delivered == PT)
    assert seen[-1] == (PT, 3_000 * MS)


# ---- K5/K6/K7: the last user word, the kind, the ask ------------------------------------------


def test_the_last_user_word_carries_the_time_and_who() -> None:
    core, _cfg, status = _core()
    _key(core, extra=0, at_ms=4_000)
    assert unpack_last_user(status.get("last_user")) == (4_000 * MS, KIND_TAKEOVER)
    _key(core, extra=V1_TAG, at_ms=4_010)
    assert unpack_last_user(status.get("last_user")) == (4_010 * MS, KIND_OTHER_STEER)
    assert status.get("last_user_qpc_keyboard") == 4_010 * MS
    assert status.get("count_other_steer") == 1


def test_input_while_steers_ask_word_is_set_is_the_answer() -> None:
    """K7: the callback reads `ask_depth` directly, every event; it never waits on Steer."""
    core, cfg, status = _core()
    cfg.set("ask_depth", 1)
    _key(core, extra=0, at_ms=5_000)
    assert status.get("last_user") == 0
    assert status.get("count_answer") == 1
    cfg.set("ask_depth", 0)
    _key(core, extra=0, at_ms=5_001)
    assert unpack_last_user(status.get("last_user"))[0] == 5_001 * MS


# ---- K4: the kill --------------------------------------------------------------------------


def test_the_hot_key_sets_kill_once_and_it_never_clears() -> None:
    core, _cfg, status = _core()
    core.hotkey(qpc=1_500 * MS)
    core.hotkey(qpc=1_600 * MS)
    assert status.get("kill") == 1


# ---- K15: nothing from the event's content reaches the words ----------------------------------

VK_SENTINEL = 0xA7
PT_SENTINEL = (31_337, 27_182)


def test_no_word_holds_a_key_code_or_a_position() -> None:
    core, _cfg, status = _core()
    _key(core, extra=0, vk=VK_SENTINEL, at_ms=6_000)
    core.mouse(message=WM_MOUSEMOVE, flags=0, extra=0, pt=PT_SENTINEL, event_ms=6_001,
               entry_ms=6_001, entry_qpc=6_001 * MS)  # fmt: skip
    values = {status.get(n) for n in status.names()}
    assert not {VK_SENTINEL, *PT_SENTINEL} & values


# ---- §5.5: gaps, run time, latency ------------------------------------------------------------


def test_a_slow_callback_is_a_gap_event_and_its_max_is_kept() -> None:
    core, _cfg, status = _core()
    core.finished(entry_qpc=7_000 * MS, exit_qpc=7_000 * MS + 1_200 * MS)  # 1.2 s > 1 s
    assert status.get("gap_events") == 1
    assert status.get("max_callback_run_qpc") == 1_200 * MS
    core.finished(entry_qpc=8_000 * MS, exit_qpc=8_000 * MS + 3 * MS)
    assert status.get("gap_events") == 1
    assert status.get("run_time_events") == 2


def test_a_late_event_is_a_gap_event() -> None:
    core, _cfg, status = _core()
    core.keyboard(flags=0, extra=RUN_TAG, vk=0x41, event_ms=9_000, entry_ms=10_500,
                  entry_qpc=10_500 * MS)  # fmt: skip
    assert status.get("gap_events") == 1
    assert status.get("max_event_lag_ms") == 1_500
    assert status.get("latency_events") == 1


def test_a_tick_wrap_is_not_a_gap() -> None:
    core, _cfg, status = _core()
    core.keyboard(flags=0, extra=RUN_TAG, vk=0x41, event_ms=0xFFFF_FFF0, entry_ms=0x10,
                  entry_qpc=11_000 * MS)  # fmt: skip
    assert status.get("gap_events") == 0
    assert status.get("max_event_lag_ms") == 0x20


def test_the_loop_writes_the_heartbeat_and_counts_a_long_loop_gap() -> None:
    core, _cfg, status = _core()
    core.loop(qpc=1_100 * MS, unbiased_100ns=1_100 * 10_000)
    assert (status.get("heartbeat_qpc"), status.get("heartbeat_count")) == (1_100 * MS, 1)
    core.loop(qpc=2_300 * MS, unbiased_100ns=2_300 * 10_000)  # 1.2 s since the last loop
    assert status.get("gap_events") == 1
    assert status.get("max_loop_gap_qpc") == 1_200 * MS


# ---- §5.6 / HH-01: a sleep is counted as activity ---------------------------------------------


def test_a_sleep_between_loops_is_a_resume_and_counts_as_the_persons_input() -> None:
    core, _cfg, status = _core()
    core.loop(qpc=1_100 * MS, unbiased_100ns=1_100 * 10_000)
    asleep = SLEEP_DIVERGENCE_MS + 20
    core.loop(qpc=(1_200 + asleep) * MS, unbiased_100ns=1_200 * 10_000)
    assert status.get("resume_events") == 1
    assert status.get("last_user_qpc_keyboard") == (1_200 + asleep) * MS
    assert unpack_last_user(status.get("last_user")) == ((1_200 + asleep) * MS, KIND_TAKEOVER)


def test_ordinary_loops_are_not_resumes() -> None:
    core, _cfg, status = _core()
    for i in range(1, 50):
        core.loop(qpc=(1_000 + 100 * i) * MS, unbiased_100ns=(1_000 + 100 * i) * 10_000)
    assert status.get("resume_events") == 0
    assert status.get("gap_events") == 0


def test_a_callback_error_is_counted() -> None:
    core, _cfg, status = _core()
    core.callback_error()
    assert status.get("callback_errors") == 1


# ---- [V2.60] Q1, kill layer 3: the helper ends a Steer that never acknowledged the kill -------


def test_a_kill_is_overdue_only_past_the_bound_and_only_while_unacknowledged() -> None:
    core, cfg, _status = _core()
    assert not core.kill_overdue(qpc=99_999 * MS)  # no kill, never overdue
    core.hotkey(qpc=2_000 * MS)
    bound_ms = int(KILL_TERMINATE_S * 1000)
    assert not core.kill_overdue(qpc=(2_000 + bound_ms) * MS)
    assert core.kill_overdue(qpc=(2_000 + bound_ms + 1) * MS)
    cfg.set("kill_ack", 1)
    assert not core.kill_overdue(qpc=(2_000 + bound_ms + 1) * MS)


def test_the_bound_is_measured_from_the_first_kill() -> None:
    core, _cfg, _status = _core()
    bound_ms = int(KILL_TERMINATE_S * 1000)
    core.hotkey(qpc=2_000 * MS)
    core.hotkey(qpc=4_000 * MS)  # a second press does not restart the bound
    assert core.kill_overdue(qpc=(2_000 + bound_ms + 1) * MS)


def test_the_bounds_are_ordered_and_the_exit_code_is_steers_stopped() -> None:
    """Rev 2b §6.1: KILL_TERMINATE_S >= 2 x KILL_ACK_S; the code is v1's EXIT_STOPPED."""
    from steerai.run import EXIT_STOPPED  # noqa: PLC0415 - this test only

    assert KILL_TERMINATE_S >= 2 * KILL_ACK_S
    assert KILL_EXIT_CODE == EXIT_STOPPED


# ---- #375 review P5: an underflowed ask word never hides a takeover ---------------------------


@pytest.mark.parametrize("depth", [MAX_ASK_DEPTH + 1, 2**64 - 1])
def test_an_ask_depth_past_the_bound_is_no_ask_and_a_callback_error(depth: int) -> None:
    """2**64-1 is a decrement below zero; read as an open ask it would make every person's
    input an answer. It is read as no ask, and counted, so Steer loses the monitor."""
    core, cfg, status = _core()
    cfg.set("ask_depth", depth)
    _key(core, extra=0, at_ms=5_000)
    assert unpack_last_user(status.get("last_user"))[0] == 5_000 * MS  # a takeover, not an answer
    assert status.get("callback_errors") == 1


def test_an_ask_depth_within_the_bound_is_an_ask() -> None:
    core, cfg, status = _core()
    cfg.set("ask_depth", MAX_ASK_DEPTH)
    _key(core, extra=0, at_ms=5_000)
    assert status.get("last_user") == 0
    assert status.get("callback_errors") == 0


# ---- #375 review T3: the order inside one publish is pinned too ------------------------------


def test_inside_one_publish_the_persons_time_is_stored_before_the_delivery() -> None:
    """A person's event and the probe classified before one publish: the times it vouches for
    are stored before `delivered` moves (§5.2), even within that single call."""
    core, _cfg, status = _core()
    order: list[str] = []
    real_set = status.set

    def set_spy(name: str, value: int) -> None:
        order.append(name)
        real_set(name, value)

    status.set = set_spy  # type: ignore[method-assign]  # a spy on every store, in order
    classifier = core._classifier
    classifier.set_probe_tag(PT)
    before = classifier.proof("keyboard")
    classifier.on_keyboard(flags=0, extra=0, now=float(3_000 * MS), vk=0x41)
    classifier.on_keyboard(
        flags=LLKHF_UP | LLKHF_INJECTED,
        extra=PT,
        now=float(3_001 * MS),
        vk=PROBE,
    )
    assert core._publish("keyboard", before)
    delivered = order.index("proof_keyboard")
    assert order.index("last_user_qpc_keyboard") < delivered
    assert order.index("last_user") < delivered
    assert order.index("last_own_qpc_keyboard") < delivered


# ---- #375 review H6 (HL-02): the gap baseline starts at ready ----------------------------------


def test_a_stall_between_ready_and_the_first_loop_is_a_gap() -> None:
    core, _cfg, status = _core()
    core.ready(qpc=1_100 * MS, unbiased_100ns=1_100 * 10_000)
    core.loop(qpc=2_300 * MS, unbiased_100ns=2_300 * 10_000)  # 1.2 s after ready
    assert status.get("gap_events") == 1


def test_a_foreign_event_with_the_probes_tag_leaves_the_probe_words_at_zero() -> None:
    """PS-31: a person's event that merely carries the tag on the wrong shape (not injected, a
    click, another key) moves neither word."""
    core, cfg, status = _core()
    cfg.set("probe_tag", PT)
    assert not core.mouse(message=WM_LBUTTONDOWN, flags=0, extra=PT, pt=(1, 1),
                          event_ms=2_000, entry_ms=2_000, entry_qpc=2_000 * MS)  # fmt: skip
    assert not _key(core, extra=PT, vk=0x41, up=True, at_ms=2_001, injected=True)
    assert not _key(core, extra=PT, vk=PROBE, up=True, at_ms=2_002)  # not injected
    assert (status.get("proof_mouse"), status.get("proof_keyboard")) == (0, 0)


def test_the_warm_up_runs_a_numbered_probe_through_both_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PS-24: the first real probe is warm, so the numbered path is exercised in the warm-up."""
    from steerai.session import helper_core  # noqa: PLC0415 - this test only

    seen: list[tuple[str, int]] = []
    real_key = helper_core.HelperCore.keyboard
    real_mouse = helper_core.HelperCore.mouse

    def key(self: HelperCore, **kw: Any) -> bool:  # Any: the callback's keywords, forwarded
        seen.append(("keyboard", kw["extra"]))
        return real_key(self, **kw)

    def mouse(self: HelperCore, **kw: Any) -> bool:  # Any: the callback's keywords, forwarded
        seen.append(("mouse", kw["extra"]))
        return real_mouse(self, **kw)

    monkeypatch.setattr(helper_core.HelperCore, "keyboard", key)
    monkeypatch.setattr(helper_core.HelperCore, "mouse", mouse)
    helper_core.warm_up(_config(), freq=FREQ, rounds=1)
    for hook in ("keyboard", "mouse"):
        tags = [extra for name, extra in seen if name == hook]
        assert tags
        assert all(0 < t < 1 << 32 and t != RUN_TAG for t in tags)


def test_a_probe_the_helper_never_saw_is_own_and_stale_through_the_published_ring() -> None:
    """The parent publishes the retired ring before `probe_tag`: A then B with no event between,
    then A's key-up: own and stale (counted as steer), no proof, never the person's."""
    core, cfg, status = _core()
    a, b = 0x3333_00A1, 0x3333_00B2
    cfg.set("probe_tag", a)
    cfg.set("retired_0", a)  # ring published for B: A is the newest retired tag
    cfg.set("probe_tag", b)
    assert not _key(core, extra=a, vk=PROBE, up=True, at_ms=2_000, injected=True)
    assert status.get("proof_keyboard") == 0
    assert status.get("last_user") == 0
    assert status.get("count_steer") == 1
    assert _key(core, extra=b, vk=PROBE, up=True, at_ms=2_001, injected=True)
    assert status.get("proof_keyboard") == b


def test_the_helper_records_when_a_hook_was_proven() -> None:
    core, cfg, status = _core()
    cfg.set("probe_tag", PT)
    assert core.mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=PT, pt=(1, 1),
                      event_ms=2_000, entry_ms=2_000, entry_qpc=2_000 * MS)  # fmt: skip
    assert status.get("proof_qpc_mouse") == 2_000 * MS
    assert status.get("proof_qpc_keyboard") == 0
