"""Actuation rules for step 3 ([V2.5], [V2.6]; V2 §7.3, §8.11): a verified actuation is never
retried, no ladder on R2/R3, and a press goes to the last acted element. Red on the 3c stub."""

from __future__ import annotations

import pytest

from steerai.loop2.actuate import may_retry, press_allowed
from steerai.orchestrator.contracts import Risk
from tests.loop2.world_fakes import FIELD, GO, pending, report


@pending("[V2.5] a verified actuation is never retried")
def test_a_verified_actuation_is_never_retried() -> None:
    assert may_retry(risk="R0", verified=True, report=report("own_state")) is False


@pending("§8.11 no ladder on R2/R3")
@pytest.mark.parametrize("risk", ["R2", "R3"])
def test_no_ladder_on_r2_or_r3(risk: Risk) -> None:
    assert may_retry(risk=risk, verified=False, report=report("none")) is False


@pending("packet §3 no retry rung in step 3")
def test_step_3_has_no_retry_rung_even_for_a_plain_click_with_no_effect() -> None:
    assert may_retry(risk="R1", verified=False, report=report("none")) is False


@pending("[V2.6] press target")
def test_a_press_goes_ahead_when_focus_is_on_the_last_acted_element() -> None:
    assert press_allowed(focus=FIELD.runtime_id, last_acted=FIELD.runtime_id) is True


@pending("[V2.6] press target")
def test_a_press_is_refused_when_focus_is_elsewhere() -> None:
    assert press_allowed(focus=GO.runtime_id, last_acted=FIELD.runtime_id) is False


@pending("[V2.6] press target")
def test_a_press_is_refused_when_focus_was_not_read() -> None:
    assert press_allowed(focus=None, last_acted=FIELD.runtime_id) is False


@pending("[V2.6] press target")
def test_a_press_is_refused_when_nothing_was_acted_on() -> None:
    assert press_allowed(focus=FIELD.runtime_id, last_acted=None) is False
