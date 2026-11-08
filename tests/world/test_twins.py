"""Twin fields (#150, design D-150 v2): two page-level copies of one field, one rule for step
targets and conditions. Measured: docs.python.org/3/ shows `Edit "Quick search"` aid
`search-box` in the header Related bar and again in the footer copy, offscreen (probe twins)."""

from __future__ import annotations

from dataclasses import replace

from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.twins import on_screen, twin_set

WINDOW = Element(
    id=0,
    depth=0,
    role="Window",
    name="Docs - Google Chrome",
    box=(0, 0, 1280, 900),
    runtime_id=(1,),
)


def _chain(base: int, doc: int = 1, name: str = "Related") -> list[Element]:
    """Document(doc) < Group 'Related' < ListItem 'Go|' < Edit, ids from `base`."""
    return [
        Element(
            id=base,
            depth=2,
            role="Group",
            name=name,
            box=(0, 0, 9, 9),
            runtime_id=(base,),
            parent_id=doc,
        ),
        Element(
            id=base + 1,
            depth=3,
            role="ListItem",
            name="Go|",
            box=(0, 0, 9, 9),
            runtime_id=(base + 1,),
            parent_id=base,
        ),
    ]


def _edit(eid: int, parent: int, y: int, offscreen: bool = False, **kw: object) -> Element:
    fields: dict[str, object] = {
        "id": eid,
        "depth": 4,
        "role": "Edit",
        "name": "Quick search",
        "box": (989, y, 1169, y + 26),
        "offscreen": offscreen,
        "automation_id": "search-box",
        "runtime_id": (eid, 7),
        "parent_id": parent,
    }
    fields.update(kw)
    return Element(**fields)  # type: ignore[arg-type]  # test builder over the dataclass fields


def _snapshot(*elements: Element) -> Snapshot:
    doc = Element(
        id=1,
        depth=1,
        role="Document",
        name="Docs",
        box=(0, 100, 1280, 900),
        runtime_id=(2,),
        parent_id=0,
    )
    return Snapshot(
        id="s",
        hwnd=0x10,
        process="chrome.exe",
        title="Docs",
        taken_at=1.0,
        elements=[WINDOW, doc, *elements],
        node_count=len(elements) + 2,
        stable=True,
    )


TOP = _edit(50, 11, 183)
BOTTOM = _edit(60, 21, 1871, offscreen=True)


def _page(*edits: Element, extra: tuple[Element, ...] = ()) -> Snapshot:
    return _snapshot(*_chain(10), *_chain(20), *extra, *edits)


def test_two_page_level_copies_of_one_field_are_twins() -> None:
    snap = _page(TOP, BOTTOM)
    assert twin_set([TOP, BOTTOM], snap)


def test_the_twin_set_rejects_every_other_shape() -> None:
    third = _edit(70, 11, 300)
    snap = _page(TOP, BOTTOM, third)
    assert not twin_set([TOP, BOTTOM, third], snap), "three: over the cap"
    for label, other in [
        ("empty aid", replace(BOTTOM, automation_id="")),
        ("another aid", replace(BOTTOM, automation_id="q")),
        ("secure", replace(BOTTOM, secure=True)),
        ("empty runtime id", replace(BOTTOM, runtime_id=())),
        ("same runtime id", replace(BOTTOM, runtime_id=TOP.runtime_id)),
    ]:
        assert not twin_set([TOP, other], _page(TOP, other)), label
    both_empty = (replace(TOP, automation_id=""), replace(BOTTOM, automation_id=""))
    assert not twin_set(list(both_empty), _page(*both_empty)), "an empty aid is never shared"


def test_twins_in_another_frame_or_row_are_not_twins() -> None:
    frame = Element(
        id=5, depth=1, role="Document", name="Ad", box=(0, 0, 9, 9), runtime_id=(5,), parent_id=0
    )
    in_frame = _snapshot(frame, *_chain(10), *_chain(20, doc=5), TOP, BOTTOM)
    assert not twin_set([TOP, BOTTOM], in_frame), "another Document (iframe)"
    rows = _snapshot(*_chain(10), *_chain(20, name="Cart row 2"), TOP, BOTTOM)
    assert not twin_set([TOP, BOTTOM], rows), "a differing ancestor (template rows)"
    orphan = replace(BOTTOM, parent_id=None)
    assert not twin_set([TOP, orphan], _page(TOP, orphan)), "no Document above"


def test_on_screen_needs_area_inside_the_window_and_no_offscreen_flag() -> None:
    snap = _page(TOP, BOTTOM)
    assert on_screen(TOP, snap)
    assert not on_screen(BOTTOM, snap)
    assert not on_screen(replace(TOP, box=(989, 183, 989, 209)), snap), "zero area"
    assert not on_screen(replace(TOP, box=(989, 1871, 1169, 1897)), snap), "below the window"
