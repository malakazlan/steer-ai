"""`gate.billing_reads` ([V2.30] (3)): the billing class read on the same names `deny_tier_of`
reads it on, path by path (principal review of #312: the name lists are shared, not copied)."""

from __future__ import annotations

import pytest

from steerai.orchestrator.gate import DenyFacts, billing_reads, deny_tier_of

PRICED = "Premium $20/mo"


@pytest.mark.parametrize(
    "facts",
    [
        DenyFacts(verb="choose", role="ComboBox", name="Plan", option=PRICED),
        DenyFacts(verb="select", role="ListItem", name=PRICED),
        DenyFacts(verb="check", role="RadioButton", name=PRICED),
        DenyFacts(verb="check", role="CheckBox", name="Renew", group_name="Billing"),
    ],
    ids=["choose", "listbox_select", "radio", "checkbox_group"],
)
def test_a_price_or_billing_word_reads_on_each_path(facts: DenyFacts) -> None:
    assert billing_reads(facts) is True
    assert deny_tier_of(facts) == "R2"


@pytest.mark.parametrize(
    "facts",
    [
        DenyFacts(verb="choose", role="ComboBox", name="Size", option="Large"),
        DenyFacts(verb="select", role="ListItem", name="Large"),
        DenyFacts(verb="check", role="RadioButton", name="Large"),
        DenyFacts(verb="click", role="Button", name=PRICED),
    ],
    ids=["choose", "listbox_select", "radio", "button_not_billed"],
)
def test_a_plain_name_or_an_unbilled_role_does_not_read(facts: DenyFacts) -> None:
    assert billing_reads(facts) is False
