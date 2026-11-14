"""[V2.30] the query conditions' form facts: the form is the [V2.22] scope of the typed field
(under its parent container, or sharing its nearest landmark; a missing fact reads as inside);
`secure_in_form` is any secure or secure-unread element in it; `risky_in_form` is any element in
it that gets the billing class or is raised to R3 by `deny_tier_of`. Both are recorded when
Steer types and computed again at the submit's dispatch, combined by OR."""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Sequence

import pytest

from steerai.loop2 import form, loop, query
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


# ---- principal review of #312 -----------------------------------------------------------------
# 1: a role no verb acts on is read as a click (deny words read names and ids on every role);
# a ComboBox is read as typed into and as chosen.


def test_a_text_reading_delete_in_the_form_makes_the_enter_r2() -> None:
    shown = screen([*PLAIN, element(70, "Text", "Delete", (9, 70), 60)])
    assert enter_risk(shown, shown) == "R2"


def test_an_image_with_a_delete_automation_id_in_the_form_makes_the_enter_r2() -> None:
    icon = element(71, "Image", "", (9, 71), 60, automation_id="delete-icon")
    shown = screen([*PLAIN, icon])
    assert enter_risk(shown, shown) == "R2"


def test_a_combobox_naming_a_price_in_the_form_makes_the_enter_r2() -> None:
    shown = screen([*PLAIN, element(72, "ComboBox", "Plan $20/mo", (9, 72), 60)])
    assert enter_risk(shown, shown) == "R2"


# ---- #314: the form is scanned only where its facts can change a risk -------------------------


def _counting(monkeypatch: pytest.MonkeyPatch, module: object) -> list[int]:
    calls: list[int] = []
    real = form.form_facts

    def counted(seen: Observed, field: tuple[int, ...]) -> tuple[bool, bool]:
        calls.append(1)
        return real(seen, field)

    monkeypatch.setattr(module, "form_facts", counted)
    return calls


