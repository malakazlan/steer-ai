"""[V2.24] item 2: `deny_tier`, `effective_risk` and the pure `deny_tier_of` give, on every row of
the grid in `deny_grid.py`, what `deny_tier` and `effective_risk` gave on main before the deny
rules were lifted out of `Step` (`deny_lock.json`, written once from main and never regenerated
by a refactor)."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass
from functools import cache

import pytest

from steerai.orchestrator.contracts import RISKS
from steerai.orchestrator.gate import (
    BILLING_PATTERNS,
    CONSENT_PATTERNS,
    CONTROL_PATTERNS,
    DENY_PATTERNS,
    DenyFacts,
    _words,
    deny_tier_of,
)
from tests.orchestrator.deny_grid import (
    LOCK,
    VERB_AXIS,
    WORD_NAMES,
    Row,
    expand,
    sections,
    step_deny,
    step_effective,
)

GRID_ROWS = 54851


@dataclass(frozen=True, slots=True)
class Locked:
    rows: list[Row]
    deny: str
    effective: str


@cache
def _locked() -> dict[str, Locked]:
    data: object = json.loads(LOCK.read_bytes())
    assert isinstance(data, dict)
    raw = data["sections"]
    assert isinstance(raw, dict)
    out: dict[str, Locked] = {}
    for name, section in raw.items():
        assert isinstance(section, dict)
        inputs = section["axes"] if "axes" in section else section["rows"]
        assert isinstance(inputs, dict | list)
        deny, effective = section["deny"], section["effective"]
        assert isinstance(deny, str)
        assert isinstance(effective, str)
        rows = expand(inputs)
        assert len(rows) == section["count"] == len(deny) == len(effective), name
        out[str(name)] = Locked(rows=rows, deny=deny, effective=effective)
    return out


def test_the_lock_holds_the_grid_the_generator_states() -> None:
    """The file is the generator's grid, row for row: neither was edited alone."""
    locked = _locked()
    grid = sections()
    assert list(locked) == list(grid)
    for name, section in grid.items():
        assert locked[name].rows == expand(section), name
    assert sum(len(s.rows) for s in locked.values()) == GRID_ROWS


def test_every_word_of_every_class_has_a_row() -> None:
    """A word added to a list without a grid row fails here: extend the grid, then relock."""
    names = [_words(name) for name in WORD_NAMES]
    for patterns in (DENY_PATTERNS, CONTROL_PATTERNS, CONSENT_PATTERNS, BILLING_PATTERNS):
        for pattern in patterns:
            wanted = pattern.split()
            found = any(
                words[i : i + len(wanted)] == wanted
                for words in names
                for i in range(len(words) - len(wanted) + 1)
            )
            assert found, pattern


def facts_of(row: Row) -> DenyFacts:
    verb, role, name, automation_id, option, bound, element_ids, group = row
    return DenyFacts(
        verb=verb,
        role=role,
        name=name,
        automation_id=automation_id,
        option=option,
        bound_name=bound,
        element_ids=element_ids,
        group_name=group,
    )


CASES = [(name, verb) for name in sections() for verb in VERB_AXIS]


@pytest.mark.parametrize(("section", "verb"), CASES, ids=[f"{s}-{v}" for s, v in CASES])
def test_deny_tier_and_effective_risk_match_the_lock(section: str, verb: str) -> None:
    locked = _locked()[section]
    deny_off: list[tuple[int, Row]] = []
    effective_off: list[tuple[int, Row]] = []
    facts_off: list[tuple[int, Row]] = []
    for index, row in enumerate(locked.rows):
        if row[0] != verb:
            continue
        if str(RISKS.index(step_deny(row))) != locked.deny[index]:
            deny_off.append((index, row))
        if step_effective(row) != locked.effective[index]:
            effective_off.append((index, row))
        if str(RISKS.index(deny_tier_of(facts_of(row)))) != locked.deny[index]:
            facts_off.append((index, row))
    assert not deny_off, (len(deny_off), deny_off[:5])
    assert not effective_off, (len(effective_off), effective_off[:5])
    assert not facts_off, (len(facts_off), facts_off[:5])


def test_the_facts_are_frozen_and_default_to_nothing_read() -> None:
    facts = DenyFacts(verb="click", role="Button", name="OK")
    assert (facts.automation_id, facts.option, facts.bound_name) == ("", None, None)
    assert (facts.element_ids, facts.group_name) == ((), None)
    with pytest.raises(FrozenInstanceError):
        facts.name = "Delete"  # type: ignore[misc]  # the assignment a frozen dataclass refuses


def test_the_facts_take_an_empty_name_without_judging_it() -> None:
    """The signature takes what v2 can see; what an unnamed target means is the action_risk
    PR's ([V2.24] item 2), so no tier is asserted here."""
    assert DenyFacts(verb="click", role="Button", name="").name == ""
