"""§7.1 `navigated` and the code-only auto proof ([V2.29] Q1), and the field-owned rules ([V2.21]
#9) they share. A subgoal is navigated since its baseline when: the URL changed or the top
Document was replaced; the URL after is read with an http(s) or file scheme; a carrier holds the
goal-span words (URL parameter, URL path, title, heading); and a page-content signal holds (a
title or heading carrier, cited evidence in the delta, or, for the auto proof, the `results`
carrier together with a URL carrier, under its own proof token)."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from steerai.loop2.owned import field_owned
from steerai.loop2.task import GoalSpan, Subgoal
from steerai.loop2.verify import auto_proof, navigated_tokens
from steerai.world.effects import Observed
from steerai.world.elements import Element
from steerai.world.landmarks import Landmark
from tests.loop2.world_fakes import DOC, FIELD, GOAL, QUERY_PAGE, RESULT, URL, element, observed

START = GOAL.index("red shoes")
SUB = Subgoal(
    id="s1", desc="search", params={"text": GoalSpan.of(GOAL, START, START + len("red shoes"))}
)
QUERY_URL = URL + "?q=red%20shoes"
BEFORE = dataclasses.replace(observed(QUERY_PAGE), document_url=URL)


def page(
    extra: Sequence[Element] = (),
    *,
    url: str = QUERY_URL,
    title: str | None = None,
    landmarks: dict[tuple[int, ...], Landmark | None] | None = None,
) -> Observed:
    seen = observed([*QUERY_PAGE, *extra])
    if title is not None:
        seen = dataclasses.replace(seen, snapshot=dataclasses.replace(seen.snapshot, title=title))
    marks = (
        landmarks if landmarks is not None else {e.runtime_id: None for e in seen.snapshot.elements}
    )
    return dataclasses.replace(seen, url=url, document_url=url, landmarks=marks)


def before() -> Observed:
    marks = {e.runtime_id: None for e in BEFORE.snapshot.elements}
    return dataclasses.replace(BEFORE, landmarks=marks)


TYPED = (FIELD.runtime_id,)


# ---- the auto proof --------------------------------------------------------------------------


def test_a_url_carrier_and_a_new_result_prove_it_with_their_own_token() -> None:
    assert auto_proof(SUB, before(), page([RESULT]), typed=TYPED) == "results_with_url"


def test_the_same_page_without_the_url_change_needs_a_claim() -> None:
    """The lead's first new test ([V2.29] Q1): no URL carrier, so no auto proof."""
    assert auto_proof(SUB, before(), page([RESULT], url=URL), typed=TYPED) is None


def test_a_url_change_without_the_words_and_a_new_result_is_no_proof() -> None:
    """The results carrier counts only beside a URL carrier ([V2.29] Q1): a URL that moved but
    does not carry the words leaves the model to claim."""
    assert auto_proof(SUB, before(), page([RESULT], url=URL + "results/123"), typed=TYPED) is None


def test_a_result_present_before_the_baseline_is_no_content_signal() -> None:
    """The results carrier needs an element new since the subgoal's baseline."""
    held = dataclasses.replace(observed([*QUERY_PAGE, RESULT]), document_url=URL)
    base = dataclasses.replace(held, landmarks={e.runtime_id: None for e in held.snapshot.elements})
    assert auto_proof(SUB, base, page([RESULT]), typed=TYPED) is None


def test_a_suggestion_list_holding_the_words_does_not_close_it() -> None:
    """The lead's second new test: the only element holding the words is field-owned (under
    the field's ComboBox), so it is no content signal, URL carrier or not."""
    combo = element(40, "ComboBox", "Search", (9, 40), DOC.id)
    box = element(41, "Edit", "Search", (9, 41), 40)
    hint = element(42, "ListItem", "red shoes", (9, 42), 40)
    shown = observed([*QUERY_PAGE, combo, box])
    base = dataclasses.replace(
        shown,
        document_url=URL,
        landmarks={e.runtime_id: None for e in shown.snapshot.elements},
    )
    after = page([combo, box, hint])
    assert auto_proof(SUB, base, after, typed=(box.runtime_id,)) is None


def test_a_title_carrier_with_a_url_change_proves_it() -> None:
    after = page(title="red shoes - Shop")
    assert auto_proof(SUB, before(), after, typed=TYPED) == "title"


def test_an_error_page_is_never_navigated() -> None:
    """Even with the words in its URL and a new result, a `chrome-error:` page is not a page."""
    after = page([RESULT], url="chrome-error://chromewebdata/?q=red%20shoes")
    assert auto_proof(SUB, before(), after, typed=TYPED) is None


