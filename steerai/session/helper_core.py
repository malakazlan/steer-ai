"""The hook helper's pure core ([V2.60], packet v2-hook-helper rev 2b §2.1, §3.2, §5.2, §5.5, §5.6).

The helper process's hook callbacks and loop call this and nothing else. It classifies each
event with the in-process rules (`Classifier`) and publishes the result as words: counters,
QPC times, enums, a nonce and a pid (K15: never a key code, a position or wheel data).

Order (§5.2, per hook): a person's event's time is stored before the delivery count of that
hook's later probe moves, so a reader that sees the count also sees the time. Each word is a
single 8-byte store with this thread the only writer (§3.4). The callback reads Steer's
`ask_depth` word directly and never waits on Steer (K7, rev 2 P2).

Only counters and clock comparisons run here ([V2.60] Q3): the gaps over the hook timeout
(§5.5) and the sleep divergence (§5.6). The session and desktop reads run in Steer's watchdog.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Final

from steerai.session.hook_classifier import (
    HOOKS,
    OTHER_STEER_INPUT,
    UNASSIGNED_VKS,
    V1_TAG,
    Classifier,
)
from steerai.session.hook_layout import (
    CONFIG_WORDS,
    ERROR_LAYOUT,
    ERROR_NONE,
    KIND_OTHER_STEER,
    KIND_TAKEOVER,
    LATENCY_BUCKETS,
    LAYOUT_VERSION,
    MAGIC,
    STATE_CLOSED,
    STATE_FAILED,
    STATE_READY,
    Words,
    config_size,
    config_words,
    pack_last_user,
    status_size,
    status_words,
)

SLEEP_DIVERGENCE_MS: Final = 50
"""[V2.60] provisional: a loop whose QPC advanced this much more than the unbiased interrupt time
(which excludes sleep, Microsoft Learn QueryUnbiasedInterruptTime) is a resume. The final value
is max(50 ms, 3 x the no-sleep maximum), from P-sleep."""
KILL_ACK_S: Final = 2.0
"""[V2.60] Q1, layer 2 (provisional, unmeasured; M10 sets it): Steer's own watchdog ends Steer
when a set kill word is not acknowledged within this."""
KILL_TERMINATE_S: Final = 5.0
"""[V2.60] Q1, layer 3 (provisional, unmeasured; M10 sets it; at least 2 x `KILL_ACK_S`): the
helper ends Steer when the kill is still unacknowledged after this."""
KILL_EXIT_CODE: Final = 4
"""The exit code of a Steer ended for the kill: v1's `EXIT_STOPPED` (pinned equal by a test)."""
MAX_ASK_DEPTH: Final = 16
"""#375 P5: the most nested asks Steer can have open. A larger word is an underflow (or a
corrupt word): read as no ask, and counted as a callback error, so Steer loses the monitor."""
WARM_ROUNDS: Final = 1000
"""HH-04: synthetic callbacks run once at start so the first real event finds its code paged in."""
HELPER_MODULE: Final = "steerai.session.hook_helper"
EXIT_OK: Final = 0
EXIT_ARGS: Final = 2
"""The command line is not five handle values and the layout version."""
EXIT_MAP: Final = 3
"""The status section could not be mapped, so nothing could be published."""
EXIT_FAILED: Final = 4
"""A failure was published in the status words (`state = failed`, an error code)."""
_STEERS_OWN: Final = frozenset({"crc", "ask_depth", "kill_ack"})
_IMMUTABLE: Final = tuple(n for n in CONFIG_WORDS if n not in _STEERS_OWN)
_COUNTS: Final = ("user", "injected_other", "steer", "answer", "other_steer")
_TICK_WRAP: Final = 0xFFFF_FFFF
_HALF: Final = 0x8000_0000


