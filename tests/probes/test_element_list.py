"""Structured element list: depth, role, name, box; pruned and capped for a decision model."""

from steerai.probes.element_list import Element, format_lines, prune

BOX = (10, 20, 110, 40)


def _el(idx: int, depth: int, role: str, name: str = "", offscreen: bool = False) -> Element:
    return Element(id=idx, depth=depth, role=role, name=name, box=BOX, offscreen=offscreen)


def test_format_lines_indents_by_depth_and_shows_box() -> None:
    lines = format_lines([_el(1, 0, "Window", "Inbox"), _el(2, 1, "Button", "Send")])
    assert lines == [
        '1 Window "Inbox" [10,20,110,40]',
        '2   Button "Send" [10,20,110,40]',
    ]


def test_prune_keeps_interactive_and_named_text_drops_unnamed_containers() -> None:
    elements = [
        _el(1, 0, "Window", "Inbox"),
        _el(2, 1, "Group"),  # unnamed container: dropped
        _el(3, 2, "Button", "Send"),
        _el(4, 2, "Text"),  # unnamed text: dropped
        _el(5, 2, "Text", "hello"),
        _el(6, 2, "Image"),  # unnamed image: dropped
    ]
    kept = prune(elements, cap=100)
    assert [e.id for e in kept] == [1, 3, 5]


def test_prune_drops_offscreen_elements() -> None:
    kept = prune([_el(1, 0, "Button", "A"), _el(2, 0, "Button", "B", offscreen=True)], cap=100)
    assert [e.id for e in kept] == [1]


def test_prune_caps_and_reports_remainder() -> None:
    elements = [_el(i, 0, "Button", f"b{i}") for i in range(1, 8)]
    kept = prune(elements, cap=5)
    assert len(kept) == 5
    assert format_lines(kept, remaining=2)[-1] == "(... 2 more below)"
