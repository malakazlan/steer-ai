"""[V2.89] (#505): a rival field, counted when a value claim is proven, never acted on in step 3.

A rival exists when another value-bearing element (a field role inside the scope Document, read
by the name rule `field_held` uses) holds a request word that the cited element's name lacks,
ignoring the value's words and the other subgoals' declared field words. Step 3 records the count
on the run's outcome and changes no behaviour: function words give some false hits, and the count
shows how many before the lead rules on it for step 4."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.loop import TaskOutcome
from steerai.loop2.runner import run_task
from steerai.loop2.verify import rival_field
from steerai.world.effects import ValueFact
from steerai.world.elements import Element
from tests.loop2.world_fakes import BAR, DOC, World, act, element, observed, task

VALUE = "x@y.test"
GOAL = f"enter {VALUE} as the billing email"
ADDRESS = element(42, "Edit", "Billing address", (9, 42), 1)
CONTACT = element(43, "Edit", "Contact email", (9, 43), 1)


def _rival(
    elements: Sequence[Element],
    cited: Element,
    *,
    goal: str = GOAL,
    value: str = VALUE,
    others: Sequence[str] = (),
) -> bool:
    shot = observed([DOC, BAR, *elements]).snapshot
    return rival_field(shot, cited, goal=goal, value=value, other_fields=others)


def test_a_field_holding_a_request_word_the_cited_field_lacks_is_a_rival() -> None:
    """#505's example: `email` declared, Contact email cited, Billing address holds `billing`."""
    assert _rival([ADDRESS, CONTACT], CONTACT)


def test_no_rival_when_the_cited_element_holds_every_request_word() -> None:
    whole = element(44, "Edit", "enter as the billing email", (9, 44), 1)
    assert not _rival([ADDRESS, CONTACT, whole], whole)


def test_the_values_words_are_ignored() -> None:
    goal = "enter billing as the contact email"
    assert not _rival([ADDRESS, CONTACT], CONTACT, goal=goal, value="billing")
    assert _rival([ADDRESS, CONTACT], CONTACT, goal=goal, value="as")  # the same page, a rival


def test_other_subgoals_field_words_are_ignored() -> None:
    goal = f"enter {VALUE} as the billing email and 555 0100 as the phone"
    billing = element(44, "Edit", "Billing email", (9, 44), 1)
    phone = element(45, "Edit", "Phone", (9, 45), 1)
    assert not _rival([billing, phone], billing, goal=goal, others=("phone",))
    assert _rival([billing, phone], billing, goal=goal)  # without the other subgoal, a rival


def test_only_a_value_bearing_element_in_the_scope_document_is_a_rival() -> None:
    """The top Document (the page's title), the address bar outside it, and a non-field role
    holding `billing` are not fields a value could go in."""
    page = element(1, "Document", "Billing", (9, 1), None)
    text = element(44, "Text", "Billing", (9, 44), 1)
    button = element(45, "Button", "Billing", (9, 45), 1)
    shot = observed([page, BAR, CONTACT, text, button]).snapshot
    assert not rival_field(shot, CONTACT, goal=GOAL, value=VALUE, other_fields=())
    combo = element(46, "ComboBox", "Billing", (9, 46), 1)
    shot = observed([page, BAR, CONTACT, combo]).snapshot
    assert rival_field(shot, CONTACT, goal=GOAL, value=VALUE, other_fields=())


def test_a_rival_is_read_by_field_helds_name_rule() -> None:
    """A field with no name of its own is read by its group name; a named field never is."""
    group = element(44, "Group", "Billing", (9, 44), 1)
    unnamed = element(45, "Edit", "", (9, 45), 44)
    assert _rival([group, unnamed, CONTACT], CONTACT)
    named = element(45, "Edit", "Notes", (9, 45), 44)
    assert not _rival([group, named, CONTACT], CONTACT)


# ---- the runner: counted on a proven value claim, behaviour unchanged ---------------------------

SAID = ValueFact(filled=True, digest=f"d:{VALUE}")


def _run(elements: Sequence[Element], cite: Element) -> tuple[TaskOutcome, World]:
    doc = SubgoalDoc(id="s1", desc="x", params={"address": VALUE}, closes="value", field="email")
    turns = [
        Decision(subgoals=[doc], actions=[act("wait", subgoal="s1")]),
        Decision(claims=[Claim(subgoal="s1", kind="value", evidence=[cite.id], param="address")]),
        Decision(stop="blocked"),
    ]
    world = World(turns, screen=observed([DOC, BAR, *elements]))
    for box in elements:
        world.values[box.runtime_id] = SAID
    world.fact_text = True
    run = dataclasses.replace(task(), goal=GOAL)
    return run_task(run, world.ports()), world


def test_a_proven_value_claim_with_a_rival_is_counted() -> None:
    outcome, _ = _run([ADDRESS, CONTACT], CONTACT)
    assert outcome.status == "done"
    assert outcome.rival_field == 1


def test_a_proven_value_claim_without_a_rival_is_not_counted() -> None:
    other = element(42, "Edit", "Notes", (9, 42), 1)
    outcome, _ = _run([other, CONTACT], CONTACT)
    assert outcome.status == "done"
    assert outcome.rival_field == 0


def test_a_refused_value_claim_is_never_counted() -> None:
    outcome, _ = _run([ADDRESS, CONTACT], ADDRESS)  # Billing address does not hold `email`
    assert outcome.status != "done"
    assert (outcome.field_mismatch, outcome.rival_field) == (1, 0)


def test_a_rival_changes_no_behaviour() -> None:
    """Step 3 records the count only: the same verdict, history and audit with and without it."""
    other = element(42, "Edit", "Notes", (9, 42), 1)
    with_rival, seen_with = _run([ADDRESS, CONTACT], CONTACT)
    without, seen_without = _run([other, CONTACT], CONTACT)
    assert (with_rival.rival_field, without.rival_field) == (1, 0)
    assert (with_rival.status, with_rival.reason) == (without.status, without.reason)
    assert with_rival.audit == without.audit
    assert [v.history for v in seen_with.views] == [v.history for v in seen_without.views]
    assert [a.proofs for a in seen_with.audits] == [a.proofs for a in seen_without.audits]


def test_the_outcome_carries_the_count() -> None:
    assert "rival_field" in {f.name for f in dataclasses.fields(TaskOutcome)}
