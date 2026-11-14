"""The #331 review round (principal REVISE and the tester's kill tests): live claim reads, the
task-wide echo guards ([V2.38] RT-39), the observer's own results rule (§6.4), one exclusion
rule for cited and auto proofs ([V2.29] Q1), and the navigated boundaries."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.loop2 import runner
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.loop import Dispatch
from steerai.loop2.owned import field_owned
from steerai.loop2.runner import run_task
from steerai.loop2.verify import auto_proof, navigated_tokens
from steerai.world.effects import Observed, poll_tier
from steerai.world.elements import Element
from steerai.world.landmarks import Landmark
from tests.loop2.test_navigated import SUB, TYPED, before, page
from tests.loop2.world_fakes import (
    DOC,
    EMPTY,
    FIELD,
    GO,
    MENU,
    PAGE,
    QUERY_PAGE,
    RESULT,
    TAB,
    URL,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

# ---- principal blockers ------------------------------------------------------------------------


def test_p1_a_value_claim_reads_the_field_live_not_a_cached_read() -> None:
    world = World(
        [
            decision(act("type", target=FIELD.id, param="text"), first=True),
            decision(act("click", target=MENU.id)),
            decision(claims=[Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")]),
        ],
        effects=[report("own_value"), report("content_changed")],
        screen=page(url=URL),
    )
    ports = world.ports()
    inner = ports.act

    def act_and_clear(sent: Dispatch) -> object:
        out = inner(sent)
        if sent.action.verb == "click":
            world.values[FIELD.runtime_id] = EMPTY  # the page cleared the field
        return out

    outcome = run_task(task(), dataclasses.replace(ports, act=act_and_clear))  # type: ignore[arg-type]  # a wrapping fake
    assert outcome.status != "done"


def test_p2_the_option_rule_holds_after_the_typed_field_left_with_its_document() -> None:
    """RT-39: the echo guards are task-wide; pending text was pruned when the Document was
    replaced, but the words Steer typed still make an option no proof."""
    newdoc = element(1, "Document", "Shop", (9, 501), None)
    box2 = element(2, "Edit", "Search", (9, 502), 1)
    filt = element(3, "Button", "Filter", (9, 503), 1)
    option = dataclasses.replace(
        element(20, "ListItem", "red shoes", (9, 520), 1), caps=("selectable",)
    )
    results = page(base=[newdoc, box2, filt, option, *QUERY_PAGE[4:]])
    world = World(
        [
            decision(act("type", target=FIELD.id, param="text"), first=True),
            decision(act("click", target=GO.id)),
            decision(act("click", target=12)),  # the Filter button's handle on the new page
        ],
        effects=[report("own_value"), report("navigated"), report("content_changed")],
        screen=page(url=URL),
        later=results,
        later_after_acts=2,
    )
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"


def test_p3_a_runtime_id_less_element_holding_the_words_before_is_no_result() -> None:
    """§6.4, the observer's own absent-before rule: the words already on the page before the
    baseline are no result, whatever element holds them."""
    note = element(30, "Text", "red shoes", (), 1)
    assert auto_proof(SUB, page([note], url=URL), page([note]), typed=TYPED) is None


def test_p4_a_cited_claim_takes_the_same_exclusions_as_the_auto_proof() -> None:
    menu = element(50, "Menu", "", (9, 50), DOC.id)
    item = element(51, "MenuItem", "red shoes", (9, 51), 50)
    option = dataclasses.replace(RESULT, caps=("selectable",))
    base = page(url=URL)
    in_menu = navigated_tokens(
        SUB, base, page([menu, item]), cited=[item.id], typed=TYPED, echo=True
    )
    an_option = navigated_tokens(
        SUB, base, page([option]), cited=[option.id], typed=TYPED, echo=True
    )
    assert "under_suggestions" in in_menu
    assert "option_evidence" in an_option


# ---- the tester's kill tests -------------------------------------------------------------------


@pytest.mark.parametrize(
    "scheme", ["javascript", "data", "blob", "about", "ftp", "view-source", "chrome", "edge"]
)
def test_b4_only_the_allow_list_is_navigated(scheme: str) -> None:
    after = page([RESULT], url=f"{scheme}://shop.test/?q=red%20shoes")
    assert auto_proof(SUB, before(), after, typed=TYPED) is None
    assert navigated_tokens(SUB, before(), after, cited=[RESULT.id], typed=TYPED) == ("not_a_page",)


@pytest.mark.parametrize("scheme", ["http", "https", "HTTPS"])
def test_b4_web_schemes_prove(scheme: str) -> None:
    after = page([RESULT], url=f"{scheme}://shop.test/?q=red%20shoes")
    assert auto_proof(SUB, before(), after, typed=TYPED) == "results_with_url"


def test_b4e_an_unread_url_with_a_title_carrier_is_never_navigated() -> None:
    after = dataclasses.replace(page(title="red shoes - Shop"), url=None, document_url=None)
    assert auto_proof(SUB, before(), after, typed=TYPED) is None
    assert navigated_tokens(SUB, before(), after, cited=[], typed=TYPED) == ("url_unread",)


def test_b3c_mixed_tiers_are_rejected_not_raised() -> None:
    base = poll_tier(before().snapshot)
    assert auto_proof(SUB, base, page([RESULT]), typed=TYPED) is None
    assert navigated_tokens(SUB, base, page([RESULT]), cited=[RESULT.id], typed=TYPED) == (
        "tier_mismatch",
    )


def test_b14_the_typed_field_echoing_the_words_is_no_result() -> None:
    after = page()
    echo = dataclasses.replace(FIELD, name="red shoes")
    elements = [echo if e.id == FIELD.id else e for e in after.snapshot.elements]
    after = dataclasses.replace(
        after, snapshot=dataclasses.replace(after.snapshot, elements=elements)
    )
    assert auto_proof(SUB, before(), after, typed=TYPED, acted=FIELD.runtime_id) is None


def test_b15_a_new_element_outside_the_document_is_no_result() -> None:
    outside = element(70, "Text", "red shoes", (9, 70), None)
    assert auto_proof(SUB, before(), page([outside]), typed=TYPED, acted=FIELD.runtime_id) is None


def test_b8_a_heading_outside_the_typed_fields_document_is_no_carrier() -> None:
    head = element(81, "Heading", "red shoes", (9, 81), None)
    base = dataclasses.replace(before(), headings={})
    after = dataclasses.replace(page([head]), headings={head.runtime_id: 1})
    assert auto_proof(SUB, base, after, typed=TYPED, acted=FIELD.runtime_id) is None


def test_o5_rule_2_a_list_named_by_controller_for_is_field_owned() -> None:
    """The rule is in `field_owned`; ControllerFor is not in the v2 fetch yet ([V2.23]), so the
    loop passes no `controlled` and the rule is inert there (recorded in the PR)."""
    seen = page([RESULT])
    owned = field_owned(RESULT, seen, FIELD.runtime_id, controlled=frozenset({RESULT.runtime_id}))
    assert owned == ("owned_by_controller_for",)


def _subgoals() -> list[SubgoalDoc]:
    return [
        SubgoalDoc(id="s1", desc="a", params={"text": "red shoes"}),
        SubgoalDoc(id="s2", desc="b", params={"text": "shop"}),
    ]


def test_b6_the_echo_guard_holds_every_field_typed_in_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = element(12, "Edit", "Other", (9, 12), 1)
    seen: list[tuple[tuple[int, ...], ...]] = []

    def spy(*_a: object, typed: tuple[tuple[int, ...], ...] = (), **_k: object) -> None:
        seen.append(tuple(typed))

    monkeypatch.setattr(runner, "auto_proof", spy)
    first = Decision(subgoals=_subgoals(), actions=[act("type", target=FIELD.id, param="text")])
    second = Decision(actions=[act("type", target=other.id, param="text", subgoal="s2")])
    moved = dataclasses.replace(
        observed([*PAGE, other]), url=URL + "?q=x", document_url=URL + "?q=x"
    )
    world = World([first, second], screen=observed([*PAGE, other]), later=moved, later_after_acts=2)
    run_task(task(), world.ports())
    assert seen
    assert set(seen[-1]) == {FIELD.runtime_id, other.runtime_id}


def test_b8b_the_acted_field_is_the_one_typed_under_the_subgoal_being_proved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """s1 typed into FIELD, s2 later into another field: s1's proof reads carriers from FIELD."""
    other = element(12, "Edit", "Other", (9, 12), 1)
    acted: dict[str, object] = {}

    def spy(subgoal: object, *_a: object, acted_field: object = None, **k: object) -> None:
        acted[getattr(subgoal, "id", "?")] = k.get("acted")

    monkeypatch.setattr(runner, "auto_proof", spy)
    first = Decision(subgoals=_subgoals(), actions=[act("type", target=FIELD.id, param="text")])
    second = Decision(actions=[act("type", target=other.id, param="text", subgoal="s2")])
    moved = dataclasses.replace(
        observed([*PAGE, other]), url=URL + "?q=x", document_url=URL + "?q=x"
    )
    world = World([first, second], screen=observed([*PAGE, other]), later=moved, later_after_acts=2)
    run_task(task(), world.ports())
    assert acted["s1"] == FIELD.runtime_id
    assert acted["s2"] == other.runtime_id


