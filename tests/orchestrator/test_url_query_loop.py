"""A Chrome search submit whose page title is in no registered frame (#148, design D): the
address bar's query parameter is the proof. Eligible exactly where [v2.39]'s rename is, without
the decoration; chrome.exe only; the before URL is read before the Enter and written ahead as a
digest and one bit; evidence `url_query`, never a label. The URL alone is not a load: Chrome
keeps it on its error page after a refused connection, and shows an address-bar navigation's
URL before the server answers (probe #157 `commit`), so a Document named with the typed words
must be there too. Live: Bing and DuckDuckGo stopped `verify_timeout` (`no_match`) on H1
(#152)."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import replace
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import url_query_eligible
from steerai.verify.predicates import Appeared, TitleMatches, Within
from steerai.verify.url_query import UrlBefore
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_edge_cases import FocusLive, _rebuild
from tests.orchestrator.test_loop import HELLO, Harness

GOAL = f"search Bing for {HELLO}"
PARAMS = {"text": HELLO, "composer_role": "Edit", "composer_name": "Message", "scope": "Page"}
REPLACING = Appeared(role="Document", name_pattern=HELLO, replaces=True)
HOME = "www.bing.com/"
RESULTS = f"www.bing.com/search?q={urllib.parse.quote_plus(HELLO)}&form=QBLH"


def _submit(**changes: Any) -> Step:
    step = Step(
        id="s3",
        subgoal="g1",
        verb="submit",
        target=Target(role="Edit", name="Message"),
        risk="R2",
        params=PARAMS,
        expect=REPLACING,
    )
    return replace(step, **changes)


# ---- the pure rule ---------------------------------------------------------------------------


def _eligible(step: Step, **kw: Any) -> bool:
    args: dict[str, Any] = {"risk": "R2", "deny": "R0", "goal": GOAL}
    args.update(kw)
    return url_query_eligible(step, step.expect, **args)


def test_a_floor_only_search_submit_is_eligible_with_or_without_a_decoration() -> None:
    assert _eligible(_submit())
    assert _eligible(_submit(expect=replace(REPLACING, decoration="google_search")))


@pytest.mark.parametrize(
    ("label", "step", "kw"),
    [
        ("a send", _submit(verb="send"), {}),
        ("an R3 step", _submit(risk="R3"), {"risk": "R3"}),
        ("a deny word or #84 id", _submit(), {"deny": "R2"}),
        ("a raised ceiling", _submit(), {"risk": "R3"}),
        ("a shorter pattern", _submit(expect=replace(REPLACING, name_pattern="hello")), {}),
        ("text not in the goal", _submit(), {"goal": "search Bing for something else"}),
        (
            "not the replacing form",
            _submit(expect=Appeared(role="Document", name_pattern=HELLO, within=Within("Page"))),
            {},
        ),
        ("another role", _submit(expect=replace(REPLACING, role="Text", replaces=False)), {}),
    ],
)
def test_om07_nothing_else_is_eligible(label: str, step: Step, kw: Any) -> None:
    assert not _eligible(step, **kw), label


# ---- through the loop ------------------------------------------------------------------------


class Bar:
    """The address bar: `before` until the Enter goes out, then `after`; None is unreadable
    (missing, focused, empty)."""

    def __init__(self, before: str | None, after: str | None) -> None:
        self.before, self.after = before, after
        self.pressed = False
        self.reads: list[int] = []

    def __call__(self, hwnd: int) -> str | None:
        self.reads.append(hwnd)
        return self.after if self.pressed else self.before


LOADED = f"{HELLO} - Search"


def _harness(
    bar: Bar, image: str = "chrome.exe", loaded: str = LOADED, home_title: str = "Bing"
) -> Harness:
    """The page's Document is renamed on the Enter to `loaded`, a title in no registered frame,
    so the expect never matches (`no_match`)."""
    h = Harness()
    h.world.composer_value = HELLO
    _rebuild(h, live=FocusLive(h.world, focused=COMPOSER[3]))
    home = (40, "Document", home_title, (42, 90), 0)
    h.world.rows.append(home)
    h.session._goal = GOAL
    h.session._identify = lambda hwnd: {"hwnd": hwnd, "image": image, "title": "Bing"}
    h.session._address_bar = bar

    def press(_executor: Any, action: Any) -> None:
        if action.verb == "press":
            bar.pressed = True
            h.world.rows[:] = [r for r in h.world.rows if r[0] != home[0]]
            h.world.rows.append((40, "Document", loaded, (42, 90), 0))

    h.physical.on_execute = press
    return h


def test_om01_the_query_in_the_address_bar_verifies_the_submit() -> None:
    """OM-10 rides on this: a page that pushState's the same URL passes too (pinned residual,
    no stronger than [v2.39] against a hostile page)."""
    bar = Bar(HOME, RESULTS)
    h = _harness(bar)
    result = h.session.run_step(_submit())
    assert result.ok is True, result.to_dict()
    assert result.evidence["reason"] == "url_query"
    assert result.label_kind is None, "never a label: the planner's name was not seen"
    assert h.ledger.records["s3"].url_before == UrlBefore.of(HOME, HELLO)


def test_a_document_named_in_another_case_is_still_a_load() -> None:
    h = _harness(Bar(HOME, RESULTS), loaded=f"{HELLO.title()} - Search")
    assert h.session.run_step(_submit()).ok is True


@pytest.mark.parametrize("loaded", ["www.bing.com", "Bing"])
def test_om13_the_url_with_no_page_named_by_the_words_is_no_load(loaded: str) -> None:
    """Chrome's error page after a refused connection carries the URL and is titled by the
    host (probe #157 `commit`: page-refused); an address-bar Enter shows the URL before the
    server answers. Neither is the results page."""
    h = _harness(Bar(HOME, RESULTS), loaded=loaded)
    result = h.session.run_step(_submit())
    assert result.ok is False
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.evidence["detail"] == "no_load"


REFINED_FROM = f"{HELLO} world - Search"
"""The page before an omnibox refinement: its title already holds the new, shorter query."""


def test_om13_a_title_already_there_before_the_submit_is_no_load() -> None:
    """#159 review item 1(i): the pending URL shows at once under the old page's title."""
    bar = Bar("www.bing.com/search?q=hello+there+world", RESULTS)
    h = _harness(bar, loaded=REFINED_FROM, home_title=REFINED_FROM)
    result = h.session.run_step(_submit())
    assert result.ok is False
    assert result.evidence["detail"] == "no_load"


def test_om08_a_resume_under_the_old_title_is_not_present() -> None:
    bar = Bar("www.bing.com/search?q=hello+there+world", RESULTS)
    h = _harness(bar, home_title=REFINED_FROM)
    _crash_after_enter(h)
    h.world.rows[:] = [r for r in h.world.rows if r[0] != 40] + [
        (40, "Document", REFINED_FROM, (42, 90), 0)
    ]
    assert h.session.run_step(_submit()).gate != "done_already"


@pytest.mark.parametrize(
    ("label", "before", "after"),
    [
        ("om02 autocompleted", HOME, RESULTS.replace("&form", "+radio&form")),
        ("om03 the same query twice", RESULTS, RESULTS + "&first=11"),
        ("om11 a POST form", HOME, HOME),
        ("om12 a redirect without the query", HOME, "www.bing.com/ck/a?u=x"),
    ],
)
def test_no_query_proof_stays_unverified(label: str, before: str, after: str) -> None:
    h = _harness(Bar(before, after))
    result = h.session.run_step(_submit())
    assert result.ok is False, label
    assert result.code is ErrorCode.VERIFY_TIMEOUT, label
    assert result.evidence["reason"] == "no_match", label


@pytest.mark.parametrize(("before", "after"), [(HOME, None), (None, RESULTS)])
def test_om05_an_unreadable_address_bar_is_no_url(before: str | None, after: str | None) -> None:
    """Focused (typed text, autocomplete), missing or empty, before or after: no proof."""
    h = _harness(Bar(before, after))
    result = h.session.run_step(_submit())
    assert result.ok is False
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.evidence["detail"] == "no_url"


@pytest.mark.parametrize(
    ("label", "step", "goal"),
    [
        ("a send", _submit(verb="send"), GOAL),
        ("a raised tier", _submit(risk="R3"), GOAL),
        ("a deny word", _submit(target=Target(role="Edit", name="Post")), GOAL),
        ("generated text", _submit(), "search Bing for something else"),
    ],
)
def test_om07_an_ineligible_step_never_reads_the_address_bar(
    label: str, step: Step, goal: str
) -> None:
    bar = Bar(HOME, RESULTS)
    h = _harness(bar)
    h.session._goal = goal
    result = h.session.run_step(step)
    assert result.ok is False, label
    assert bar.reads == [], label


def test_om09_another_app_never_reads_the_address_bar() -> None:
    bar = Bar(HOME, RESULTS)
    h = _harness(bar, image="msedge.exe")
    assert h.session.run_step(_submit()).ok is False
    assert bar.reads == []


def _crash_after_enter(h: Harness) -> list[Any]:
    presses: list[Any] = []
    press = h.physical.on_execute

    def counting(executor: Any, action: Any) -> None:
        if press is not None:
            press(executor, action)
        if action.verb == "press":
            presses.append(action)

    h.physical.on_execute = counting
    assert h.session.run_step(_submit()).ok is True
    key = h.ledger.key_for(_submit())
    h.ledger.executions[key]["outcome"] = None  # "crash" before the outcome was recorded
    h.ledger.executions[key]["phase"] = "acting"
    h.ledger.mark(_submit(), "acting")
    return presses


def test_om08_a_crash_after_enter_resumes_done_without_a_second_enter() -> None:
    h = _harness(Bar(HOME, RESULTS))
    presses = _crash_after_enter(h)
    result = h.session.run_step(_submit())
    assert result.gate == "done_already", result.to_dict()
    assert len(presses) == 1, "never a second Enter"


def test_om08_a_resume_with_no_page_named_by_the_words_is_not_present() -> None:
    h = _harness(Bar(HOME, RESULTS))
    _crash_after_enter(h)
    h.world.rows[:] = [r for r in h.world.rows if r[0] != 40] + [(40, "Document", "x", (42, 90), 0)]
    assert h.session.run_step(_submit()).gate != "done_already"


def test_om08_a_record_with_no_digest_is_not_present() -> None:
    h = _harness(Bar(HOME, RESULTS))
    _crash_after_enter(h)
    h.ledger.records["s3"] = replace(h.ledger.records["s3"], url_before=None)
    assert h.session.run_step(_submit()).gate != "done_already"


# ---- the subgoal: the step's own proof ticks it -----------------------------------------------


def _ticks(h: Harness) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in h.trace.lines]
    return [r for r in records if r.get("kind") == "tick"]


