"""A saved full read (`world.read_json`): the snapshot as v1's JSON plus the page facts a v2 rating
reads (URLs, AriaRoles, headings, landmarks, focus, windows), so a fixture can be rated offline."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import pytest

from steerai.world.effects import Observed
from steerai.world.landmarks import Landmark
from steerai.world.read_json import observed_from_json, observed_to_json
from tests.loop2.world_fakes import DOC, FIELD, GO, observed


def _full() -> Observed:
    base = observed(focus=FIELD.runtime_id)
    return dataclasses.replace(
        base,
        headings={DOC.runtime_id: 1},
        landmarks={
            DOC.runtime_id: None,
            FIELD.runtime_id: Landmark(kind="search", runtime_id=DOC.runtime_id),
            GO.runtime_id: Landmark(kind="other", runtime_id=DOC.runtime_id),
        },
        url_observed=False,
    )


def test_a_full_read_round_trips() -> None:
    read = _full()
    assert observed_from_json(observed_to_json(read)) == read


def test_unread_facts_stay_unread() -> None:
    """None is not read, never assumed: an unread map comes back None, not empty."""
    read = dataclasses.replace(observed(), aria_roles=None, landmarks=None, windows=None)
    back = observed_from_json(observed_to_json(read))
    assert back.aria_roles is None
    assert back.landmarks is None
    assert back.headings is None
    assert back.windows is None
    assert back.focus is None


def test_the_value_digest_is_never_saved() -> None:
    """A digest is keyed to its task; a saved read carries no acted value."""
    assert "acted_value" not in json.loads(observed_to_json(observed()))


Mutation = Callable[[dict[str, Any]], object]  # Any: a JSON object


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=99),
        lambda d: d.update(tier="slow"),
        lambda d: d.update(aria_roles=[[[1, "x"], "searchbox"]]),
        lambda d: d.update(landmarks=[[[9, 2], {"kind": "nowhere", "runtime_id": [9, 1]}]]),
        lambda d: d.update(headings=[[[9, 1], "one"]]),
        lambda d: d.update(url=3),
        lambda d: d.pop("snapshot"),
    ],
)
def test_a_record_of_another_shape_is_refused(mutate: Mutation) -> None:
    payload = json.loads(observed_to_json(observed()))
    mutate(payload)
    with pytest.raises(ValueError, match="read JSON"):
        observed_from_json(json.dumps(payload))


def test_text_that_is_not_json_is_refused() -> None:
    with pytest.raises(ValueError, match="read JSON"):
        observed_from_json("{not json")