def test_a_field_that_is_not_query_class_is_not_scanned_and_reads_as_risky(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _counting(monkeypatch, query)
    found = facts(screen(searchbox=False, marks_read=False))
    assert calls == []
    assert (found.secure_in_form, found.risky_in_form) == (True, True)


def test_a_submit_that_cannot_be_a_query_does_not_scan_the_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = PendingText(BOX.runtime_id, TYPED, TYPED, facts(screen()), True)
    calls = _counting(monkeypatch, loop)
    declared = act("press", key="Enter", commit="send")
    risk = action_risk(declared, BOX, goal=GOAL, observed=screen(), pending=[held]).risk
    assert (risk, calls) == ("R2", [])


def test_a_submit_that_may_be_a_query_scans_the_form(monkeypatch: pytest.MonkeyPatch) -> None:
    held = PendingText(BOX.runtime_id, TYPED, TYPED, facts(screen()), True)
    calls = _counting(monkeypatch, loop)
    enter = act("press", key="Enter")
    assert action_risk(enter, BOX, goal=GOAL, observed=screen(), pending=[held]).risk == "R1"
    assert calls == [1]


# ---- #319 reviews ----------------------------------------------------------------------------


def test_a_risky_element_appearing_after_the_type_is_seen_through_a_warm_memo() -> None:
    """S3-20, [V2.35] (1): the dispatch-time scan is real, not the type-time one replayed."""
    typed_on = screen()
    search_facts(typed_on, BOX.runtime_id)  # warms the memo on the plain form
    assert enter_risk(typed_on, screen([*PLAIN, BUY])) == "R2"


def test_a_password_appearing_after_the_type_is_seen_through_a_warm_memo() -> None:
    typed_on = screen()
    search_facts(typed_on, BOX.runtime_id)
    assert enter_risk(typed_on, screen([*PLAIN, LATE_PASSWORD])) == "R2"


def test_a_choice_whose_group_is_not_in_the_snapshot_is_risky() -> None:
    """S1-07: its group name cannot be read, so it is never read as "no group" (as
    `action_risk`'s parent rule)."""
    orphan = element(73, "RadioButton", "Yearly", (9, 73), 99)
    marks_shared = dataclasses.replace(
        screen([*PLAIN, orphan]),
        landmarks={**screen([*PLAIN, orphan]).landmarks, orphan.runtime_id: SEARCH},  # type: ignore[dict-item]  # read in `screen`
    )
    assert facts(marks_shared).risky_in_form is True


def test_a_form_over_the_member_cap_reads_both_facts_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[V2.35] (1): over budget means true; a fact is never left unread to meet a budget."""
    monkeypatch.setattr(form, "MAX_FORM_MEMBERS", 1)  # the form has 2 members
    found = facts(screen([*PLAIN, element(74, "Text", "One", (9, 74), 60)]))
    assert (found.secure_in_form, found.risky_in_form) == (True, True)


def test_a_form_over_the_reading_cap_reads_risky(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(form, "MAX_RISKY_READS", 1)
    plans = [element(80 + i, "Button", f"Subscribe {i}", (9, 80 + i), 60) for i in range(3)]
    assert facts(screen([*PLAIN, *plans])).risky_in_form is True


def test_within_the_caps_the_same_form_is_not_risky() -> None:
    plans = [element(80 + i, "Button", f"Subscribe {i}", (9, 80 + i), 60) for i in range(3)]
    assert facts(screen([*PLAIN, *plans])).risky_in_form is False


# ---- S1-05: when no query can follow, the skipped scan reads as true ---------------------------

STATES = {"held": TYPED, "changed": ValueFact(filled=True, digest="other"), "unread": None}


@pytest.mark.parametrize(
    ("searchbox", "landmark", "goal_span", "state", "commit", "denied"),
    list(
        itertools.product(
            (False, True),
            (False, True),
            (False, True),
            sorted(STATES),
            ("none", "send"),
            (False, True),
        )
    ),
)
def test_no_query_possible_is_always_r2_and_reads_the_form_as_true(  # noqa: PLR0917 - the axes
    monkeypatch: pytest.MonkeyPatch,
    searchbox: bool,
    landmark: bool,
    goal_span: bool,
    state: str,
    commit: str,
    denied: bool,
) -> None:
    seen: list[SearchFacts] = []
    real = query.classify

    def spy(**kw: object) -> str:
        seen.append(kw["field"])  # type: ignore[arg-type]  # the keyword `classify` takes
        return real(**kw)  # type: ignore[arg-type]  # passed through unchanged

    monkeypatch.setattr(loop, "classify", spy)
    field = dataclasses.replace(BOX, name="Delete search") if denied else BOX
    shown = [*PAGE, FORM, field, SEARCH_BUTTON]
    live = screen(shown, searchbox=searchbox, marks_read=True)
    if not landmark:
        live = dataclasses.replace(live, landmarks={e.runtime_id: None for e in shown})
    recorded = SearchFacts(
        searchbox=searchbox, in_search_landmark=landmark, secure_in_form=False, risky_in_form=False
    )
    held = PendingText(field.runtime_id, TYPED, STATES[state], recorded, goal_span)
    enter = act("press", key="Enter", commit=commit)
    risk = action_risk(enter, field, goal=GOAL, observed=live, pending=[held]).risk
    may_query = (searchbox or landmark) and goal_span and state == "held"
    may_query = may_query and commit == "none" and not denied
    if not may_query:
        assert risk in {"R2", "R3"}
        assert all(f.secure_in_form and f.risky_in_form for f in seen)


# ---- [V2.35] (1): the budget in CI is counted in operations, never in milliseconds -----------


def _big_page(n: int, hits: int) -> tuple[Observed, tuple[int, ...]]:
    """A searchbox directly under the Document (the whole page is its form): `hits` names hold
    a pattern's first word without being risky ("Subscribe" is the control class, R2)."""
    doc = element(1, "Document", "Page", (8, 1), None)
    rows = [
        element(i, "Button", f"Subscribe {i}" if i < hits + 2 else f"Result {i}", (8, i), 1)
        for i in range(2, n)
    ]
    box = element(n, "Edit", "Search", (8, n), 1)
    shown = [doc, *rows, box]
    seen = dataclasses.replace(
        observed(shown),
        aria_roles={box.runtime_id: "searchbox"},
        landmarks={e.runtime_id: None for e in shown},
    )
    return seen, box.runtime_id


def _count(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    calls: list[int] = []
    real = getattr(form, name)

    def counted(*args: object, **kw: object) -> object:
        calls.append(1)
        return real(*args, **kw)

    monkeypatch.setattr(form, name, counted)
    return calls


def test_a_3000_element_form_reads_each_member_once_and_runs_the_gate_on_hits_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen, box = _big_page(3000, hits=300)
    prefilter = _count(monkeypatch, "may_read")
    gate_runs = _count(monkeypatch, "deny_tier_of")
    assert form.form_facts(seen, box) == (False, False)
    members = 3000 - 1
    assert len(prefilter) <= members
    assert len(gate_runs) <= 300 * max(len(form.fitting_verbs(r)) for r in ("Button",))


def test_the_dispatch_rescan_of_an_unchanged_form_runs_no_prefilter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen, box = _big_page(3000, hits=300)
    form.form_facts(seen, box)
    prefilter = _count(monkeypatch, "may_read")
    assert form.form_facts(seen, box) == (False, False)
    assert prefilter == []
