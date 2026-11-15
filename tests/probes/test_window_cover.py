"""[V2.62] the cover read's pure part: the windows above the bound one, from one top-down
enumeration (EnumWindows, as Chromium and `list_top_windows` enumerate), never a GetWindow walk
that can loop forever or reach a destroyed handle."""

from __future__ import annotations

import pytest

from steerai.probes.windows import windows_above

BOUND = 0x10


def test_the_windows_above_are_those_enumerated_before_the_bound_one() -> None:
    assert windows_above([0x30, 0x20, BOUND, 0x05], BOUND) == [0x30, 0x20]


def test_a_window_in_front_has_none_above() -> None:
    assert windows_above([BOUND, 0x20], BOUND) == []


def test_a_bound_window_never_enumerated_is_an_unread_cover() -> None:
    """Gone or on another desktop: never "nothing above"; the caller reads it as cover_unread."""
    with pytest.raises(OSError, match="not_enumerated"):
        windows_above([0x30, 0x20], BOUND)


def test_the_enumeration_stops_at_the_bound_window_even_if_listed_again() -> None:
    assert windows_above([0x30, BOUND, 0x30, BOUND], BOUND) == [0x30]
