"""[V2.62] the cover check, pure: a bound window is covered when it is minimised, cloaked (or that
fact is unread), or the windows above it cover its whole window rect, measured as an exact area.
Not being the foreground is not a cover (probe `occlusion`, 2026-11-15: a background but visible
window and a 52 % covered one both exposed the page, 5 of 5)."""

from __future__ import annotations

import pytest

from steerai.world.cover import CoverFacts, cover_of, covered_fraction

TARGET = (0, 0, 100, 100)
WINDOW = (0, 0, 1200, 900)
SCREEN = (0, 0, 1920, 1080)


def _facts(**changes: object) -> CoverFacts:
    base: dict[str, object] = {
        "minimized": False,
        "cloaked": False,
        "rect": WINDOW,
        "above": (),
        "screen": SCREEN,
    }
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


def test_cover_of_reads_a_one_pixel_sliver_as_uncovered() -> None:
    """The sliver through `cover_of` itself (principal review of #380): full cover means the whole
    area, so a threshold below 1.0 (`>= 0.99`) fails here."""
    assert cover_of(_facts(above=((0, 0, 1200, 899),))) is None
    assert cover_of(_facts(above=((7, 0, 1193, 893),))) is None
    assert cover_of(_facts(above=((0, 0, 1200, 900),))) == "covered"


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


# ---- the screen clip (Chromium clips the window to the virtual screen) -------------------------


def test_a_window_overhanging_the_screen_is_covered_when_its_on_screen_part_is() -> None:
    """A maximised window's rect overhangs the screen by its invisible border (7 px here); the
    part off the screen shows nothing, so full cover of the on-screen part is a cover."""
    overhang = (-7, -7, 1927, 1087)
    on_screen = ((0, 0, 1920, 1080),)
    assert cover_of(_facts(rect=overhang, above=on_screen)) == "covered"


def test_the_screen_clip_still_needs_the_whole_on_screen_part() -> None:
    overhang = (-7, -7, 1927, 1087)
    assert cover_of(_facts(rect=overhang, above=((0, 0, 1920, 1079),))) is None


def test_a_window_wholly_off_the_screen_is_covered() -> None:
    assert cover_of(_facts(rect=(2000, 0, 2600, 400))) == "covered"


def test_an_empty_screen_reading_covers_everything() -> None:
    """A virtual screen with no area (an unread metric) shows nothing: never "uncovered"."""
    assert cover_of(_facts(screen=(0, 0, 0, 0))) == "covered"
