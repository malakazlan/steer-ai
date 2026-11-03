"""Compatibility adapter: the element list now lives in `steerai.world.elements`."""

from __future__ import annotations

from collections.abc import Sequence

from steerai.probes.uia_snapshot import RawNode, UiaClient
from steerai.world.elements import INTERACTIVE_ROLES, Element, format_lines, prune

__all__ = ["INTERACTIVE_ROLES", "Element", "element_list_for_window", "format_lines", "prune"]


def elements_from_nodes(nodes: Sequence[RawNode]) -> list[Element]:
    return [
        Element(
            id=index + 1,
            depth=node.depth,
            role=node.role,
            name=node.name,
            box=node.box,
            offscreen=node.offscreen,
            enabled=node.enabled,
            automation_id=node.automation_id,
            runtime_id=node.runtime_id,
        )
        for index, node in enumerate(nodes)
    ]


def element_list_for_window(client: UiaClient, hwnd: int, *, cap: int = 150) -> list[str]:
    """One cached subtree fetch → pruned, capped, formatted lines."""
    tree = client.cached_tree(hwnd)
    elements = elements_from_nodes(tree.nodes)
    kept = prune(elements, cap=cap)
    eligible = prune(elements, cap=len(elements))
    return format_lines(kept, remaining=len(eligible) - len(kept))
