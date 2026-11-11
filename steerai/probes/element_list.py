"""Compatibility adapter: the element list now lives in `steerai.world.elements`."""

from __future__ import annotations

from collections.abc import Sequence

from steerai.probes.uia_snapshot import RawNode, UiaClient
from steerai.world.elements import INTERACTIVE_ROLES, Element, format_lines, prune

__all__ = ["INTERACTIVE_ROLES", "Element", "element_list_for_window", "format_lines", "prune"]


def elements_from_nodes(nodes: Sequence[RawNode]) -> list[Element]:
    """Elements in tree order with ids = index + 1 and parent ids from the depth stack."""
    elements: list[Element] = []
    stack: list[int] = []  # ids of the ancestors of the current node, by depth
    for index, node in enumerate(nodes):
        element_id = index + 1
        del stack[node.depth :]
        parent_id = stack[-1] if stack else None
        stack.append(element_id)
        elements.append(
            Element(
                id=element_id,
                depth=node.depth,
                role=node.role,
                name=node.name,
                box=node.box,
                offscreen=node.offscreen,
                enabled=node.enabled,
                automation_id=node.automation_id,
                runtime_id=node.runtime_id,
                parent_id=parent_id,
                secure=node.secure,
                caps=node.caps,
            )
        )
    return elements


def element_list_for_window(client: UiaClient, hwnd: int, *, cap: int = 150) -> list[str]:
    """One cached subtree fetch → pruned, capped, formatted lines."""
    tree = client.cached_tree(hwnd)
    elements = elements_from_nodes(tree.nodes)
    kept = prune(elements, cap=cap)
    eligible = prune(elements, cap=max(len(elements), 1))
    return format_lines(kept, remaining=len(eligible) - len(kept))
