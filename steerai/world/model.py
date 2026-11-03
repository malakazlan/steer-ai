"""The live world model (design 5.7): observe until stable, resolve ids only against their
snapshot, diff by RuntimeId.

A `Snapshot` is one cached subtree fetch turned into elements, lines and a fingerprint. Element
ids are snapshot-scoped [v0.9]: an id is resolved by RuntimeId against a fresh snapshot and the
resolved element must still have the same role and name, else `element_not_found`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from steerai.errors import ErrorCode, SteerError
from steerai.probes.element_list import elements_from_nodes
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import list_top_windows, process_name
from steerai.world.elements import Element, format_lines, prune
from steerai.world.fingerprint import Fingerprint, landmarks

DEFAULT_CAP = 150
STABLE_WINDOW = 3


@dataclass(frozen=True, slots=True)
class Snapshot:
    id: str
    hwnd: int
    process: str
    title: str
    taken_at: float
    elements: list[Element]
    node_count: int
    stable: bool
    handles: list[Any] = field(default_factory=list, repr=False)
    """Live cached COM elements aligned with `elements` (comtypes has no stubs, hence Any)."""
    cap: int = DEFAULT_CAP

    @property
    def lines(self) -> list[str]:
        kept = prune(self.elements, cap=self.cap)
        eligible = prune(self.elements, cap=len(self.elements))
        return format_lines(kept, remaining=len(eligible) - len(kept))

    @property
    def fingerprint(self) -> Fingerprint:
        return Fingerprint(
            process=self.process, title_pattern=self.title, marks=landmarks(self.elements)
        )

    def element(self, element_id: int) -> Element:
        for element in self.elements:
            if element.id == element_id:
                return element
        raise SteerError(ErrorCode.ELEMENT_NOT_FOUND, f"no element {element_id} in {self.id}")

    def handle(self, element_id: int) -> Any:  # COM element pointer; comtypes has no stubs
        for element, handle in zip(self.elements, self.handles, strict=True):
            if element.id == element_id:
                return handle
        raise SteerError(ErrorCode.ELEMENT_NOT_FOUND, f"no element {element_id} in {self.id}")


@dataclass(frozen=True, slots=True)
class Diff:
    appeared: list[Element]
    disappeared: list[Element]


def diff(before: Snapshot, after: Snapshot) -> Diff:
    before_ids = {e.runtime_id for e in before.elements if e.runtime_id}
    after_ids = {e.runtime_id for e in after.elements if e.runtime_id}
    return Diff(
        appeared=[e for e in after.elements if e.runtime_id and e.runtime_id not in before_ids],
        disappeared=[e for e in before.elements if e.runtime_id and e.runtime_id not in after_ids],
    )


def resolve_in(snapshot: Snapshot, element_id: int, fresh: Snapshot) -> Element:
    """Find `snapshot`'s element in `fresh` by RuntimeId; its role and name must be unchanged."""
    chosen = snapshot.element(element_id)
    for candidate in fresh.elements:
        if candidate.runtime_id == chosen.runtime_id:
            if candidate.role != chosen.role or candidate.name != chosen.name:
                raise SteerError(
                    ErrorCode.ELEMENT_NOT_FOUND,
                    f"element {element_id} changed from {chosen.role} {chosen.name!r} "
                    f"to {candidate.role} {candidate.name!r}",
                )
            return candidate
    raise SteerError(ErrorCode.ELEMENT_NOT_FOUND, f"element {element_id} of {snapshot.id} is gone")


class WorldModel:
    def __init__(self, client: UiaClient) -> None:
        self._client = client

    def observe(
        self,
        hwnd: int,
        *,
        cap_s: float = 2.0,
        poll_s: float = 0.25,
        stable_window: int = STABLE_WINDOW,
        cap: int = DEFAULT_CAP,
    ) -> Snapshot:
        """Fetch until `stable_window` equal node counts, or until `cap_s` (then stable=False)."""
        deadline = time.monotonic() + cap_s
        counts: list[int] = []
        tree = self._client.cached_tree(hwnd)
        counts.append(len(tree.nodes))
        while len(counts) < stable_window or len(set(counts[-stable_window:])) != 1:
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_s)
            tree = self._client.cached_tree(hwnd)
            counts.append(len(tree.nodes))
        stable = len(counts) >= stable_window and len(set(counts[-stable_window:])) == 1
        window = next((w for w in list_top_windows() if w.hwnd == hwnd), None)
        return Snapshot(
            id=uuid.uuid4().hex[:12],
            hwnd=hwnd,
            process=process_name(window.pid) if window else "",
            title=window.title if window else "",
            taken_at=time.time(),
            elements=elements_from_nodes(tree.nodes),
            node_count=len(tree.nodes),
            stable=stable,
            handles=tree.handles,
            cap=cap,
        )

    def resolve(self, snapshot: Snapshot, element_id: int, fresh: Snapshot) -> Element:
        return resolve_in(snapshot, element_id, fresh)

    def diff(self, before: Snapshot, after: Snapshot) -> Diff:
        return diff(before, after)
