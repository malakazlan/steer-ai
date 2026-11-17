"""The self-probe's bookkeeping ([V2.60] hook helper, packet rev 2b §4): when a probe is due,
whether every probe sent since the last proof was delivered, and which hook missed its deadline.

Moved out of `MonitorCore` unchanged. It runs on Steer's side and reads the delivery counts and
own-event times through `HookCounts`: the in-process `Classifier` today, the helper's shared words
later. Proof is by sequence number, never by timestamps or counts (LW-01, #364 P1, [V2.80] #492):
each probe carries its own number and a hook is proven only by that number; no other own event,
and no event of an earlier probe, is proof.
"""

from __future__ import annotations

from typing import Final, Protocol

from steerai.session.hook_classifier import HOOKS, next_probe_seq

PROBE_PERIOD_S: Final = 1.0
"""Quiet time after the last tagged event before a self-probe is due (unmeasured)."""
PROBE_DEADLINE_S: Final = 0.25
"""v1's deadline for a sent probe to reach the hook; measured p95 under load is 48 ms, max
110 ms (probe `hook-latency-2026-11-04`). v2 derives its own ([V2.59])."""


class HookCounts(Protocol):
    """What the ledger reads: per hook, the sequence of the last probe it saw and the last own
    event's time."""

    def probe_seq(self, hook: str) -> int: ...
    def last_own(self, hook: str) -> float: ...
    def last_steer(self) -> float: ...


class ProbeLedger:
    """`per_hook` (v2, [V2.57] LW-01): each hook proves itself against `deadline_s`. Without it
    (v1): one proof for both, against `PROBE_DEADLINE_S`."""

    def __init__(self, *, per_hook: bool, deadline_s: float) -> None:
        self._per_hook = per_hook
        self._deadline_s = deadline_s
        self._seq = 0
        """The last probe's sequence number (0: none sent); both hooks must see exactly it."""
        self._probe_sent_at: float | None = None

    def probe_due(self, *, now: float, counts: HookCounts) -> bool:
        if self._per_hook:
            if self._seq and not self.probe_seen(counts=counts):
                return False  # one probe in flight until both hooks saw it
            return now - min(counts.last_own(h) for h in HOOKS) >= PROBE_PERIOD_S
        if self._probe_sent_at is not None and self._probe_sent_at >= counts.last_steer():
            return False  # one probe in flight
        return now - counts.last_steer() >= PROBE_PERIOD_S

    def probe_seen(self, *, counts: HookCounts) -> bool:
        """The last probe was seen, by its own number, on both hooks."""
        return self._seq > 0 and not self._unproven(counts)

    def lost_hooks(self, *, now: float, counts: HookCounts) -> tuple[str, ...]:
        """The hooks that have not seen the last probe past its deadline, in order."""
        sent = self._probe_sent_at
        if sent is None or not self._seq or now - sent <= self._deadline_s:
            return ()
        return self._unproven(counts)

    def probe_sent(self, *, now: float) -> int:
        """Start a probe: the sequence number its events must carry."""
        self._seq = next_probe_seq(self._seq)
        self._probe_sent_at = now
        return self._seq

    def probe_ok(self, *, now: float, counts: HookCounts) -> bool:
        """The probe half of health: v2, no hook lost; v1, the last probe not missed."""
        if self._per_hook:
            return not self.lost_hooks(now=now, counts=counts)
        sent = self._probe_sent_at
        missed = sent is not None and counts.last_steer() < sent and now - sent > PROBE_DEADLINE_S
        return not missed

    def _unproven(self, counts: HookCounts) -> tuple[str, ...]:
        return tuple(h for h in HOOKS if counts.probe_seq(h) != self._seq)
