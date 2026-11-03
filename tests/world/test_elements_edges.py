"""Element list boundaries: empty lists, caps of 0 and 1, negative cap, names at and over the
cap, names that are only control characters, line-separator characters, escape splitting."""

import pytest

from steerai.world.elements import MAX_NAME_CHARS, Element, format_lines, prune, safe_name

BOX = (10, 20, 110, 40)


def _el(idx: int, role: str, name: str = "", *, offscreen: bool = False) -> Element:
    return Element(id=idx, depth=0, role=role, name=name, box=BOX, offscreen=offscreen)


# --- prune ------------------------------------------------------------------------------------


def test_prune_of_an_empty_list_is_empty() -> None:
    assert prune([], cap=150) == []


def test_prune_rejects_cap_below_one() -> None:
    # Edge case WM-26: a cap of 0 or less is a caller bug, never a silent slice.
    with pytest.raises(ValueError, match="cap must be >= 1"):
        prune([_el(1, "Button", "a")], cap=0)


def test_prune_with_cap_one_keeps_only_the_first_eligible_in_tree_order() -> None:
    elements = [_el(1, "Group"), _el(2, "Button", "a"), _el(3, "Button", "b")]
    assert [e.id for e in prune(elements, cap=1)] == [2]


def test_prune_with_cap_equal_to_eligible_keeps_all() -> None:
    elements = [_el(1, "Button", "a"), _el(2, "Button", "b")]
    assert [e.id for e in prune(elements, cap=2)] == [1, 2]


def test_prune_rejects_negative_cap() -> None:
    with pytest.raises(ValueError, match="cap must be >= 1"):
        prune([_el(1, "Button", "a"), _el(2, "Button", "b")], cap=-1)


def test_prune_keeps_a_name_made_only_of_control_characters() -> None:
    assert [e.id for e in prune([_el(1, "Text", "\x00\x01")], cap=5)] == [1]


def test_prune_keeps_a_whitespace_only_name() -> None:
    assert [e.id for e in prune([_el(1, "Text", " ")], cap=5)] == [1]


def test_prune_drops_an_offscreen_interactive_element_even_with_a_name() -> None:
    assert prune([_el(1, "Button", "a", offscreen=True)], cap=5) == []


def test_prune_keeps_an_unnamed_interactive_element() -> None:
    assert [e.id for e in prune([_el(1, "Edit")], cap=5)] == [1]


# --- safe_name --------------------------------------------------------------------------------


def test_safe_name_of_empty_string_is_empty() -> None:
    assert safe_name("") == ""


def test_safe_name_of_only_control_characters_is_all_escapes_on_one_line() -> None:
    assert safe_name("\x00\x01\n\r\t") == "\\x00\\x01\\n\\r\\t"


def test_safe_name_exactly_at_the_cap_is_untouched() -> None:
    name = "a" * MAX_NAME_CHARS
    assert safe_name(name) == name
    assert len(safe_name(name)) == MAX_NAME_CHARS


def test_safe_name_one_over_the_cap_keeps_head_and_tail_with_an_ellipsis() -> None:
    # Edge case WM-19: head-and-tail truncation so trailing discriminators survive.
    out = safe_name("a" * (MAX_NAME_CHARS + 1))
    assert len(out) == MAX_NAME_CHARS
    assert out.startswith("a" * 10)
    assert out.endswith("a" * 10)
    assert "…" in out


def test_safe_name_cap_applies_after_escaping() -> None:
    # 119 printable chars plus one newline escape to two chars: 121 > cap, so it truncates
    out = safe_name("a" * (MAX_NAME_CHARS - 1) + "\n")
    assert len(out) == MAX_NAME_CHARS
    assert out.endswith("\\n")


def test_safe_name_tail_keeps_a_trailing_escape_whole() -> None:
    # A trailing escape lands in the kept tail, so it is never cut in half.
    out = safe_name("a" * (MAX_NAME_CHARS + 20) + "\x1b")
    assert out.endswith("\\x1b")


def test_safe_name_escapes_unicode_line_and_paragraph_separators() -> None:
    assert safe_name("x\u2028y\u2029z") == "x\\u2028y\\u2029z"


def test_safe_name_escapes_next_line_and_zero_width_characters() -> None:
    assert safe_name("x\x85y\u200bz") == "x\\x85y\\u200bz"


def test_safe_name_keeps_printable_non_ascii() -> None:
    assert safe_name("Amir · hi · 2h ✓") == "Amir · hi · 2h ✓"


def test_safe_name_does_not_escape_double_quotes() -> None:
    """Documents current behaviour: a quote inside a name is kept, so a line parser must not
    treat the first closing quote as the end of the name."""
    assert safe_name('say "hi"') == 'say "hi"'


# --- format_lines -----------------------------------------------------------------------------


def test_format_lines_of_an_empty_list_is_empty() -> None:
    assert format_lines([]) == []


def test_format_lines_with_zero_remaining_has_no_note() -> None:
    assert format_lines([_el(1, "Button", "a")], remaining=0) == ['1 Button "a" [10,20,110,40]']


def test_format_lines_with_negative_remaining_has_no_note() -> None:
    assert format_lines([], remaining=-1) == []


def test_format_lines_with_one_remaining_adds_the_note() -> None:
    assert format_lines([], remaining=1) == ["(... 1 more not shown)"]


def test_format_lines_control_only_name_renders_as_escapes() -> None:
    assert format_lines([_el(1, "Text", "\x00")]) == ['1 Text "\\x00" [10,20,110,40]']


def test_format_lines_disabled_tag_follows_the_box_even_with_a_capped_name() -> None:
    line = format_lines(
        [Element(id=1, depth=0, role="Button", name="a" * 200, box=BOX, enabled=False)]
    )[0]
    assert line.endswith('" [10,20,110,40] disabled')
    assert "…" in line


def test_format_lines_every_line_is_a_single_line() -> None:
    names = ["a\nb", "c\rd", "e\u2028f", "\x0bg", "h\x0c"]
    lines = format_lines([_el(i, "Text", n) for i, n in enumerate(names, 1)])
    assert len(lines) == 5
    assert all(len(line.splitlines()) == 1 for line in lines)
