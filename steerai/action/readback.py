"""Readback poll (design 5.9 [v0.7]): after an action, poll the verb's postcondition in 50 ms
steps for up to 400 ms. The outcome decides the delivery disposition: verified when the condition
held, otherwise delivered-but-unverified, which is never a label of record (5.10)."""

from __future__ import annotations

import time
from collections.abc import Callable

from steerai.action.dispositions import Delivery

STEP_S = 0.05
DEADLINE_S = 0.4


def poll_until(
    condition: Callable[[], bool], *, step_s: float = STEP_S, deadline_s: float = DEADLINE_S
) -> bool:
    """True as soon as `condition()` holds; False once `deadline_s` has elapsed."""
    if step_s <= 0:
        raise ValueError("step_s must be > 0")
    if deadline_s < 0:
        raise ValueError("deadline_s must be >= 0")
    attempts = max(round(deadline_s / step_s), 0)
    for _ in range(attempts):
        if condition():
            return True
        time.sleep(step_s)
    return condition()


def readback_delivery(*, verified: bool | None) -> Delivery:
    """`None` means the verb had no readable postcondition: delivered, unverified."""
    return Delivery.DELIVERED_VERIFIED if verified else Delivery.DELIVERED_UNVERIFIED
