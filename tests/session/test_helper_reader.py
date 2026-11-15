"""[V2.60] hook helper, PR 3 (packet rev 2b §5.3, §5.4): Steer's reader of the helper's words.

Every read is validated; `kill` is read first and wins even when everything else is wrong. The
monotonic rule keeps one set of last-read values per reader thread (rev 2a), so two readers
interleaved never see a false decrease. A person's input is converted to Steer's clock by its age,
never by comparing raw clocks across processes, and only ever towards "more recent"."""

from __future__ import annotations

import itertools

import pytest

from steerai.session.helper_reader import (
    ReaderState,
    StatusRead,
    last_user_seconds,
    read_status,
)
from steerai.session.hook_layout import (
    KIND_OTHER_STEER,
    STATE_CLOSED,
    STATE_FAILED,
    STATE_READY,
    Words,
    pack_last_user,
    status_size,
    status_words,
)

FREQ = 10_000_000
NONCE = (11, 22)
PID = 4242
DEADLINE_S = 1.05


def _status() -> Words:
    s = status_words(memoryview(bytearray(status_size())))
    s.set("state", STATE_READY)
    s.set("pid", PID)
    s.set("nonce_echo_lo", NONCE[0])
    s.set("nonce_echo_hi", NONCE[1])
    s.set("heartbeat_qpc", 1_000 * FREQ)
    s.set("heartbeat_count", 1)
    return s


def _read(
    s: Words, state: ReaderState, *, now_s: float = 1_000.5, exited: bool = False
) -> StatusRead:
    return read_status(
        s,
        state,
        qpc_now=lambda: int(now_s * FREQ),
        freq=FREQ,
        deadline_s=DEADLINE_S,
        nonce=NONCE,
        pid=PID,
        helper_exited=exited,
    )


def test_a_healthy_read_has_no_fault() -> None:
    read = _read(_status(), ReaderState())
    assert (read.killed, read.fault) == (False, None)


# ---- §5.4, one rule at a time ----------------------------------------------------------------


def _break(rule: str, s: Words, state: ReaderState) -> dict[str, object]:
    """Break exactly one rule; returns overrides for `_read`."""
    if rule == "helper_exited":
        return {"exited": True}
    if rule == "not_ready":
        s.set("state", STATE_CLOSED)
    elif rule == "failed":
        s.set("state", STATE_FAILED)
    elif rule == "nonce":
        s.set("nonce_echo_hi", 23)
    elif rule == "pid":
        s.set("pid", PID + 1)
    elif rule == "decreased":
        _read(s, state)
        s.set("delivered_mouse", 5)
        _read(s, state)
        s.set("delivered_mouse", 4)
    elif rule == "future":
        s.set("last_own_qpc_keyboard", int(1_000.5 * FREQ) + 2)
    elif rule == "stale":
        return {"now_s": 1_000 + DEADLINE_S + 0.01}
    elif rule == "callback_errors":
        s.set("callback_errors", 1)
    elif rule == "gap":
        _read(s, state)
        s.set("gap_events", 1)
    return {}


RULES = ["helper_exited", "not_ready", "failed", "nonce", "pid", "decreased", "future", "stale",
         "callback_errors", "gap"]  # fmt: skip


@pytest.mark.parametrize("rule", RULES)
def test_each_broken_rule_is_a_fault(rule: str) -> None:
    """P5 (rev 2): one test per rule, each with exactly that rule broken."""
    s, state = _status(), ReaderState()
    overrides = _break(rule, s, state)
    read = _read(s, state, **overrides)  # type: ignore[arg-type]  # the per-rule override
    assert read.fault is not None
    assert read.fault.startswith(rule if rule != "failed" else "not_ready"), read.fault


@pytest.mark.parametrize("rule", RULES)
def test_kill_is_read_first_and_wins_whatever_else_is_wrong(rule: str) -> None:
    """K4, rev 2 P2: `kill` is its own word, read first, even when the rest is invalid."""
    s, state = _status(), ReaderState()
    overrides = _break(rule, s, state)
    s.set("kill", 1)
    assert _read(s, state, **overrides).killed  # type: ignore[arg-type]  # the override


