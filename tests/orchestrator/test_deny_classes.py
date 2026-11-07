"""Phase 3 deny classes (5.13 1a [v2.17], packet deny-classes-2026-11-07, schema gate REVISE 6):
checkout, the sign-out family, the engagement words and the irreversible "don't save" family
raise a step's tier on every role; "continue" and purchase labels without a cited source wait."""

from __future__ import annotations

import pytest

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import CONTROL_PATTERNS, DENY_PATTERNS, _words, effective_risk


def _click(name: str, role: str = "Button", automation_id: str = "") -> Step:
    return Step(
        id="c1",
        subgoal="g1",
        verb="click",
        target=Target(role=role, name=name, automation_id=automation_id),
        risk="R0",
    )


def test_every_pattern_is_in_the_form_it_is_matched() -> None:
    """Gate item 1: "don't save" split into don/t/save never matched; keys are word forms."""
    for pattern in [*DENY_PATTERNS, *CONTROL_PATTERNS]:
        assert " ".join(_words(pattern)) == pattern, pattern


@pytest.mark.parametrize(
    ("name", "risk"),
    [
        ("Checkout", "R3"),
        ("Discard", "R3"),
        ("Don't Save", "R3"),
        ("Don" + chr(0x2019) + "t save", "R3"),  # a curly apostrophe
        ("Clear history", "R3"),
        ("Check out", "R2"),
        ("Log out", "R2"),
        ("Log off", "R2"),
        ("Sign off", "R2"),
        ("Subscribe", "R2"),
        ("Unsubscribe", "R2"),
        ("Join", "R2"),
        ("Like", "R2"),
        ("Follow", "R2"),
        ("Report", "R2"),
        ("Block", "R2"),
        ("Leave group", "R2"),
        ("Accept all", "R2"),
        ("Allow", "R2"),
    ],
)
def test_the_new_words_raise_a_control(name: str, risk: str) -> None:
    assert effective_risk(_click(name)) == risk


def test_check_out_in_a_link_title_keeps_the_declared_tier() -> None:
    """Re-review ruling (a): "check out" is in the control-role class."""
    assert effective_risk(_click("Check out this video", role="Hyperlink")) == "R0"


def test_automation_ids_carry_the_words() -> None:
    assert effective_risk(_click("OK", automation_id="btnCheckout")) == "R3"
    assert effective_risk(_click("OK", automation_id="logOutButton")) == "R2"


def test_continue_and_unsourced_purchase_labels_wait() -> None:
    """Gate Q2 and item 5: "continue" has no checkable scope yet; "place order" is from memory."""
    assert effective_risk(_click("Continue")) == "R0"
    assert effective_risk(_click("Place order")) == "R0"


@pytest.mark.parametrize("name", ["Logout", "LOGOFF", "signoff"])
def test_single_token_sign_out_forms_raise(name: str) -> None:
    """Hunter DC-10: the existing list has `signout` for the same reason."""
    assert effective_risk(_click(name)) == "R2"


@pytest.mark.parametrize("apostrophe", [0x27, 0x2019, 0x02BC, 0xFF07, 0x60, 0xB4])
def test_any_apostrophe_splits_dont_save(apostrophe: int) -> None:
    """Hunter DC-15: a modifier-letter apostrophe (U+02BC) is a word character to the matcher,
    so it would keep "Don" + it + "t" one token; every apostrophe form splits for matching."""
    assert effective_risk(_click(f"Don{chr(apostrophe)}t save")) == "R3"


# ---- schema gate re-review: option (a), control roles for the engagement class ---------------


def test_an_engagement_word_in_a_link_title_keeps_the_declared_tier() -> None:
    """DC-19/20: a raised click has no template and blocks; "Like That" is a video title."""
    assert effective_risk(_click("Drake - Like That (Official)", role="Hyperlink")) == "R0"
    assert effective_risk(_click("Like", role="Button")) == "R2"


@pytest.mark.parametrize("verb", ["type", "focus"])
def test_a_check_out_date_field_keeps_the_declared_tier(verb: str) -> None:
    """DC-24: a hotel "Check-out" date Edit is not a checkout."""
    step = Step(
        id="d1",
        subgoal="g1",
        verb=verb,
        target=Target(role="Edit", name="Check-out"),
        risk="R1",
        text="2026-12-01" if verb == "type" else None,
    )
    assert effective_risk(step) == "R1"


def test_an_automation_id_raises_on_every_role() -> None:
    assert effective_risk(_click("Watch", role="Hyperlink", automation_id="follow-button")) == "R2"


def test_a_person_row_named_like_a_word_keeps_the_declared_tier() -> None:
    """DC-25: a contact "Lee Block" is a ListItem, not a control."""
    assert effective_risk(_click("Lee Block", role="ListItem")) == "R0"


def test_the_every_role_words_still_raise_a_link() -> None:
    assert effective_risk(_click("Unsubscribe", role="Hyperlink")) == "R2"
    assert effective_risk(_click("Checkout", role="Hyperlink")) == "R3"
    assert effective_risk(_click("Log out", role="Hyperlink")) == "R2"


def test_a_planned_r1_click_on_a_checkout_button_is_r3() -> None:
    assert effective_risk(_click("Checkout")) == "R3"