@dataclass(frozen=True)
class HelperHandles:
    """The five handles the helper inherits (rev 2 §3.3); never the job's."""

    config: int
    status: int
    quit: int
    wake: int
    parent: int

    def all(self) -> tuple[int, int, int, int, int]:
        return (self.config, self.status, self.quit, self.wake, self.parent)


def helper_argv(handles: HelperHandles, layout_version: int) -> list[str]:
    """The interpreter's arguments: isolated mode, the module, then integers only (no secret
    on a command line another process can read)."""
    return ["-I", "-m", HELPER_MODULE, *(str(h) for h in handles.all()), str(layout_version)]


def parse_helper_argv(args: list[str]) -> tuple[HelperHandles, int]:
    """The helper's own arguments (after the module name); ValueError unless exactly six
    non-negative decimal integers."""
    if len(args) != 6 or not all(a.isascii() and a.isdecimal() for a in args):
        raise ValueError("the helper takes five handle values and the layout version")
    config, status, quit_event, wake, parent, version = (int(a) for a in args)
    return HelperHandles(config, status, quit_event, wake, parent), version


def _crc(config: Words) -> int:
    return zlib.crc32(b"".join(config.get(n).to_bytes(8, "little") for n in _IMMUTABLE))


def write_config(  # noqa: PLR0913 - every immutable config value, keyword-only
    config: Words,
    *,
    run_tag: int,
    probe_vk: int,
    kill_mods: int,
    kill_vk: int,
    dead_band_px: int,
    heartbeat_ms: int,
    hook_timeout_ms: int,
    nonce: tuple[int, int],
) -> None:
    """Steer, before the helper exists: every immutable word, then the CRC over them."""
    values = {
        "magic": MAGIC,
        "layout_version": LAYOUT_VERSION,
        "run_tag": run_tag,
        "v1_tag": V1_TAG,
        "probe_vk": probe_vk,
        "kill_mods": kill_mods,
        "kill_vk": kill_vk,
        "dead_band_px": dead_band_px,
        "heartbeat_ms": heartbeat_ms,
        "hook_timeout_ms": hook_timeout_ms,
        "nonce_lo": nonce[0],
        "nonce_hi": nonce[1],
    }
    for name, value in values.items():
        config.set(name, value)
    config.set("crc", _crc(config))


def config_error(config: Words) -> int:
    """F15: `ERROR_LAYOUT` when the config is not this layout, was changed, or is unusable."""
    tag = config.get("run_tag")
    ok = (
        config.get("magic") == MAGIC
        and config.get("layout_version") == LAYOUT_VERSION
        and config.get("crc") == _crc(config)
        and tag not in {0, V1_TAG}
        and config.get("v1_tag") == V1_TAG
        and config.get("probe_vk") in UNASSIGNED_VKS
        and config.get("hook_timeout_ms") > 0
        and config.get("heartbeat_ms") > 0
    )
    return ERROR_NONE if ok else ERROR_LAYOUT