def test_a_qpc_word_within_one_tick_of_now_is_not_in_the_future() -> None:
    """HH-10: values ±1 tick apart from different threads are ambiguously ordered."""
    s = _status()
    s.set("last_own_qpc_mouse", int(1_000.5 * FREQ) + 1)
    assert _read(s, ReaderState()).fault is None


def test_now_is_taken_after_the_words_are_read() -> None:
    """HH-10: a word written between the read and `qpc_now` must not look like the future."""
    s = _status()
    order: list[str] = []
    real_get = s.get

    def get(name: str) -> int:
        order.append("read")
        return real_get(name)

    def qpc_now() -> int:
        order.append("now")
        return int(1_000.5 * FREQ)

    s.get = get  # type: ignore[method-assign]  # a spy on every word read
    read_status(s, ReaderState(), qpc_now=qpc_now, freq=FREQ, deadline_s=DEADLINE_S,
                nonce=NONCE, pid=PID, helper_exited=False)  # fmt: skip
    assert order.index("now") > max(i for i, o in enumerate(order) if o == "read")


# ---- rev 2a: per-thread last-read values ---------------------------------------------------------


def test_two_readers_interleaved_never_see_a_false_decrease() -> None:
    """The loop thread and the watchdog each keep their own last-read values: a newer read by
    one never becomes the other's baseline. Every interleaving of two readers over a helper
    whose words only grow gives no fault."""
    script = [("delivered_mouse", 1), ("heartbeat_count", 2), ("delivered_keyboard", 1),
              ("delivered_mouse", 2), ("heartbeat_count", 3)]  # fmt: skip
    for order in set(itertools.permutations(["w"] * len(script) + ["a", "b"])):
        s = _status()
        readers = {"a": ReaderState(), "b": ReaderState()}
        writes = iter(script)
        for step in order:
            if step == "w":
                name, value = next(writes)
                s.set(name, value)
            else:
                assert _read(s, readers[step]).fault is None, order


def test_sharing_one_reader_state_between_two_threads_is_what_fails() -> None:
    """The mutation the two-reader test guards: one shared baseline, a slower reader's view."""
    s = _status()
    shared = ReaderState()
    s.set("delivered_mouse", 2)
    assert _read(s, shared).fault is None
    stale = status_words(memoryview(bytearray(status_size())))
    for name in s.names():
        stale.set(name, s.get(name))
    stale.set("delivered_mouse", 1)  # what a reader that sampled earlier would hold
    assert _read(stale, shared).fault is not None


# ---- §5.3: the person's last input on Steer's clock -------------------------------------------


def test_no_input_yet_is_the_monitors_start_time() -> None:
    assert last_user_seconds(word=0, qpc_now=5 * FREQ, freq=FREQ, clock_now=50.0,
                             clock_resolution=0.015625, started_at=40.0) == 40.0  # fmt: skip


def test_an_input_is_placed_by_its_age_and_one_clock_tick_later() -> None:
    word = pack_last_user(4 * FREQ, KIND_OTHER_STEER)
    got = last_user_seconds(word=word, qpc_now=5 * FREQ, freq=FREQ, clock_now=50.0,
                            clock_resolution=0.015625, started_at=40.0)  # fmt: skip
    assert got == pytest.approx(50.0 - 1.0 + 0.015625)


def test_a_negative_age_within_a_tick_is_just_now() -> None:
    word = pack_last_user(5 * FREQ + 1, KIND_OTHER_STEER)
    got = last_user_seconds(word=word, qpc_now=5 * FREQ, freq=FREQ, clock_now=50.0,
                            clock_resolution=0.015625, started_at=40.0)  # fmt: skip
    assert got == pytest.approx(50.0 + 0.015625)
