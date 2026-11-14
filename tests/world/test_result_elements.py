"""`result_elements` (§6.4, [V2.29] Q1): the elements the observer's `results` carrier rests on,
which the verifier applies its own exclusions to. Each case asserts both sides, so the observer
and the verifier can never disagree on what a result is."""

from __future__ import annotations

from collections.abc import Sequence

from steerai.world.effects import observe, result_elements
from tests.world.test_effects_conditions import Row
from tests.world.test_effects_scope import BROWSER, FIELD, ITEM, WINDOW, _obs, _renamed

WORDS = "red shoes"
RESULT: Row = (14, "ListItem", "Red shoes", (9, 14), 12)


def _both(
    before: Sequence[Row], after: Sequence[Row], *, text: str, acted: Row | None = FIELD
) -> tuple[list[int], bool]:
    """The result ids `result_elements` gives and whether `observe` reports the carrier."""
    b, a = _obs(before), _obs(after)
    rid = None if acted is None else acted[3]
    ids = [e.id for e in result_elements(b, a, acted=rid, text=text)]
    return ids, "results" in observe(b, a, acted=rid, text=text).carriers


def test_a_new_result_in_the_page_is_a_result_on_both_sides() -> None:
    assert _both(BROWSER, [*BROWSER, RESULT], text=WORDS) == ([RESULT[0]], True)


def test_a_short_single_token_is_never_a_result() -> None:
    tiny: Row = (15, "Text", "ab", (9, 15), 12)
    assert _both(BROWSER, [*BROWSER, tiny], text="ab") == ([], False)


def test_words_outside_the_acted_fields_document_are_never_a_result() -> None:
    outside: Row = (16, "Text", WORDS, (9, 16), WINDOW[0])
    assert _both(BROWSER, [*BROWSER, outside], text=WORDS) == ([], False)


def test_the_acted_fields_own_list_is_exempt() -> None:
    group: Row = (20, "Group", "", (9, 20), 10)
    box: Row = (21, "Edit", "Find", (9, 21), 20)
    hint: Row = (22, "ListItem", WORDS, (9, 22), 20)
    before = [*BROWSER, group, box]
    assert _both(before, [*before, hint], text=WORDS, acted=box) == ([], False)
    assert _both(before, [*before, hint], text=WORDS, acted=None) == ([hint[0]], True)


def test_words_in_the_scope_before_under_another_runtime_id_leave_no_new_result() -> None:
    old: Row = (17, "Text", "Red shoes sale", (9, 17), 12)
    before = [*BROWSER, old]
    assert _both(before, [*before, RESULT], text=WORDS) == ([], False)


def test_an_element_renamed_to_the_words_is_a_result() -> None:
    after = _renamed(BROWSER, ITEM, "Red shoes")
    assert _both(BROWSER, after, text=WORDS) == ([ITEM[0]], True)
