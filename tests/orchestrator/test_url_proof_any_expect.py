"""The address bar proves an Enter search submit whatever effect the planner guessed (5.10
[v2.79], packet url-proof-any-expect-2026-11-12; gallery g23 0/3: a GET form on a settings-like
page, planned with an in-page result as its expect, navigated to the results page and stopped
`unverified_effect`). `url_proof_eligible` widens only the URL read: an Enter on an Edit or a
ComboBox composer, at exactly R2 from its floor, whose text the goal holds verbatim, with a lone
`appeared` naming exactly that text; it stands in for a deadline miss only, and needs a page
named by the words (hunter UA-01/04/05). The rename
rule and the no-effect scope stay on `url_query_eligible` (principal item 1). A non-navigation
expect polls the address bar to the navigation deadline (item 3); the planner's miss stays in
the trace as `missed` (item 4); the step's own subgoal ticks on its exact expect (item 5)."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import rename_counts, url_proof_eligible, url_query_eligible
from steerai.orchestrator.loop import NAVIGATION_VERIFY_S, URL_POLL_S
from steerai.verify.predicates import All, Appeared, Disappeared, Within
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_loop import HELLO, Harness
from tests.orchestrator.test_run_monitor import FakeMonitor
from tests.orchestrator.test_url_query_loop import (
    GOAL,
    HOME,
    LOADED,
    REPLACING,
    RESULTS,
    SEARCH_BUTTON,
    Bar,
    _button_harness,
    _harness,
    _submit,
)

IN_PAGE = Appeared(role="Text", name_pattern=HELLO, within=Within("Conversation"))
"""g23's planned expect: the results shown in the page the form sits on."""
PLACED = Appeared(role="Text", name_pattern="Order placed")


def _guessed(**changes: Any) -> Step:
    return _submit(expect=IN_PAGE, **changes)


def _proof(step: Step, role: str | None = "Edit", **kw: Any) -> bool:
    args: dict[str, Any] = {"risk": "R2", "deny": "R0", "goal": GOAL}
    args.update(kw)
    return url_proof_eligible(step, step.expect, role=role, **args)


# ---- the pure rule ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["Edit", "ComboBox"])
def test_an_enter_on_a_field_composer_is_proof_eligible_whatever_its_expect(role: str) -> None:
    assert _proof(_guessed(), role=role)
    assert _proof(_submit(expect=replace(REPLACING, replaces=False)), role=role)


def test_every_url_query_eligible_step_stays_proof_eligible_whatever_its_target() -> None:
    """A Button submit with the replacing-Document expect is npm1's path (#199)."""
    assert _proof(_submit(), role="Button")


@pytest.mark.parametrize(
    ("label", "step", "role", "kw"),
    [
        ("a Button submit", _guessed(), "Button", {}),
        ("a Document composer", _guessed(), "Document", {}),
        ("no resolved element", _guessed(), None, {}),
        ("a send", _guessed(verb="send"), "Edit", {}),
        ("an R3 plan tier", _guessed(risk="R3"), "Edit", {"risk": "R3"}),
        ("a raised ceiling", _guessed(), "Edit", {"risk": "R3"}),
        ("a deny word or id", _guessed(), "Edit", {"deny": "R2"}),
        ("an R1 effective tier", _guessed(), "Edit", {"risk": "R1"}),
        ("text not in the goal", _guessed(), "Edit", {"goal": "search Bing for other words"}),
        ("no text", _guessed(params={}), "Edit", {}),
        ("no expect", _submit(expect=None), "Edit", {}),
        ("other words", _submit(expect=replace(IN_PAGE, name_pattern="hello")), "Edit", {}),
        ("a composite (UA-01)", _submit(expect=All(parts=(IN_PAGE, PLACED))), "Edit", {}),
        ("a disappeared (UA-16)", _submit(expect=Disappeared("Edit", "Message")), "Edit", {}),
    ],
)
def test_nothing_else_is_proof_eligible(
    label: str, step: Step, role: str | None, kw: dict[str, Any]
) -> None:
    assert not _proof(step, role=role, **kw), label


def test_the_rename_rule_keeps_the_narrow_shape() -> None:
    """Principal item 1: a decoration is legal on any role, so a Text renamed in place must
    never prove an R2 step ([v1.2]/[v2.39])."""
    decorated = _submit(expect=replace(IN_PAGE, decoration="google_search"))
    assert not url_query_eligible(decorated, decorated.expect, risk="R2", deny="R0", goal=GOAL)
    assert not rename_counts(decorated, decorated.expect, risk="R2", deny="R0", goal=GOAL)