def test_the_url_proof_ticks_its_own_subgoal_and_the_task_is_done() -> None:
    """The fixture of #150 (probe `twins`, 2026-11-08) and every H1 Bing/DDG plan: the
    subgoal's condition is the submit's own expect, which the title never meets exactly."""
    h = _harness(Bar(HOME, RESULTS))
    plan = Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g1", desc="results", condition=REPLACING),),
        steps=(_submit(),),
    )
    result = h.session.run_task(plan)
    assert result.status == "done", result
    assert [t["by"] for t in _ticks(h)] == ["url_query"]


@pytest.mark.parametrize(
    ("label", "condition", "loaded"),
    [
        (
            "another condition",
            Appeared(role="Document", name_pattern="other", replaces=True),
            LOADED,
        ),
        ("no load", REPLACING, "Bing"),
    ],
)
def test_the_url_proof_ticks_nothing_else(label: str, condition: Any, loaded: str) -> None:
    h = _harness(Bar(HOME, RESULTS), loaded=loaded)
    plan = Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g1", desc="results", condition=condition),),
        steps=(_submit(),),
    )
    result = h.session.run_task(plan)
    assert result.status != "done", label
    assert all(t["by"] != "url_query" for t in _ticks(h)), label


# ---- the Document's own URL (5.10 [v2.51]; probe document_url) --------------------------------

