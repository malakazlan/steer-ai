"""A named pair through the loop (H4 dr1/deb1; packet h4-2026-11-11 round 2, items 1-4; hunter
H4-04..13): two same-named text fields that are not copies (distinct automation ids: different
forms) never get a silent pick. A focus, type or choose at R0/R1 asks the person which one; the
choice is kept for the task's later R0/R1 resolves on that name, replaced by a verified type's
pin, cleared with the pins; the act runs on a look taken after the answer. An Enter submit keeps
rule C (the focused twin, never an ask at R2); three copies stay ambiguous."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from steerai.orchestrator.contracts import Step
from steerai.world.elements import Element
from steerai.world.twins import pin_key
from tests.orchestrator.test_twins_loop import FIELD, QUERY, Live, Page, _harness, _submit, _type
from tests.world.test_twins import BOTTOM, TOP

HEADER = replace(TOP, automation_id="nav-search")
BODY = replace(BOTTOM, automation_id="search", offscreen=False, box=(436, 373, 1021, 425))
KEY = pin_key(0x10, "Edit", "Quick search")


def _focus(**changes: Any) -> Step:
    step = Step(id="s1", subgoal="g1", verb="focus", target=FIELD, risk="R0")
    return replace(step, **changes)


def _pair_harness(*, answer: str | None = "1") -> tuple[Any, Page, Live]:
    """The page shows the pair; the person answers the which ask with `answer` (an index) or
    `no`."""
    page = Page(top=HEADER, bottom=BODY)
    h, page, live = _harness(page)

    def on_ask(ask: Any) -> None:
        if ask.kind == "which":
            h.confirmer.answers.append(
                f"no:{ask.decision_id}" if answer is None else f"{answer}:{ask.decision_id}"
            )

    h.confirmer.on_ask = on_ask
    return h, page, live


def _which_asks(h: Any) -> list[Any]:
    return [a for a in h.confirmer.asked if a.kind == "which"]


def test_a_focus_on_a_named_pair_asks_which_and_takes_the_answer() -> None:
    h, _page, live = _pair_harness(answer="2")
    result = h.session.run_step(_focus())
    assert result.ok is True, result.to_dict()
    asks = _which_asks(h)
    assert len(asks) == 1
    assert [c.automation_id for c in asks[0].candidates] == ["nav-search", "search"]
    assert result.evidence["twin"] == "chosen"
    assert result.evidence["candidates"] == 2
    assert result.evidence["chosen_index"] == 2
    assert live.focused == BODY.runtime_id, "the body field, as chosen"


def test_the_choice_carries_to_the_next_steps_without_a_second_ask() -> None:
    h, _page, live = _pair_harness(answer="2")
    assert h.session.run_step(_focus()).ok is True
    typed = h.session.run_step(_type())
    assert typed.ok is True, typed.to_dict()
    assert len(_which_asks(h)) == 1, "one ask per task for that name"
    assert live.values[BODY.runtime_id] == QUERY
    assert h.session._pins[KEY] == BODY.runtime_id, "the verified type pins the chosen twin"


def test_no_answer_means_nothing_is_touched() -> None:
    h, _page, live = _pair_harness(answer=None)
    result = h.session.run_step(_focus())
    assert result.ok is False
    assert result.evidence["detail"] == "none_chosen"
    assert live.focused == ()
    assert h.physical.calls == []


def test_the_ask_runs_on_a_look_taken_after_the_answer() -> None:
    """H4-06/09: the chosen field re-rendered while the person answered: nothing is touched."""
    h, page, live = _pair_harness(answer="2")

    def swap(ask: Any) -> None:
        if ask.kind == "which":
            h.confirmer.answers.append(f"2:{ask.decision_id}")
            page.bottom = replace(BODY, runtime_id=(42, 999))

    h.confirmer.on_ask = swap
    result = h.session.run_step(_focus())
    assert result.ok is False
    assert result.evidence["detail"] == "screen_changed_during_ask"
    assert live.focused == ()


def test_a_third_copy_during_the_ask_stops_too() -> None:
    """H4-08."""
    h, page, live = _pair_harness(answer="1")

    def grow(ask: Any) -> None:
        if ask.kind == "which":
            h.confirmer.answers.append(f"1:{ask.decision_id}")
            page.extra.append(replace(BODY, id=70, runtime_id=(42, 70), automation_id="third"))

    h.confirmer.on_ask = grow
    result = h.session.run_step(_focus())
    assert result.ok is False
    assert result.evidence["detail"] == "screen_changed_during_ask"
    assert live.focused == ()


def test_the_which_ask_brackets_the_monitor_like_the_person_ask() -> None:
    """H4-07: the person's typed answer is never a takeover."""
    h, _page, _live = _pair_harness(answer="1")
    marks: list[str] = []

    class Monitor:
        def mark_ask_open(self) -> None:
            marks.append("open")

        def mark_ask_closed(self) -> None:
            marks.append("closed")

        def kill_requested(self) -> bool:
            return False

        def last_user_input(self) -> float:
            return 0.0

        def healthy(self) -> bool:
            return True

        def detail(self) -> str:
            return ""

        def maintain(self) -> None:
            return None

        def counts(self) -> dict[str, int]:
            return {}

        def close(self) -> None:
            return None

    h.session._monitor = Monitor()
    assert h.session.run_step(_focus()).ok is True
    assert marks[:2] == ["open", "closed"]