def test_b14_another_typed_field_holding_the_words_is_no_result() -> None:
    """The observer exempts only the acted field; a second field Steer typed into, renamed to the
    words, is excluded by the typed-field rule itself."""
    other = element(12, "Edit", "Notes", (9, 12), 1)
    base = page([other], url=URL)
    renamed = dataclasses.replace(other, name="red shoes")
    after = page([renamed])
    typed = (FIELD.runtime_id, other.runtime_id)
    assert auto_proof(SUB, base, after, typed=typed, acted=FIELD.runtime_id) is None


def test_p2_a_field_typed_then_emptied_still_owns_its_search_landmark() -> None:
    """RT-39 (field ownership is task-wide): the field is emptied, so its pending text ends, but a
    row sharing its Search landmark that holds the words still proves nothing."""
    form = element(60, "Group", "", (9, 60), DOC.id)
    box = element(61, "Edit", "Search", (9, 61), 60)
    hint = element(62, "ListItem", "red shoes", (9, 62), DOC.id)
    search = Landmark(kind="search", runtime_id=form.runtime_id)

    def screen(extra: list[Element], url: str) -> Observed:
        shown = [*QUERY_PAGE, form, box, *extra]
        marks = {e.runtime_id: None for e in shown} | {box.runtime_id: search}
        marks |= {e.runtime_id: search for e in extra}
        seen = observed(shown, focus=box.runtime_id, searchbox=False)
        return dataclasses.replace(seen, url=url, document_url=url, landmarks=marks)

    world = World(
        [
            decision(act("type", target=box.id, param="text"), first=True),
            decision(act("click", target=MENU.id)),
            decision(act("click", target=GO.id)),
        ],
        effects=[report("own_value"), report("content_changed"), report("url_changed_in_place")],
        screen=screen([], URL),
        cleared={box.runtime_id: 1},
        timeline=[(3, screen([hint], URL + "?q=red%20shoes"))],
    )
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"


