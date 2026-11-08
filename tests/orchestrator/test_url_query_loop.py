"""A Chrome search submit whose page title is in no registered frame (#148, design D): the
address bar's query parameter is the proof. Eligible exactly where [v2.39]'s rename is, without
the decoration; chrome.exe only; the before URL is read before the Enter and written ahead as a
digest and one bit; evidence `url_query`, never a label. The URL alone is not a load: Chrome
keeps it on its error page after a refused connection, and shows an address-bar navigation's
URL before the server answers (probe #157 `commit`), so a Document named with the typed words
must be there too. Live: Bing and DuckDuckGo stopped `verify_timeout` (`no_match`) on H1
(#152)."""

from __future__ import annotations

import urllib.parse
from dataclasses import replace
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import url_query_eligible
from steerai.verify.predicates import Appeared, Within
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
