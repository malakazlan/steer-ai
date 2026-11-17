"""The hook helper's shared words ([V2.60], packet v2-hook-helper rev 2b §3.2, §3.4, K15).

Two sections, one writer each:
- config: written by Steer before the helper starts, then immutable, except `ask_depth` and
  `kill_ack`, each its own word with Steer the only writer;
- status: written by the helper's one thread only.

Every field is one 8-byte aligned word, read and written directly: no seqlock, so the hook
callback never waits on anything Steer must finish (rev 2 P2), and a reader never retries (HH-13).
That an aligned 8-byte store or load through this view is one untorn access, seen in program
order by the other process, is probe P6 (packet §3.4, §13), not yet a measured fact.
Pairs that must agree share one word (`last_user`: the QPC time in its low 63 bits, the kind in
the top bit).

K15: the helper records timestamps and counters only. The status list is fixed and pinned by a
test; no field can carry a key code, a scan code, a position or wheel data. The two histograms
are counts per bucket, diagnostic only, never read for a decision.
"""

from __future__ import annotations

from typing import Final

STATUS_WORDS: Final = (
    "kill",
    "state",
    "error",
    "pid",
    "nonce_echo_lo",
    "nonce_echo_hi",
    "heartbeat_qpc",
    "heartbeat_count",
    "probe_seq_keyboard",
    "probe_seq_mouse",
    "last_own_qpc_keyboard",
    "last_own_qpc_mouse",
    "last_user_qpc_keyboard",
    "last_user_qpc_mouse",
    "last_user",
    "gap_events",
    "resume_events",
    "callback_errors",
    "loop_error",
    "count_user",
    "count_injected_other",
    "count_steer",
    "count_answer",
    "count_other_steer",
    "max_callback_run_qpc",
    "max_loop_gap_qpc",
    "max_event_lag_ms",
    "latency_events",
    "run_time_events",
    "ws_floor",
    "end_steer_failures",
)
CONFIG_WORDS: Final = (
    "magic",
    "layout_version",
    "run_tag",
    "v1_tag",
    "probe_vk",
    "kill_mods",
    "kill_vk",
    "dead_band_px",
    "heartbeat_ms",
    "hook_timeout_ms",
    "nonce_lo",
    "nonce_hi",
    "crc",
    "ask_depth",
    "kill_ack",
    "end_self_failures",
)
LATENCY_BUCKETS: Final = 1001
"""Event tick to entry tick, one bucket per ms, 0..1000; 1000 holds 1000 ms and over (the hook
timeout's cap, Microsoft Learn LowLevelKeyboardProc)."""
RUN_TIME_BUCKETS: Final = 1001
"""Callback entry to exit on QPC, one bucket per 100 µs, 0..100 ms; the last holds 100 ms and
over (#365 item 1; the M2 bar is a max of 100 ms)."""
HISTOGRAMS: Final = (("latency", LATENCY_BUCKETS), ("run_time", RUN_TIME_BUCKETS))

MAGIC: Final = 0x5354_4545_484F_4F4B
"""`STEEHOOK`: the config section's first word."""
LAYOUT_VERSION: Final = 2

STATE_STARTING: Final = 0
STATE_READY: Final = 1
STATE_CLOSED: Final = 2
STATE_FAILED: Final = 3
STATES: Final = frozenset({STATE_STARTING, STATE_READY, STATE_CLOSED, STATE_FAILED})

ERROR_NONE: Final = 0
ERROR_LAYOUT: Final = 1
"""F15: the config's magic, version, CRC or values are not usable."""
ERROR_HOOKS: Final = 2
ERROR_HOTKEY: Final = 3
"""`RegisterHotKey` failed: another program (or an orphan helper) holds the chord."""
ERROR_LOOP: Final = 4
"""The helper's wait or message pump failed after it was ready."""
ERROR_MAP: Final = 5
"""The config section could not be mapped."""
ERROR_INTERNAL: Final = 6
"""Any other exception in a start step or the loop: the helper has no stderr, so this code is its
only report (the accepted standard-handles deviation)."""

KIND_TAKEOVER: Final = 0
KIND_OTHER_STEER: Final = 1
_KIND_BIT: Final = 1 << 63
_WORD_MAX: Final = (1 << 64) - 1
_WORD: Final = 8


class Words:
    """Named 8-byte words, then histograms, over one mapped view (or a test's bytearray)."""

    def __init__(
        self,
        view: memoryview,
        names: tuple[str, ...],
        histograms: tuple[tuple[str, int], ...] = (),
    ) -> None:
        need = _WORD * (len(names) + sum(n for _, n in histograms))
        if view.nbytes != need:
            raise ValueError(f"the view's size is {view.nbytes} bytes, the layout's {need}")
        self._words = view.cast("B").cast("Q")
        self._index = {name: i for i, name in enumerate(names)}
        self._buckets: dict[str, tuple[int, int]] = {}
        start = len(names)
        for name, count in histograms:
            self._buckets[name] = (start, count)
            start += count

    def names(self) -> tuple[str, ...]:
        return tuple(self._index)

    def get(self, name: str) -> int:
        return int(self._words[self._index[name]])

    def set(self, name: str, value: int) -> None:
        if not 0 <= value <= _WORD_MAX:
            raise ValueError(f"{name}: a word holds 0..2**64-1")
        self._words[self._index[name]] = value

    def bump_bucket(self, histogram: str, bucket: int) -> None:
        start, count = self._buckets[histogram]
        i = start + min(max(bucket, 0), count - 1)
        self._words[i] = int(self._words[i]) + 1

    def bucket(self, histogram: str, bucket: int) -> int:
        start, count = self._buckets[histogram]
        if not 0 <= bucket < count:
            raise IndexError(f"{histogram} has buckets 0..{count - 1}")
        return int(self._words[start + bucket])


def status_words(view: memoryview) -> Words:
    return Words(view, STATUS_WORDS, HISTOGRAMS)


def config_words(view: memoryview) -> Words:
    return Words(view, CONFIG_WORDS)


def status_size() -> int:
    return _WORD * (len(STATUS_WORDS) + sum(n for _, n in HISTOGRAMS))


def config_size() -> int:
    return _WORD * len(CONFIG_WORDS)


def status_offset(name: str) -> int:
    return _WORD * STATUS_WORDS.index(name)


def pack_last_user(qpc: int, kind: int) -> int:
    """The person's last input as one word: the QPC time (63 bits) and who made it."""
    if qpc == 0:
        raise ValueError("a zero time is the no-input sentinel")
    if not 0 < qpc < _KIND_BIT:
        raise ValueError("the time must fit in 63 bits")
    if kind not in {KIND_TAKEOVER, KIND_OTHER_STEER}:
        raise ValueError("unknown kind")
    return qpc | (_KIND_BIT if kind == KIND_OTHER_STEER else 0)


def unpack_last_user(word: int) -> tuple[int, int]:
    """(QPC time, kind); a time of 0 means no input from anyone yet."""
    kind = KIND_OTHER_STEER if word & _KIND_BIT else KIND_TAKEOVER
    return word & (_KIND_BIT - 1), kind
