"""The hook-latency probe's pure parts: matching injections to callbacks, classification, and
the report; the live run is a probe (real hooks, real input)."""

from __future__ import annotations

import pytest

from steerai.probes.hook_latency import (
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    Ledger,
    classify,
    summarise,
)
from steerai.probes.win_input import STEER_EXTRA_INFO


def test_classification_uses_the_tag_first_then_the_injected_bit() -> None:
    assert classify(flags=LLMHF_INJECTED, extra=STEER_EXTRA_INFO, mouse=True) == "steer"
    assert classify(flags=0, extra=STEER_EXTRA_INFO, mouse=True) == "steer"
    assert classify(flags=LLMHF_INJECTED, extra=0, mouse=True) == "injected_other"
    assert classify(flags=LLKHF_INJECTED, extra=7, mouse=False) == "injected_other"
    assert classify(flags=0, extra=0, mouse=True) == "user"
    assert classify(flags=LLMHF_INJECTED, extra=0, mouse=False) == "user", "keyboard bit is 0x10"


def test_injections_match_callbacks_in_order_and_unmatched_are_counted() -> None:
    ledger = Ledger()
    ledger.sent(1_000)
    ledger.sent(2_000)
    ledger.seen(kind="steer", entered_ns=1_400, body_ns=20)
    ledger.seen(kind="user", entered_ns=1_500, body_ns=15)
    ledger.seen(kind="steer", entered_ns=2_900, body_ns=30)
    ledger.seen(kind="steer", entered_ns=3_000, body_ns=10)  # a tagged event we did not send
    assert [s.latency_ns for s in ledger.samples] == [400, 900]
    assert [s.body_ns for s in ledger.samples] == [20, 30]
    assert ledger.counts == {"steer": 3, "user": 1}
    assert ledger.unmatched == 1
    assert ledger.bodies_ns == [20, 15, 30, 10]


def test_summary_reports_percentiles_in_milliseconds() -> None:
    ledger = Ledger()
    for i in range(100):
        ledger.sent(i * 1_000_000)
        ledger.seen(kind="steer", entered_ns=i * 1_000_000 + (i + 1) * 10_000, body_ns=5_000)
    report = summarise(ledger, timeout_ms=300)
    assert report["samples"] == 100
    assert report["latency_ms"]["p50"] == pytest.approx(0.505, abs=0.02)
    assert report["latency_ms"]["p95"] == pytest.approx(0.955, abs=0.02)
    assert report["latency_ms"]["max"] == pytest.approx(1.0, abs=0.001)
    assert report["body_ms"]["max"] == pytest.approx(0.005, abs=0.0001)
    assert report["over_timeout"] == 0
    assert report["timeout_ms"] == 300


def test_an_empty_ledger_summarises_to_nulls_not_a_crash() -> None:
    report = summarise(Ledger(), timeout_ms=None)
    assert report["samples"] == 0
    assert report["latency_ms"] == {"p50": None, "p95": None, "max": None}
    assert report["timeout_ms"] is None
