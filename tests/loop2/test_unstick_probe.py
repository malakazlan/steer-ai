"""The unstick probe's pure part: the first read with a page. Its live paths run only in a slot."""

from __future__ import annotations

from steerai.loop2.unstick_probe import first_page, flicker_s


def test_the_first_page_is_the_first_read_with_nodes_under_the_document() -> None:
    reads: list[dict[str, object]] = [
        {"t": 0.0, "doc_nodes": None},
        {"t": 0.25, "doc_nodes": 0},
        {"t": 0.5, "doc_nodes": 7},
    ]
    assert first_page(reads) == 0.5
    assert first_page(reads[:2]) is None
    assert first_page([]) is None

def test_the_flicker_runs_from_the_minimise_to_the_restore_or_the_maximise() -> None:
    assert flicker_s([("minimize", 10.0), ("restore", 10.25)]) == 0.25
    assert flicker_s([("minimize", 10.0), ("restore", 10.25), ("maximize", 10.4)]) == 0.4
    assert flicker_s([]) is None
