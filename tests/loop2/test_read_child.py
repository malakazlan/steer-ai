"""The pure-read child's pure parts: a read as counts, the first read with a page, the arms. The
child and the sequence are live (Windows, Chrome, the hooks) and run only in a cleared slot."""

from __future__ import annotations

import pytest

from steerai.loop2.read_child import first_page_s, parse_arms, read_shape
from tests.loop2.world_fakes import DOC, PAGE, observed, snapshot


def test_a_read_is_counts_only() -> None:
    shape = read_shape(observed(PAGE), 1.23456)
    assert shape == {
        "t": 1.235,
        "documents": 1,
        "doc_nodes": 5,
        "nodes": len(PAGE),
        "stable": True,
    }


def test_a_frame_with_no_document_has_no_nodes_under_one() -> None:
    frame = [e for e in PAGE if DOC.id not in (e.id, e.parent_id)]
    shape = read_shape(observed(frame), 0.0)
    assert (shape["documents"], shape["doc_nodes"]) == (0, None)


def test_an_unstable_read_says_so() -> None:
    seen = observed(PAGE).__class__(snapshot=snapshot(PAGE, stable=False))
    assert read_shape(seen, 0.0)["stable"] is False


def test_the_first_page_is_the_first_read_with_nodes_under_the_document() -> None:
    reads: list[dict[str, object]] = [
        {"t": 0.0, "doc_nodes": None},
        {"t": 0.25, "doc_nodes": 0},
        {"t": 0.5, "doc_nodes": 7},
    ]
    assert first_page_s(reads) == 0.5
    assert first_page_s(reads[:2]) is None
    assert first_page_s([{"t": 0.0, "stopped": "WindowCovered"}]) is None


def test_the_arms_are_parsed_in_order() -> None:
    assert parse_arms("mta,sta") == ("mta", "sta")
    assert parse_arms(" sta ") == ("sta",)


@pytest.mark.parametrize("text", ["", "mta,both", "MTA", ","])
def test_an_unknown_arm_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="arms must be"):
        parse_arms(text)