UNTITLED = "Search results | Bing"
"""A results title without the query, as MDN ("Search | MDN") and docs.python.org measured."""


def _with_document_url(bar: Bar, after: str | None, **kw: Any) -> Harness:
    h = _harness(bar, loaded=UNTITLED, **kw)
    h.session._document_url = lambda _hwnd: after if bar.pressed else "https://www.bing.com/"
    return h


def test_a_title_without_the_query_loads_by_the_document_url() -> None:
    bar = Bar(HOME, RESULTS)
    h = _with_document_url(bar, "https://" + RESULTS)
    result = h.session.run_step(_submit())
    assert result.ok is True, result.to_dict()
    assert result.evidence["reason"] == "url_query"


@pytest.mark.parametrize(
    ("label", "after"),
    [
        ("an error or certificate page", "chrome-error://chromewebdata/"),
        ("unreadable", None),
        ("another query", "https://www.bing.com/search?q=other"),
    ],
)
def test_no_document_url_proof_is_no_load(label: str, after: str | None) -> None:
    h = _with_document_url(Bar(HOME, RESULTS), after)
    result = h.session.run_step(_submit())
    assert result.ok is False, label
    assert result.evidence["detail"] == "no_load", label


def test_a_resume_loads_by_the_document_url_too() -> None:
    bar = Bar(HOME, RESULTS)
    h = _with_document_url(bar, "https://" + RESULTS)
    presses = _crash_after_enter(h)
    result = h.session.run_step(_submit())
    assert result.gate == "done_already", result.to_dict()
    assert len(presses) == 1


# ---- a title condition naming the typed words (H3 pypi1, #198) --------------------------------


def _title_plan(condition: Any, *extra: Any) -> Plan:
    subgoals = (Subgoal(id="g1", desc="results", condition=condition), *extra)
    return Plan(goal=GOAL, subgoals=subgoals, steps=(_submit(),))


