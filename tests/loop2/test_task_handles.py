"""[V2.38] RT-17: listing ids are task-scoped handles. Each handle is bound to one RuntimeId for
the whole task and never reassigned; a re-rendered element (a new RuntimeId) gets a new handle; a
handle whose element is gone is refused as unlisted; a number remembered from an old listing can
never land on a different element; history tokens carry no ids."""

from __future__ import annotations

import dataclasses
import re

import pytest

from steerai.loop2 import runner
from steerai.loop2.controller import v2_listing
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.runner import run_task
from steerai.world.elements import Element
from tests.loop2.world_fakes import (
    GO,
    MENU,
    PAGE,
    World,
    act,
    decision,
    element,
    observed,
    report,
    snapshot,
    task,
)


def _shuffled() -> list[Element]:
    """The same elements, the same RuntimeIds, but GO and MENU swap their snapshot ordinals."""
    swap = {GO.id: MENU.id, MENU.id: GO.id}
    return [dataclasses.replace(e, id=swap.get(e.id, e.id)) for e in PAGE]


def test_a_number_from_an_old_listing_lands_on_its_own_element_after_ordinals_shuffle() -> None:
    """Handle 3 is GO's for the whole task, though GO's snapshot ordinal is now 6."""
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("click", target=GO.id))],
        effects=[report("content_changed"), report("content_changed")],
        later=observed(_shuffled()),
    )
    run_task(task(), world.ports())
    assert [d.runtime_id for d in world.dispatched] == [GO.runtime_id, GO.runtime_id]


def test_the_view_lists_each_element_by_its_task_handle() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("wait"))],
        effects=[report("content_changed")],
        later=observed(_shuffled()),
    )
    run_task(task(), world.ports())
    second = world.views[1]
    by_rid = {e.runtime_id: second.handles[e.id] for e in second.observed.snapshot.elements}
    assert by_rid[GO.runtime_id] == GO.id
    assert by_rid[MENU.runtime_id] == MENU.id


def test_a_re_rendered_element_gets_a_new_handle_and_the_old_one_is_refused() -> None:
    again = dataclasses.replace(GO, runtime_id=(9, 33))
    rerendered = [again if e.id == GO.id else e for e in PAGE]
    world = World(
        [decision(act("click", target=MENU.id), first=True), decision(act("click", target=GO.id))],
        effects=[report("content_changed")],
        later=observed(rerendered),
    )
    run_task(task(), world.ports())
    second = world.views[1]
    assert second.handles[GO.id] != GO.id, "a new RuntimeId never inherits a handle"
    assert [d.runtime_id for d in world.dispatched] == [MENU.runtime_id]
    assert "refused:id_not_listed" in second.history + world.views[-1].history


def test_a_handle_whose_element_is_gone_is_refused_as_unlisted() -> None:
    gone = [e for e in PAGE if e.id != GO.id]
    world = World(
        [decision(act("click", target=MENU.id), first=True), decision(act("click", target=GO.id))],
        effects=[report("content_changed")],
        later=observed(gone),
    )
    run_task(task(), world.ports())
    assert [d.runtime_id for d in world.dispatched] == [MENU.runtime_id]


def test_a_handle_is_never_reassigned_to_another_runtime_id() -> None:
    """GO leaves; a new element takes its old snapshot ordinal: it gets a fresh handle."""
    newcomer = element(GO.id, "Button", "Delete", (9, 44), 1)
    replaced = [newcomer if e.id == GO.id else e for e in PAGE]
    world = World(
        [decision(act("click", target=MENU.id), first=True), decision(act("click", target=GO.id))],
        effects=[report("content_changed")],
        later=observed(replaced),
    )
    outcome = run_task(task(), world.ports())
    assert world.views[1].handles[GO.id] != GO.id
    assert [d.runtime_id for d in world.dispatched] == [MENU.runtime_id]
    assert outcome.reason != "commit_not_in_step", "the old number never reaches the Delete"


def test_an_element_without_a_runtime_id_has_no_handle() -> None:
    bare = element(11, "Button", "Bare", (), 1)
    world = World(
        [decision(act("click", target=bare.id), first=True)], screen=observed([*PAGE, bare])
    )
    run_task(task(), world.ports())
    assert bare.id not in world.views[0].handles
    assert world.dispatched == []


def test_history_tokens_carry_no_ids() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("click", target=99))],
        effects=[report("content_changed")],
    )
    run_task(task(), world.ports())
    for token in world.views[-1].history:
        assert not any(re.fullmatch(r"\d+", part) for part in token.split(":")), token


