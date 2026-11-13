"""An act refused `unstable_ui` with nothing sent gets the act's one retry (dev wk1 2026-11-13: the
click revealed Wikipedia's search box, which was still expanding when the type sampled its rect
twice 34 ms apart, so the type was refused, never retried, and the submit then found the
composer empty). The retry is the one `element_not_found` already has: a fresh observe, the same
asked element re-resolved, the dispatch again, under the same gate decision. Prior art:
Playwright's actionability checks wait for a stable box and retry instead of failing on one
sample."""

from __future__ import annotations

from typing import Any

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode
from tests.orchestrator.fakes import COMPOSER, FakeExecutor
from tests.orchestrator.test_loop import HELLO, Harness, _send, _type


def _moving(verb: str = "type", *, sent: bool = False) -> ActionResult:
    """The executor's refusal when the target's rect moved between its two samples."""
    focus = ActionStep(label="SetFocus", mechanism="semantic", outcome="succeeded")
    steps = [focus] if sent else []
    return ActionResult(
        verb=verb,
        delivery=Delivery.NOT_DELIVERED,
        steps=steps,
        code=ErrorCode.UNSTABLE_UI,
        evidence={"reason": "unstable_ui"},
    )


def _settles_after(executor: FakeExecutor, element_id: int, moving: int, **kw: Any) -> list[int]:
    """The element's rect moves for the first `moving` dispatches, then holds still."""
    attempts = [0]

    def sample(ex: FakeExecutor, action: Any) -> None:
        if action.element_id != element_id:
            return
        attempts[0] += 1
        if attempts[0] <= moving:
            ex.outcomes[element_id] = _moving(**kw)
        else:
            ex.outcomes.pop(element_id, None)

    executor.on_execute = sample
    return attempts


def test_a_type_refused_while_its_field_moved_is_retried_once_and_lands() -> None:
    h = Harness()
    attempts = _settles_after(h.physical, COMPOSER[0], moving=1)
    result = h.session.run_step(_type())
    assert attempts[0] == 2
    assert result.ok is True, result.to_dict()
    assert result.evidence["retries"] == 1
    assert result.evidence["act_retry"] == "unstable_ui", "the trace tells this retry apart"
    assert h.world.composer_value == HELLO


def test_a_field_still_moving_on_the_retry_ends_the_step_unstable() -> None:
    h = Harness()
    attempts = _settles_after(h.physical, COMPOSER[0], moving=2)
    result = h.session.run_step(_type())
    assert attempts[0] == 2, "one retry, never a loop"
    assert result.ok is False
    assert result.code is ErrorCode.UNSTABLE_UI
    assert result.evidence["retries"] == 1


def test_an_unstable_refusal_after_a_sent_step_is_not_retried() -> None:
    h = Harness()
    attempts = _settles_after(h.physical, COMPOSER[0], moving=1, sent=True)
    result = h.session.run_step(_type())
    assert attempts[0] == 1, "something reached the app: never sent again"
    assert result.ok is False
    assert result.evidence["retries"] == 0
    assert "act_retry" not in result.evidence


def test_an_approved_send_retried_after_its_button_moved_asks_once() -> None:
    h = Harness()
    h.session.run_step(_type())
    send_id = next(e.id for e in h.world.observe(0x10).elements if e.name == "Send")
    attempts = _settles_after(h.semantic, send_id, moving=1, verb="click")
    result = h.session.run_step(_send())
    assert attempts[0] == 2
    assert result.ok is True, result.to_dict()
    assert result.evidence["retries"] == 1
    assert len(h.confirmer.asked) == 1, "one decision covers the local retry"
    assert h.world.sent == [HELLO], "sent once"
