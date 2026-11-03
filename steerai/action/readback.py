"""Readback poll (design 5.9 [v0.7], [v1.1]): after an action, poll the verb's postcondition in
50 ms steps, bounded by the earlier of a 400 ms monotonic deadline and 8 steps so a blocking
cross-process read cannot stretch it. A readback that settles on a state other than the wanted one
is `verify_failed`; one that never settles is `verify_timeout`; recovery (5.11) branches on the
difference. Executor readback is a delivery fact, never a label of record (5.10)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from steerai.action.dispositions import Delivery
from steerai.errors import ErrorCode

STEP_S = 0.05
DEADLINE_S = 0.4
MAX_STEPS = 8
_EPSILON_S = 1e-6
"""Below this remaining time the deadline counts as reached (float accumulation guard)."""

_UNSET = object()


@dataclass(frozen=True, slots=True)
class Readback:
    verified: bool
    settled: bool
    """The last two samples agreed on a state other than the wanted one."""

    def __post_init__(self) -> None:
        if self.verified and self.settled:
            raise ValueError("a verified readback cannot be settled on a wrong state")

    @property
    def delivery(self) -> Delivery:
        return Delivery.DELIVERED_VERIFIED if self.verified else Delivery.DELIVERED_UNVERIFIED

    @property
    def code(self) -> ErrorCode | None:
        if self.verified:
            return None
        return ErrorCode.VERIFY_FAILED if self.settled else ErrorCode.VERIFY_TIMEOUT


def poll_state[T](
    read: Callable[[], T],
    wanted: T,
    *,
    step_s: float = STEP_S,
    deadline_s: float = DEADLINE_S,
    max_steps: int = MAX_STEPS,
) -> Readback:
    """Sample `read()` until it equals `wanted`, the deadline passes or `max_steps` sleeps ran.

    The first sample is immediate; each sleep is clipped to the time left, so the poll never
    overshoots the deadline and a read that itself blocks past it ends the poll.
    """
    if step_s <= 0:
        raise ValueError("step_s must be > 0")
    if deadline_s < 0:
        raise ValueError("deadline_s must be >= 0")
    if max_steps < 0:
        raise ValueError("max_steps must be >= 0")
    start = time.monotonic()
    previous: object = _UNSET
    sample = read()
    steps = 0
    while sample != wanted:
        remaining = deadline_s - (time.monotonic() - start)
        if remaining <= _EPSILON_S or steps >= max_steps:
            break
        time.sleep(min(step_s, remaining))
        steps += 1
        previous = sample
        sample = read()
    if sample == wanted:
        return Readback(verified=True, settled=False)
    return Readback(verified=False, settled=previous is not _UNSET and previous == sample)


def poll_until(
    condition: Callable[[], bool],
    *,
    step_s: float = STEP_S,
    deadline_s: float = DEADLINE_S,
    max_steps: int = MAX_STEPS,
) -> bool:
    """True as soon as `condition()` holds; False once the deadline or step bound is reached."""
    return poll_state(
        lambda: bool(condition()), True, step_s=step_s, deadline_s=deadline_s, max_steps=max_steps
    ).verified
