"""[V2.13] the landmark fact and the page facts read in one v2-only cached fetch.

Every element read gets its nearest landmark ancestor (kind and RuntimeId), or None when it has
none; an element missing from the map was not observed, and the query conditions then fail
closed. UIA landmark type ids, UIAutomationClient.h: custom 80000, form 80001, main 80002,
navigation 80003, search 80004 (https://learn.microsoft.com/en-us/windows/win32/winauto/
landmark-type-identifiers); Chrome 157 gives 80004 on `role=search`, `<search>` and
`<form role=search>` (probe #266). Pure: the COM walk lives in `probes.uia_snapshot`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

RuntimeId = tuple[int, ...]
LandmarkKind = Literal["custom", "form", "main", "navigation", "search", "other"]

_KINDS: Mapping[int, LandmarkKind] = {
    80000: "custom",
    80001: "form",
    80002: "main",
    80003: "navigation",
    80004: "search",
}


@dataclass(frozen=True, slots=True)
class Landmark:
    kind: LandmarkKind
    runtime_id: RuntimeId


@dataclass(frozen=True, slots=True)
class PageFacts:
    """One cached subtree fetch: AriaRoles, heading levels and nearest landmarks by RuntimeId."""

    roles: Mapping[RuntimeId, str]
    headings: Mapping[RuntimeId, int]
    landmarks: Mapping[RuntimeId, Landmark | None]


def kind_of(code: int | None) -> LandmarkKind | None:
    """The landmark kind of a UIA LandmarkType value; None for 0 (not a landmark) or unread."""
    if not code:
        return None
    return _KINDS.get(code, "other")


def nearest_landmarks(
    nodes: Sequence[tuple[RuntimeId, RuntimeId | None, int | None]],
) -> dict[RuntimeId, Landmark | None]:
    """For each (RuntimeId, parent RuntimeId, LandmarkType) node, its nearest landmark strictly
    above it. A parent missing from `nodes` ends the walk: no ancestor, never a guess."""
    parent = {rid: up for rid, up, _ in nodes}
    own = {rid: kind_of(code) for rid, _, code in nodes}
    found: dict[RuntimeId, Landmark | None] = {}
    for rid, _, _ in nodes:
        seen: set[RuntimeId] = {rid}
        up = parent.get(rid)
        result: Landmark | None = None
        while up is not None and up in parent and up not in seen:
            kind = own[up]
            if kind is not None:
                result = Landmark(kind, up)
                break
            seen.add(up)
            up = parent[up]
        found[rid] = result
    return found
