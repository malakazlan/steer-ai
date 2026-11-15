"""The unstick probe's pure parts: the first read with a page, the nudge's span and the plan of
cells. Its live paths run only in a slot."""

from __future__ import annotations

from steerai.loop2.unstick_probe import RUNS, first_page, nudge_span_s, plan


def test_the_first_page_is_the_first_read_with_nodes_under_the_document() -> None:
    reads: list[dict[str, object]] = [
        {"t": 0.0, "doc_nodes": None},
        {"t": 0.25, "doc_nodes": 0},
        {"t": 0.5, "doc_nodes": 7},
    ]
    assert first_page(reads) == 0.5
    assert first_page(reads[:2]) is None
    assert first_page([]) is None


def test_the_span_runs_from_the_first_nudge_call_to_the_last() -> None:
    assert nudge_span_s([("show:minimize", 10.0), ("placement", 10.25)]) == 0.25
    assert nudge_span_s([("move:+1", 3.0), ("move:-1", 3.2)]) == 0.2
    assert nudge_span_s([]) is None


def test_the_plan_runs_each_kind_and_nudge_at_least_runs_times() -> None:
    cells = plan()
    pairs = {(kind, nudge) for _, kind, nudge in cells}
    assert pairs == {
        ("normal", "move1px"),
        ("normal", "minrestore"),
        ("half", "move1px"),
        ("half", "minrestore"),
        ("maximized", "minrestore"),
    }
    for kind, nudge in pairs:
        assert sum(1 for _, k, n in cells if (k, n) == (kind, nudge)) >= RUNS >= 5
    assert len({name for name, _, _ in cells}) == len(cells)
