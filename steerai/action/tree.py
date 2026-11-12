"""A tree item's own row ([v2.81], packet tree-expand-2026-11-12 v2).

Chrome folds an ARIA tree item's label text into its name and gives an open item a box that
covers its children (Chrome 157, gallery: `TreeItem 'Projects' (132,722,1375,767)` over
`TreeItem 'Alpha' (182,744,1375,767)`), so the item's centre is its child's top row and a click
there selects the child (3/3). A person clicks the item's own row: from its top to its first
child item. What sits inside a child item belongs to the child, never to the item.
"""

from __future__ import annotations

from typing import Literal

from steerai.action.execution import RefusalError
from steerai.errors import ErrorCode
from steerai.world.elements import Element
from steerai.world.model import Snapshot

TREE_ITEM = "TreeItem"
MIN_OWN_ROW_PX = 3
"""The thinnest own row aimed at: under it the band is an edge pixel, not a row (hunter TE-11).
A guard, not a measured budget: unmeasured."""
ACTING_ROLES = frozenset(
    {
        "Hyperlink",
        "Button",
        "SplitButton",
        "MenuItem",
        "CheckBox",
        "RadioButton",
        "TabItem",
        "ComboBox",
        "Edit",
    }
)
"""Roles a row may hold that act on their own: more than the one link means the row is not
just a link (a "Delete chat" button inside it), so the click stays refused."""


def own_descendants(snapshot: Snapshot, element: Element) -> list[Element]:
    """The element's descendants; for a TreeItem, those outside every descendant TreeItem's
    subtree (a child item and what it holds are the child's, TE-06/TE-10)."""
    children: dict[int | None, list[Element]] = {}
    for candidate in snapshot.elements:
        children.setdefault(candidate.parent_id, []).append(candidate)
    out: list[Element] = []
    stack = list(children.get(element.id, []))
    while stack:
        node = stack.pop()
        if element.role == TREE_ITEM and node.role == TREE_ITEM:
            continue
        out.append(node)
        stack.extend(children.get(node.id, []))
    return out


def own_row(snapshot: Snapshot, element: Element) -> int | None:
    """The height of a TreeItem's own row from its top: to the top of its nearest child item,
    or of the Group holding one, below its top. None when it has no child item on screen (its
    centre is its row) or is not a TreeItem; `no_own_row` when a child item starts at or above
    the item's top, or under MIN_OWN_ROW_PX below it (a horizontal or reversed tree, TE-12)."""
    if element.role != TREE_ITEM:
        return None
    top = element.box[1]
    ends: list[int] = []
    for node in _child_items_and_groups(snapshot, element):
        ends.append(node.box[1] - top)
    if not ends:
        return None
    height = min(ends)
    if height < MIN_OWN_ROW_PX:
        raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "no_own_row")
    return height


def _by_parent(snapshot: Snapshot) -> dict[int | None, list[Element]]:
    by_parent: dict[int | None, list[Element]] = {}
    for candidate in snapshot.elements:
        by_parent.setdefault(candidate.parent_id, []).append(candidate)
    return by_parent


def _holds_item(
    by_parent: dict[int | None, list[Element]], node: Element, *, with_area: bool
) -> bool:
    """Whether a TreeItem sits anywhere under the node (only one with area, if `with_area`)."""
    stack = list(by_parent.get(node.id, []))
    while stack:
        below = stack.pop()
        if below.role == TREE_ITEM and (below.has_area or not with_area):
            return True
        stack.extend(by_parent.get(below.id, []))
    return False


def _child_items_and_groups(snapshot: Snapshot, element: Element) -> list[Element]:
    """The descendant TreeItems with area, and the Groups with area that hold one."""
    by_parent = _by_parent(snapshot)
    found: list[Element] = []
    stack = list(by_parent.get(element.id, []))
    while stack:
        node = stack.pop()
        if not node.has_area:
            stack.extend(by_parent.get(node.id, []))
            continue
        if node.role == TREE_ITEM or (
            node.role == "Group" and _holds_item(by_parent, node, with_area=True)
        ):
            found.append(node)
            continue
        stack.extend(by_parent.get(node.id, []))
    return found


def own_hit_ids(snapshot: Snapshot, element: Element) -> frozenset[tuple[int, ...]]:
    """A tree item's hit-test acceptance set ([v2.81], hunter TE-01): its own RuntimeId and
    those of its descendants outside its child items, never an acting control nor anything
    inside one (a Delete button the page left in the row, its Text included), nor the Group that
    holds the child items: it is the children's list, a point on it is on a child's line (#235).
    A child item with no area still makes its Group the list's: leaving it out fails closed."""
    children = _by_parent(snapshot)
    ids: set[tuple[int, ...]] = {element.runtime_id} if element.runtime_id else set()
    stack = list(children.get(element.id, []))
    while stack:
        node = stack.pop()
        if node.role == TREE_ITEM or node.role in ACTING_ROLES:
            continue
        if node.role == "Group" and _holds_item(children, node, with_area=False):
            continue
        if node.runtime_id:
            ids.add(node.runtime_id)
        stack.extend(children.get(node.id, []))
    return frozenset(ids)


ExpandDecision = Literal["no_pattern", "in_state", "leaf_node", "partial_state", "click"]
_WANTED = {"expand": 1, "collapse": 0}
_LEAF_NODE = 3


def expand_decision(state: int | None, verb: str) -> ExpandDecision:
    """What an expand / collapse on a tree item does with its ExpandCollapseState ([v2.81]
    rules 2 and 3): the executor reads it before refusing `expand_by_click`, the loop again on
    the look the click is sent against. Collapsed 0, expanded 1; LeafNode 3 has nothing to
    open; PartiallyExpanded 2 or an unknown value is not a state a click is known to toggle."""
    if state is None:
        return "no_pattern"
    if state == _WANTED[verb]:
        return "in_state"
    if state == _LEAF_NODE:
        return "leaf_node"
    if state not in _WANTED.values():
        return "partial_state"
    return "click"
