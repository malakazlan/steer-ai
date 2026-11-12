"""A tree item is clicked on its label (gallery g20, H6 u3; [v2.77]): Chrome gives an ARIA tree
item no Invoke and ignores Expand on it, so the click falls back to the person's act, a physical
click; a person clicks the item's label text, because a click in the row's middle can select the
row without opening it (the H6 page's folder opens on its label only)."""

from __future__ import annotations

from steerai.action.executor import click_point, physical_click_target
from steerai.world.elements import Element
from steerai.world.model import Snapshot

DOC = Element(id=1, depth=0, role="Document", name="Gallery", box=(0, 0, 900, 900), runtime_id=(1,))
TREE = Element(
    id=2, depth=1, role="Tree", name="Folders", box=(10, 10, 400, 200), runtime_id=(2,), parent_id=1
)
FOLDER = Element(
    id=3,
    depth=2,
    role="TreeItem",
    name="Projects",
    box=(10, 10, 400, 40),
    runtime_id=(3,),
    parent_id=2,
)
LABEL = Element(
    id=4, depth=3, role="Text", name="Projects", box=(30, 15, 90, 35), runtime_id=(4,), parent_id=3
)
LEAF = Element(
    id=5,
    depth=2,
    role="TreeItem",
    name="Drafts",
    box=(10, 50, 400, 80),
    runtime_id=(5,),
    parent_id=2,
)


def _snapshot(*elements: Element) -> Snapshot:
    return Snapshot(
        id="s",
        hwnd=0x10,
        process="chrome.exe",
        title="Gallery",
        taken_at=1.0,
        elements=[DOC, TREE, *elements],
        node_count=len(elements) + 2,
        stable=True,
    )


def test_a_tree_item_may_take_the_physical_click() -> None:
    snapshot = _snapshot(FOLDER, LABEL, LEAF)
    assert physical_click_target(snapshot, FOLDER)
    assert physical_click_target(snapshot, LEAF)


def test_the_click_lands_on_the_items_own_label() -> None:
    snapshot = _snapshot(FOLDER, LABEL, LEAF)
    assert click_point(snapshot, FOLDER).id == LABEL.id
    assert click_point(snapshot, LEAF).id == LEAF.id, "no label child: the item itself"


def test_another_roles_click_point_is_itself() -> None:
    button = Element(
        id=6, depth=1, role="Button", name="Save", box=(0, 0, 50, 20), runtime_id=(6,), parent_id=1
    )
    text = Element(
        id=7, depth=2, role="Text", name="Save", box=(5, 5, 40, 15), runtime_id=(7,), parent_id=6
    )
    assert click_point(_snapshot(button, text), button).id == button.id