def test_claim_evidence_is_read_through_the_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    cited: list[list[int]] = []

    def spy(claim: Claim, *_a: object, **_k: object) -> tuple[str, ...]:
        cited.append(list(claim.evidence))
        return ("value_mismatch",)

    monkeypatch.setattr(runner, "check_claim", spy)
    monkeypatch.setattr(runner, "holds_words", lambda *_a: True)  # names are not this test's point
    claim = Claim(subgoal="s1", kind="state", evidence=[GO.id], wanted="on")
    world = World(
        [
            Decision(
                subgoals=[
                    SubgoalDoc(id="s1", desc="tick", params={"text": "shop"}, closes="state")
                ],
                actions=[act("click", target=MENU.id)],
            ),
            decision(claims=[claim]),
        ],
        effects=[report("content_changed")],
        later=observed(_shuffled()),
    )
    run_task(task(), world.ports())
    assert cited == [[MENU.id]], "handle 3 is GO, whose snapshot ordinal is now 6"


def test_the_listing_shows_handles_and_no_ordinal_for_an_element_without_one() -> None:
    bare = element(11, "Text", "Note", (), 1)
    shown = snapshot([*PAGE[:3], bare])
    handles = {e.id: e.id + 100 for e in PAGE[:3]}
    rows = [row.text for row in v2_listing(shown, handles=handles)]
    assert rows[1].startswith("102 ")
    assert rows[3].startswith("- ")


# ---- principal review of #330 ------------------------------------------------------------------


def test_a_runtime_id_shared_by_two_elements_gets_no_handle_and_is_never_dispatched() -> None:
    """An "Archive" button carrying GO's RuntimeId, listed first: neither gets a handle in this
    snapshot, and the old handle of GO lands on nothing."""
    dup = element(11, "Button", "Archive", GO.runtime_id, 1)
    screen = observed([PAGE[0], dup, *PAGE[1:]])
    world = World(
        [decision(act("click", target=11), first=True), decision(act("click", target=GO.id))],
        screen=screen,
    )
    run_task(task(), world.ports())
    assert dup.id not in world.views[0].handles
    assert GO.id not in world.views[0].handles
    assert world.dispatched == []


def test_evidence_naming_a_shared_runtime_id_reads_as_unlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cited: list[list[int]] = []

    def spy(claim: Claim, *_a: object, **_k: object) -> tuple[str, ...]:
        cited.append(list(claim.evidence))
        return ("value_mismatch",)

    monkeypatch.setattr(runner, "check_claim", spy)
    dup = element(11, "Button", "Archive", GO.runtime_id, 1)
    claim = Claim(subgoal="s1", kind="state", evidence=[GO.id], wanted="on")
    world = World(
        [decision(act("click", target=MENU.id), first=True), decision(claims=[claim])],
        effects=[report("content_changed")],
        later=observed([PAGE[0], dup, *PAGE[1:]]),
    )
    run_task(task(), world.ports())
    assert cited == [], "never checked: the evidence handle is not listed"
    assert "refused:id_not_listed" in world.views[-1].history


def test_the_name_re_check_reads_names_as_v1_does() -> None:
    """Only the normalised name counts (NFKC, collapsed whitespace), as v1's re-check."""
    spaced = dataclasses.replace(MENU, name="  Options ")
    world = World(
        [decision(act("click", target=GO.id), act("click", target=MENU.id), first=True)],
        effects=[report("own_state"), report("own_state")],
        after=observed([e if e.id != MENU.id else spaced for e in PAGE]),
    )
    run_task(task(), world.ports())
    assert [d.runtime_id for d in world.dispatched] == [GO.runtime_id, MENU.runtime_id]


def test_a_next_target_whose_runtime_id_is_doubled_mid_batch_is_not_dispatched() -> None:
    """The read after the first action shows the next target's RuntimeId on two elements with
    the same role and name: the handle names neither (the name re-check cannot tell them apart)."""
    twin = dataclasses.replace(MENU, id=12, box=(0, 900, 100, 908))
    world = World(
        [decision(act("click", target=GO.id), act("click", target=MENU.id), first=True)],
        effects=[report("own_state")],
        after=observed([*PAGE[:5], twin, *PAGE[5:]]),
    )
    run_task(task(), world.ports())
    assert [d.runtime_id for d in world.dispatched] == [GO.runtime_id]


def test_h6_a_next_target_gone_mid_batch_is_not_dispatched() -> None:
    """The tester's H6: a stale handle is never served from the listing the decision saw."""
    gone = observed([e for e in PAGE if e.id != MENU.id])
    world = World(
        [decision(act("click", target=GO.id), act("click", target=MENU.id), first=True)],
        effects=[report("own_state")],
        after=gone,
    )
    run_task(task(), world.ports())
    assert [d.action.target for d in world.dispatched] == [GO.id]
    assert not any(t.startswith("refused:") for v in world.views for t in v.history)
