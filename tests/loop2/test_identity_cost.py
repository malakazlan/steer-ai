"""#361 T1: the committed benchmark behind LW-03's measured cost."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from steerai.loop2.identity_cost import main, measure
from steerai.loop2.live_world import WindowIdentity

IDENTITY = WindowIdentity(pid=1, started=2, image="x.exe", root=True, visible=True)


def test_measure_reports_the_reads_and_the_percentiles() -> None:
    row = measure(lambda: IDENTITY, 20)
    assert row["reads"] == 20
    assert 0 <= row["p50_ms"] <= row["p95_ms"] <= row["max_ms"]


def test_an_unreadable_identity_is_an_error_not_a_fast_number() -> None:
    with pytest.raises(OSError, match="identity"):
        measure(lambda: None, 3)
    with pytest.raises(ValueError, match="reads"):
        measure(lambda: IDENTITY, 0)


@pytest.mark.windows
def test_the_benchmark_reads_the_desktop_window_and_prints_one_line() -> None:
    """Read-only on the real desktop window (Progman): no input is sent."""
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["--reads", "200"]) == 0
    row = json.loads(out.getvalue())
    assert row["probe"] == "identity_cost"
    assert row["reads"] == 200
    assert row["p95_ms"] < 50  # a bound, not the measurement: the number is in the PR body
