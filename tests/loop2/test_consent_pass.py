"""The gate pass's own choices beyond the step 4 invariants (packet v2-step4-claims §3.3, §3.4;
[V2.25] (c); [V2.88] L2, L4, L5; [V2.90] (3)): what the ask shows, when the yes is spent, the
input check before v1's one local retry, and the bracket's edge cases."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import pytest

from steerai.errors import SteerError
from steerai.loop2.consent import (
    RETRY_CODES,
    Consents,
    InputClass,
    InputEvent,
    commit_refusal,
    effective_commit,
    input_after_answer,
)
from steerai.orchestrator.loop import _ACT_RETRY_CODES
from tests.loop2.test_invariants_commit import TEXT, TYPED
from tests.loop2.test_invariants_consent import KEY, LEFT, REQUEST, Script, run
from tests.loop2.world_fakes import GOAL

ENTER = 0x0D


def test_the_local_retry_codes_are_v1s() -> None:
    """[V2.25] (c): v1's one local retry, by v1's own codes."""
    assert frozenset(RETRY_CODES) == _ACT_RETRY_CODES


@dataclass
class _LateInput(Script):
    """Input that arrives after the first dispatch: the second read sees it."""

    late: InputClass | None = None
    reads: int = 0

    def _input(self) -> InputClass | None:
        self.events.append("input_check")
        self.reads += 1
        return None if self.reads == 1 else self.late


@pytest.mark.parametrize("who", ["takeover", "other_steer_input"])
def test_input_before_the_local_retry_ends_the_task_and_sends_nothing_more(
    who: InputClass,
) -> None:
    """[V2.88] L5: the retry is a dispatch too, so input since the answer stops it; the first
    dispatch was called, so nothing is voided ([V2.90] (3))."""
    script = _LateInput(results=[LEFT], late=who)
    end = run(script)
    assert (end.kind, end.token, end.status) == ("ended", who, "stopped")
    assert script.events.count("dispatch") == 1
    assert "void" not in script.events


def test_an_ask_for_a_plain_action_shows_no_request_and_no_recipient_line() -> None:
    script = Script()
    run(script, dataclasses.replace(REQUEST, commit="none"))
    ask = script.asks[0]
    assert GOAL not in ask.params.values()
    assert "recipient_unchecked" not in ask.reasons
    assert "commit" not in ask.params


def test_the_ask_shows_the_text_the_commit_and_the_window() -> None:
    script = Script()
    run(script)
    ask = script.asks[0]
    assert (ask.params["text"], ask.params["commit"]) == ("red shoes", "send")
    assert (ask.target_role, ask.target_name, ask.window) == ("Button", "Send", "Shop")
    assert ask.previous_attempt is None


def test_an_unknown_outcome_shows_unknown_as_the_previous_attempt() -> None:
    script = Script(admission="confirm")
    run(script)
    assert script.asks[0].previous_attempt == "unknown"


def test_every_ask_gets_a_new_decision_id() -> None:
    first, second = Script(), Script()
    consents = Consents()
    run(first, consents=consents)
    run(second, consents=consents)
    assert first.asks[0].decision_id != second.asks[0].decision_id


def test_the_dispatched_yes_is_spent() -> None:
    consents = Consents()
    script = Script()
    run(script, consents=consents)
    asked = script.asks[0].decision_id
    assert consents.spend(decision_id=asked, action_key=KEY) is False


class _Unspendable(Consents):
    def spend(self, *, decision_id: str, action_key: str) -> bool:  # noqa: ARG002 - scripted
        return False


def test_a_yes_that_cannot_be_spent_writes_and_sends_nothing() -> None:
    """Fail closed: the pass never writes or dispatches on a yes it could not spend."""
    script = Script()
    with pytest.raises(SteerError):
        run(script, consents=_Unspendable())
    assert "write_ahead" not in script.events
    assert script.dispatched == []


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_a_decision_is_answered_once(answer: str) -> None:
    consents = Consents()
    consents.grant(decision_id="d-1", action_key=KEY, answer=answer)
    assert consents.grant(decision_id="d-1", action_key=KEY, answer="yes") is False


def test_a_yes_is_spent_only_by_its_own_key() -> None:
    consents = Consents()
    assert consents.grant(decision_id="d-1", action_key=KEY, answer="yes:d-1") is True
    assert consents.spend(decision_id="d-1", action_key="k-other") is False
    assert consents.spend(decision_id="d-1", action_key=KEY) is True


def test_a_second_late_key_up_of_the_answer_key_is_input() -> None:
    events = [
        InputEvent(ENTER, down=True, in_bracket=True),
        InputEvent(ENTER, down=False, in_bracket=False),
        InputEvent(ENTER, down=False, in_bracket=False),
    ]
    assert input_after_answer(events) is True


def test_a_key_up_inside_the_bracket_leaves_no_late_key_up_owed() -> None:
    events = [
        InputEvent(ENTER, down=True, in_bracket=True),
        InputEvent(ENTER, down=False, in_bracket=True),
        InputEvent(ENTER, down=False, in_bracket=False),
    ]
    assert input_after_answer(events) is True


def test_a_held_answer_keys_repeat_after_the_bracket_is_input() -> None:
    """Fail closed: an auto-repeat key-down after the bracket is input like any other."""
    events = [
        InputEvent(ENTER, down=True, in_bracket=True),
        InputEvent(ENTER, down=True, in_bracket=False),
    ]
    assert input_after_answer(events) is True


def test_no_events_is_no_input() -> None:
    assert input_after_answer([]) is False


def test_a_type_under_a_send_subgoal_is_not_a_send() -> None:
    """The content checks bind to the action that declares the send, not to every action of
    the subgoal: typing the content has no pending-text record yet."""
    refused = commit_refusal(
        subgoal_commit="send",
        prior=(),
        action_commit="none",
        verb="type",
        raised_by=None,
        params={"text": TEXT},
        pending=None,
    )
    assert refused is None


def test_a_raised_send_checks_its_record() -> None:
    assert (
        commit_refusal(
            subgoal_commit="none",
            prior=(),
            action_commit="send",
            verb="click",
            raised_by="deny",
            params={"text": TEXT},
            pending=dataclasses.replace(TYPED, read_back=None),
        )
        == "composer_unreadable"
    )


def test_conflicting_dispatched_kinds_have_no_effective_commit() -> None:
    """`commit_conflict` refuses the later action, so two kinds never both dispatch."""
    with pytest.raises(ValueError, match="commit_conflict"):
        effective_commit("none", ["send", "submit"])
