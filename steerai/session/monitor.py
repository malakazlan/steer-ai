"""Takeover monitor (design 5.1 [v1.4]): the pure core that the hook thread feeds and the loop
reads.

Rules (edge cases TK-01..49, probe `hook-latency-2026-11-04`):
- Only Steer's own `dwExtraInfo` tag exempts an event. Any other keyboard, button or wheel
  event is the person's, injected bit or not (RDP, VMs, remappers, pen and touch all inject);
  an injected bare mouse move is counted but is never a takeover.
- Real mouse moves pass a dead band around the last cursor position any move set, so a nudged
  desk is not a takeover; wheel and button events always count.
- Input while an ask is open is the person's answer, not a takeover.
- Health is proven, not assumed: our own tagged events are a free probe; after a quiet period
  a self-probe is due (the caller injects an absolute move to the current cursor position) and
  must be seen within a deadline, or the hook was removed by the OS and the monitor is lost.
- The core keeps O(1) counters and one `last_user` instant: no key codes, no positions beyond
  the dead-band reference, no event list, nothing that could carry what the person typed.
"""

from __future__ import annotations

from steerai.probes.win_input import STEER_EXTRA_INFO

LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_MOUSEWHEEL = 0x020A
DEAD_BAND_PX = 4
"""Physical pixels; unmeasured across DPI settings (TK-42)."""
PROBE_PERIOD_S = 1.0
"""Quiet time after the last tagged event before a self-probe is due (unmeasured)."""
PROBE_DEADLINE_S = 0.25
"""How long a sent probe may take to reach the hook; measured p95 under load is 48 ms, max
110 ms (probe `hook-latency-2026-11-04`)."""


class MonitorCore:
    def __init__(self, *, started_at: float, dead_band_px: int = DEAD_BAND_PX) -> None:
        self._started_at = started_at
        self._dead_band = dead_band_px
        self._last_user: float | None = None
        self._last_steer: float = started_at
        self._probe_sent_at: float | None = None
        self._reference: tuple[int, int] | None = None
        self._counts: dict[str, int] = {}
        self._ask_open = False
        self._kill = False
        self._errors = 0
        self._loop_failed = False

    # ---- what the hook thread feeds -------------------------------------------------------------

    def on_keyboard(self, *, flags: int, extra: int, now: float) -> None:
        if extra == STEER_EXTRA_INFO:
            self._steer(now)
            return
        self._person(now, injected=bool(flags & LLKHF_INJECTED))

    def on_mouse(
        self, *, message: int, flags: int, extra: int, pt: tuple[int, int], now: float
    ) -> None:
        x, y = pt
        if extra == STEER_EXTRA_INFO:
            self._steer(now)
            if message == WM_MOUSEMOVE:
                self._reference = (x, y)
            return
        injected = bool(flags & LLMHF_INJECTED)
        if message != WM_MOUSEMOVE:
            self._person(now, injected=injected)
            return
        moved = self._reference is None or (
            abs(x - self._reference[0]) > self._dead_band
            or abs(y - self._reference[1]) > self._dead_band
        )
        if injected:
            self._reference = (x, y)
            self._count("injected_other")
            return
        if moved:
            # the reference moves only with a counted move, so a slow drift still adds up
            self._reference = (x, y)
            self._person(now, injected=False)
        else:
            self._count("user")

    def on_hotkey(self) -> None:
        self._kill = True

    def on_callback_error(self) -> None:
        self._errors += 1
        self._count("callback_errors")

    def on_loop_error(self) -> None:
        self._loop_failed = True

    def mark_ask_open(self) -> None:
        self._ask_open = True

    def mark_ask_closed(self) -> None:
        self._ask_open = False

    # ---- what the loop reads ----------------------------------------------------------------

    def last_user_input(self) -> float:
        """The last instant the person acted; the monitor's start time until the first one
        (unknown is treated as "just now", TK-29)."""
        return self._started_at if self._last_user is None else self._last_user

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def kill_requested(self) -> bool:
        return self._kill

    def probe_due(self, *, now: float) -> bool:
        if self._probe_sent_at is not None and self._probe_sent_at >= self._last_steer:
            return False  # one probe in flight
        return now - self._last_steer >= PROBE_PERIOD_S

    def probe_sent(self, *, now: float) -> None:
        self._probe_sent_at = now

    def healthy(self, *, now: float, thread_alive: bool) -> bool:
        if not thread_alive or self._errors or self._loop_failed:
            return False
        sent = self._probe_sent_at
        probe_missed = (
            sent is not None and self._last_steer < sent and now - sent > PROBE_DEADLINE_S
        )
        return not probe_missed

    def detail(self) -> str:
        """The 5.4 detail token for an unhealthy monitor."""
        return "monitor_lost"

    # ---- internals -----------------------------------------------------------------------------

    def _steer(self, now: float) -> None:
        self._last_steer = now
        self._count("steer")

    def _person(self, now: float, *, injected: bool) -> None:
        if self._ask_open:
            self._count("answer")
            return
        self._count("injected_other" if injected else "user")
        self._last_user = now

    def _count(self, kind: str) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + 1