def test_the_url_proof_ticks_a_title_condition_naming_the_typed_words() -> None:
    """H3 pypi1: the submit was proven by the address bar, but the subgoal's condition was
    `title <words>` on a results page titled without them, so the run ended unfinished."""
    bar = Bar(HOME, RESULTS)
    h = _with_document_url(bar, "https://" + RESULTS)
    result = h.session.run_task(_title_plan(TitleMatches(pattern=HELLO)))
    assert result.status == "done", result
    assert [t["by"] for t in _ticks(h)] == ["url_query_title"]


def test_a_title_condition_with_other_words_never_ticks_by_the_url_proof() -> None:
    bar = Bar(HOME, RESULTS)
    h = _with_document_url(bar, "https://" + RESULTS)
    result = h.session.run_task(_title_plan(TitleMatches(pattern="something else")))
    assert result.status != "done", result
    assert _ticks(h) == []


def test_the_url_proof_never_ticks_another_subgoals_title() -> None:
    """Only the submit's own subgoal: a later subgoal's `title <words>` is not proven by it."""
    bar = Bar(HOME, RESULTS)
    h = _with_document_url(bar, "https://" + RESULTS)
    later = Subgoal(id="g2", desc="still open", condition=TitleMatches(pattern=HELLO))
    other = Step(
        id="s4",
        subgoal="g2",
        verb="click",
        target=Target(role="Button", name="Chat options"),
        risk="R0",
        expect=Appeared(role="Text", name_pattern="never shown"),
    )
    plan = Plan(
        goal=GOAL, subgoals=(_title_plan(REPLACING).subgoals[0], later), steps=(_submit(), other)
    )
    result = h.session.run_task(plan)
    assert result.status != "done", result
    assert [t["by"] for t in _ticks(h)] == ["url_query"]

# ---- a submit on the form's one submit Button (#199; 5.10 [v2.64] npm1) ----------------------

SEARCH_BUTTON = (41, "Button", "Search", (42, 91), 7)
"""npmjs.com: while the suggestion dropdown is open the form's first submit button is an
invisible backdrop, so Enter only closes the dropdown; Invoke on this Button submits (3/3)."""


class LoggedBar(Bar):
    """The address bar, with each read in a log the click shares: the before URL must be
    read before the click goes out, as it is before an Enter."""

    def __init__(self, before: str | None, after: str | None, log: list[str]) -> None:
        super().__init__(before, after)
        self.log = log

    def __call__(self, hwnd: int) -> str | None:
        self.log.append("read")
        return super().__call__(hwnd)


def _button_submit() -> Step:
    return _submit(target=Target(role="Button", name="Search"))


def _button_harness(bar: Bar, log: list[str]) -> Harness:
    """The Button's click, never an Enter, loads the results page."""
    h = _harness(bar)
    h.world.rows.append(SEARCH_BUTTON)

    def click(_executor: Any, action: Any) -> None:
        if action.verb == "click":
            log.append("click")
            bar.pressed = True
            h.world.rows[:] = [r for r in h.world.rows if r[0] != 40]
            h.world.rows.append((40, "Document", LOADED, (42, 90), 0))

    h.semantic.on_execute = click
    return h


def test_npm1_a_submit_on_the_search_button_is_verified_by_the_url() -> None:
    """The same proof as an Enter submit, from the same params: the composer holds the text,
    the before URL is read ahead of the click, the Button is invoked, and the address bar's
    query verifies the step with no label."""
    log: list[str] = []
    bar = LoggedBar(HOME, RESULTS, log)
    h = _button_harness(bar, log)
    result = h.session.run_step(_button_submit())
    assert result.ok is True, result.to_dict()
    assert result.evidence["reason"] == "url_query"
    assert result.label_kind is None, "never a label: the planner's name was not seen"
    assert result.evidence.get("submit") != "enter"
    assert h.semantic.calls == [("click", SEARCH_BUTTON[0], None)]
    assert h.physical.calls == [], "a Button submit presses nothing"
    assert log == ["read", "click", "read"], "the before URL is read before the click"
    assert h.ledger.records["s3"].url_before == UrlBefore.of(HOME, HELLO)


def test_npm1_a_button_submit_whose_composer_differs_never_clicks() -> None:
    """OR-31 / SE-02 through a Button: the click submits whatever the field shows, so a field
    that does not hold the approved text blocks the step before anything goes out. The one
    address-bar read is the write-ahead record's (`_record` runs before the act, as for an
    Enter); the proof itself is the last check before the dispatch."""
    log: list[str] = []
    bar = LoggedBar(HOME, RESULTS, log)
    h = _button_harness(bar, log)
    h.world.composer_value = "something else entirely"
    result = h.session.run_step(_button_submit())
    assert result.ok is False
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "composer_differs"
    assert h.semantic.calls == []
    assert h.physical.calls == []
    assert bar.reads == [h.session.hwnd], "the record's read only, never a second one"
    assert "something else" not in json.dumps(result.to_dict())
