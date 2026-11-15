"""The self-probe's bookkeeping ([V2.60] hook helper, packet rev 2b §4): when a probe is due,
whether every probe sent since the last proof was delivered, and which hook missed its deadline.

Moved out of `MonitorCore` unchanged. It runs on Steer's side and reads the delivery counts and
own-event times through `HookCounts`: the in-process `Classifier` today, the helper's shared words
later. Proof is by counts, never by timestamps (LW-01, #364 P1): an own event of the probe's tick
is not proof, nor is a late event of an earlier probe the proof of a later one.
"""

from __future__ import annotations

from typing import Final, Protocol

from steerai.session.hook_classifier import HOOKS

PROBE_PERIOD_S: Final = 1.0
"""Quiet time after the last tagged event before a self-probe is due (unmeasured)."""
PROBE_DEADLINE_S: Final = 0.25
"""v1's deadline for a sent probe to reach the hook; measured p95 under load is 48 ms, max
110 ms (probe `hook-latency-2026-11-04`). v2 derives its own ([V2.59])."""


class HookCounts(Protocol):
    """What the ledger reads: per hook, own events delivered and the last own event's time."""

    def delivered(self, hook: str) -> int: ...
    def last_own(self, hook: str) -> float: ...
    def last_steer(self) -> float: ...


class ProbeLedger:
    """`per_hook` (v2, [V2.57] LW-01): each hook proves itself against `deadline_s`. Without it
    (v1): one proof for both, against `PROBE_DEADLINE_S`."""

    def __init__(self, *, per_hook: bool, deadline_s: float) -> None:
        self._per_hook = per_hook
        self._deadline_s = deadline_s
        self._base = dict.fromkeys(HOOKS, 0)
        self._outstanding = 0
        """Probes sent since the hooks were last proven; each must be delivered on both."""
        self._probe_sent_at: float | None = None

    def probe_due(self, *, now: float, counts: HookCounts) -> bool:
        if self._per_hook:
            if self._outstanding and not self.probe_seen(counts=counts):
                return False  # one probe in flight until both hooks saw it
            return now - min(counts.last_own(h) for h in HOOKS) >= PROBE_PERIOD_S
        if self._probe_sent_at is not None and self._probe_sent_at >= counts.last_steer():
            return False  # one probe in flight
        return now - counts.last_steer() >= PROBE_PERIOD_S

    def probe_seen(self, *, counts: HookCounts) -> bool:
        """Every probe sent since the last proof was delivered on both hooks."""
        return self._outstanding > 0 and not self._unproven(counts)

    def lost_hooks(self, *, now: float, counts: HookCounts) -> tuple[str, ...]:
        """The hooks that have not seen the last probe past its deadline, in order."""
        sent = self._probe_sent_at
        if sent is None or not self._outstanding or now - sent <= self._deadline_s:
            return ()
        return self._unproven(counts)

    def probe_sent(self, *, now: float, counts: HookCounts) -> None:
        if not self._outstanding or self.probe_seen(counts=counts):  # a new proof starts here
            self._base = {h: counts.delivered(h) for h in HOOKS}
            self._outstanding = 0
        self._outstanding += 1
        self._probe_sent_at = now

    def probe_ok(self, *, now: float, counts: HookCounts) -> bool:
        """The probe half of health: v2, no hook lost; v1, the last probe not missed."""
        if self._per_hook:
            return not self.lost_hooks(now=now, counts=counts)
        sent = self._probe_sent_at
        missed = sent is not None and counts.last_steer() < sent and now - sent > PROBE_DEADLINE_S
        return not missed

    def _unproven(self, counts: HookCounts) -> tuple[str, ...]:
        return tuple(h for h in HOOKS if counts.delivered(h) - self._base[h] < self._outstanding)
