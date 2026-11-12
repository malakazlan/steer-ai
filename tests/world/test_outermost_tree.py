"""`outermost` folds a same-named DataItem cell into its row, never a tree item into another
([v2.81], hunter TE-02): a folder `src` inside a folder `src` is two items."""

from __future__ import annotations

from steerai.world.elements import Element, outermost


def _el(element_id: int, role: str, name: str, parent: int | None) -> Element:
    return Element(
        id=element_id,
        depth=0,
        role=role,
        name=name,
        box=(0, element_id * 10, 100, element_id * 10 + 10),
        runtime_id=(element_id,),
        parent_id=parent,
    )


def test_a_grid_rows_same_named_cell_folds_into_the_row() -> None:
    row, cell = _el(1, "DataItem", "Order 1001", None), _el(2, "DataItem", "Order 1001", 1)
    assert outermost([row, cell], [row, cell]) == [row]


def test_nested_same_named_tree_items_are_both_kept() -> None:
    outer = _el(1, "TreeItem", "src", None)
    group = _el(2, "Group", "", 1)
    inner = _el(3, "TreeItem", "src", 2)
    assert outermost([outer, inner], [outer, group, inner]) == [outer, inner]
