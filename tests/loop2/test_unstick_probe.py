"""The unstick probe's pure part: the first read with a page. Its live paths run only in a slot."""

from __future__ import annotations

from steerai.loop2.unstick_probe import first_page


def test_the_first_page_is_the_first_read_with_nodes_under_the_document() -> None:
    reads: list[dict[str, object]] = [
        {"t": 0.0, "doc_nodes": None},
        {"t": 0.25, "doc_nodes": 0},
        {"t": 0.5, "doc_nodes": 7},
    ]
    assert first_page(reads) == 0.5
    assert first_page(reads[:2]) is None
    assert first_page([]) is None
