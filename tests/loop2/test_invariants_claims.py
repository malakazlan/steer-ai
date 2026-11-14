"""`content_present` at the standard tier (packet v2-step4-claims §3.1, §3.2; [V2.3], [V2.15] Q4
and amendment 1). Red on the step 4 stub."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from steerai.loop2.claims import Checked, RuntimeId, SubgoalFacts, check_content
from steerai.loop2.decision import Claim
from steerai.loop2.task import GoalSpan
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from tests.loop2.world_fakes import DOC, FIELD, GOAL, PAGE, element, pending, snapshot

TEXT = GoalSpan.of(GOAL, 20, 29)
"""`red shoes`."""
SHORT = GoalSpan.of("go ox", 3, 5)
BASELINE = snapshot(PAGE)
RESULT = element(10, "Text", "Red shoes in stock", (9, 10), 1)


BASE = SubgoalFacts(params={"text": TEXT, "short": SHORT}, commit="none", baseline=BASELINE)


def facts(
    *,
    baseline: Snapshot = BASELINE,
    typed_fields: tuple[RuntimeId, ...] = (),
    revealed_roots: tuple[RuntimeId, ...] = (),
    controlled: tuple[RuntimeId, ...] = (),
) -> SubgoalFacts:
    return dataclasses.replace(
        BASE,
        baseline=baseline,
        typed_fields=typed_fields,
        revealed_roots=revealed_roots,
        controlled=controlled,
    )


def claim(*ids: int, param: str | None = "text") -> Claim:
    return Claim(subgoal="s1", kind="content_present", evidence=list(ids), param=param)


def check(cited: Claim, extra: Sequence[Element], subgoal: SubgoalFacts | None = None) -> Checked:
    return check_content(
        cited,
        snapshot([*PAGE, *extra]),
        subgoal or facts(),
        scope_document=DOC.runtime_id,
    )


@pending("§3.1 content_present standard")
def test_a_new_element_in_the_scope_holding_the_words_is_a_standard_proof() -> None:
    result = check(claim(RESULT.id), [RESULT])
    assert result.tokens == ()
    assert result.proof is not None
    assert (result.proof.tier, result.proof.preexisting) == ("standard", False)
    assert "in_delta" in result.proof.signals


@pending("[V2.15] amendment 1: every param word, in any order")
def test_the_words_in_any_order_pass() -> None:
    reordered = element(11, "Text", "Shoes, red", (9, 11), 1)
    assert check(claim(reordered.id), [reordered]).proof is not None


@pending("[V2.15] amendment 1: every param word, in one cited name")
def test_a_missing_word_is_words_missing() -> None:
    boots = element(11, "Text", "Red boots", (9, 11), 1)
    assert "words_missing" in check(claim(boots.id), [boots]).tokens


@pending("[V2.15] amendment 1: every param word, in one cited name")
def test_words_split_over_two_names_are_words_missing() -> None:
    red = element(11, "Text", "Red", (9, 11), 1)
    shoes = element(12, "Text", "Shoes", (9, 12), 1)
    assert "words_missing" in check(claim(red.id, shoes.id), [red, shoes]).tokens


@pending("[V2.15] amendment 1: whole tokens, casefolded")
def test_a_word_inside_another_word_is_words_missing() -> None:
    joined = element(11, "Text", "Redshoes", (9, 11), 1)
    assert "words_missing" in check(claim(joined.id), [joined]).tokens


@pending("§3.1 rule 1 param_missing")
def test_a_claim_without_a_param_is_param_missing() -> None:
    assert "param_missing" in check(claim(RESULT.id, param=None), [RESULT]).tokens


@pending("§3.1 rule 1 param_missing")
def test_a_param_the_subgoal_lacks_is_param_missing() -> None:
    assert "param_missing" in check(claim(RESULT.id, param="other"), [RESULT]).tokens


@pending("§3.1 rule 2 evidence_missing")
def test_no_evidence_is_evidence_missing() -> None:
    assert "evidence_missing" in check(claim(), [RESULT]).tokens


@pending("§3.1 rule 2 too_much_evidence")
def test_more_than_four_ids_is_too_much_evidence() -> None:
    many = [element(20 + i, "Text", "Red shoes", (9, 20 + i), 1) for i in range(5)]
    assert "too_much_evidence" in check(claim(*(e.id for e in many)), many).tokens


@pending("§3.1 rule 3 id_not_listed")
def test_an_id_not_in_the_snapshot_is_not_listed() -> None:
    assert "id_not_listed" in check(claim(99), [RESULT]).tokens


@pending("§3.1 rule 4 outside_scope ([V2.3])")
def test_a_tab_name_holding_the_words_is_outside_scope() -> None:
    tab = element(30, "TabItem", "Red shoes - results", (9, 30), None)
    assert "outside_scope" in check(claim(tab.id), [tab]).tokens


@pending("§3.1 rule 4 outside_scope ([V2.3])")
def test_the_address_bar_is_outside_scope() -> None:
    bar = element(31, "Edit", "red shoes", (9, 31), None)
    assert "outside_scope" in check(claim(bar.id), [bar]).tokens


@pending("§3.1 rule 5 not_in_delta")
def test_an_element_already_in_the_baseline_is_not_in_delta() -> None:
    old = element(11, "Text", "Red shoes", (9, 11), 1)
    subgoal = facts(baseline=snapshot([*PAGE, old]))
    assert "not_in_delta" in check(claim(old.id), [old], subgoal).tokens


@pending("§3.1 rule 5: a changed name is in the delta")
def test_an_element_whose_name_changed_since_the_baseline_is_in_the_delta() -> None:
    before = element(11, "Text", "Loading", (9, 11), 1)
    after = element(11, "Text", "Red shoes", (9, 11), 1)
    subgoal = facts(baseline=snapshot([*PAGE, before]))
    assert check(claim(after.id), [after], subgoal).proof is not None


@pending("§3.1 rule 5: a replaced top Document")
def test_a_replaced_top_document_gives_the_new_top_document_signal() -> None:
    page = element(40, "Document", "Results", (8, 40), None)
    line = element(41, "Text", "Red shoes", (8, 41), 40)
    result = check_content(
        claim(line.id), snapshot([page, line]), facts(), scope_document=page.runtime_id
    )
    assert result.proof is not None
    assert "new_top_document" in result.proof.signals


@pending("§3.1 rule 6 inside_field")
def test_an_element_inside_the_typed_fields_container_is_inside_field() -> None:
    group = element(50, "Group", "", (9, 50), 1)
    field = element(51, "Edit", "Search", (9, 51), 50)
    hint = element(52, "Text", "red shoes", (9, 52), 50)
    subgoal = facts(typed_fields=(field.runtime_id,))
    assert "inside_field" in check(claim(hint.id), [group, field, hint], subgoal).tokens


@pending("§3.1 rule 10 too_short")
def test_a_single_token_under_three_characters_is_too_short() -> None:
    ox = element(11, "Text", "ox", (9, 11), 1)
    assert "too_short" in check(claim(ox.id, param="short"), [ox]).tokens


@pending("§3.1: a proof and reject tokens never come together")
def test_a_rejected_claim_has_no_proof() -> None:
    result = check(claim(99), [RESULT])
    assert result.proof is None
    assert result.tokens != ()


# ---- field-owned lists and options ([V2.15] Q4, packet §3.2) --------------------------------

COMBO = element(60, "ComboBox", "Search", (9, 60), 1)
COMBO_FIELD = element(61, "Edit", "Search", (9, 61), 60)
COMBO_LIST = element(62, "List", "", (9, 62), 60)
COMBO_ITEM = element(63, "Text", "red shoes", (9, 63), 62)


@pending("[V2.15] Q4: a list under the field's ComboBox ancestor is field-owned")
def test_a_list_under_the_fields_combobox_ancestor_is_under_suggestions() -> None:
    subgoal = facts(typed_fields=(COMBO_FIELD.runtime_id,))
    shown = [COMBO, COMBO_FIELD, COMBO_LIST, COMBO_ITEM]
    assert "under_suggestions" in check(claim(COMBO_ITEM.id), shown, subgoal).tokens


@pending("[V2.15] Q4: a list named by the field's ControllerFor is field-owned")
def test_a_list_named_by_controller_for_is_under_suggestions() -> None:
    popup = element(64, "List", "", (9, 64), 1)
    line = element(65, "Text", "red shoes", (9, 65), 64)
    subgoal = facts(typed_fields=(FIELD.runtime_id,), controlled=(popup.runtime_id,))
    assert "under_suggestions" in check(claim(line.id), [popup, line], subgoal).tokens


@pending("[V2.15] Q4: field-owned is decided by ancestry, not by the effect class")
def test_a_field_owned_list_is_rejected_even_when_not_a_revealed_root() -> None:
    subgoal = facts(typed_fields=(COMBO_FIELD.runtime_id,), revealed_roots=())
    shown = [COMBO, COMBO_FIELD, COMBO_LIST, COMBO_ITEM]
    assert "under_suggestions" in check(claim(COMBO_ITEM.id), shown, subgoal).tokens


@pending("§3.1 rule 7: a Menu ancestor")
def test_an_element_under_a_menu_is_under_suggestions() -> None:
    menu = element(66, "Menu", "", (9, 66), 1)
    entry = element(67, "MenuItem", "red shoes", (9, 67), 66)
    assert "under_suggestions" in check(claim(entry.id), [menu, entry]).tokens


@pending("§3.1 rule 7: a ToolTip ancestor")
def test_an_element_under_a_tooltip_is_under_suggestions() -> None:
    tip = element(68, "ToolTip", "", (9, 68), 1)
    words = element(69, "Text", "red shoes", (9, 69), 68)
    assert "under_suggestions" in check(claim(words.id), [tip, words]).tokens


FORM = element(70, "Group", "Filters", (9, 70), 1)
INNER = element(71, "Group", "", (9, 71), 70)
FORM_FIELD = element(72, "Edit", "Search", (9, 72), 71)
FORM_LIST = element(73, "List", "", (9, 73), 70)


@pending("[V2.15] Q4: a shared container does not make a list field-owned")
def test_a_list_that_only_shares_a_container_with_the_field_is_not_field_owned() -> None:
    row = element(74, "ListItem", "Red shoes", (9, 74), 73)
    subgoal = facts(typed_fields=(FORM_FIELD.runtime_id,))
    result = check(claim(row.id), [FORM, INNER, FORM_FIELD, FORM_LIST, row], subgoal)
    assert result.proof is not None


@pending("[V2.15] Q4: a selectable ListItem is never evidence for a search subgoal")
def test_a_selectable_list_item_is_option_evidence_for_a_search_subgoal() -> None:
    option = Element(
        id=75,
        depth=2,
        role="ListItem",
        name="Red shoes",
        box=(0, 750, 100, 758),
        runtime_id=(9, 75),
        parent_id=73,
        caps=("selectable",),
    )
    subgoal = facts(typed_fields=(FORM_FIELD.runtime_id,))
    shown = [FORM, INNER, FORM_FIELD, FORM_LIST, option]
    assert "option_evidence" in check(claim(option.id), shown, subgoal).tokens


@pending("[V2.15] Q4: a selected ListItem is a listbox option too")
def test_a_selected_list_item_is_option_evidence_for_a_search_subgoal() -> None:
    option = Element(
        id=76,
        depth=2,
        role="ListItem",
        name="Red shoes",
        box=(0, 760, 100, 768),
        runtime_id=(9, 76),
        parent_id=73,
        caps=("selected",),
    )
    subgoal = facts(typed_fields=(FORM_FIELD.runtime_id,))
    shown = [FORM, INNER, FORM_FIELD, FORM_LIST, option]
    assert "option_evidence" in check(claim(option.id), shown, subgoal).tokens


@pending("[V2.3]: under a revealed root that is not field-owned, with under_revealed")
def test_a_revealed_list_that_is_not_field_owned_passes_with_under_revealed() -> None:
    panel = element(80, "Group", "Details", (9, 80), 1)
    words = element(81, "Text", "Red shoes", (9, 81), 80)
    subgoal = facts(revealed_roots=(panel.runtime_id,))
    result = check(claim(words.id), [panel, words], subgoal)
    assert result.proof is not None
    assert "under_revealed" in result.proof.signals


@pending("[V2.3]: under_revealed is only set under a revealed root")
def test_evidence_outside_any_revealed_root_has_no_under_revealed() -> None:
    result = check(claim(RESULT.id), [RESULT])
    assert result.proof is not None
    assert "under_revealed" not in result.proof.signals