# ---- principal re-review of #331: a browser-frame element is never a result ----------------------


def test_a_renamed_tab_is_never_a_result() -> None:
    """§7.1: a tab name is never evidence; the result must be inside the scope Document."""
    renamed = dataclasses.replace(TAB, name="red shoes - Shop")
    base = page(url=URL)
    after = page(base=[e if e.id != TAB.id else renamed for e in QUERY_PAGE])
    assert auto_proof(SUB, base, after) is None


def test_a_renamed_tab_is_no_result_when_the_acted_field_is_not_in_the_baseline() -> None:
    renamed = dataclasses.replace(TAB, name="red shoes - Shop")
    base = page(url=URL)
    after = page(base=[e if e.id != TAB.id else renamed for e in QUERY_PAGE])
    assert auto_proof(SUB, base, after, typed=((9, 777),), acted=(9, 777), echo=True) is None


def test_with_no_single_top_document_nothing_is_a_result() -> None:
    """Fail closed: with no single top Document (none, or several roots) there is no scope, and
    the whole window is never one."""
    second = element(90, "Document", "Other", (9, 900), None)
    base = page([second], url=URL)
    after = page([second, RESULT])
    assert auto_proof(SUB, base, after, typed=TYPED, acted=FIELD.runtime_id) is None


def test_the_echo_guard_matches_words_case_folded_and_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """s1 typed "Red  Shoes"; s2's words are "red shoes": the option rule applies to s2."""
    echoes: dict[str, object] = {}

    def spy(subgoal: object, *_a: object, **k: object) -> None:
        echoes[getattr(subgoal, "id", "?")] = k.get("echo")

    monkeypatch.setattr(runner, "auto_proof", spy)
    goal = "type Red  Shoes then find red shoes"
    subs = [
        SubgoalDoc(id="s1", desc="a", params={"text": "Red  Shoes"}),
        SubgoalDoc(id="s2", desc="b", params={"text": "red shoes"}),
    ]
    first = Decision(subgoals=subs, actions=[act("type", target=FIELD.id, param="text")])
    second = Decision(actions=[act("click", target=GO.id, subgoal="s2")])
    moved = dataclasses.replace(observed(QUERY_PAGE), url=URL + "?q=x", document_url=URL + "?q=x")
    world = World(
        [first, second],
        effects=[report("own_value"), report("url_changed_in_place")],
        screen=observed(QUERY_PAGE),
    )
    world.later = moved
    world.later_after_acts = 2
    run_task(dataclasses.replace(task(), goal=goal), world.ports())
    assert echoes.get("s2") is True
