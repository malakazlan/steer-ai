"""The live world model (design 5.7, [v1.0]): observe until stable, resolve ids only against their
snapshot, diff by RuntimeId.

A `Snapshot` is one cached subtree fetch turned into elements, lines and a fingerprint. Element
ids are snapshot-scoped [v0.9]: an id resolves by RuntimeId against a fresh snapshot of the same
window, and the resolved element must keep its role and (normalised) name, else
`element_not_found`. Microsoft documents RuntimeIds as unique only within the desktop and reusable
over time, which is why the role and name re-check is required, not optional.

Stability [v1.0]: the RuntimeId set (ignoring transient roles) is unchanged across `stable_window`
consecutive fetches. Measured 2026-11-03: a fetch costs ~27 ms on Notepad and ~230 ms on a 460-node
Chrome window, so defaults are two fetches, a 50 ms poll and the 1.5 s cap from design section 4.
"""

from __future__ import annotations

import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

from steerai.errors import ErrorCode, SteerError
from steerai.probes.element_list import elements_from_nodes
from steerai.probes.uia_snapshot import TreeSnapshot, UiaClient
from steerai.probes.windows import (
    is_cloaked,
    is_minimized,
    is_window,
    is_window_enabled,
    process_name,
    window_pid,
    window_title,
)
from steerai.world.elements import Element, format_lines, prune
from steerai.world.fingerprint import Fingerprint, landmarks, normalise_title

DEFAULT_CAP = 150
STABLE_WINDOW = 2
POLL_S = 0.05
CAP_S = 1.5
MIN_POLL_S = 0.01
TRANSIENT_ROLES = frozenset({"ProgressBar", "ToolTip", "Separator"})


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
    minimized: bool = False
    pid: int = 0
    handles: list[Any] = field(default_factory=list, repr=False)
    """Live cached COM elements aligned with `elements` (comtypes has no stubs, hence Any)."""
    cap: int = DEFAULT_CAP

    @property
    def lines(self) -> list[str]:
        kept = prune(self.elements, cap=self.cap)
        eligible = prune(self.elements, cap=max(len(self.elements), 1))
        return format_lines(kept, remaining=len(eligible) - len(kept))

    @property
    def fingerprint(self) -> Fingerprint:
        return Fingerprint(
            process=self.process,
            title_pattern=normalise_title(self.title),
            marks=landmarks(self.elements, title=self.title),
        )

    def element(self, element_id: int) -> Element:
        for element in self.elements:
            if element.id == element_id:
                return element
        raise SteerError(ErrorCode.ELEMENT_NOT_FOUND, f"no element {element_id} in {self.id}")

    def handle(self, element_id: int) -> Any:  # COM element pointer; comtypes has no stubs
        if len(self.handles) != len(self.elements):
            raise SteerError(
                ErrorCode.ELEMENT_NOT_FOUND, f"snapshot {self.id} holds no live handles"
            )
        for element, handle in zip(self.elements, self.handles, strict=True):
            if element.id == element_id:
                return handle
        raise SteerError(ErrorCode.ELEMENT_NOT_FOUND, f"no element {element_id} in {self.id}")


@dataclass(frozen=True, slots=True)
class Diff:
    appeared: list[Element]
    disappeared: list[Element]
    changed: list[tuple[Element, Element]] = field(default_factory=list)
    """(before, after) pairs with the same RuntimeId but a different name, enabled state or box."""
    before_stable: bool = True
    after_stable: bool = True


def _same_window(a: Snapshot, b: Snapshot) -> None:
    if a.hwnd != b.hwnd:
        raise ValueError(f"snapshots are of different windows ({a.hwnd:#x} vs {b.hwnd:#x})")


def diff(before: Snapshot, after: Snapshot) -> Diff:
    """Appeared, disappeared and changed elements by RuntimeId; both must share a window."""
    _same_window(before, after)
    before_by_id = {e.runtime_id: e for e in before.elements if e.runtime_id}
    after_by_id = {e.runtime_id: e for e in after.elements if e.runtime_id}
    changed = [
        (before_by_id[rid], after_by_id[rid])
        for rid in before_by_id.keys() & after_by_id.keys()
        if (before_by_id[rid].name, before_by_id[rid].enabled, before_by_id[rid].box)
        != (after_by_id[rid].name, after_by_id[rid].enabled, after_by_id[rid].box)
    ]
    return Diff(
        appeared=[e for e in after.elements if e.runtime_id and e.runtime_id not in before_by_id],
        disappeared=[
            e for e in before.elements if e.runtime_id and e.runtime_id not in after_by_id
        ],
        changed=changed,
        before_stable=before.stable,
        after_stable=after.stable,
    )


def normalise_name(name: str) -> str:
    """NFKC with collapsed whitespace: the name equality used for identity re-checks."""
    return " ".join(unicodedata.normalize("NFKC", name).split())


