"""[V2.13] the search-landmark facts from `Observed.landmarks`: a searchbox qualifies on its own,
a button only in the field's own search landmark, and a missing fact fails closed."""

from __future__ import annotations

import dataclasses

from steerai.loop2.batch import batch_continues
from steerai.loop2.query import search_landmark_of
from steerai.world.landmarks import Landmark
from tests.loop2.world_fakes import FIELD, observed, report

SEARCH = Landmark(kind="search", runtime_id=(9, 50))
MAIN = Landmark(kind="main", runtime_id=(9, 52))


def test_a_field_under_a_search_landmark_has_it() -> None:
    seen = dataclasses.replace(observed(), landmarks={FIELD.runtime_id: SEARCH})
    assert search_landmark_of(seen, FIELD.runtime_id) == SEARCH.runtime_id


def test_another_landmark_kind_is_no_search_landmark() -> None:
    seen = dataclasses.replace(observed(), landmarks={FIELD.runtime_id: MAIN})
    assert search_landmark_of(seen, FIELD.runtime_id) is None


def test_no_landmark_ancestor_is_none() -> None:
    seen = dataclasses.replace(observed(), landmarks={FIELD.runtime_id: None})
    assert search_landmark_of(seen, FIELD.runtime_id) is None


def test_a_missing_map_or_element_fails_closed() -> None:
    assert (
        search_landmark_of(dataclasses.replace(observed(), landmarks=None), FIELD.runtime_id)
        is None
    )
    assert (
        search_landmark_of(dataclasses.replace(observed(), landmarks={}), FIELD.runtime_id) is None
    )


def test_scrolled_alone_does_not_end_the_batch() -> None:
    """[V2.18]: `scrolled` does not end the batch by itself when the next target re-resolves."""
    effect = report(tokens=["scrolled"])
    assert batch_continues(effect, verified_type=False, same_target=True) is True
    assert batch_continues(effect, verified_type=False, same_target=False) is False
