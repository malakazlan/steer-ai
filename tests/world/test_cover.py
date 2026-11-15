"""[V2.62] the cover check, pure: a bound window is covered when it is minimised, cloaked (or that
fact is unread), or the windows above it cover its whole window rect, measured as an exact area.
Not being the foreground is not a cover (probe `occlusion`, 2026-11-15: a background but visible
window and a 52 % covered one both exposed the page, 5 of 5)."""

from __future__ import annotations

import pytest

from steerai.world.cover import CoverFacts, cover_of, covered_fraction

TARGET = (0, 0, 100, 100)
WINDOW = (0, 0, 1200, 900)


def _facts(**changes: object) -> CoverFacts:
    base: dict[str, object] = {"minimized": False, "cloaked": False, "rect": WINDOW, "above": ()}
    base.update(changes)
    return CoverFacts(**base)  # type: ignore[arg-type]  # test values


# ---- the exact area -----------------------------------------------------------------------------


def test_nothing_above_covers_nothing() -> None:
    assert covered_fraction(TARGET, []) == 0.0


def test_a_window_over_the_whole_target_covers_it() -> None:
    assert covered_fraction(TARGET, [(-5, -5, 105, 105)]) == 1.0


def test_two_halves_together_cover_it() -> None:
    assert covered_fraction(TARGET, [(0, 0, 50, 100), (50, 0, 100, 100)]) == 1.0


def test_a_window_over_the_left_half_covers_half() -> None:
    assert covered_fraction(TARGET, [(0, 0, 50, 100)]) == 0.5


def test_overlapping_covers_count_once() -> None:
    assert covered_fraction(TARGET, [(0, 0, 60, 100), (40, 0, 100, 100)]) == 1.0
    assert covered_fraction(TARGET, [(0, 0, 60, 50), (40, 0, 100, 50)]) == 0.5


def test_a_window_beside_it_covers_nothing() -> None:
    assert covered_fraction(TARGET, [(100, 0, 200, 100)]) == 0.0


def test_a_thin_sliver_left_uncovered_is_not_full_cover() -> None:
    """The probe's own artifact: a cover sized to the target's visible frame leaves a few pixels
    of its window rect uncovered, and Chrome then shows the page; never sampled, always exact."""
    assert covered_fraction(WINDOW, [(7, 0, 1193, 893)]) < 1.0
    assert covered_fraction(WINDOW, [(0, 0, 1200, 899)]) < 1.0
    assert covered_fraction(WINDOW, [(0, 0, 1200, 900)]) == 1.0


def test_an_empty_target_is_never_covered() -> None:
    assert covered_fraction((10, 10, 10, 50), [(0, 0, 100, 100)]) == 0.0


# ---- the cover token ----------------------------------------------------------------------------


def test_a_window_in_front_and_uncovered_is_not_covered() -> None:
    assert cover_of(_facts()) is None


def test_a_partly_covered_window_is_not_covered() -> None:
    assert cover_of(_facts(above=((0, 0, 600, 900),))) is None


def test_a_fully_covered_window_is_covered() -> None:
    assert cover_of(_facts(above=((0, 0, 600, 900), (600, 0, 1200, 900)))) == "covered"


def test_a_minimised_window_is_covered_whatever_is_above() -> None:
    assert cover_of(_facts(minimized=True)) == "minimized"


@pytest.mark.parametrize("cloaked", [True, None])
def test_a_cloaked_or_unread_cloak_is_covered(cloaked: bool | None) -> None:
    """Unread is never "visible" (fail closed, as v1 reads an unanswered DWM)."""
    assert cover_of(_facts(cloaked=cloaked)) == "cloaked"


def test_an_empty_window_rect_is_covered() -> None:
    """A window with no area shows nothing: never read as uncovered."""
    assert cover_of(_facts(rect=(10, 10, 10, 10))) == "covered"
