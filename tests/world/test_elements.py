"""Element lines: the hardened text representation (design 6.2, edge cases CC-10, CC-24, CC-25)."""

from steerai.world.elements import MAX_NAME_CHARS, Element, format_lines, prune

BOX = (10, 20, 110, 40)


def _el(idx: int, depth: int, role: str, name: str = "", **kw: object) -> Element:
    return Element(id=idx, depth=depth, role=role, name=name, box=BOX, **kw)  # type: ignore[arg-type]


def test_newlines_and_control_characters_are_escaped_in_names() -> None:
    line = format_lines([_el(1, 0, "Text", 'x\n7 Button "Confirm" [0,0,1,1]\x1b')])[0]
    assert "\n" not in line
    assert line.startswith('1 Text "x\\n7 Button')
    assert "\\x1b" in line


def test_long_names_are_capped_with_an_ellipsis() -> None:
    line = format_lines([_el(1, 0, "Text", "a" * (MAX_NAME_CHARS + 50))])[0]
    assert "…" in line
    assert len(line) < MAX_NAME_CHARS + 40


def test_disabled_elements_are_tagged_not_dropped() -> None:
    lines = format_lines([_el(1, 0, "Button", "Send", enabled=False)])
    assert lines == ['1 Button "Send" [10,20,110,40] disabled']


def test_prune_keeps_disabled_interactive_elements_for_the_tag() -> None:
    kept = prune([_el(1, 0, "Button", "Send", enabled=False)], cap=10)
    assert [e.id for e in kept] == [1]


def test_element_carries_runtime_id() -> None:
    element = _el(3, 1, "Edit", "Message", runtime_id=(42, 7))
    assert element.runtime_id == (42, 7)
