"""Actuation rules for step 3 ([V2.5], [V2.6]; V2 §7.3, §8.11): a verified actuation is never
retried, no ladder on R2/R3, and a press goes to the last acted element. Red on the 3c stub."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.action.dispositions import ActionResult, Delivery, Step
from steerai.errors import ErrorCode
from steerai.loop2 import actuate
from steerai.loop2.actuate import may_retry, press_allowed
from steerai.orchestrator.contracts import Risk
from tests.loop2.world_fakes import FIELD, GO, report


def test_a_verified_actuation_is_never_retried() -> None:
    assert may_retry(risk="R0", verified=True, report=report("own_state")) is False


@pytest.mark.parametrize("risk", ["R2", "R3"])
def test_no_ladder_on_r2_or_r3(risk: Risk) -> None:
    assert may_retry(risk=risk, verified=False, report=report("none")) is False


def test_step_3_has_no_retry_rung_even_for_a_plain_click_with_no_effect() -> None:
    assert may_retry(risk="R1", verified=False, report=report("none")) is False


def test_a_press_goes_ahead_when_focus_is_on_the_last_acted_element() -> None:
    assert press_allowed(focus=FIELD.runtime_id, last_acted=FIELD.runtime_id) is True


def test_a_press_is_refused_when_focus_is_elsewhere() -> None:
    assert press_allowed(focus=GO.runtime_id, last_acted=FIELD.runtime_id) is False


def test_a_press_is_refused_when_focus_was_not_read() -> None:
    assert press_allowed(focus=None, last_acted=FIELD.runtime_id) is False


def test_a_press_is_refused_when_nothing_was_acted_on() -> None:
    assert press_allowed(focus=FIELD.runtime_id, last_acted=None) is False


# ---- [V2.22] #25: a query click's rung is any R1 click's; a caution click has none -----------


def test_a_query_click_with_proven_none_retries_exactly_as_any_r1_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In step 3 no click gets a rung (`RETRY_RUNGS = 0`); once the ladder allows one, a query
    click (R1) with a proven `none` gets it like any R1 click."""
    assert may_retry(risk="R1", verified=False, report=report("none")) is False
    monkeypatch.setattr(actuate, "RETRY_RUNGS", 1)
    assert may_retry(risk="R1", verified=False, report=report("none")) is True


def test_a_caution_click_never_gets_a_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """A submit click classified R2 (a caution click) is never retried, whatever the ladder."""
    monkeypatch.setattr(actuate, "RETRY_RUNGS", 1)
    assert may_retry(risk="R2", verified=False, report=report("none")) is False


def test_a_query_click_with_any_effect_but_proven_none_gets_no_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actuate, "RETRY_RUNGS", 1)
    for seen in (report("focus"), report("unknown"), report("none", unread=["url"])):
        assert may_retry(risk="R1", verified=False, report=seen) is False


# ---- [V2.25] (c): nothing sent gets v1's one local retry; uncertain is unknown, no retry -------

LEFT = ActionResult(verb="click", delivery=Delivery.NOT_DELIVERED, code=ErrorCode.ELEMENT_NOT_FOUND)


@pytest.mark.parametrize("risk", ["R0", "R1", "R2", "R3"])
def test_nothing_sent_gets_v1s_one_local_retry_under_the_same_decision(risk: Risk) -> None:
    assert may_retry(risk=risk, verified=False, report=report("none"), act=LEFT) is True


def test_v1s_local_retry_is_once() -> None:
    assert may_retry(risk="R1", verified=False, report=report("none"), act=LEFT, retries=1) is False


def test_a_not_delivered_act_that_sent_steps_is_not_retried() -> None:
    tried = dataclasses.replace(
        LEFT, steps=[Step(label="Invoke", mechanism="semantic", outcome="failed")]
    )
    assert may_retry(risk="R1", verified=False, report=report("none"), act=tried) is False


def test_a_not_delivered_act_with_another_code_is_not_retried() -> None:
    other = dataclasses.replace(LEFT, code=ErrorCode.ELEMENT_NOT_ACTIONABLE)
    assert may_retry(risk="R1", verified=False, report=report("none"), act=other) is False


def test_an_uncertain_delivery_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actuate, "RETRY_RUNGS", 1)
    uncertain = ActionResult(
        verb="click", delivery=Delivery.DELIVERY_UNCERTAIN, code=ErrorCode.VERIFY_TIMEOUT
    )
    assert may_retry(risk="R0", verified=False, report=report("none"), act=uncertain) is False


def test_the_local_retry_codes_are_v1s() -> None:
    from steerai.orchestrator import loop as v1_loop  # noqa: PLC0415 - the v1 constant, locked

    assert actuate.ACT_RETRY_CODES == v1_loop._ACT_RETRY_CODES
