"""An action whose target is already in the wanted state (5.9 [v0.7]: nothing sent, counts as
success; widget-verbs hunter WV-15/WV-44): a `type` into a field that already holds the text
used to end ok=False with code None, and was counted as sent, so a later refusal in the same step
never released its write-ahead key."""

from __future__ import annotations

from steerai.action.dispositions import ActionResult
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_loop import HELLO, Harness, _type


def test_a_field_that_already_holds_the_text_is_a_success() -> None:
    h = Harness()
    h.world.composer_value = HELLO
    h.physical.outcomes[COMPOSER[0]] = ActionResult.already_satisfied("type")
    result = h.session.run_step(_type())
    assert result.ok is True, result.to_dict()
    assert result.code is None


def test_an_already_satisfied_action_is_never_counted_as_sent() -> None:
    h = Harness()
    h.world.composer_value = HELLO
    h.physical.outcomes[COMPOSER[0]] = ActionResult.already_satisfied("type")
    h.session.run_step(_type())
    assert h.session._sent_in_act is False


def test_a_skipped_action_whose_effect_does_not_hold_still_fails() -> None:
    """Skipped means nothing was sent; it is a success only when the postcondition holds."""
    h = Harness()
    h.world.composer_value = "something else"
    h.physical.outcomes[COMPOSER[0]] = ActionResult.already_satisfied("type")
    result = h.session.run_step(_type())
    assert result.ok is False
