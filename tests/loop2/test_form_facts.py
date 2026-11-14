"""[V2.30] the query conditions' form facts: the form is the [V2.22] scope of the typed field
(under its parent container, or sharing its nearest landmark; a missing fact reads as inside);
`secure_in_form` is any secure or secure-unread element in it; `risky_in_form` is any element in
it that gets the billing class or is raised to R3 by `deny_tier_of`. Both are recorded when
Steer types and computed again at the submit's dispatch, combined by OR."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from steerai.loop2.loop import PendingText, action_risk
from steerai.loop2.query import SearchFacts, search_facts
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from steerai.world.landmarks import Landmark
from tests.loop2.world_fakes import GOAL, PAGE, act, element, observed, snapshot

TYPED = ValueFact(filled=True, digest="typed")
FORM = element(60, "Group", "Search", (9, 60), 1)
BOX = element(61, "Edit", "Search", (9, 61), 60)
SEARCH_BUTTON = element(62, "Button", "Search", (9, 62), 60)
BUY = element(63, "Button", "Buy", (9, 63), 60)
LATE_PASSWORD = element(64, "Edit", "Password", (9, 64), 60, secure=True)
PLAN = element(65, "RadioButton", "Pro $10/mo", (9, 65), 60)
FAR_DELETE = element(66, "Button", "Delete", (9, 66), 1)
SEARCH = Landmark(kind="search", runtime_id=FORM.runtime_id)
PLAIN = [*PAGE, FORM, BOX, SEARCH_BUTTON]


def screen(
    shown: Sequence[Element] = PLAIN, *, searchbox: bool = True, marks_read: bool = True
) -> Observed:
    seen = dataclasses.replace(
        observed(shown),
        aria_roles={BOX.runtime_id: "searchbox"} if searchbox else {},
    )
    if not marks_read:
        return seen
    marks: dict[tuple[int, ...], Landmark | None] = {e.runtime_id: None for e in shown}
    marks |= {e.runtime_id: SEARCH for e in shown if e.parent_id == FORM.id}
    return dataclasses.replace(seen, landmarks=marks)


def enter_risk(typed_on: Observed, sent_on: Observed) -> str:
    """Steer typed into BOX on `typed_on`; the Enter goes out on `sent_on`."""
    held = PendingText(BOX.runtime_id, TYPED, TYPED, search_facts(typed_on, BOX.runtime_id), True)
    enter = act("press", key="Enter")
    return action_risk(enter, BOX, goal=GOAL, observed=sent_on, pending=[held]).risk


# ---- the lead's three ------------------------------------------------------------------------


def test_a_plain_search_landmark_with_a_field_and_a_search_button_stays_a_query() -> None:
    assert enter_risk(screen(), screen()) == "R1"


def test_a_buy_button_beside_a_searchbox_makes_the_enter_r2() -> None:
    with_buy = screen([*PLAIN, BUY])
    assert enter_risk(with_buy, with_buy) == "R2"


def test_a_password_field_appearing_after_the_type_makes_the_enter_r2() -> None:
    assert enter_risk(screen(), screen([*PLAIN, LATE_PASSWORD])) == "R2"


def test_a_risky_element_at_type_time_counts_though_it_left_before_the_enter() -> None:
    """Type-time and dispatch-time facts combine by OR."""
    assert enter_risk(screen([*PLAIN, BUY]), screen()) == "R2"


# ---- the facts --------------------------------------------------------------------------------


def facts(seen: Observed) -> SearchFacts:
    return search_facts(seen, BOX.runtime_id)


def test_a_searchbox_in_a_plain_search_landmark_has_no_form_fact() -> None:
    assert facts(screen()) == SearchFacts(
        searchbox=True, in_search_landmark=True, secure_in_form=False, risky_in_form=False
    )


def test_an_unread_aria_role_is_never_a_searchbox() -> None:
    seen = dataclasses.replace(screen(), aria_roles=None, landmarks=None)
    found = facts(seen)
    assert (found.searchbox, found.in_search_landmark) == (False, False)


def test_another_aria_role_is_not_a_searchbox() -> None:
    assert facts(screen(searchbox=False)).searchbox is False


def test_a_secure_flag_left_unread_in_the_form_counts_as_secure() -> None:
    unread = dataclasses.replace(
        screen(), snapshot=dataclasses.replace(snapshot(PLAIN), unread_secure=frozenset({62}))
    )
    assert facts(unread).secure_in_form is True


def test_a_billing_choice_in_the_form_is_risky() -> None:
    assert facts(screen([*PLAIN, PLAN])).risky_in_form is True


def test_a_deny_word_outside_the_form_is_not_risky() -> None:
    assert facts(screen([*PLAIN, FAR_DELETE])).risky_in_form is False


def test_with_the_landmark_map_unread_a_deny_word_anywhere_counts() -> None:
    """A missing fact reads as inside the form: fail closed."""
    assert facts(screen([*PLAIN, FAR_DELETE], marks_read=False)).risky_in_form is True


def test_with_the_landmark_map_unread_a_password_anywhere_counts() -> None:
    far = element(67, "Edit", "Password", (9, 67), 1, secure=True)
    assert facts(screen([*PLAIN, far], marks_read=False)).secure_in_form is True


def test_a_broken_parent_chain_reads_as_inside_the_form() -> None:
    orphan = element(68, "Button", "Delete", (9, 68), 99)
    assert facts(screen([*PLAIN, orphan])).risky_in_form is True
