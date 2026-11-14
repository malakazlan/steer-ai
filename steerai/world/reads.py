"""[V2.34] read logging: how many reads a watch or a first observation made, and how long they
took, by tier (`poll`: the snapshot only; `full`: every fact). For the wall-time split; step 6
starts from these numbers. Times come from the caller's injected clock."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class ReadCost:
    """The reads one watch, first observation or hold made: count and milliseconds per tier."""

    poll_reads: int = 0
    poll_ms: int = 0
    full_reads: int = 0
    full_ms: int = 0


class _Tiered(Protocol):
    @property
    def tier(self) -> Literal["poll", "full"]: ...


class ReadMeter:
    """Times each read on `clock` and files it under the tier the read returned."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._count = {"poll": 0, "full": 0}
        self._seconds = {"poll": 0.0, "full": 0.0}

    def timed[T: _Tiered](self, read: Callable[[], T]) -> T:
        """`read()`, timed from its call to its return (its completion)."""
        started = self._clock()
        seen = read()
        self._count[seen.tier] += 1
        self._seconds[seen.tier] += self._clock() - started
        return seen

    def cost(self) -> ReadCost:
        return ReadCost(
            poll_reads=self._count["poll"],
            poll_ms=round(self._seconds["poll"] * 1000),
            full_reads=self._count["full"],
            full_ms=round(self._seconds["full"] * 1000),
        )
