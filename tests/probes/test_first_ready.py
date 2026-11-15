"""The first-read diagnostic's pure helpers: Document counts and the first non-empty sample."""

from __future__ import annotations

from steerai.probes.first_ready import doc_counts, first_nonempty
from steerai.world.elements import Element


def _e(eid: int, role: str, parent: int | None) -> Element:
    return Element(id=eid, depth=0, role=role, name="", box=(0, 0, 1, 1), parent_id=parent)


def test_a_frame_with_no_document_has_no_top() -> None:
    assert doc_counts([_e(1, "Window", None), _e(2, "Pane", 1)]) == (0, None)


def test_an_empty_document_counts_zero_under_it() -> None:
    assert doc_counts([_e(1, "Window", None), _e(2, "Document", 1)]) == (1, 0)


def test_the_top_document_counts_every_descendant_and_an_inner_document_is_not_a_top() -> None:
    elements = [
        _e(1, "Window", None),
        _e(2, "Document", 1),
        _e(3, "Group", 2),
        _e(4, "Document", 3),
        _e(5, "Text", 4),
    ]
    assert doc_counts(elements) == (2, 3)


def test_two_root_documents_have_no_single_top() -> None:
    elements = [_e(1, "Window", None), _e(2, "Document", 1), _e(3, "Document", 1)]
    assert doc_counts(elements) == (2, None)


def test_the_first_nonempty_sample_is_the_first_with_content() -> None:
    samples: list[dict[str, float | None]] = [
        {"t": 0.0, "k": None},
        {"t": 0.25, "k": 0},
        {"t": 0.5, "k": 7},
        {"t": 0.75, "k": 9},
    ]
    assert first_nonempty(samples, "k") == 0.5
    assert first_nonempty(samples[:2], "k") is None
