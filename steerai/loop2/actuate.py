"""Actuation rules for step 3: retries and the press target ([V2.5], [V2.6]; V2 §7.3, §8.11)."""

from __future__ import annotations

from typing import Final

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Risk
from steerai.world.effects import EffectReport
from steerai.world.elements import Element
from steerai.world.model import Snapshot

RETRY_RUNGS = 0
"""Packet §3: no retry rung in step 3. §7.3's one more rung for a plain R0/R1 click that provably
changed nothing returns with the ladder (step 5)."""
LADDER_RISKS = frozenset({"R0", "R1"})
"""§8.11: no ladder on R2/R3, ever."""


ACT_RETRY_CODES: Final = frozenset({ErrorCode.ELEMENT_NOT_FOUND, ErrorCode.UNSTABLE_UI})
"""v1's refusals with nothing sent that get the act's one retry under the same decision
(`orchestrator.loop._ACT_RETRY_CODES`, #89; a test locks the two equal)."""


def may_retry(
    *,
    risk: Risk,
    verified: bool,
    report: EffectReport | None,
    act: ActionResult | None = None,
    retries: int = 0,
) -> bool:
    """Whether the action may be sent again.

    [V2.25] (c), first: `act` is the dispatch's v1 result. Nothing sent (`not_delivered` with one
    of v1's retry codes and no step tried) gets v1's one local retry under the same decision, at
    any tier; `retries` counts it. An uncertain delivery is unknown: never retried.

    Otherwise a second rung: never after a verified actuation ([V2.5]), never at R2/R3, only
    after a proven `none`, and only when a rung is allowed at all (none in step 3)."""
    if act is not None:
        if act.delivery is Delivery.DELIVERY_UNCERTAIN:
            return False
        nothing_sent = act.delivery is Delivery.NOT_DELIVERED and not act.steps
        if nothing_sent and act.code in ACT_RETRY_CODES:
            return retries == 0
    if verified or risk not in LADDER_RISKS:
        return False
    if report is None:  # nothing was watched: no proven `none`
        return False
    proven_none = report.classes == frozenset({"none"}) and not report.unread
    return proven_none and RETRY_RUNGS > 0


def press_allowed(*, focus: tuple[int, ...] | None, last_acted: tuple[int, ...] | None) -> bool:
    """[V2.6] a key press goes to the last acted element: allowed only when the keyboard focus
    was read and is on that element; refused when it is elsewhere, unread, or nothing was
    acted on."""
    return focus is not None and last_acted is not None and focus == last_acted


def key_targets(element: Element, snapshot: Snapshot) -> frozenset[tuple[int, ...]]:
    """AP-10, [V2.51] (3): where keys for this target may go: the target itself, and for a
    ComboBox its own Edit (a direct child), never any other descendant."""
    allowed = {element.runtime_id}
    if element.role == "ComboBox":
        allowed |= {
            e.runtime_id
            for e in snapshot.elements
            if e.parent_id == element.id and e.role == "Edit" and e.runtime_id
        }
    return frozenset(allowed)


def type_focus_ok(*, focus: tuple[int, ...] | None, target: Element, snapshot: Snapshot) -> bool:
    """[V2.51] (3): the keyboard focus, as `snapshot`'s observation read it, is where a type
    into `target` goes: the target (found in `snapshot` by its RuntimeId, exactly once) or its
    ComboBox's own Edit. Unread focus is never there."""
    if focus is None or not target.runtime_id:
        return False
    found = [e for e in snapshot.elements if e.runtime_id == target.runtime_id]
    if len(found) != 1:
        return False
    return focus in key_targets(found[0], snapshot)