# ---- through the loop ------------------------------------------------------------------------


def test_g23_an_in_page_expect_on_a_page_that_navigates_is_proven_by_the_url() -> None:
    bar = Bar(HOME, RESULTS)
    h = _harness(bar)
    result = h.session.run_step(_guessed())
    assert result.ok is True, result.to_dict()
    assert result.evidence["reason"] == "url_query"
    assert result.evidence["missed"] == "no_match", "the planner's guess stays in the trace"
    assert result.label_kind is None, "never a label: the planner's name was not seen"


def test_an_in_page_expect_that_holds_needs_no_address_bar_proof() -> None:
    """The verify met the planner's expect: no URL proof, no `missed`."""
    bar = Bar(HOME, HOME)
    h = _harness(bar)

    def shown(_executor: Any, action: Any) -> None:
        if action.verb == "press":
            bar.pressed = True
            h.world.rows.append((60, "Text", HELLO, (42, 60), 5))

    h.physical.on_execute = shown
    result = h.session.run_step(_guessed())
    assert result.ok is True, result.to_dict()
    assert result.evidence.get("reason") != "url_query"
    assert "missed" not in result.evidence


class SlowBar(Bar):
    """The results page lands `delay` seconds after the Enter on the harness clock: before
    that the bar and the page are the home page's."""

    def __init__(self, h: Harness, delay: float) -> None:
        super().__init__(HOME, RESULTS)
        self.h, self.delay = h, delay
        self.at: float | None = None

    def __call__(self, hwnd: int) -> str | None:
        self.reads.append(hwnd)
        if not self.pressed or self.at is None:
            return self.before
        if self.h.clock["t"] < self.at + self.delay:
            return self.before
        rows = self.h.world.rows
        if not any(r[0] == 40 and r[2] == LOADED for r in rows):
            rows[:] = [r for r in rows if r[0] != 40] + [(40, "Document", LOADED, (42, 91), 0)]
        return self.after


def _slow(delay: float) -> tuple[Harness, SlowBar]:
    h = _harness(Bar(HOME, HOME))
    bar = SlowBar(h, delay)
    h.session._address_bar = bar

    def press(_executor: Any, action: Any) -> None:
        if action.verb == "press":
            bar.pressed, bar.at = True, h.clock["t"]

    h.physical.on_execute = press
    return h, bar


def test_a_page_slower_than_the_in_page_deadline_is_caught_by_polling() -> None:
    """Principal item 3: an in-page expect waits the step deadline, not the navigation one;
    the proof keeps polling the address bar and the page up to `NAVIGATION_VERIFY_S`."""
    h, bar = _slow(delay=2.0)
    result = h.session.run_step(_guessed())
    assert result.ok is True, result.to_dict()
    assert result.evidence["reason"] == "url_query"
    assert len(bar.reads) >= 3, "the before read and more than one after read"


def test_a_page_slower_than_the_navigation_deadline_stays_unverified() -> None:
    h, _bar = _slow(delay=NAVIGATION_VERIFY_S + 1.0)
    result = h.session.run_step(_guessed())
    assert result.ok is False
    assert result.code is ErrorCode.VERIFY_TIMEOUT


def test_the_poll_ends_even_on_a_clock_that_never_moves() -> None:
    """A bound on reads, not only on time: a clock that stops once the verify is over (the
    first read after the Enter) never spins the poll forever."""
    h, bar = _slow(delay=NAVIGATION_VERIFY_S + 1.0)
    read = bar.__call__

    def freezing(hwnd: int) -> str | None:
        if bar.pressed:
            h.session._monotonic = lambda: 0.0
            h.session._sleep = lambda _s: None
        return read(hwnd)

    h.session._address_bar = freezing
    result = h.session.run_step(_guessed())
    assert result.ok is False
    assert len(bar.reads) <= NAVIGATION_VERIFY_S / URL_POLL_S + 2


def test_a_button_submit_with_an_in_page_expect_never_reads_the_address_bar() -> None:
    log: list[str] = []
    bar = Bar(HOME, RESULTS)
    h = _button_harness(bar, log)
    result = h.session.run_step(_guessed(target=Target(role="Button", name="Search")))
    assert result.ok is False
    assert bar.reads == []
    assert h.semantic.calls == [("click", SEARCH_BUTTON[0], None)]


