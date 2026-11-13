"""`observer.watch` (V2 §5, step 3b): after an action, read until the first change, then until the
screen holds still for the settle window, or until the deadline.

Pure: the read, the clock and the sleep are injected, and the report is always
`observe(before, last read)`. The screen "holds" when the after snapshot is stable and its
element signature (RuntimeId, role, name, enabled, offscreen, caps), URL and focus have not
changed for `settle_s`. The number of reads is bounded by `deadline_s / poll_s` whatever the
clock does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from steerai.world.effects import EffectReport, Observed, observe

Signature = tuple[object, ...]


@dataclass(frozen=True, slots=True)
class Watched:
    report: EffectReport
    after: Observed
    first_change_ms: int | None
    """When the report first differed from `none`; None when it never did."""
    settled_ms: int | None
    """When the screen last changed before it held still; None when nothing changed."""
    timed_out: bool


def _signature(seen: Observed) -> Signature:
    elements = tuple(
        (e.runtime_id, e.role, e.name, e.enabled, e.offscreen, e.caps)
        for e in seen.snapshot.elements
    )
    return (elements, seen.url, seen.focus, seen.acted_value)


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


def watch(  # noqa: PLR0913 - the action's facts and the injected clock, keyword-only
    read: Callable[[], Observed],
    before: Observed,
    *,
    acted: tuple[int, ...] | None = None,
    text: str | None = None,
    scope: tuple[int, ...] | None = None,
    url_fact: bool = True,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_s: float,
    settle_s: float,
    deadline_s: float,
    full: Callable[[Observed], Observed] | None = None,
) -> Watched:
    """The effect of the action just sent, against `before`. `read` is the cheap poll; `full`,
    when given, adds the costly facts once, on the settled (or last) read ([V2.7] two tiers)."""
    if poll_s <= 0 or settle_s < 0 or deadline_s <= 0:
        raise ValueError("watch needs a positive poll and deadline and a settle window >= 0")
    start = clock()
    first: float | None = None
    changed_at = 0.0
    last_signature = _signature(before)
    seen = before
    report = observe(before, before, acted=acted, text=text, scope=scope, url_fact=url_fact)
    for _ in range(int(deadline_s / poll_s) + 1):
        seen = read()
        report = observe(before, seen, acted=acted, text=text, scope=scope, url_fact=url_fact)
        elapsed = clock() - start
        if first is None and report.classes != frozenset({"none"}):
            first = elapsed
        signature = _signature(seen)
        if signature != last_signature or not seen.snapshot.stable:
            changed_at = elapsed
            last_signature = signature
        held = seen.snapshot.stable and elapsed - changed_at >= settle_s
        if first is not None and held:
            if full is not None:
                seen = full(seen)
                report = observe(
                    before, seen, acted=acted, text=text, scope=scope, url_fact=url_fact
                )
            return Watched(report, seen, _ms(first), _ms(changed_at), timed_out=False)
        if elapsed >= deadline_s:
            break
        sleep(poll_s)
    if full is not None:
        seen = full(seen)
        report = observe(before, seen, acted=acted, text=text, scope=scope, url_fact=url_fact)
    settled = None if first is None else _ms(changed_at)
    return Watched(report, seen, None if first is None else _ms(first), settled, timed_out=True)
