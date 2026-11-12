"""A tree item's own row ([v2.81], packet tree-expand-2026-11-12 v2): the band from the item's top
to its first child item, where a person clicks to open or close it. Measured (Chrome 157,
gallery): an open item's box covers its children and its centre is the child's top row, so a
centre click selects the child (3/3)."""

from __future__ import annotations

import pytest

from steerai.action.execution import RefusalError
from steerai.action.executor import click_point, physical_click_target
from steerai.action.tree import MIN_OWN_ROW_PX, own_row
from steerai.errors import ErrorCode
from steerai.world.elements import Element
from steerai.world.model import Snapshot

DOC = Element(
    id=1, depth=0, role="Document", name="Gallery", box=(0, 0, 1500, 900), runtime_id=(1,)
)
TREE = Element(
    id=2,
    depth=1,
    role="Tree",
    name="Folders",
    box=(82, 722, 1375, 767),
    runtime_id=(2,),
    parent_id=1,
)


def _item(
    element_id: int, name: str, box: tuple[int, int, int, int], parent: int, depth: int
) -> Element:
    return Element(
        id=element_id,
        depth=depth,
        role="TreeItem",
        name=name,
        box=box,
        runtime_id=(element_id,),
        parent_id=parent,
    )


def _el(
    element_id: int, role: str, name: str, box: tuple[int, int, int, int], parent: int
) -> Element:
    return Element(
        id=element_id,
        depth=9,
        role=role,
        name=name,
        box=box,
        runtime_id=(element_id,),
        parent_id=parent,
    )


OPEN = _item(3, "Projects", (132, 722, 1375, 767), 2, 2)
GROUP = _el(4, "Group", "", (132, 744, 1375, 767), 3)
ALPHA = _item(5, "Alpha", (182, 744, 1375, 767), 4, 4)
CLOSED = _item(3, "Projects", (132, 722, 1375, 745), 2, 2)


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


def test_an_open_items_row_ends_at_its_child_group() -> None:
    """The measured gallery shape: the row is 722-744, the centre would be 744 (the child)."""
    assert own_row(_snapshot(OPEN, GROUP, ALPHA), OPEN) == 22


def test_a_row_ends_at_a_child_item_without_a_group() -> None:
    alpha = _item(5, "Alpha", (182, 744, 1375, 767), 3, 3)
    assert own_row(_snapshot(OPEN, alpha), OPEN) == 22


def test_a_closed_item_has_no_band_its_centre_is_its_row() -> None:
    assert own_row(_snapshot(CLOSED), CLOSED) is None


def test_an_icon_or_text_inside_the_row_never_ends_it() -> None:
    """Hunter TE-07: only a child item, or the group holding one, ends the row."""
    icon = _el(6, "Image", "folder", (136, 726, 150, 740), 3)
    label = _el(7, "Text", "Projects", (154, 724, 220, 742), 3)
    assert own_row(_snapshot(OPEN, icon, label, GROUP, ALPHA), OPEN) == 22


def test_a_group_without_a_child_item_never_ends_the_row() -> None:
    group = _el(4, "Group", "", (132, 744, 1375, 767), 3)
    note = _el(6, "Text", "empty folder", (182, 744, 400, 767), 4)
    assert own_row(_snapshot(OPEN, group, note), OPEN) is None


def test_a_hidden_child_item_with_no_area_is_ignored() -> None:
    hidden = _item(5, "Alpha", (0, 0, 0, 0), 3, 3)
    assert own_row(_snapshot(CLOSED, hidden), CLOSED) is None


@pytest.mark.parametrize("child_top", [722, 722 + MIN_OWN_ROW_PX - 1, 700])
def test_a_child_item_at_or_above_the_items_top_leaves_no_row(child_top: int) -> None:
    """Hunter TE-11/12: a zero-height wrapper, a column-reverse or a horizontal tree."""
    child = _item(5, "Alpha", (132, child_top, 1375, child_top + 22), 3, 3)
    with pytest.raises(RefusalError) as info:
        own_row(_snapshot(OPEN, child), OPEN)
    assert info.value.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert info.value.detail == "no_own_row"


def test_the_nearest_child_item_ends_the_row() -> None:
    late = _item(6, "Beta", (182, 770, 1375, 790), 3, 3)
    early = _item(5, "Alpha", (182, 744, 1375, 767), 3, 3)
    assert own_row(_snapshot(OPEN, late, early), OPEN) == 22


def test_a_non_tree_role_has_no_row() -> None:
    button = _el(6, "Button", "Save", (0, 0, 50, 20), 1)
    assert own_row(_snapshot(button), button) is None


# ---- the label and the acting-child guard skip child items (TE-06, TE-10) -----------------------


def test_the_label_search_never_takes_a_child_items_label() -> None:
    """A child named like its parent (src > src): its label is the child's, not the parent's."""
    child = _item(5, "Projects", (182, 744, 1375, 767), 3, 3)
    child_label = _el(6, "Text", "Projects", (190, 746, 260, 765), 5)
    assert click_point(_snapshot(OPEN, child, child_label), OPEN).id == OPEN.id


def test_the_items_own_label_is_still_taken() -> None:
    label = _el(6, "Text", "Projects", (154, 724, 220, 742), 3)
    assert click_point(_snapshot(OPEN, label, GROUP, ALPHA), OPEN).id == label.id


def test_a_link_inside_a_child_item_never_refuses_the_parents_click() -> None:
    link = _el(6, "Hyperlink", "Alpha", (190, 746, 260, 765), 5)
    assert physical_click_target(_snapshot(OPEN, GROUP, ALPHA, link), OPEN)


def test_a_button_in_the_items_own_row_still_refuses_it() -> None:
    twisty = _el(6, "Button", "Toggle", (134, 724, 150, 742), 3)
    assert not physical_click_target(_snapshot(OPEN, twisty, GROUP, ALPHA), OPEN)