def test_a_document_composer_with_an_in_page_expect_never_reads_the_address_bar() -> None:
    """Principal item 2: the resolved element's role, as `_act` reads it, not the target's."""
    bar = Bar(HOME, RESULTS)
    h = _harness(bar)
    h.world.rows[:] = [
        (r[0], "Document", r[2], r[3], r[4]) if r[0] == COMPOSER[0] else r for r in h.world.rows
    ]
    result = h.session.run_step(_guessed(target=Target(role="Document", name="Message")))
    assert result.ok is False
    assert bar.reads == []


def test_a_send_with_an_in_page_expect_never_reads_the_address_bar() -> None:
    bar = Bar(HOME, RESULTS)
    h = _harness(bar)
    result = h.session.run_step(_guessed(verb="send"))
    assert result.ok is False
    assert bar.reads == []


def test_a_widened_step_that_changed_nothing_is_never_no_effect() -> None:
    """Question 3: the no-effect scope stays on `url_query_eligible`; an in-place page
    update would read as nothing happened under fact 4."""
    h = _harness(Bar(HOME, HOME))
    h.physical.on_execute = lambda _e, _a: None
    result = h.session.run_step(_guessed())
    assert result.ok is False
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.evidence.get("reason") != "no_effect"
    assert result.evidence.get("detail") != "no_effect"


# ---- the subgoal and the resume -----------------------------------------------------------------


def _ticks(h: Harness) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in h.trace.lines]
    return [r for r in records if r.get("kind") == "tick"]


def test_g23_the_step_s_own_subgoal_ticks_on_its_exact_expect_through_the_gate() -> None:
    """Principal item 5: g23's plan through the real gate path; staying open would replan a
    second R2 Enter."""
    h = _harness(Bar(HOME, RESULTS))
    plan = Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g1", desc="results", condition=IN_PAGE),),
        steps=(_guessed(),),
    )
    result = h.session.run_task(plan)
    assert result.status == "done", result
    assert [t["by"] for t in _ticks(h)] == ["url_query"]


def test_a_later_subgoal_naming_the_same_words_never_ticks_by_the_proof() -> None:
    h = _harness(Bar(HOME, RESULTS))
    later = Subgoal(id="g2", desc="still open", condition=IN_PAGE)
    other = Step(
        id="s4",
        subgoal="g2",
        verb="click",
        target=Target(role="Button", name="Chat options"),
        risk="R0",
        expect=Appeared(role="Text", name_pattern="never shown"),
    )
    plan = Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g1", desc="results", condition=REPLACING), later),
        steps=(_submit(), other),
    )
    result = h.session.run_task(plan)
    assert result.status != "done", result
    assert [t["by"] for t in _ticks(h)] == ["url_query"]


def test_a_crash_after_the_enter_resumes_done_without_a_second_enter() -> None:
    h = _harness(Bar(HOME, RESULTS))
    presses: list[Any] = []
    press = h.physical.on_execute

    def counting(executor: Any, action: Any) -> None:
        if press is not None:
            press(executor, action)
        if action.verb == "press":
            presses.append(action)

    h.physical.on_execute = counting
    assert h.session.run_step(_guessed()).ok is True
    key = h.ledger.key_for(_guessed())
    h.ledger.executions[key]["outcome"] = None
    h.ledger.executions[key]["phase"] = "acting"
    h.ledger.mark(_guessed(), "acting")
    result = h.session.run_step(_guessed())
    assert result.gate == "done_already", result.to_dict()
    assert len(presses) == 1, "never a second Enter"


def test_a_positive_contradiction_is_never_overridden_by_the_url() -> None:
    """Hunter UA-04: two new rows naming the words is a duplicate, not a miss; the address
    bar carrying them proves nothing about which one is the effect."""
    bar = Bar(HOME, RESULTS)
    h = _harness(bar)
    press = h.physical.on_execute

    def twice(executor: Any, action: Any) -> None:
        if press is not None:
            press(executor, action)
        if action.verb == "press":
            h.world.rows.append((60, "Text", HELLO, (42, 60), 5))
            h.world.rows.append((61, "Text", HELLO, (42, 61), 5))

    h.physical.on_execute = twice
    result = h.session.run_step(_guessed())
    assert result.ok is False
    assert result.evidence.get("reason") != "url_query"


