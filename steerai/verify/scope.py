"""Scope ladder (design 5.10 [v0.3]) and transient exclusion ([v0.4]), pure over the element
list so it also works on a persisted, handle-less snapshot.

`within` resolves in order: (1) a named landmark container (a container-role element whose
normalised name equals the landmark; UIA `LandmarkType` is not cached today, so this is the
free form of the rule), (2) the anchor element's nearest container ancestor, (3) window-wide
with a strict, anchored name match. Several containers with the landmark's name is ambiguous
and resolves to nothing: unclear means fail. Every level drops transient subtrees (tooltips,
progress bars, separators, containers named like notifications or toasts) and, when asked, the
step's own composer subtree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from steerai.verify.predicates import Within
from steerai.world.elements import Element
from steerai.world.model import TRANSIENT_ROLES, Snapshot, normalise_name

CONTAINER_ROLES: frozenset[str] = frozenset(
    {"Group", "Pane", "Document", "List", "Tree", "Table", "DataGrid", "Tab", "Window", "Custom"}
)
TRANSIENT_CONTAINER_WORDS: tuple[str, ...] = ("notification", "toast")


@dataclass(frozen=True, slots=True)
class Scope:
    level: int
    """1 landmark, 2 anchor container, 3 window-wide; 0 when the landmark was ambiguous."""
    ids: set[int] = field(default_factory=set)
    container_id: int | None = None
    strict: bool = True
    reason: str | None = None


def resolve_scope(
    snapshot: Snapshot, within: Within | None, *, excluding: int | None = None
) -> Scope:
    by_id = {e.id: e for e in snapshot.elements}
    children = _children(snapshot.elements)
    excluded = _transient_ids(snapshot.elements, children)
    if excluding is not None and excluding in by_id:
        # The step's own composer: its draft area is the anchor's nearest container.
        root = _nearest_container(by_id[excluding], by_id) or by_id[excluding]
        excluded |= _subtree(root.id, children)
    if within is not None and within.landmark is not None:
        candidates = [
            e
            for e in snapshot.elements
            if e.role in CONTAINER_ROLES
            and normalise_name(e.name).casefold() == within.landmark
            and e.id not in excluded
        ]
        if len(candidates) > 1:
            return Scope(level=0, reason="ambiguous_landmark")
        if candidates:
            root = candidates[0]
            return Scope(
                level=1,
                ids=_subtree(root.id, children) - excluded,
                container_id=root.id,
                strict=False,
            )
    if within is not None and within.anchor_id is not None and within.anchor_id in by_id:
        container = _nearest_container(by_id[within.anchor_id], by_id)
        if container is not None:
            ids = _subtree(container.id, children) - excluded
            if not ids:
                # The anchor sits in a toast or tooltip: nothing there is ever evidence.
                return Scope(level=0, reason="anchor_transient")
            return Scope(level=2, ids=ids, container_id=container.id, strict=False)
    return Scope(level=3, ids={e.id for e in snapshot.elements} - excluded, strict=True)


def _children(elements: list[Element]) -> dict[int | None, list[int]]:
    out: dict[int | None, list[int]] = {}
    for element in elements:
        out.setdefault(element.parent_id, []).append(element.id)
    return out


def _subtree(root_id: int, children: dict[int | None, list[int]]) -> set[int]:
    ids: set[int] = set()
    stack = [root_id]
    while stack:
        node = stack.pop()
        ids.add(node)
        stack.extend(children.get(node, []))
    return ids


def _transient_ids(elements: list[Element], children: dict[int | None, list[int]]) -> set[int]:
    excluded: set[int] = set()
    for element in elements:
        name = normalise_name(element.name).casefold()
        if element.role in TRANSIENT_ROLES or (
            element.role in CONTAINER_ROLES
            and any(word in name for word in TRANSIENT_CONTAINER_WORDS)
        ):
            excluded |= _subtree(element.id, children)
    return excluded


def _nearest_container(element: Element, by_id: dict[int, Element]) -> Element | None:
    node: Element | None = element
    while node is not None:
        if node.role in CONTAINER_ROLES:
            return node
        node = by_id.get(node.parent_id) if node.parent_id is not None else None
    return None
