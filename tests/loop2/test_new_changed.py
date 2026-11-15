"""[V2.56]: `new` and `changed`, from the observer's own comparison of the observation the model
last saw with the current one. A re-render is not new; position and the offscreen flag are not a
change; unread states give no `changed`; a replaced top Document gives no comparison. Both feed
always-keep. The prompt's legend names exactly the tokens the listing can emit."""

from __future__ import annotations

import dataclasses
import re

from steerai.loop2.controller import ROW_TOKENS, SYSTEM_RULES, VERBS, ListingRow, v2_listing
from steerai.loop2.loop import TurnView
from steerai.loop2.prompt_inputs import controller_inputs
from steerai.loop2.runner import run_task
from steerai.world.effects import listing_delta
from steerai.world.elements import Element
from tests.loop2.world_fakes import (
    DOC,
    FIELD,
    GO,
    QUERY_PAGE,
    World,
    act,
    decision,
    observed,
    report,
    snapshot,
    task,
)

BOX = Element(
    id=42,
    depth=1,
    role="CheckBox",
    name="Gift wrap",
    box=(0, 420, 100, 428),
    runtime_id=(9, 42),
    parent_id=DOC.id,
    caps=("toggle:off",),
)
ITEM = Element(
    id=50, depth=1, role="MenuItem", name="Dark", box=(0, 500, 100, 508), runtime_id=(9, 50),
    parent_id=DOC.id,
)  # fmt: skip
PAGE = [*QUERY_PAGE, BOX]


def _seen(elements: list[Element], *, caps_read: bool = True) -> object:
    page = observed(elements)
    return dataclasses.replace(
        page, snapshot=dataclasses.replace(page.snapshot, caps_read=caps_read)
    )


# ---- the comparison (the observer's own rules) ---------------------------------------------------


def test_an_element_that_appeared_is_new() -> None:
    delta = listing_delta(observed(PAGE), observed([*PAGE, ITEM]))
    assert delta is not None
    assert delta.new == frozenset({ITEM.runtime_id})
    assert delta.changed == frozenset()


def test_a_re_rendered_element_is_not_new() -> None:
    """Same role, name and parent chain under a new RuntimeId: the observer's re-render pair."""
    again = dataclasses.replace(GO, id=77, runtime_id=(9, 77))
    after = [e for e in PAGE if e.id != GO.id] + [again]
    delta = listing_delta(observed(PAGE), observed(after))
    assert delta is not None
    assert delta.new == frozenset()


def test_a_flipped_state_is_changed() -> None:
    flipped = dataclasses.replace(BOX, caps=("toggle:on",))
    delta = listing_delta(observed(PAGE), observed([*QUERY_PAGE, flipped]))
    assert delta is not None
    assert delta.changed == frozenset({BOX.runtime_id})
    assert delta.new == frozenset()


def test_a_renamed_or_re_roled_element_is_changed_not_new() -> None:
    renamed = dataclasses.replace(GO, name="Search now")
    reroled = dataclasses.replace(BOX, role="Button")
    after = [e for e in PAGE if e.id not in {GO.id, BOX.id}] + [renamed, reroled]
    delta = listing_delta(observed(PAGE), observed(after))
    assert delta is not None
    assert delta.changed == frozenset({GO.runtime_id, BOX.runtime_id})
    assert delta.new == frozenset()


def test_position_and_the_offscreen_flag_are_not_a_change() -> None:
    moved = dataclasses.replace(GO, box=(0, 900, 100, 908), offscreen=True)
    after = [e for e in PAGE if e.id != GO.id] + [moved]
    delta = listing_delta(observed(PAGE), observed(after))
    assert delta is not None
    assert (delta.new, delta.changed) == (frozenset(), frozenset())


