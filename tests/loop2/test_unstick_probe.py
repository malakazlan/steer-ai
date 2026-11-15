"""The unstick probe's pure parts: the first read with a page, the nudge's span and the plan of
cells. Its live paths run only in a slot."""

from __future__ import annotations

from steerai.loop2.unstick_probe import (
    RUNS,
    doc_plan,
    doc_times,
    first_page,
    nudge_span_s,
    p99,
    plan,
    put_back_same,
    wait_from,
)


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


# ---- #400 REVISE 2: the measured wait for a Document ---------------------------------------------


def test_doc_times_split_the_first_document_from_the_first_page() -> None:
    reads: list[dict[str, object]] = [
        {"t": 0.0, "doc_nodes": None},
        {"t": 0.4, "doc_nodes": 0},
        {"t": 0.9, "doc_nodes": 12},
    ]
    assert doc_times(reads) == {"first_document_s": 0.4, "first_page_s": 0.9}
    assert doc_times(reads[:1]) == {"first_document_s": None, "first_page_s": None}


def test_the_p99_is_the_nearest_rank() -> None:
    assert p99([0.1 * n for n in range(1, 21)]) == 2.0  # n=20: rank ceil(19.8) = 20
    assert p99([0.3]) == 0.3


def test_the_wait_is_the_p99_plus_the_margin_rounded_up_to_a_tenth() -> None:
    assert wait_from([0.2, 0.31, 1.04], margin_s=0.5) == 1.6
    assert wait_from([0.2, 0.3], margin_s=0.5) == 0.8


def test_a_start_that_never_showed_a_page_refuses_to_set_the_wait() -> None:
    import pytest  # noqa: PLC0415 - this test only

    with pytest.raises(ValueError, match="no page"):
        wait_from([], margin_s=0.5)


def test_the_doc_plan_mixes_cold_and_loaded_starts() -> None:
    starts = doc_plan()
    assert len(starts) >= 20
    assert {kind for _, kind in starts} == {"cold", "loaded"}
    assert abs(sum(k == "cold" for _, k in starts) - sum(k == "loaded" for _, k in starts)) <= 1


def test_the_put_back_is_compared_without_the_minimised_position() -> None:
    before = (2, 3, (13, 13, 1326, 1019), (-1, -1), (-1, -1))
    assert put_back_same(before, (2, 3, (13, 13, 1326, 1019), (-32000, -32000), (-1, -1)))
    assert not put_back_same(before, (2, 1, (13, 13, 1326, 1019), (-1, -1), (-1, -1)))
    assert not put_back_same(before, (0, 3, (13, 13, 1326, 1019), (-1, -1), (-1, -1)))