def test_a_page_not_named_by_the_words_is_no_proof_for_an_in_page_expect() -> None:
    """Hunter UA-05: "pay 100" through a GET form lands on `/pay/review?amount=100`, a page
    whose URL carries the value and whose title does not; the replacing-Document expect keeps
    its URL-bound loads ([v2.51]/[v2.73]), the widened one needs the words in the title."""
    from steerai.world.address_bar import DocumentUrl  # noqa: PLC0415 - the read's shape

    bar = Bar(HOME, RESULTS)
    h = _harness(bar, loaded="Search results | Bing")
    h.session._document_url = lambda _hwnd: DocumentUrl(
        runtime_id=(42, 90), url="https://" + (RESULTS if bar.pressed else HOME)
    )
    result = h.session.run_step(_guessed())
    assert result.ok is False
    assert result.evidence["detail"] == "no_named_load"
    assert h.session.run_step(_submit()).evidence.get("load") == "name_url"


def test_the_missed_token_is_closed_and_names_nothing_from_the_page() -> None:
    """Hunter UA-13: only the verify's miss reason, never the pattern or the landmark."""
    h = _harness(Bar(HOME, RESULTS))
    result = h.session.run_step(_guessed())
    assert result.ok is True
    assert result.evidence["missed"] in {"no_match", "not_new", "not_replaced"}
    assert "Conversation" not in json.dumps(result.evidence)


@dataclass
class KillSwitch(FakeMonitor):
    """The run's monitor, with a kill the test throws."""

    kill: bool = False

    def kill_requested(self) -> bool:
        return self.kill


def _crashed(h: Harness, step: Step) -> None:
    """The attempt left open in the acting phase, as a crash after the Enter leaves it."""
    key = h.ledger.key_for(step)
    h.ledger.executions[key]["outcome"] = None
    h.ledger.executions[key]["phase"] = "acting"
    h.ledger.mark(step, "acting")


def _untitled(bar: Bar) -> Harness:
    """The results page loads titled without the words; its own URL carries them."""
    from steerai.world.address_bar import DocumentUrl  # noqa: PLC0415 - the read's shape

    h = _harness(bar, loaded="Search results | Bing")
    h.session._document_url = lambda _hwnd: DocumentUrl(
        runtime_id=(42, 90), url="https://" + (RESULTS if bar.pressed else HOME)
    )
    return h


def test_a_widened_resume_needs_a_page_named_by_the_words_too() -> None:
    """Principal re-review item 2: the resume guard applies `narrow or load == name`; a
    widened step whose page loads only by its URL is not done on a resume, while the
    replacing-Document step on the same page is."""
    h = _untitled(Bar(HOME, RESULTS))
    assert h.session.run_step(_guessed()).ok is False
    _crashed(h, _guessed())
    assert h.session.run_step(_guessed()).gate != "done_already"

    h = _untitled(Bar(HOME, RESULTS))
    assert h.session.run_step(_submit()).ok is True
    _crashed(h, _submit())
    assert h.session.run_step(_submit()).gate == "done_already"


def test_a_kill_during_the_poll_keeps_the_record_open_and_never_sends_twice() -> None:
    """Principal re-review item 3: the Enter went out before the poll, so a kill there
    must never release the record as `not_delivered` (that would allow a second R2
    dispatch); the resume finds the page loaded and sends nothing."""
    h, bar = _slow(delay=4.0)
    switch = KillSwitch()
    h.session._monitor = switch
    read = bar.__call__
    presses: list[Any] = []
    press = h.physical.on_execute

    def counting(executor: Any, action: Any) -> None:
        if press is not None:
            press(executor, action)
        if action.verb == "press":
            presses.append(action)

    def killing(hwnd: int) -> str | None:
        value = read(hwnd)
        if bar.pressed:
            switch.kill = True  # the person stops Steer while the proof polls
        return value

    h.physical.on_execute = counting
    h.session._address_bar = killing
    killed = h.session.run_step(_guessed())
    assert killed.ok is False
    assert killed.evidence.get("detail") == "killed", killed.to_dict()
    assert h.ledger.executions[h.ledger.key_for(_guessed())]["outcome"] != "not_delivered"
    switch.kill = False
    h.clock["t"] += 10.0  # the page lands while Steer is stopped
    rows = h.world.rows
    rows[:] = [r for r in rows if r[0] != 40] + [(40, "Document", LOADED, (42, 91), 0)]
    resumed = h.session.run_step(_guessed())
    assert resumed.gate == "done_already", resumed.to_dict()
    assert len(presses) == 1, "never a second Enter"
