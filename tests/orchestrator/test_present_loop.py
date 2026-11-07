"""`present` through the loop (#124, 5.10 [v2.36]): the selection click always runs, a chat
already open verifies on the first poll, another open chat verifies only once the click
switched it, a click that switches nothing fails, nothing ticks at start, and a tier raised
to R2 blocks (hunter PS-01/06/10/19/25)."""

from __future__ import annotations

import json
from typing import Any

from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.verify.predicates import Appeared, Present, Within
from tests.orchestrator.fakes import CHATS, CONVERSATION, HEADER, WINDOW, Row
from tests.orchestrator.test_loop import Harness, _send, _type

ROW: Row = (11, "ListItem", "Amir Khan", (42, 60), CHATS[0])
ROW_TEXT: Row = (12, "Text", "Amir Khan", (42, 61), ROW[0])
GOAL = "open the chat with Amir Khan"
DOC: Row = (20, "Document", "Steer probe inbox", (42, 2), WINDOW[0])
PRESENT = Present(
    role="Text", name_pattern="Amir Khan", within=Within(landmark="Steer probe inbox")
)


def _title(name: str) -> Row:
    return (13, "Text", name, (42, 62), HEADER[0])


def _click(risk: str = "R1") -> Step:
    return Step(
        id="s1",
        subgoal="g1",
        verb="click",
        target=Target(role="ListItem", name="Amir Khan"),
        risk=risk,  # type: ignore[arg-type]
        expect=PRESENT,
    )


def _plan(risk: str = "R1") -> Plan:
    return Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g1", desc="the chat is open", condition=PRESENT),),
        steps=(_click(risk),),
    )


def _reply_plan() -> Plan:
    """g1 the chat, g2 the reply: the send's scope is the content landmark."""
    sent = Appeared(role="Text", name_pattern="hello there", within=Within(landmark="Conversation"))
    return Plan(
        goal="send hello there to Amir Khan",
        subgoals=(
            Subgoal(id="g1", desc="the chat is open", condition=PRESENT),
            Subgoal(id="g2", desc="sent", condition=sent),
        ),
        steps=(_click(), *(_in_g2(s) for s in (_type(), _send()))),
    )


def _in_g2(step: Step) -> Step:
    from dataclasses import replace  # noqa: PLC0415 - this helper only

    return replace(step, subgoal="g2")


def _harness(open_chat: str) -> Harness:
    h = Harness()
    rows = [(r[0], r[1], r[2], r[3], DOC[0] if r[4] == WINDOW[0] else r[4]) for r in h.world.rows]
    h.world.rows = [*rows, DOC, ROW, ROW_TEXT, _title(open_chat)]
    return h


def _switches(h: Harness) -> None:
    def switch(_executor: Any, action: Any) -> None:
        if action.verb == "click" and action.element_id == ROW[0]:
            h.world.rows = [r for r in h.world.rows if r[0] != 13] + [_title("Amir Khan")]

    h.semantic.on_execute = switch


def _ticks(h: Harness) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in h.trace.lines]
    return [r for r in records if r.get("kind") == "tick"]


def test_a_chat_already_open_still_clicks_and_verifies() -> None:
    h = _harness("Amir Khan")
    result = h.session.run_task(_plan())
    assert result.status == "done", [r.to_dict() for r in result.results]
    assert [c[:2] for c in h.semantic.calls] == [("click", ROW[0])], "the click always runs"
    assert result.results[0].label_kind == "t1"
    assert [t["by"] for t in _ticks(h)] == ["condition"], "never a start tick"


def test_another_open_chat_verifies_once_the_click_switched_it() -> None:
    h = _harness("Bob")
    _switches(h)
    result = h.session.run_task(_plan())
    assert result.status == "done", [r.to_dict() for r in result.results]
    assert [c[:2] for c in h.semantic.calls] == [("click", ROW[0])]


def test_a_click_that_switches_nothing_fails() -> None:
    """The row's own name Text is in the chat list: the bound target's container never counts,
    so Bob's chat staying open is no proof (PS-02)."""
    h = _harness("Bob")
    result = h.session.run_task(_plan())
    assert result.status == "stopped"
    assert result.results[0].ok is False
    assert _ticks(h) == []


def test_the_row_text_alone_never_proves_the_chat() -> None:
    h = _harness("Bob")
    h.world.rows = [r for r in h.world.rows if r[0] != 13]
    result = h.session.run_task(_plan())
    assert result.status == "stopped"


def test_a_present_expect_above_r1_is_blocked_before_any_input() -> None:
    h = _harness("Amir Khan")
    result = h.session.run_task(_plan(risk="R2"))
    assert result.results[0].gate == "block"
    assert result.results[0].evidence["detail"] == "weak_predicate"
    assert h.semantic.calls == []


def test_a_continuation_runs_the_click_too() -> None:
    h = _harness("Amir Khan")
    result = h.session.run_task(_plan(), continuation=True)
    assert result.status == "done"
    assert len(h.semantic.calls) == 1


def test_the_trace_never_holds_the_name() -> None:
    h = _harness("Amir Khan")
    h.session.run_task(_plan())
    assert "Amir Khan" not in "\n".join(h.trace.lines)


def test_an_exact_name_bubble_in_bob_s_chat_never_lets_the_reply_through() -> None:
    """The click is dropped, Bob's chat stays open and shows a bubble reading exactly "Amir
    Khan": the send's scope is excluded, so the click fails and nothing is sent (PS-09)."""
    h = _harness("Bob")
    h.world.rows.append((21, "Text", "Amir Khan", (42, 63), CONVERSATION[0]))
    result = h.session.run_task(_reply_plan())
    assert result.results[0].ok is False
    assert h.world.sent == []


def test_a_reply_in_the_chat_already_open_is_done() -> None:
    h = _harness("Amir Khan")
    result = h.session.run_task(_reply_plan())
    assert result.status == "done", [r.to_dict() for r in result.results]
    assert h.world.sent == ["hello there"]


def test_a_click_raised_to_r2_after_a_present_with_no_content_landmark_stops() -> None:
    """A deny word raises an unscoped click to R2 at run time: nothing would be excluded from
    the present, so it never starts (#124 re-review, blocking 1)."""
    from dataclasses import replace  # noqa: PLC0415 - this test only

    from steerai.verify.predicates import TitleMatches  # noqa: PLC0415

    h = _harness("Amir Khan")
    h.world.rows.append((22, "Button", "Delete chat now", (42, 64), HEADER[0]))
    raised = replace(
        _click(),
        id="s2",
        subgoal="g2",
        target=Target(role="Button", name="Delete chat now"),
        expect=TitleMatches(pattern="Chat deleted"),
    )
    plan = Plan(
        goal=GOAL,
        subgoals=(
            Subgoal(id="g1", desc="open", condition=PRESENT),
            Subgoal(id="g2", desc="gone", condition=TitleMatches(pattern="Chat deleted")),
        ),
        steps=(_click(), raised),
    )
    result = h.session.run_task(plan)
    assert result.status == "stopped"
    assert result.evidence["detail"] == "content_unscoped"
    assert [c[1] for c in h.semantic.calls] == [ROW[0]], "the raised step never runs"
