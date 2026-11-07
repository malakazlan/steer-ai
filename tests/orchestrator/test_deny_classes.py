"""Phase 3 deny classes (5.13 1a [v2.17], packet deny-classes-2026-11-07, schema gate REVISE 6):
checkout, the sign-out family, the engagement words and the irreversible "don't save" family
raise a step's tier on every role; "continue" and purchase labels without a cited source wait."""

from __future__ import annotations

import pytest

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import DENY_PATTERNS, _words, effective_risk


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
    for pattern in DENY_PATTERNS:
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


def test_the_words_apply_on_every_role() -> None:
    """Gate Q1: a link is not exempt (an email "Unsubscribe" link acts)."""
    assert effective_risk(_click("Unsubscribe", role="Hyperlink")) == "R2"
    assert effective_risk(_click("Check out this video", role="Hyperlink")) == "R2"


def test_automation_ids_carry_the_words() -> None:
    assert effective_risk(_click("OK", automation_id="btnCheckout")) == "R3"
    assert effective_risk(_click("OK", automation_id="logOutButton")) == "R2"


def test_continue_and_unsourced_purchase_labels_wait() -> None:
    """Gate Q2 and item 5: "continue" has no checkable scope yet; "place order" is from memory."""
    assert effective_risk(_click("Continue")) == "R0"
    assert effective_risk(_click("Place order")) == "R0"