def resolve_in(snapshot: Snapshot, element_id: int, fresh: Snapshot) -> Element:
    """Find `snapshot`'s element in `fresh` by RuntimeId; role and name must be unchanged.

    `fresh` must be the same window and not older than `snapshot`. An element without a RuntimeId
    never resolves. Among duplicate RuntimeIds the candidate matching role and box wins.
    """
    _same_window(snapshot, fresh)
    if fresh.taken_at < snapshot.taken_at:
        raise ValueError("fresh snapshot is older than the snapshot being resolved")
    chosen = snapshot.element(element_id)
    if not chosen.runtime_id:
        raise SteerError(
            ErrorCode.ELEMENT_NOT_FOUND, f"element {element_id} of {snapshot.id} has no RuntimeId"
        )
    candidates = [c for c in fresh.elements if c.runtime_id == chosen.runtime_id]
    if not candidates:
        raise SteerError(
            ErrorCode.ELEMENT_NOT_FOUND, f"element {element_id} of {snapshot.id} is gone"
        )
    exact = [c for c in candidates if c.role == chosen.role and c.box == chosen.box]
    candidate = exact[0] if exact else candidates[0]
    same_name = normalise_name(candidate.name) == normalise_name(chosen.name)
    if candidate.role != chosen.role or not same_name:
        raise SteerError(
            ErrorCode.ELEMENT_NOT_FOUND,
            f"element {element_id} changed from {chosen.role} {chosen.name!r} "
            f"to {candidate.role} {candidate.name!r}",
        )
    return candidate


def _identity_set(tree: TreeSnapshot) -> frozenset[tuple[int, ...]]:
    return frozenset(n.runtime_id for n in tree.nodes if n.role not in TRANSIENT_ROLES)


class WorldModel:
    def __init__(self, client: UiaClient) -> None:
        self._client = client

    def observe(  # noqa: PLR0913 - keyword-only tuning knobs, each with a measured default
        self,
        hwnd: int,
        *,
        cap_s: float = CAP_S,
        poll_s: float = POLL_S,
        stable_window: int = STABLE_WINDOW,
        cap: int = DEFAULT_CAP,
        min_nodes: int = 1,
    ) -> Snapshot:
        """Fetch until the RuntimeId set repeats `stable_window` times, or until `cap_s`.

        Refuses windows that cannot be perceived correctly: gone (`app_crashed`), on another desktop
        (`window_other_desktop`), disabled by a modal (`interrupt_modal`), or read by a client whose
        DPI opt-in failed (`perception_degraded`). A tree smaller than `min_nodes` is never stable.
        """
        if stable_window < 1:
            raise ValueError("stable_window must be >= 1")
        if poll_s < MIN_POLL_S:
            raise ValueError(f"poll_s must be >= {MIN_POLL_S}")
        if cap_s < 0:
            raise ValueError("cap_s must be >= 0")
        if cap < 1:
            raise ValueError("cap must be >= 1")
        if not self._client.dpi_per_monitor_v2:
            raise SteerError(
                ErrorCode.PERCEPTION_DEGRADED, "process is not Per-Monitor-V2 DPI aware"
            )
        if not is_window(hwnd):
            raise SteerError(ErrorCode.APP_CRASHED, f"hwnd {hwnd:#x} is not a window")
        if is_cloaked(hwnd):
            raise SteerError(ErrorCode.WINDOW_OTHER_DESKTOP, f"hwnd {hwnd:#x} is cloaked")
        if not is_window_enabled(hwnd):
            raise SteerError(ErrorCode.INTERRUPT_MODAL, f"hwnd {hwnd:#x} is disabled by a modal")

        deadline = time.monotonic() + cap_s
        tree = self._fetch(hwnd)
        streak = 1
        previous = _identity_set(tree)
        while not (streak >= stable_window and len(tree.nodes) >= min_nodes):
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_s)
            if time.monotonic() >= deadline:
                break
            tree = self._fetch(hwnd)
            current = _identity_set(tree)
            streak = streak + 1 if current == previous else 1
            previous = current
        stable = streak >= stable_window and len(tree.nodes) >= min_nodes
        pid = window_pid(hwnd)
        return Snapshot(
            id=uuid.uuid4().hex[:12],
            hwnd=hwnd,
            pid=pid,
            process=process_name(pid) if pid else "",
            title=window_title(hwnd),
            taken_at=time.time(),
            elements=elements_from_nodes(tree.nodes),
            node_count=len(tree.nodes),
            stable=stable,
            minimized=is_minimized(hwnd),
            handles=tree.handles,
            cap=cap,
        )

    def _fetch(self, hwnd: int) -> TreeSnapshot:
        try:
            return self._client.cached_tree(hwnd)
        except OSError as exc:
            if not is_window(hwnd):
                raise SteerError(ErrorCode.APP_CRASHED, f"window {hwnd:#x} went away") from exc
            raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, str(exc)) from exc

    def resolve(self, snapshot: Snapshot, element_id: int, fresh: Snapshot) -> Element:
        return resolve_in(snapshot, element_id, fresh)

    def diff(self, before: Snapshot, after: Snapshot) -> Diff:
        return diff(before, after)
