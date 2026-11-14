"""[V2.13] decision 5: a task's end reason is held as a pair in code and rendered in one place."""

from __future__ import annotations

import pytest

from steerai.loop2.loop import Reason, render_reason


def test_a_plain_reason_renders_as_its_token() -> None:
    assert render_reason(Reason("commit_not_in_step")) == "commit_not_in_step"


def test_a_model_fault_renders_as_after_refusal_and_its_token() -> None:
    reason = Reason("id_not_listed", after_refusal=True)
    assert render_reason(reason) == "after_refusal:id_not_listed"


def test_no_reason_renders_as_none() -> None:
    assert render_reason(None) is None


@pytest.mark.parametrize("token", ["", "Has Caps", "two words", "a:b", "x" * 65])
def test_a_reason_token_is_a_closed_code_token(token: str) -> None:
    with pytest.raises(ValueError, match="reason_token"):
        Reason(token)
