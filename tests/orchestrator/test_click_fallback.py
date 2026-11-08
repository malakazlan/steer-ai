"""A click on a button with no InvokePattern (#149; principal APPROVE-WITH-CHANGES on D-149): a
semantic click refused `no_InvokePattern` with nothing sent is dispatched once more as the
physical, hit-tested click on the same element, at any tier, only when the target is a plain
acting control with no acting descendant (never a row holding a Delete button: #84/#124).
Measured: GitHub's header `Button "Search or jump to, type / to search"` exposes only
ExpandCollapse and LegacyIAccessible (scout 2026-11-08, Chrome 156)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from steerai.verify.predicates import Appeared
from tests.orchestrator.fakes import CHAT_AMIR, CONVERSATION, SEND, FakeExecutor, not_delivered
from tests.orchestrator.test_bind_asked import FOCUS_FALLBACK, scripted_waits, swap
from tests.orchestrator.test_loop import HELLO, Harness

OPTIONS_ID = 10  # Button "Chat options"
MENU = (60, "Text", "Options menu", (42, 60), CONVERSATION[0])
OPENED = Appeared(role="Text", name_pattern="Options menu")


def _click(**kw: Any) -> Step:
    fields: dict[str, Any] = {
        "id": "c1",
        "subgoal": "g1",
        "verb": "click",
        "target": Target(role="Button", name="Chat options"),
        "risk": "R0",
        "expect": OPENED,
    }
    fields.update(kw)
    return Step(**fields)


def _harness() -> Harness:
    h = Harness()

    def opens(_executor: FakeExecutor, action: Any) -> None:
        if action.verb == "click" and action.element_id == OPTIONS_ID and MENU not in h.world.rows:
            h.world.rows.append(MENU)

    h.physical.on_execute = opens
    return h


def _no_invoke(h: Harness, element_id: int = OPTIONS_ID) -> None:
    h.semantic.outcomes[element_id] = not_delivered(
        ErrorCode.ELEMENT_NOT_ACTIONABLE, "no_InvokePattern"
    )


def test_a_button_without_invoke_is_clicked_physically_once() -> None:
    h = _harness()
    _no_invoke(h)
    result = h.session.run_step(_click())
    assert result.ok is True, result.to_dict()
    assert h.semantic.calls == [("click", OPTIONS_ID, None)]
    assert h.physical.calls == [("click", OPTIONS_ID, None)]
    assert result.evidence["click_fallback"] == "physical"
    assert result.evidence["semantic_detail"] == "no_InvokePattern"


def test_a_button_with_invoke_never_goes_physical() -> None:
    h = Harness()

    def opens(_executor: FakeExecutor, action: Any) -> None:
        if action.verb == "click":
            h.world.rows.append(MENU)

    h.semantic.on_execute = opens
    result = h.session.run_step(_click())
    assert result.ok is True, result.to_dict()
    assert h.physical.calls == []
    assert "click_fallback" not in result.evidence


@pytest.mark.parametrize("detail", ["disabled", "offscreen_unscrollable", "read_only"])
def test_other_refusals_never_fall_back(detail: str) -> None:
    h = _harness()
    h.semantic.outcomes[OPTIONS_ID] = not_delivered(ErrorCode.ELEMENT_NOT_ACTIONABLE, detail)
    result = h.session.run_step(_click())
    assert result.ok is False
    assert h.physical.calls == []
    assert result.evidence["detail"] == detail, "the action's detail reaches the step"


def test_a_row_holding_a_delete_button_stays_refused() -> None:
    """A physical hit test accepts any descendant: the row's Delete would be pressed."""
    h = _harness()
    h.world.rows.append((61, "Button", "Delete chat", (42, 61), CHAT_AMIR[0]))
    _no_invoke(h, CHAT_AMIR[0])
    step = _click(target=Target(role="ListItem", name=CHAT_AMIR[2]))
    result = h.session.run_step(step)
    assert result.ok is False
    assert h.physical.calls == []
    assert result.evidence["detail"] == "no_InvokePattern"


def test_a_button_holding_another_acting_control_stays_refused() -> None:
    """The acting-descendant rule on an allowed role: the hit test could press the inner one."""
    h = _harness()
    h.world.rows.append((62, "Hyperlink", "Remove", (42, 62), OPTIONS_ID))
    _no_invoke(h)
    result = h.session.run_step(_click())
    assert result.ok is False
    assert h.physical.calls == []


@pytest.mark.parametrize("role", ["Group", "Pane", "CheckBox"])
def test_other_roles_stay_refused(role: str) -> None:
    h = _harness()
    h.world.rows = [(r[0], role, *r[2:]) if r[0] == OPTIONS_ID else r for r in h.world.rows]
    _no_invoke(h)
    result = h.session.run_step(_click(target=Target(role=role, name="Chat options")))
    assert result.ok is False
    assert h.physical.calls == []


def test_a_refused_physical_click_reports_both_causes() -> None:
    h = _harness()
    _no_invoke(h)
    h.physical.outcomes[OPTIONS_ID] = not_delivered(ErrorCode.ELEMENT_NOT_ACTIONABLE, "covered")
    result = h.session.run_step(_click())
    assert result.ok is False
    assert len(h.semantic.calls) == 1
    assert len(h.physical.calls) == 1
    assert result.evidence["detail"] == "covered"
    assert result.evidence["semantic_detail"] == "no_InvokePattern"
    assert result.evidence["click_fallback"] == "physical"


def test_an_r3_delete_falls_back_under_one_acting_mark_and_one_key() -> None:
    h = _harness()
    _no_invoke(h, 4)  # Button "Delete chat"

    def deletes(_executor: FakeExecutor, action: Any) -> None:
        if action.element_id == 4:
            h.world.rows = [r for r in h.world.rows if r[0] != 4]

    h.physical.on_execute = deletes
    step = Step(
        id="d1",
        subgoal="g1",
        verb="delete",
        target=Target(role="Button", name="Delete chat"),
        risk="R3",
    )
    key = h.ledger.key_for(step)
    result = h.session.run_step(step)
    assert result.ok is True, result.to_dict()
    assert h.physical.calls == [("click", 4, None)]
    assert list(h.ledger.executions) == [key]
    lines = [json.loads(line) for line in h.trace.lines]
    assert sum(1 for r in lines if r.get("kind") == "step") == 1


# ---- a wait at the fallback's boundary ---------------------------------------------------------

FALLBACK_WAIT = FOCUS_FALLBACK  # the same boundary position: the one before a fallback dispatch


def test_a_label_moved_during_the_fallback_wait_never_clicks_the_other_button() -> None:
    h = _harness()
    _no_invoke(h)
    scripted_waits(h, {FALLBACK_WAIT: swap(h, "Chat options", "Delete chat")})
    result = h.session.run_step(_click())
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "screen_changed_during_wait"
    assert h.physical.calls == []


def test_a_composer_rewritten_during_the_fallback_wait_never_sends() -> None:
    h = _harness()
    h.world.composer_value = HELLO
    _no_invoke(h, SEND[0])

    def rewrite() -> None:
        h.world.composer_value = "something else"

    scripted_waits(h, {FALLBACK_WAIT: rewrite})
    step = Step(
        id="s1",
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        risk="R2",
        params={
            "text": HELLO,
            "composer_role": "Edit",
            "composer_name": "Message",
            "scope": "Conversation",
        },
    )
    result = h.session.run_step(step)
    assert result.ok is False
    assert result.evidence["detail"] == "composer_differs"
    assert h.physical.calls == []
