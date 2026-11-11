"""Twin fields (#150, design D-150 v2): two page-level copies of one field, one rule for step
targets and conditions. Measured: docs.python.org/3/ shows `Edit "Quick search"` aid
`search-box` in the header Related bar and again in the footer copy, offscreen (probe twins)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.twins import named_pair, on_screen, twin_set

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


# ---- a field typed into renames its content-named ancestor (#150 typed, review of #172) ------


def _typed(top_value: str, top_row: str = "Go|", bottom_row: str = "Go|") -> Snapshot:
    """Sphinx's ListItem is named from its content: "Go|" empty, "<text>Go|" after typing."""
    rows = _chain(10)
    rows[1] = replace(rows[1], name=top_row)
    other = _chain(20)
    other[1] = replace(other[1], name=bottom_row)
    return _snapshot(*rows, *other, TOP, BOTTOM)


def _values(top: str | None, bottom: str | None) -> Callable[[Element], str | None]:
    found = {TOP.runtime_id: top, BOTTOM.runtime_id: bottom}
    return lambda element: found[element.runtime_id]


def test_a_typed_twin_stays_a_twin_once_its_value_is_set_aside() -> None:
    snap = _typed("pathlib", top_row="pathlibGo|")
    assert not twin_set([TOP, BOTTOM], snap), "no live values: the chains differ"
    assert twin_set([TOP, BOTTOM], snap, value_of=_values("pathlib", ""))
    spaced = _typed("pathlib", top_row="pathlib Go |", bottom_row="Go |")
    assert twin_set([TOP, BOTTOM], spaced, value_of=_values("pathlib", ""))


def test_rows_that_differ_by_their_own_values_are_never_twins() -> None:
    cart = _typed("1", top_row="Qty 1 Remove", bottom_row="Qty 2 Remove")
    assert not twin_set([TOP, BOTTOM], cart, value_of=_values("1", "2")), "two values"
    assert not twin_set([TOP, BOTTOM], cart, value_of=_values("1", "")), "still differs"
    collide = _typed("1", top_row="1Go|")
    assert not twin_set([TOP, BOTTOM], collide, value_of=_values("1", "2")), "two values"


def test_only_one_occurrence_of_the_value_is_set_aside() -> None:
    snap = _typed("pathlib", top_row="pathlib pathlibGo|")
    assert not twin_set([TOP, BOTTOM], snap, value_of=_values("pathlib", ""))


def test_an_unreadable_value_is_never_a_twin_set() -> None:
    snap = _typed("pathlib", top_row="pathlibGo|")
    assert not twin_set([TOP, BOTTOM], snap, value_of=_values(None, ""))
    assert not twin_set([TOP, BOTTOM], snap, value_of=_values("pathlib", None))


# ---- a named pair: two same-named fields that are not copies (H4 dr1/deb1, TW-45) ------------


def test_two_same_named_fields_with_distinct_ids_are_a_named_pair() -> None:
    """docs.rs: `nav-search` and `search`, both on screen; Debian: `kw` and `keyword`, both
    below the fold. Distinct forms, so never a silent pick: the class exists to ask."""
    header = replace(TOP, automation_id="nav-search")
    body = replace(BOTTOM, automation_id="search", offscreen=False)
    assert named_pair([header, body], _page(header, body))
    assert not twin_set([header, body], _page(header, body)), "distinct ids: not copies"
    both_empty = (replace(TOP, automation_id=""), replace(BOTTOM, automation_id=""))
    assert named_pair(list(both_empty), _page(*both_empty)), "ids may both be empty"


def test_the_named_pair_rejects_every_other_shape() -> None:
    header = replace(TOP, automation_id="nav-search")
    body = replace(BOTTOM, automation_id="search")
    third = replace(_edit(70, 11, 300), automation_id="footer")
    assert not named_pair([header, body, third], _page(header, body, third)), "three"
    assert not named_pair([header], _page(header)), "one"
    for label, other in [
        ("secure", replace(body, secure=True)),
        ("empty runtime id", replace(body, runtime_id=())),
        ("same runtime id", replace(body, runtime_id=header.runtime_id)),
        ("another name", replace(body, name="Quick find")),
        ("another role", replace(body, role="ComboBox")),
    ]:
        assert not named_pair([header, other], _page(header, other)), label
    frame = Element(
        id=5, depth=1, role="Document", name="Ad", box=(0, 0, 9, 9), runtime_id=(5,), parent_id=0
    )
    in_frame = _snapshot(frame, *_chain(10), *_chain(20, doc=5), header, body)
    assert not named_pair([header, body], in_frame), "another Document (iframe)"
    rows = _snapshot(*_chain(10), *_chain(20, name="Cart row 2"), header, body)
    assert named_pair([header, body], rows), "chains may differ: the ask is the guard"
    copies = [TOP, BOTTOM]
    assert twin_set(copies, _page(*copies))
    assert not named_pair(copies, _page(*copies)), "copies are a twin set, never a pair"
