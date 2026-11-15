"""The occlusion probe's pure helper: the first read that shows a page (the cover area is
`world.cover`, tested in `tests/world/test_cover.py`)."""

from __future__ import annotations

from steerai.probes.occlusion import first_page


def test_the_first_page_is_the_first_read_with_content() -> None:
    samples: list[dict[str, float | int | None]] = [
        {"t": 0.0, "doc_nodes": None},
        {"t": 0.25, "doc_nodes": 0},
        {"t": 0.5, "doc_nodes": 7},
    ]
    assert first_page(samples) == 0.5
    assert first_page(samples[:2]) is None
