"""A full read saved as JSON, so a fixture page can be rated offline from its own read (stage 1:
the gallery's expected outcomes, [V2.14] style, never from watching a run).

The snapshot is v1's record (`snapshot_to_json`, unchanged); beside it go the facts a v2 rating
reads: the URLs, the window list, the focus, the AriaRoles, the heading levels, the nearest
landmarks, the tier, and the snapshot's unread sets and capability flag (which v1's record does not
hold). None stays None: an unread fact is never saved as empty. The acted value is never saved
(its digest is keyed to one task). A record is data from disk: every field is checked on load.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Mapping
from typing import Any, Final, get_args

from steerai.world.effects import Observed, Tier
from steerai.world.landmarks import Landmark, LandmarkKind
from steerai.world.model import Snapshot, snapshot_from_json, snapshot_to_json

READ_FORMAT: Final = 1
RuntimeId = tuple[int, ...]


def observed_to_json(observed: Observed) -> str:
    """The read as one JSON object (the snapshot's names are page data: save only fixture
    pages)."""
    snapshot = observed.snapshot
    payload = {
        "version": READ_FORMAT,
        "snapshot": json.loads(snapshot_to_json(snapshot)),
        "unread_secure": sorted(snapshot.unread_secure),
        "unread_automation_id": sorted(snapshot.unread_automation_id),
        "caps_read": snapshot.caps_read,
        "url": observed.url,
        "document_url": observed.document_url,
        "url_observed": observed.url_observed,
        "windows": None if observed.windows is None else list(observed.windows),
        "focus": None if observed.focus is None else list(observed.focus),
        "tier": observed.tier,
        "aria_roles": _pairs(observed.aria_roles, str),
        "headings": _pairs(observed.headings, int),
        "landmarks": _pairs(observed.landmarks, _landmark_out),
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def observed_from_json(text: str) -> Observed:
    """The inverse of `observed_to_json`; anything of another shape is a `ValueError`."""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError("read JSON is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("version") != READ_FORMAT:
        raise ValueError("read JSON has an unknown version or shape")
    try:
        return _observed(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"read JSON is malformed: {type(exc).__name__}") from exc


def _observed(payload: Mapping[str, Any]) -> Observed:  # Any: a JSON object
    snapshot = snapshot_from_json(json.dumps(payload["snapshot"]))
    snapshot = dataclasses.replace(
        snapshot,
        unread_secure=frozenset(_ints(payload["unread_secure"])),
        unread_automation_id=frozenset(_ints(payload["unread_automation_id"])),
        caps_read=_bool(payload["caps_read"]),
    )
    tier = payload["tier"]
    if tier not in get_args(Tier):
        raise ValueError("tier")
    windows = payload["windows"]
    focus = payload["focus"]
    return Observed(
        snapshot=_checked(snapshot),
        url=_text(payload["url"]),
        document_url=_text(payload["document_url"]),
        url_observed=_bool(payload["url_observed"]),
        windows=None if windows is None else tuple(_ints(windows)),
        focus=None if focus is None else _rid(focus),
        tier=tier,
        aria_roles=_mapping(payload["aria_roles"], _role),
        headings=_mapping(payload["headings"], _level),
        landmarks=_mapping(payload["landmarks"], _landmark_in),
    )


def _checked(snapshot: Snapshot) -> Snapshot:
    if len({e.id for e in snapshot.elements}) != len(snapshot.elements):
        raise ValueError("duplicate element id")
    return snapshot


def _pairs[V](
    facts: Mapping[RuntimeId, V] | None, out: Callable[[V], object]
) -> list[list[object]] | None:
    if facts is None:
        return None
    return [[list(rid), out(value)] for rid, value in facts.items()]


def _mapping[V](raw: object, read: Callable[[object], V]) -> dict[RuntimeId, V] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise TypeError("facts")
    found: dict[RuntimeId, V] = {}
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != 2:
            raise TypeError("fact pair")
        found[_rid(pair[0])] = read(pair[1])
    return found


def _landmark_out(landmark: Landmark | None) -> dict[str, object] | None:
    if landmark is None:
        return None
    return {"kind": landmark.kind, "runtime_id": list(landmark.runtime_id)}


def _landmark_in(raw: object) -> Landmark | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("kind") not in get_args(LandmarkKind):
        raise ValueError("landmark")
    return Landmark(kind=raw["kind"], runtime_id=_rid(raw["runtime_id"]))


def _role(raw: object) -> str:
    if not isinstance(raw, str):
        raise TypeError("role")
    return raw


def _level(raw: object) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise TypeError("heading level")
    return raw


def _rid(raw: object) -> RuntimeId:
    return tuple(_ints(raw))


def _ints(raw: object) -> list[int]:
    if not isinstance(raw, list) or not all(
        isinstance(v, int) and not isinstance(v, bool) for v in raw
    ):
        raise TypeError("ints")
    return list(raw)


def _bool(raw: object) -> bool:
    if not isinstance(raw, bool):
        raise TypeError("flag")
    return raw


def _text(raw: object) -> str | None:
    if raw is not None and not isinstance(raw, str):
        raise TypeError("text")
    return raw