def test_unread_states_give_no_changed_token_and_say_so() -> None:
    flipped = dataclasses.replace(BOX, caps=("toggle:on",))
    renamed = dataclasses.replace(GO, name="Search now")
    after = [e for e in QUERY_PAGE if e.id != GO.id] + [renamed, flipped]
    delta = listing_delta(_seen(PAGE), _seen(after, caps_read=False))  # type: ignore[arg-type]
    assert delta is not None
    assert delta.states_read is False
    assert delta.changed == frozenset({GO.runtime_id})  # the name still counts


def test_a_replaced_document_has_no_comparison() -> None:
    replaced = dataclasses.replace(DOC, runtime_id=(9, 999))
    after = [replaced, *(e for e in PAGE if e.id != DOC.id)]
    assert listing_delta(observed(PAGE), observed(after)) is None


# ---- the listing rows and the header -------------------------------------------------------------


def _row_of(rows: list[ListingRow], handle: int) -> str:
    return next(r.text for r in rows if r.text.startswith(f"{handle} "))


def test_the_rows_carry_new_and_changed() -> None:
    shown = snapshot([*PAGE, ITEM])
    rows = v2_listing(
        shown,
        handles={e.id: e.id for e in shown.elements},
        new=frozenset({ITEM.runtime_id}),
        changed=frozenset({BOX.runtime_id}),
    )
    assert _row_of(rows, ITEM.id).endswith(" new")
    assert _row_of(rows, BOX.id).endswith(" changed")
    assert not _row_of(rows, GO.id).endswith((" new", " changed"))


def _view(**changes: object) -> TurnView:
    page = observed([*PAGE, ITEM], focus=FIELD.runtime_id)
    handles = {e.id: e.id for e in page.snapshot.elements}
    view = TurnView(observed=page, log=(), first_turn=False, handles=handles)
    return dataclasses.replace(view, **changes)  # type: ignore[arg-type]


def test_the_header_says_when_there_is_no_comparison_or_states_were_unread() -> None:
    assert controller_inputs(_view(changes="no_comparison"), goal="g").changes_note == (
        "changes: no comparison this turn"
    )
    assert controller_inputs(_view(changes="states_unread"), goal="g").changes_note == (
        "changes: states unread"
    )
    assert controller_inputs(_view(changes="compared"), goal="g").changes_note is None


def test_new_and_changed_feed_always_keep() -> None:
    view = _view(new=frozenset({ITEM.runtime_id}), changed=frozenset({BOX.runtime_id}))
    inputs = controller_inputs(view, goal="g", cap=3)
    assert {ITEM.id, BOX.id} <= inputs.listed_ids


# ---- the runner: the observation the model last saw ----------------------------------------------


def test_the_runner_marks_what_appeared_since_the_model_last_looked() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("wait"))],
        screen=observed(PAGE, focus=FIELD.runtime_id),
        effects=[report("content_changed")],
        timeline=[(1, observed([*PAGE, ITEM], focus=FIELD.runtime_id))],
    )
    run_task(task(), world.ports())
    first, second = world.views[0], world.views[1]
    assert first.changes == "no_comparison"
    assert second.new == frozenset({ITEM.runtime_id})
    assert ITEM.id in controller_inputs(second, goal="g", cap=3).listed_ids  # always-kept


def test_an_element_seen_last_turn_is_never_new_even_if_it_was_cut() -> None:
    """[V2.56] (4): new means the world changed; the comparison is with the observation."""
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("wait"))],
        screen=observed([*PAGE, ITEM], focus=FIELD.runtime_id),
        effects=[report("content_changed")],
    )
    run_task(task(), world.ports())
    assert world.views[1].new == frozenset()


# ---- the legend and the listing agree, both ways -------------------------------------------------


def _legend_tokens() -> set[str]:
    legend = SYSTEM_RULES.split("Listing legend:\n", 1)[1].split("Rules:\n", 1)[0]
    quoted = re.findall(r"`([^`]+)`", legend)
    return {t for t in quoted if " " not in t and t not in VERBS}


def test_every_token_the_legend_names_is_one_the_listing_emits_and_the_reverse() -> None:
    assert _legend_tokens() == set(ROW_TOKENS)