def test_an_unread_url_after_is_never_navigated() -> None:
    after = dataclasses.replace(page([RESULT]), url=None, document_url=None)
    assert auto_proof(SUB, before(), after, typed=TYPED) is None


def test_a_result_under_a_menu_is_no_content_signal() -> None:
    menu = element(50, "Menu", "", (9, 50), DOC.id)
    item = element(51, "MenuItem", "red shoes", (9, 51), 50)
    assert auto_proof(SUB, before(), page([menu, item]), typed=TYPED) is None


def test_a_selectable_option_holding_typed_words_is_no_content_signal() -> None:
    """The option rule: Steer typed the param, so a listbox option echoing it proves nothing."""
    option = dataclasses.replace(RESULT, caps=("selectable",))
    assert auto_proof(SUB, before(), page([option]), typed=TYPED) is None


def test_with_the_landmark_map_unread_a_typed_search_fails_closed() -> None:
    after = dataclasses.replace(page([RESULT]), landmarks=None)
    assert auto_proof(SUB, before(), after, typed=TYPED) is None


# ---- a navigated claim -------------------------------------------------------------------------


def test_a_claim_citing_a_new_result_is_proven_on_a_url_change() -> None:
    assert navigated_tokens(SUB, before(), page([RESULT]), cited=[RESULT.id], typed=TYPED) == ()


def test_a_claim_with_no_change_since_the_baseline_is_no_change() -> None:
    after = page([RESULT], url=URL)
    tokens = navigated_tokens(SUB, before(), after, cited=[RESULT.id], typed=TYPED)
    assert "no_change" in tokens


def test_a_claim_citing_an_element_present_before_is_not_in_the_delta() -> None:
    tokens = navigated_tokens(SUB, before(), page([RESULT]), cited=[FIELD.id], typed=TYPED)
    assert "not_in_delta" in tokens


# ---- the field-owned rules ([V2.21] #9) --------------------------------------------------------

SEARCH = Landmark(kind="search", runtime_id=(9, 60))
MAIN = Landmark(kind="main", runtime_id=(9, 61))


def test_rule_1_under_the_fields_combobox() -> None:
    combo = element(40, "ComboBox", "Search", (9, 40), DOC.id)
    box = element(41, "Edit", "Search", (9, 41), 40)
    hint = element(42, "ListItem", "red shoes", (9, 42), 40)
    seen = page([combo, box, hint])
    assert "owned_by_combobox" in field_owned(hint, seen, box.runtime_id)


def test_rule_3_sharing_the_fields_search_landmark() -> None:
    form = element(60, "Group", "", (9, 62), DOC.id)
    box = element(61, "Edit", "Search", (9, 63), 60)
    hint = element(64, "ListItem", "red shoes", (9, 64), DOC.id)
    shown = [*QUERY_PAGE, form, box, hint]
    marks = {e.runtime_id: None for e in shown} | {box.runtime_id: SEARCH, hint.runtime_id: SEARCH}
    seen = page([form, box, hint], landmarks=marks)
    assert field_owned(hint, seen, box.runtime_id) == ("owned_by_search_landmark",)


def test_rule_4_under_the_fields_parent_container() -> None:
    form = element(60, "Group", "", (9, 62), DOC.id)
    box = element(61, "Edit", "Search", (9, 63), 60)
    hint = element(64, "ListItem", "red shoes", (9, 64), 60)
    seen = page([form, box, hint])
    assert field_owned(hint, seen, box.runtime_id) == ("owned_by_parent",)


def test_anything_merely_below_the_document_is_not_field_owned() -> None:
    assert field_owned(RESULT, page([RESULT]), FIELD.runtime_id) == ()


def test_a_parent_that_is_a_main_landmark_is_no_container() -> None:
    main = element(65, "Group", "", MAIN.runtime_id, DOC.id)
    box = element(66, "Edit", "Search", (9, 66), 65)
    hit = element(67, "ListItem", "red shoes", (9, 67), 65)
    shown = [*QUERY_PAGE, main, box, hit]
    marks = {e.runtime_id: None for e in shown} | {box.runtime_id: MAIN, hit.runtime_id: MAIN}
    assert field_owned(hit, page([main, box, hit], landmarks=marks), box.runtime_id) == ()


def test_an_unread_landmark_map_fails_closed_with_its_own_token() -> None:
    seen = dataclasses.replace(page([RESULT]), landmarks=None)
    assert field_owned(RESULT, seen, FIELD.runtime_id) == ("landmarks_unread",)
