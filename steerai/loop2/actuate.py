"""Actuation rules for step 3: retries and the press target ([V2.5], [V2.6]; V2 §7.3, §8.11)."""

from __future__ import annotations

from steerai.orchestrator.contracts import Risk
from steerai.world.effects import EffectReport

RETRY_RUNGS = 0
"""Packet §3: no retry rung in step 3. §7.3's one more rung for a plain R0/R1 click that provably
changed nothing returns with the ladder (step 5)."""
LADDER_RISKS = frozenset({"R0", "R1"})
"""§8.11: no ladder on R2/R3, ever."""


def may_retry(*, risk: Risk, verified: bool, report: EffectReport) -> bool:
    """Whether a second rung may run: never after a verified actuation ([V2.5]), never at R2/R3,
    only after a proven `none`, and only when a rung is allowed at all (none in step 3)."""
    if verified or risk not in LADDER_RISKS:
        return False
    proven_none = report.classes == frozenset({"none"}) and not report.unread
    return proven_none and RETRY_RUNGS > 0


def press_allowed(*, focus: tuple[int, ...] | None, last_acted: tuple[int, ...] | None) -> bool:
    """[V2.6] a key press goes to the last acted element: allowed only when the keyboard focus
    was read and is on that element; refused when it is elsewhere, unread, or nothing was
    acted on."""
    return focus is not None and last_acted is not None and focus == last_acted
