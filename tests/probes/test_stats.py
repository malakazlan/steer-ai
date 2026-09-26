"""Latency statistics: nearest-rank percentiles over millisecond samples."""

import pytest

from steerai.probes.stats import LatencyStats


def test_percentiles_use_nearest_rank() -> None:
    stats = LatencyStats.from_samples([10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0])
    assert stats.n == 10
    assert stats.p50_ms == 50.0
    assert stats.p95_ms == 100.0
    assert stats.max_ms == 100.0
    assert stats.mean_ms == 55.0


def test_single_sample_is_every_percentile() -> None:
    stats = LatencyStats.from_samples([42.5])
    assert (stats.p50_ms, stats.p95_ms, stats.max_ms, stats.mean_ms) == (42.5, 42.5, 42.5, 42.5)


def test_samples_need_not_be_sorted() -> None:
    stats = LatencyStats.from_samples([30.0, 10.0, 20.0])
    assert stats.p50_ms == 20.0
    assert stats.max_ms == 30.0


def test_empty_samples_rejected() -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        LatencyStats.from_samples([])
