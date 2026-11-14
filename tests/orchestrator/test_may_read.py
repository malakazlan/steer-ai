"""`gate.may_read` (#314, [V2.32]): a cheap prefilter that is False only when no deny, control,
consent or billing pattern can read the texts. Locked against `deny_tier_of` and
`billing_reads` on every name, group name and automation id of the deny grid, under every role
and verb the form facts read with: when `may_read` says no, both must say nothing."""

from __future__ import annotations

import itertools

import pytest

from steerai.orchestrator.gate import DenyFacts, billing_reads, deny_tier_of, may_read
from tests.orchestrator.deny_grid import (
    ALL_ROLES,
    BOUND_NAMES,
    GROUP_NAMES,
    GROUPED_NAMES,
    ID_FORMS,
    OPTION_NAMES,
    ROLE_IDS,
    SAMPLE_NAMES,
    WORD_NAMES,
)

NAMES = sorted(
    {n for n in (*WORD_NAMES, *SAMPLE_NAMES, *GROUPED_NAMES, *OPTION_NAMES, *BOUND_NAMES) if n}
    | {""}
)
GROUPS = sorted({g for g in GROUP_NAMES if g} | {""})
IDS = sorted({*ID_FORMS, *ROLE_IDS, ""})
FORM_VERBS = ("type", "choose", "click", "select", "check")
"""The verbs the form facts read with (`loop2.form.fitting_verbs`)."""


def _silent(name: str, group: str, automation_id: str) -> list[DenyFacts]:
    """Every reading `may_read` vouches for: each role and verb of the grid, with these texts."""
    return [
        DenyFacts(
            verb=verb, role=role, name=name, automation_id=automation_id, group_name=group or None
        )
        for verb, role in itertools.product(FORM_VERBS, ALL_ROLES)
    ]


@pytest.mark.parametrize("name", NAMES)
def test_no_reading_hides_behind_a_name(name: str) -> None:
    for group, automation_id in itertools.product(GROUPS, IDS):
        if may_read([name, group], [automation_id]):
            continue
        for facts in _silent(name, group, automation_id):
            assert deny_tier_of(facts) == "R0", facts
            assert billing_reads(facts) is False, facts


def test_the_prefilter_lets_through_what_the_gate_reads() -> None:
    assert may_read(["Delete"], [])
    assert may_read(["Pro $10/mo"], [])
    assert may_read([""], ["btnDelete"])
    assert may_read(["Subscription"], [])


def test_the_prefilter_rules_out_plain_text() -> None:
    assert not may_read(["Search", "Results for red shoes"], ["searchInput"])
    assert not may_read(["", ""], [""])
