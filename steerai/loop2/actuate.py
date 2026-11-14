"""Actuation rules for step 3: retries and the press target ([V2.5], [V2.6]; V2 §7.3).

Interface stub for step 3c; the tests in `tests/loop2/test_invariants_actuate.py` come first.
"""

from __future__ import annotations

from steerai.loop2._stub import stub
from steerai.orchestrator.contracts import Risk
from steerai.world.effects import EffectReport


@stub
def may_retry(*, risk: Risk, verified: bool, report: EffectReport) -> bool:
    """Whether a second rung may run. Never after a verified actuation, never at R2/R3, and no
    retry rung at all in step 3."""
    raise NotImplementedError("3c: retry rule")


@stub
def press_allowed(*, focus: tuple[int, ...] | None, last_acted: tuple[int, ...] | None) -> bool:
    """A key press goes to the last acted element: refused when the keyboard focus (read) is
    elsewhere, unread, or nothing was acted on."""
    raise NotImplementedError("3c: press target")
