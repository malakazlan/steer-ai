"""[V2.85] (iv): the history shows the specific refusal token whenever code knows one. A refused
act with a detail is `refused:<detail>:<handle>`, never the broader `not_delivered`; no refusal
token the act port or the runner produces is collapsed into another in the renderer; a detail
outside the closed sets stays `not_delivered`."""

from __future__ import annotations

from typing import cast

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.execution import REFUSAL_DETAILS
from steerai.errors import ErrorCode
from steerai.loop2.history import (
    ACT_CAUSES,
    GRAMMAR,
    REFUSAL_TOKENS,
    Cause,
    Refused,
    Vocab,
    record_ok,
    render,
)
from steerai.loop2.loop import PORT_DETAILS, ActResult
from steerai.loop2.runner import run_task
from steerai.session.monitor import TAKEOVER_TOKENS
from tests.loop2.world_fakes import GO, World, act, decision, task

HANDLED_ELSEWHERE = frozenset(TAKEOVER_TOKENS) | {"wall_budget", "field_not_empty"}
"""A takeover stops the task, `wall_budget` ends it at the budget, `field_not_empty` is a
refusal token of its own."""
CAUSES = sorted(ACT_CAUSES - HANDLED_ELSEWHERE)


def _refused(detail: str) -> ActResult:
    sent_nothing = ActionResult(
        "click", Delivery.NOT_DELIVERED, code=ErrorCode.ELEMENT_NOT_ACTIONABLE,
        evidence={"detail": detail},
    )  # fmt: skip
    return ActResult(sent_nothing, None)


def _history(detail: str) -> tuple[str, ...]:
    world = World([decision(act("click", target=GO.id), first=True)], results=[_refused(detail)])
    run_task(task(), world.ports())
    return world.views[1].history


def test_the_causes_are_the_act_port_and_the_executors_details() -> None:
    assert ACT_CAUSES == REFUSAL_DETAILS | PORT_DETAILS
    assert {"expand_needs_click", "covered", "target_moved", "target_gone"} <= ACT_CAUSES


@pytest.mark.parametrize("detail", CAUSES)
def test_every_cause_is_shown_as_itself_with_the_elements_handle(detail: str) -> None:
    history = _history(detail)
    assert f"refused:{detail}:{GO.id}" in history
    assert f"refused:not_delivered:{GO.id}" not in history


@pytest.mark.parametrize("detail", [None, "not_a_known_detail"])
def test_no_known_cause_is_not_delivered(detail: str | None) -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        results=[_refused(detail)],  # type: ignore[arg-type]  # None: a result with no detail
    )
    run_task(task(), world.ports())
    assert f"refused:not_delivered:{GO.id}" in world.views[1].history


def test_a_field_that_held_text_is_its_own_token_as_before() -> None:
    assert f"refused:field_not_empty:{GO.id}" in _history("field_not_empty")


def _refusal(token: str, handle: int | None) -> Refused:
    return Refused(cast(Cause, token), handle)  # a str that the vocabulary check judges


def test_no_two_refusal_tokens_render_the_same_line() -> None:
    every = sorted(REFUSAL_TOKENS | ACT_CAUSES)
    lines = [render(_refusal(t, None if t == "id_not_listed" else 1)) for t in every]
    assert len(set(lines)) == len(lines)


def test_every_cause_passes_the_history_vocabulary_and_grammar() -> None:
    vocab = Vocab({}, frozenset({1}))
    for cause in ACT_CAUSES:
        record = _refusal(cause, 1)
        assert record_ok(record, vocab), cause
        assert GRAMMAR.fullmatch(render(record)), cause
    assert not record_ok(_refusal("made_up", 1), vocab)
