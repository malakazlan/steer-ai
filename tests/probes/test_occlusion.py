"""The occlusion probe's pure helpers: how much of a window the windows above it cover, and the
first read that shows a page."""

from __future__ import annotations

from steerai.probes.occlusion import covered_fraction, first_page

TARGET = (0, 0, 100, 100)


def test_nothing_above_covers_nothing() -> None:
    assert covered_fraction(TARGET, []) == 0.0


def test_a_window_over_the_whole_target_covers_it() -> None:
    assert covered_fraction(TARGET, [(-5, -5, 105, 105)]) == 1.0


def test_two_halves_together_cover_it() -> None:
    assert covered_fraction(TARGET, [(0, 0, 50, 100), (50, 0, 100, 100)]) == 1.0


def test_a_window_over_the_left_half_covers_half() -> None:
    assert covered_fraction(TARGET, [(0, 0, 50, 100)]) == 0.5


def test_a_window_beside_it_covers_nothing() -> None:
    assert covered_fraction(TARGET, [(100, 0, 200, 100)]) == 0.0


def test_a_thin_sliver_left_uncovered_is_not_full_cover() -> None:
    """A cover a few pixels short of the target's edge (a window rect set to the target's
    smaller visible frame) leaves it not fully covered; the area is exact, never sampled."""
    target = (0, 0, 1200, 900)
    assert covered_fraction(target, [(7, 0, 1193, 893)]) < 1.0
    assert covered_fraction(target, [(0, 0, 1200, 900)]) == 1.0


def test_overlapping_covers_count_once() -> None:
    assert covered_fraction(TARGET, [(0, 0, 60, 100), (40, 0, 100, 100)]) == 1.0
    assert covered_fraction(TARGET, [(0, 0, 60, 50), (40, 0, 100, 50)]) == 0.5


def test_an_empty_target_is_never_covered() -> None:
    assert covered_fraction((10, 10, 10, 50), [(0, 0, 100, 100)]) == 0.0


def test_the_first_page_is_the_first_read_with_content() -> None:
    samples: list[dict[str, float | int | None]] = [
        {"t": 0.0, "doc_nodes": None},
        {"t": 0.25, "doc_nodes": 0},
        {"t": 0.5, "doc_nodes": 7},
    ]
    assert first_page(samples) == 0.5
    assert first_page(samples[:2]) is None
