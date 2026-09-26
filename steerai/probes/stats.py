"""Latency statistics over millisecond samples (nearest-rank percentiles)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LatencyStats:
    n: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    mean_ms: float

    @classmethod
    def from_samples(cls, samples_ms: Sequence[float]) -> LatencyStats:
        if not samples_ms:
            raise ValueError("at least one sample is required")
        ordered = sorted(samples_ms)
        return cls(
            n=len(ordered),
            p50_ms=_nearest_rank(ordered, 50),
            p95_ms=_nearest_rank(ordered, 95),
            max_ms=ordered[-1],
            mean_ms=sum(ordered) / len(ordered),
        )


def _nearest_rank(ordered: Sequence[float], percentile: int) -> float:
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[max(rank, 1) - 1]
