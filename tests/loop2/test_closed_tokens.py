"""#349: the refusal, claim and proven tokens are typed Literals ([V2.49]: record fields are
enums), so mypy checks every producer, and the history's closed sets ARE those Literals.
#350: the risk path also derives `submit` from its one classification (`action_class`).
"""

from __future__ import annotations

import itertools
import typing

import pytest

from steerai.loop2 import decision, history, loop, runner, verify
from steerai.loop2.history import (
    CLAIM_RESULTS,
    PROVEN_HOW,
    REFUSAL_TOKENS,
    ClaimResult,
    ClaimWord,
    Proven,
    ProvenHow,
    RefusalWord,
    Refused,
)
from steerai.loop2.loop import Dispatch, action_class, action_risk
from steerai.loop2.world_ports import watch_kind
from steerai.world.elements import Element
from tests.loop2.test_action_risk import BOX, BOX_GO, SEARCHY, pending, screen
from tests.loop2.test_action_risk import SHOWN as SHOWN_FOR_RISK
from tests.loop2.world_fakes import GOAL, act, element


def _words(annotation: object) -> set[str]:
    """Every string a (union of) Literal type allows."""
    out: set[str] = set()
    for arg in typing.get_args(annotation):
        if isinstance(arg, str):
            out.add(arg)
        else:
            out |= _words(arg)
    return out


def test_the_closed_sets_are_the_literals() -> None:
    assert _words(RefusalWord) == REFUSAL_TOKENS
    assert _words(ClaimWord) == CLAIM_RESULTS
    assert _words(ProvenHow) == PROVEN_HOW


def test_the_record_fields_are_typed_with_the_literals() -> None:
    assert typing.get_type_hints(Refused)["token"] == RefusalWord
    assert typing.get_type_hints(ClaimResult)["result"] == ClaimWord
    assert typing.get_type_hints(Proven)["how"] == ProvenHow


def _open(annotation: object) -> bool:
    """A bare `str` anywhere in the annotation: a producer mypy cannot hold to its set."""
    if annotation is str:
        return True
    return any(_open(a) for a in typing.get_args(annotation))


PRODUCERS = [
    decision._action_tokens,
    decision._type_tokens,
    decision._claim_tokens,
    decision._subgoal_doc_tokens,
    decision._subgoal_tokens,
    decision.subgoal_tokens,
    decision._shape_tokens,
    decision.validate,
    verify.evidence_tokens,
    verify.check_claim,
    verify.navigated_tokens,
    verify._cited_tokens,
    verify._excluded,
    loop.type_refusal,
]


@pytest.mark.parametrize("producer", PRODUCERS, ids=lambda f: f.__name__)
def test_every_producer_returns_a_closed_token_type(producer: object) -> None:
    hints = typing.get_type_hints(producer)
    assert not _open(hints["return"]), hints["return"]


def test_the_runner_takes_and_gives_closed_tokens() -> None:
    hints = typing.get_type_hints(runner._Run._refused)
    assert not _open(hints["return"])
    refuse = typing.get_type_hints(runner._Run._refuse)
    assert refuse["tokens"] == RefusalWord
    assert typing.get_args(typing.get_type_hints(runner._Run._reject)["tokens"]) == (ClaimWord,)
    assert typing.get_type_hints(runner._Run._tick)["how"] == ProvenHow


def test_history_names_the_runner_tokens() -> None:
    """The runner's own refusals and claim results live in `history` (the runner imports it)."""
    assert {"repeat_send", "not_delivered"} <= _words(history.RunnerRefusal)
    assert {"accepted", "kind_mismatch"} <= _words(history.RunnerClaim)


# ---- #350: submit from the same classification -------------------------------------------------

VERBS = ["click", "focus", "check", "press"]
KEYS = ["Enter", "Space", "Tab"]


@pytest.mark.parametrize(
    ("verb", "key", "target"),
    [
        (verb, key, target)
        for verb, target in itertools.product(VERBS, [BOX, BOX_GO])
        for key in (KEYS if verb == "press" else [None])
    ],
)
def test_submit_and_trigger_come_from_one_call_with_pending_text(
    verb: str, key: str | None, target: Element, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With text pending in a search field (the submit rule's context), the one call to
    `action_class` gives the risk, `submit` and `trigger`, and the watch agrees with both."""
    calls: list[str] = []
    real = loop.action_class

    def spy(action: object, role: str) -> str:
        calls.append(role)
        return real(action, role)  # type: ignore[arg-type]  # forwarding the spied call

    monkeypatch.setattr(loop, "action_class", spy)
    element = target  # BOX (the field) or BOX_GO (its button)
    action = act(verb, key=key) if verb == "press" else act(verb, target=element.id)
    seen = action_risk(
        action,
        element,
        goal=GOAL,
        observed=screen(),
        pending=pending(BOX, SEARCHY),
    )
    assert len(calls) == 1
    sent = Dispatch(
        action=action,
        runtime_id=element.runtime_id,
        role=element.role,
        name=element.name,
        risk=seen.risk,
        provenance=None,
        submit=seen.submit,
        trigger=seen.trigger,
    )
    expected = "submit" if seen.submit else ("trigger" if seen.trigger else "plain")
    assert watch_kind(sent) == expected


def test_a_submit_click_beside_pending_text_is_a_submit() -> None:
    seen = action_risk(
        act("click", target=BOX_GO.id),
        BOX_GO,
        goal=GOAL,
        observed=screen(),
        pending=pending(BOX, SEARCHY),
    )
    assert seen.submit


def test_an_enter_on_the_field_is_a_submit() -> None:
    assert action_class(act("press", key="Enter"), BOX.role) == "enter_field"
    seen = action_risk(
        act("press", key="Enter"), BOX, goal=GOAL, observed=screen(), pending=pending(BOX, SEARCHY)
    )
    assert seen.submit


PANE = element(77, "Pane", "Panel", (9, 77), 40)
CHECKBOX = element(78, "CheckBox", "Shop", (9, 78), 40)


@pytest.mark.parametrize(
    ("verb", "key", "target", "submit", "trigger"),
    [
        # written from the rulings, not from the code's formula
        ("press", "Enter", BOX, True, False),  # enter_field: the Enter table (a submit)
        ("press", "Enter", PANE, False, True),  # enter_other: R2, an Enter (trigger), NOT submit
        ("press", "Space", CHECKBOX, False, False),  # state_press: neither ([V2.50])
        ("press", "Enter", CHECKBOX, False, False),  # state_press: neither ([V2.50])
        ("click", None, BOX_GO, True, True),  # a click beside pending text: the submit rule
    ],
)
def test_submit_and_trigger_by_class_with_pending_text(
    verb: str, key: str | None, target: Element, submit: bool, trigger: bool
) -> None:
    """#351 review (M6): the expected values come from [V2.48] and [V2.50]: an Enter on a
    non-field, non-acting role is never a submit (it would take the 5 s guard)."""
    action = act(verb, key=key) if verb == "press" else act(verb, target=target.id)
    shown = screen([*SHOWN_FOR_RISK, PANE, CHECKBOX])
    seen = action_risk(action, target, goal=GOAL, observed=shown, pending=pending(BOX, SEARCHY))
    assert (seen.submit, seen.trigger) == (submit, trigger)