class HelperCore:
    """One helper's state: the classifier and the words it publishes."""

    def __init__(
        self, *, config: Words, status: Words, freq: int, started_qpc: int, pid: int
    ) -> None:
        self._config = config
        self._status = status
        self._freq = freq
        self._pid = pid
        self._timeout_ms = config.get("hook_timeout_ms")
        self._timeout_qpc = self._timeout_ms * freq // 1000
        self._classifier = Classifier(
            started_at=float(started_qpc),
            extra_info=config.get("run_tag"),
            probe_vk=config.get("probe_vk"),
            dead_band_px=config.get("dead_band_px"),
        )
        self._published_user: float | None = None
        self._published_own = dict.fromkeys(HOOKS, float(started_qpc))
        self._last_loop: tuple[int, int] | None = None
        self._kill_qpc: int | None = None

    # ---- lifecycle -----------------------------------------------------------------------------

    def ready(self, *, qpc: int, unbiased_100ns: int) -> None:
        """Published once the hooks and the hot key are in place; the loop's gap and sleep
        baseline starts here (#375 H6), so a stall before the first loop is still a gap."""
        self._last_loop = (qpc, unbiased_100ns)
        s = self._status
        s.set("pid", self._pid)
        s.set("nonce_echo_lo", self._config.get("nonce_lo"))
        s.set("nonce_echo_hi", self._config.get("nonce_hi"))
        s.set("state", STATE_READY)

    def failed(self, error: int) -> None:
        self._status.set("error", error)
        self._status.set("state", STATE_FAILED)

    def closed(self) -> None:
        self._status.set("state", STATE_CLOSED)

    def ws_floor(self, *, set_ok: bool) -> None:
        """HH-04: whether the hard minimum working set was granted (not fatal when not)."""
        self._status.set("ws_floor", int(set_ok))

    # ---- the hook callbacks --------------------------------------------------------------------

    def keyboard(  # noqa: PLR0913 - the event's fields and its timing, keyword-only
        self, *, flags: int, extra: int, vk: int, event_ms: int, entry_ms: int, entry_qpc: int
    ) -> bool:
        """One keyboard event; True when it delivered a probe (the caller wakes Steer)."""
        self._lag(event_ms, entry_ms)
        self._sync_ask()
        before = self._classifier.delivered("keyboard")
        self._classifier.on_keyboard(flags=flags, extra=extra, now=float(entry_qpc), vk=vk)
        return self._publish("keyboard", before)

    def mouse(  # noqa: PLR0913 - the event's fields and its timing, keyword-only
        self,
        *,
        message: int,
        flags: int,
        extra: int,
        pt: tuple[int, int],
        event_ms: int,
        entry_ms: int,
        entry_qpc: int,
    ) -> bool:
        """One mouse event; True when it delivered a probe. `pt` reaches only the dead band."""
        self._lag(event_ms, entry_ms)
        self._sync_ask()
        before = self._classifier.delivered("mouse")
        self._classifier.on_mouse(
            message=message, flags=flags, extra=extra, pt=pt, now=float(entry_qpc)
        )
        return self._publish("mouse", before)

    def finished(self, *, entry_qpc: int, exit_qpc: int) -> None:
        """A callback's run time (§5.5, #365 item 1); over the hook timeout it is a gap."""
        run = max(0, exit_qpc - entry_qpc)
        s = self._status
        s.bump_bucket("run_time", run * 10_000 // self._freq)
        s.set("run_time_events", s.get("run_time_events") + 1)
        if run > s.get("max_callback_run_qpc"):
            s.set("max_callback_run_qpc", run)
        if run > self._timeout_qpc:
            s.set("gap_events", s.get("gap_events") + 1)

    def hotkey(self, *, qpc: int) -> None:
        """The person's kill: set once, never cleared; the first press starts layer 3's bound."""
        self._classifier.on_hotkey()
        self._status.set("kill", 1)
        if self._kill_qpc is None:
            self._kill_qpc = qpc

    def kill_overdue(self, *, qpc: int) -> bool:
        """[V2.60] Q1, layer 3: the kill is set, Steer has not acknowledged it (`kill_ack`), and
        `KILL_TERMINATE_S` has passed since the first press. The only reason the helper ever
        ends Steer."""
        if self._kill_qpc is None or self._config.get("kill_ack"):
            return False
        return qpc - self._kill_qpc > KILL_TERMINATE_S * self._freq

    def callback_error(self) -> None:
        self._classifier.on_callback_error()
        self._status.set("callback_errors", self._classifier.errors())

    # ---- the loop ------------------------------------------------------------------------------

    def loop(self, *, qpc: int, unbiased_100ns: int) -> None:
        """Each loop return: the gap since the last one, a sleep in between, the heartbeat."""
        s = self._status
        if self._last_loop is not None:
            last_qpc, last_unbiased = self._last_loop
            gap = max(0, qpc - last_qpc)
            if gap > s.get("max_loop_gap_qpc"):
                s.set("max_loop_gap_qpc", gap)
            if gap > self._timeout_qpc:
                s.set("gap_events", s.get("gap_events") + 1)
            awake_ms = max(0, unbiased_100ns - last_unbiased) // 10_000
            if gap * 1000 // self._freq - awake_ms > SLEEP_DIVERGENCE_MS:
                self._resumed(qpc)
        self._last_loop = (qpc, unbiased_100ns)
        s.set("heartbeat_qpc", qpc)
        s.set("heartbeat_count", s.get("heartbeat_count") + 1)

    # ---- internals -----------------------------------------------------------------------------

    def _sync_ask(self) -> None:
        """K7: Steer's ask word, read directly; past `MAX_ASK_DEPTH` it is no ask (#375 P5)."""
        depth = self._config.get("ask_depth")
        if depth > MAX_ASK_DEPTH:
            self.callback_error()
            depth = 0
        self._classifier.sync_ask_depth(depth)

    def _resumed(self, qpc: int) -> None:
        """HH-01: the wake input happened while the hooks were blind: counted as the person's."""
        s = self._status
        s.set("resume_events", s.get("resume_events") + 1)
        s.set("last_user_qpc_keyboard", qpc)
        s.set("last_user", pack_last_user(qpc, KIND_TAKEOVER))

    def _lag(self, event_ms: int, entry_ms: int) -> None:
        late = (entry_ms - event_ms) & _TICK_WRAP
        late = 0 if late >= _HALF else late  # an event stamped after entry: a skewed tick
        s = self._status
        s.bump_bucket("latency", min(late, LATENCY_BUCKETS - 1))
        s.set("latency_events", s.get("latency_events") + 1)
        if late > s.get("max_event_lag_ms"):
            s.set("max_event_lag_ms", late)
        if late > self._timeout_ms:
            s.set("gap_events", s.get("gap_events") + 1)

    def _publish(self, hook: str, delivered_before: int) -> bool:
        c = self._classifier
        s = self._status
        user = c.last_user()
        if user is not None and user != self._published_user:
            qpc = int(user)
            kind = KIND_OTHER_STEER if c.last_kind() == OTHER_STEER_INPUT else KIND_TAKEOVER
            s.set(f"last_user_qpc_{hook}", qpc)
            s.set("last_user", pack_last_user(qpc, kind))
            self._published_user = user
        counts = c.counts()
        for name in _COUNTS:
            s.set(f"count_{name}", counts.get(name, 0))
        own = c.last_own(hook)
        if own != self._published_own[hook]:
            s.set(f"last_own_qpc_{hook}", int(own))
            self._published_own[hook] = own
        delivered = c.delivered(hook)
        if delivered == delivered_before:
            return False
        s.set(f"delivered_{hook}", delivered)  # last: after the times it vouches for (§5.2)
        return True


def warm_up(config: Words, *, freq: int, rounds: int = WARM_ROUNDS) -> None:
    """HH-04: run the callback paths over scratch words, so the real first event is warm.
    The real status words are untouched."""
    scratch_config = config_words(memoryview(bytearray(config_size())))
    for name in CONFIG_WORDS:
        scratch_config.set(name, config.get(name))
    core = HelperCore(
        config=scratch_config,
        status=status_words(memoryview(bytearray(status_size()))),
        freq=freq,
        started_qpc=1,
        pid=0,
    )
    tag = config.get("run_tag")
    probe = config.get("probe_vk")
    for i in range(rounds):
        at = 1_000 + i
        core.keyboard(flags=0x80, extra=tag, vk=probe, event_ms=at, entry_ms=at, entry_qpc=at)
        core.mouse(message=0x0200, flags=0, extra=0, pt=(i, i), event_ms=at, entry_ms=at,
                   entry_qpc=at)  # fmt: skip
        core.finished(entry_qpc=at, exit_qpc=at + 1)
        core.loop(qpc=at * freq // 1000, unbiased_100ns=at * 10_000)