def test_three_same_named_fields_stay_ambiguous_without_an_ask() -> None:
    page = Page(top=HEADER, bottom=BODY)
    page.extra.append(replace(BODY, id=70, runtime_id=(42, 70), automation_id="third"))
    h, _page, _live = _harness(page)
    result = h.session.run_step(_focus())
    assert result.ok is False
    assert result.evidence["detail"] == "ambiguous_target"
    assert _which_asks(h) == []


def test_a_secure_field_never_makes_a_pair() -> None:
    page = Page(top=HEADER, bottom=replace(BODY, secure=True))
    h, _page, _live = _harness(page)
    result = h.session.run_step(_focus())
    assert result.ok is False
    assert result.evidence["detail"] == "ambiguous_target"
    assert _which_asks(h) == []


def test_an_enter_submit_keeps_rule_c_and_never_asks() -> None:
    """Item 2: the focused twin that the verified type pinned; a pair with no focus is
    `none_focused`."""
    h, _page, live = _pair_harness(answer="2")
    assert h.session.run_step(_focus()).ok is True
    assert h.session.run_step(_type()).ok is True
    live.focused = BODY.runtime_id
    result = h.session.run_step(_submit())
    assert result.ok is True, result.to_dict()
    assert result.evidence["twin"] == "focused"
    assert len(_which_asks(h)) == 1
    other = _pair_harness(answer="1")[0]
    other.session._live.focused = ()
    blocked = other.session.run_step(_submit())
    assert blocked.ok is False
    assert blocked.evidence["twin"] == "none_focused"
    assert len(_which_asks(other)) == 0


def test_a_new_plan_clears_the_choice_and_asks_once_more() -> None:
    """H4-40: the choice goes with the pins."""
    h, _page, _live = _pair_harness(answer="2")
    assert h.session.run_step(_focus()).ok is True
    h.session._pins.clear()
    h.session._chosen.clear()
    h.confirmer.answers.clear()
    h.confirmer.on_ask = lambda ask: h.confirmer.answers.append(f"1:{ask.decision_id}")
    assert h.session.run_step(_focus(id="s9")).ok is True
    assert len(_which_asks(h)) == 2


def test_the_chosen_field_gone_with_the_pair_standing_is_ambiguous_not_a_second_ask() -> None:
    """Item 3: a chosen rid that vanished while two same-named fields remain."""
    h, page, _live = _pair_harness(answer="2")
    assert h.session.run_step(_focus()).ok is True
    page.bottom = replace(BODY, runtime_id=(42, 999))
    h.clock["t"] += 6.0  # past the reuse window: the type looks afresh and sees the change
    result = h.session.run_step(_type())
    assert result.ok is False
    assert result.evidence["detail"] == "ambiguous_target"
    assert len(_which_asks(h)) == 1


def test_a_twin_set_still_takes_the_one_on_screen_without_an_ask() -> None:
    """Copies ([v2.49]) keep rule B: no ask."""
    page = Page(top=TOP, bottom=BOTTOM)
    h, _page, _live = _harness(page)
    result = h.session.run_step(_focus())
    assert result.ok is True, result.to_dict()
    assert result.evidence["twin"] == "onscreen"
    assert _which_asks(h) == []


@pytest.mark.parametrize("verb", ["click"])
def test_only_text_verbs_ask(verb: str) -> None:
    page = Page(top=HEADER, bottom=BODY)
    h, _page, _live = _harness(page)
    step = Step(id="s1", subgoal="g1", verb=verb, target=FIELD, risk="R0")
    result = h.session.run_step(step)
    assert result.ok is False
    assert result.evidence["detail"] == "ambiguous_target"
    assert _which_asks(h) == []


def test_a_candidate_line_carries_the_automation_id_and_whether_it_is_on_screen() -> None:
    """Item 5: the twin ask's lines carry `automation_id` as a typed field (the harness oracle
    reads it; the product never chooses by it), `offscreen`, and the nearest named container."""
    from steerai.orchestrator.identity import lines_for  # noqa: PLC0415 - this test only

    body = replace(BODY, offscreen=True)
    lines = lines_for([HEADER, body], "Quick search", ["Main navigation", ""])
    assert [line.automation_id for line in lines] == ["nav-search", "search"]
    assert [line.offscreen for line in lines] == [False, True]
    assert "off screen" in lines[1].tags
    assert "in Main navigation" in lines[0].tags
    assert "nav-search" not in repr(lines[0]), "the id stays out of the repr like the name"
    assert isinstance(lines[0], type(lines[1]))
    assert Element is not None


def test_the_post_answer_look_never_outlives_its_step() -> None:
    """A step that stops after the answer (here: the act raises) leaves no look behind: the
    next step resolves on its own fresh look and sees the chosen field gone."""
    h, page, _live = _pair_harness(answer="2")

    def refuse(_executor: Any, action: Any) -> None:
        raise RuntimeError("the act fails after the ask")

    h.semantic.on_execute = refuse
    h.physical.on_execute = refuse
    with pytest.raises(RuntimeError):
        h.session.run_step(_focus())
    page.bottom = replace(BODY, runtime_id=(42, 999))
    h.clock["t"] += 6.0
    h.semantic.on_execute = None
    h.physical.on_execute = None
    result = h.session.run_step(_type())
    assert result.ok is False
    assert result.evidence["detail"] == "ambiguous_target", result.to_dict()
