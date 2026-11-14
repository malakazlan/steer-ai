"""[V2.13] the landmark fact: for every element read, its nearest landmark ancestor (kind and
RuntimeId), or None when it has none. An element missing from the map was not observed."""

from __future__ import annotations

import pytest

from steerai.world.landmarks import Landmark, kind_of, nearest_landmarks

PAGE = (9, 1)
FORM = (9, 2)
FIELD = (9, 3)
BUTTON = (9, 4)
NAV = (9, 5)
LINK = (9, 6)
LOOSE = (9, 7)
SEARCH = 80004
NAVIGATION = 80003


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (80000, "custom"),
        (80001, "form"),
        (80002, "main"),
        (80003, "navigation"),
        (80004, "search"),
        (80099, "other"),
        (0, None),
        (None, None),
    ],
)
def test_kind_of_maps_the_uia_landmark_type_ids(code: int | None, kind: str | None) -> None:
    assert kind_of(code) == kind


def test_each_element_gets_its_nearest_landmark_ancestor() -> None:
    nodes = [
        (PAGE, None, 0),
        (FORM, PAGE, SEARCH),
        (FIELD, FORM, 0),
        (BUTTON, FORM, 0),
        (NAV, PAGE, NAVIGATION),
        (LINK, NAV, 0),
        (LOOSE, PAGE, 0),
    ]
    found = nearest_landmarks(nodes)
    assert found[FIELD] == Landmark("search", FORM)
    assert found[BUTTON] == Landmark("search", FORM)
    assert found[LINK] == Landmark("navigation", NAV)
    assert found[LOOSE] is None, "read, with no landmark above it: None, not missing"
    assert found[FORM] is None, "a landmark's own entry is its ancestor's, not itself"


def test_the_nearest_landmark_wins_when_landmarks_nest() -> None:
    nodes = [(PAGE, None, 80002), (FORM, PAGE, SEARCH), (FIELD, FORM, 0)]
    found = nearest_landmarks(nodes)
    assert found[FIELD] == Landmark("search", FORM)
    assert found[FORM] == Landmark("main", PAGE)


def test_a_child_listed_before_its_parent_still_resolves() -> None:
    nodes = [(FIELD, FORM, 0), (FORM, PAGE, SEARCH), (PAGE, None, 0)]
    assert nearest_landmarks(nodes)[FIELD] == Landmark("search", FORM)


def test_an_unknown_parent_is_no_ancestor_never_a_guess() -> None:
    nodes = [(FIELD, (9, 99), 0)]
    assert nearest_landmarks(nodes) == {FIELD: None}
