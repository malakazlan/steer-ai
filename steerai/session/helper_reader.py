"""Steer's reader of the hook helper's words ([V2.60], packet v2-hook-helper rev 2b §5.3, §5.4).

Pure: it reads a `Words` view (the mapped status section, or a test's bytearray) and decides.

The order of every read (§5.4):
1. `kill` first: when set, the read is `killed`, whatever else is wrong (K4).
2. The rest, each a fault when broken: the helper exited; the state is not ready; the nonce or
   the pid does not match; a monotonic word went down since *this reader's* last read; a QPC word
   is past now plus one tick; the heartbeat is older than the deadline; a callback error; a gap
   over the hook timeout since this reader's last read.

`qpc_now` is taken after the words are read (HH-10). The last-read values live in a
`ReaderState` each reader thread owns (rev 2a): the loop thread and the watchdog never share one,
or a newer read by one would look like a decrease to the other.

The person's last input is placed on Steer's clock by its age (§5.3): never by comparing raw clocks
across processes, and always one clock tick later, never earlier (fail closed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from steerai.session.hook_layout import (
    KIND_OTHER_STEER,
    STATE_READY,
    Words,
    unpack_last_user,
)

MONOTONIC: Final = (
    "heartbeat_count",
    "heartbeat_qpc",
    "delivered_keyboard",
    "delivered_mouse",
    "last_own_qpc_keyboard",
    "last_own_qpc_mouse",
    "last_user_qpc_keyboard",
    "last_user_qpc_mouse",
    "gap_events",
    "resume_events",
    "callback_errors",
)
"""Words the helper only ever raises."""
QPC_WORDS: Final = (
    "heartbeat_qpc",
    "last_own_qpc_keyboard",
    "last_own_qpc_mouse",
    "last_user_qpc_keyboard",
    "last_user_qpc_mouse",
)
_READ: Final = (
    "state",
    "pid",
    "nonce_echo_lo",
    "nonce_echo_hi",
    "last_user",
    *MONOTONIC,
)


@dataclass
class ReaderState:
    """One reader thread's last-read values (rev 2a). Never shared between threads."""

    last: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class StatusRead:
    """One validated read: `killed` (checked first), the first broken rule (None: none), and the
    words as read."""

    killed: bool
    fault: str | None
    words: dict[str, int]


def read_status(  # noqa: PLR0913 - the words, the reader's state and each check's input
    status: Words,
    state: ReaderState,
    *,
    qpc_now: Callable[[], int],
    freq: int,
    deadline_s: float,
    nonce: tuple[int, int],
    pid: int,
    helper_exited: bool,
) -> StatusRead:
    if status.get("kill") != 0:  # #383 HM-06: fail closed
        return StatusRead(killed=True, fault=None, words={})
    words = {name: status.get(name) for name in _READ}
    now = qpc_now()  # HH-10: after the read
    fault = _fault(words, state, now=now, freq=freq, deadline_s=deadline_s, nonce=nonce, pid=pid,
                   helper_exited=helper_exited)  # fmt: skip
    for name in MONOTONIC:
        state.last[name] = max(state.last.get(name, 0), words[name])
    state.last["last_user"] = max(state.last.get("last_user", 0), _time(words["last_user"]))
    return StatusRead(killed=False, fault=fault, words=words)


def _time(last_user: int) -> int:
    return unpack_last_user(last_user)[0]


def _fault(  # noqa: PLR0911, PLR0913 - one return per rule, in the stated order
    words: dict[str, int],
    state: ReaderState,
    *,
    now: int,
    freq: int,
    deadline_s: float,
    nonce: tuple[int, int],
    pid: int,
    helper_exited: bool,
) -> str | None:
    if helper_exited:
        return "helper_exited"
    if words["state"] != STATE_READY:
        return "not_ready"
    if (words["nonce_echo_lo"], words["nonce_echo_hi"]) != nonce:
        return "nonce"
    if words["pid"] != pid:
        return "pid"
    for name in (*MONOTONIC, "last_user"):
        value = _time(words[name]) if name == "last_user" else words[name]
        if value < state.last.get(name, 0):
            return f"decreased:{name}"
    for name in (*QPC_WORDS, "last_user"):
        value = _time(words[name]) if name == "last_user" else words[name]
        if value > now + 1:
            return f"future:{name}"
    if now - words["heartbeat_qpc"] > deadline_s * freq:
        return "stale"
    if words["callback_errors"]:
        return "callback_errors"
    if words["gap_events"] > state.last.get("gap_events", 0):
        return "gap"
    return None


def last_user_seconds(  # noqa: PLR0913 - the word and every clock it is converted across
    *,
    word: int,
    qpc_now: int,
    freq: int,
    clock_now: float,
    clock_resolution: float,
    started_at: float,
) -> float:
    """§5.3: the person's last input on Steer's clock, by its age and one clock tick later; no
    input yet is the monitor's start (TK-29); a negative age within a tick is "just now"."""
    qpc, _kind = unpack_last_user(word)
    if qpc == 0:
        return started_at
    age = max(0, qpc_now - qpc) / freq
    return clock_now - age + clock_resolution


def is_other_steer(word: int) -> bool:
    """[V2.58] (2): the latest foreign input carried v1's constant tag."""
    return unpack_last_user(word)[1] == KIND_OTHER_STEER
