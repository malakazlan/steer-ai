"""The batch rule (V2 §5; step 3 packet §3; [V2.2] condition 3; [V2.18]): whether the next action
of a batch may run. It reads the report's class set, tokens and unread facts, never `primary`."""

from __future__ import annotations

from steerai.world.effects import EffectReport

OWN_CLASSES = frozenset({"own_state", "own_value", "focus"})
"""Effects that stayed on the acted element."""
CONTINUING_TOKENS = frozenset({"moved", "scrolled"})
"""A box move, and [V2.18]'s `scrolled` (the viewport moved inside the scope), do not end the
batch by themselves; every other token does."""


def batch_continues(report: EffectReport, *, verified_type: bool, same_target: bool) -> bool:
    """True only when the last report's classes are within {`own_state`, `own_value`, `focus`}
    (or the report is `none` after a `type` whose read-back was True), its tokens within
    {`moved`, `scrolled`}, nothing is unread, and the next target re-resolved to the same
    RuntimeId, role and name."""
    if not same_target or report.unread or report.tokens - CONTINUING_TOKENS:
        return False
    if report.classes == frozenset({"none"}):
        return verified_type
    return report.classes <= OWN_CLASSES
