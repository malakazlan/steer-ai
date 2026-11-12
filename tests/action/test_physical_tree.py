"""The physical click on a tree item ([v2.81]; principal items 2-3, hunter TE-01/08/14/15).

The aim is the item's own row, from the live rectangle; the hit test accepts the item and its
plain descendants, never a child item nor an acting control; a row outside the visible part is
refused `clipped`, and the last look before the button goes down recomputes the same aim."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from steerai.action.dispositions import Delivery
from steerai.action.physical import PhysicalAction
from steerai.errors import ErrorCode
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from tests.action.fakes import TYPE_IDS, FakeHandle
from tests.action.test_physical import CHILD_ID as BUTTON_CHILD
from tests.action.test_physical import (  # noqa: F401 - fast_clock is a fixture
    FakeInput,
    _executor,
    _snapshot,
    fast_clock,
)

ITEM_ID = (9, 1)
GROUP_ID = (9, 2)
CHILD_ID = (9, 3)
LABEL_ID = (9, 4)
BUTTON_ID = (9, 5)
BUTTON_TEXT_ID = (9, 6)
PARENT_ID = (9, 0)
OPEN_BOX = (132, 722, 1375, 767)
CLOSED_BOX = (132, 722, 1375, 745)
OWN_ROW_POINT = (753, 733)
CLICK = PhysicalAction(verb="click", element_id=1)


def _tree(
    item_box: tuple[int, int, int, int], *, open_: bool = True, button: bool = False
) -> Snapshot:
    """A Tree holding one item; open, it holds a Group with a child item (the gallery's shape)."""
    item_handle = FakeHandle(box=item_box, control_type=TYPE_IDS["TreeItem"], name="Projects")
    elements = [
        Element(
            id=0,
            depth=0,
            role="Tree",
            name="Folders",
            box=(82, 700, 1400, 800),
            runtime_id=PARENT_ID,
        ),
        Element(
            id=1,
            depth=1,
            role="TreeItem",
            name="Projects",
            box=item_box,
            runtime_id=ITEM_ID,
            parent_id=0,
        ),
    ]
    handles: list[Any] = [FakeHandle(control_type=50023, name="Folders"), item_handle]
    if open_:
        elements += [
            Element(
                id=2,
                depth=2,
                role="Group",
                name="",
                box=(132, 744, 1375, 767),
                runtime_id=GROUP_ID,
                parent_id=1,
            ),
            Element(
                id=3,
                depth=3,
                role="TreeItem",
                name="Alpha",
                box=(182, 744, 1375, 767),
                runtime_id=CHILD_ID,
                parent_id=2,
            ),
            Element(
                id=4,
                depth=4,
                role="Text",
                name="Alpha",
                box=(190, 746, 240, 765),
                runtime_id=LABEL_ID,
                parent_id=3,
            ),
        ]
        handles += [FakeHandle(name=""), FakeHandle(name="Alpha"), FakeHandle(name="Alpha")]
    if button:
        elements += [
            Element(
                id=5,
                depth=2,
                role="Button",
                name="More",
                box=(1300, 724, 1370, 742),
                runtime_id=BUTTON_ID,
                parent_id=1,
            ),
            Element(
                id=6,
                depth=3,
                role="Text",
                name="More",
                box=(1305, 726, 1365, 740),
                runtime_id=BUTTON_TEXT_ID,
                parent_id=5,
            ),
        ]
        handles += [FakeHandle(name="More"), FakeHandle(name="More")]
    return Snapshot(
        id="s1",
        hwnd=0x10,
        process="chrome.exe",
        title="Gallery",
        taken_at=0.0,
        elements=elements,
        node_count=len(elements),
        stable=True,
        pid=777,
        handles=handles,
    )


def _inputs(*hits: tuple[int, ...]) -> FakeInput:
    return FakeInput(hits=list(hits) or [ITEM_ID], focused=ITEM_ID, window_box=(0, 0, 1500, 900))


def test_an_open_item_is_clicked_on_its_own_row_not_its_centre(fast_clock: Any) -> None:  # noqa: F811
    """Measured: the centre of the open item is y=744, the child's top row (3/3 selected it)."""
    inputs = _inputs()
    result = _executor(inputs).execute(CLICK, _tree(OPEN_BOX))
    assert inputs.clicks == [OWN_ROW_POINT]
    assert result.evidence["clicked_at"] == list(OWN_ROW_POINT)
    assert result.evidence["hit_after"] == "target"


def test_a_closed_item_is_clicked_at_its_centre(fast_clock: Any) -> None:  # noqa: F811
    inputs = _inputs()
    _executor(inputs).execute(CLICK, _tree(CLOSED_BOX, open_=False))
    assert inputs.clicks == [(753, 733)]


def test_the_row_is_aimed_on_the_live_rectangle(fast_clock: Any) -> None:  # noqa: F811
    """The snapshot's row height applied to where the item is now (scrolled 100 px up)."""
    snapshot = _tree(OPEN_BOX)
    snapshot.handles[1].box = (132, 622, 1375, 667)
    inputs = _inputs()
    _executor(inputs).execute(CLICK, snapshot)
    assert inputs.clicks == [(753, 633)]


def test_a_point_on_a_child_item_is_refused(fast_clock: Any) -> None:  # noqa: F811
    """TE-01: the child is in the item's subtree, and the subtree is no longer enough."""
    inputs = _inputs(CHILD_ID)
    result = _executor(inputs).execute(CLICK, _tree(OPEN_BOX))
    assert inputs.clicks == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.evidence["detail"] == "covered_in_window"


def test_a_point_on_a_child_items_label_is_refused(fast_clock: Any) -> None:  # noqa: F811
    inputs = _inputs(LABEL_ID)
    result = _executor(inputs).execute(CLICK, _tree(OPEN_BOX))
    assert inputs.clicks == []
    assert result.evidence["detail"] == "covered_in_window"


def test_a_point_on_the_group_holding_the_children_is_refused(fast_clock: Any) -> None:  # noqa: F811
    """#235: the group is the children's list, so a point on it is not the item's row."""
    inputs = _inputs(GROUP_ID)
    result = _executor(inputs).execute(CLICK, _tree(OPEN_BOX))
    assert inputs.clicks == []
    assert result.evidence["detail"] == "covered_in_window"


def test_a_point_on_an_acting_control_in_the_row_is_refused(fast_clock: Any) -> None:  # noqa: F811
    """TE-01: a Delete button the page exposes in the row, however it got under the point."""
    for hit in (BUTTON_ID, BUTTON_TEXT_ID):
        inputs = _inputs(hit)
        result = _executor(inputs).execute(CLICK, _tree(OPEN_BOX, button=True))
        assert inputs.clicks == []
        assert result.evidence["detail"] == "covered_in_window"


def test_the_last_look_refuses_a_child_under_the_point(fast_clock: Any) -> None:  # noqa: F811
    """TE-15: the re-check before the button goes down uses the same narrow set."""
    inputs = _inputs(ITEM_ID, CHILD_ID)
    result = _executor(inputs).execute(CLICK, _tree(OPEN_BOX))
    assert inputs.clicks == []
    assert result.evidence["detail"] == "target_moved"


def test_hit_after_on_a_child_reads_other(fast_clock: Any) -> None:  # noqa: F811
    inputs = _inputs(ITEM_ID, ITEM_ID, CHILD_ID)
    result = _executor(inputs).execute(CLICK, _tree(OPEN_BOX))
    assert inputs.clicks == [OWN_ROW_POINT]
    assert result.evidence["hit_after"] == "other"


def test_a_row_above_the_window_is_refused_clipped(fast_clock: Any) -> None:  # noqa: F811
    """TE-08: scrolled so only the children show; the row is outside the visible part."""
    snapshot = _tree(OPEN_BOX)
    snapshot.handles[1].box = (132, -10, 1375, 35)  # own row -10..12, window from 0, aim y=1
    inputs = FakeInput(hits=[ITEM_ID], focused=ITEM_ID, window_box=(0, 5, 1500, 900))
    result = _executor(inputs).execute(CLICK, snapshot)
    assert inputs.moves == []
    assert inputs.clicks == []
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "clipped"


def test_a_row_that_moved_before_the_click_is_target_moved(fast_clock: Any) -> None:  # noqa: F811
    """TE-14: the last look recomputes the own-row aim from the live rectangle."""
    snapshot = _tree(OPEN_BOX)
    item = snapshot.handles[1]
    inputs = _inputs()

    def shift(*_a: Any) -> None:
        item.box = (132, 742, 1375, 787)

    inputs.after_move = shift
    result = _executor(inputs).execute(CLICK, snapshot)
    assert inputs.clicks == []
    assert result.evidence["detail"] == "target_moved"


def test_a_horizontal_tree_is_refused_no_own_row(fast_clock: Any) -> None:  # noqa: F811
    snapshot = _tree(OPEN_BOX)
    elements = list(snapshot.elements)
    elements[3] = Element(
        id=3,
        depth=3,
        role="TreeItem",
        name="Alpha",
        box=(700, 722, 900, 767),
        runtime_id=CHILD_ID,
        parent_id=2,
    )
    elements[2] = Element(
        id=2,
        depth=2,
        role="Group",
        name="",
        box=(700, 722, 900, 767),
        runtime_id=GROUP_ID,
        parent_id=1,
    )
    inputs = _inputs()
    result = _executor(inputs).execute(CLICK, replace(snapshot, elements=elements))
    assert inputs.clicks == []
    assert result.evidence["detail"] == "no_own_row"


def test_another_roles_click_keeps_its_whole_subtree(fast_clock: Any) -> None:  # noqa: F811
    """A Button's Text child under the point is the Button (unchanged, #149)."""
    inputs = FakeInput(hits=[BUTTON_CHILD])
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == [(60, 30)]
    assert result.evidence["hit_after"] == "target"
