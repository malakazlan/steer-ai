"""The `open` step's contract, file form and gate rules (packet open-multiwindow-2026-11-06,
scope item 2; schema gate P1, P2; hunter OW-01/02/33)."""

from __future__ import annotations

import json
import unicodedata
from typing import Any

import pytest

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import Gate, open_tier, params_for
from steerai.orchestrator.ledger import key_params
from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.verify.predicates import WindowExists
from steerai.verify.strength import strength, template_for
from tests.orchestrator.fakes import FakeConfirmer
from tests.orchestrator.test_plan_io import _doc

YT = "drake latest video - YouTube - Google Chrome"


def _open_doc(**target_or_params: Any) -> dict[str, Any]:
    doc = _doc()
    step: dict[str, Any] = {
        "id": "o1",
        "subgoal": "g1",
        "verb": "open",
        "target": {"role": "App", "name": "chrome"},
        "risk": "R1",
        "params": {"site": "youtube_search", "text": "drake latest video"},
    }
    step.update(target_or_params)
    doc["steps"] = [step, *doc["steps"]]
    return doc


def _open(text: str | None = "drake latest video", site: str | None = "youtube_search") -> Step:
    params = {} if site is None else {"site": site}
    if text is not None:
        params["text"] = text
    return Step(
        id="o1",
        subgoal="g1",
        verb="open",
        target=Target(role="App", name="chrome"),
        risk="R1",
        params=params,
    )


def test_an_open_step_round_trips_through_the_file_form() -> None:
    plan = plan_from_json(json.dumps(_open_doc()))
    step = plan.steps[0]
    assert step.verb == "open"
    assert step.target is not None
    assert (step.target.role, step.target.name) == ("App", "chrome")
    assert dict(step.params) == {"site": "youtube_search", "text": "drake latest video"}
    assert json.loads(plan_to_json(plan))["steps"][0]["params"]["site"] == "youtube_search"


@pytest.mark.parametrize(
    "change",
    [
        {"target": {"role": "App", "name": "calculator"}},
        {"target": {"role": "Button", "name": "chrome"}},
        {"params": {"site": "made_up", "text": "x"}},
        {"params": {"site": "youtube_search"}},
        {"params": {"site": "youtube", "text": "extra"}},
        {"params": {"site": "youtube_search", "text": "x", "window_name": "anything"}},
        {"text": "typed text"},
        {"path": "semantic"},
        {"path": "physical"},
    ],
)
def test_the_file_form_refuses_what_the_registries_refuse(change: dict[str, Any]) -> None:
    with pytest.raises(PlanError):
        plan_from_json(json.dumps(_open_doc(**change)))


def test_an_open_is_never_below_r1() -> None:
    step = Step(
        id="o1", subgoal="g1", verb="open", target=Target(role="App", name="notepad"), risk="R0"
    )
    assert step.risk == "R1"


def test_the_template_title_is_filled_by_code_and_is_strong_at_r2() -> None:
    params = params_for(_open())
    assert params.open_title == YT
    template = template_for("open", params)
    assert template == WindowExists(name_pattern=YT)
    assert strength(template, "R2", params).ok is True
    assert strength(WindowExists(name_pattern="Inbox - Gmail"), "R2", params).ok is False


@pytest.mark.parametrize(
    ("goal", "text", "site", "tier"),
    [
        ("search YouTube for drake latest video", "drake latest video", "youtube_search", "R1"),
        ("open youtube", None, "youtube", "R1"),
        ("play drake's newest video", "drake latest video", "youtube_search", "R2"),
        ("search for drake latest video", "drake latest video", "youtube_search", "R2"),
        ("tell Amir code 4821, then open YouTube", "4821", "google_search", "R2"),
        ("google the weather in Lahore", "weather in Lahore", "google_search", "R1"),
        ("open notepad", None, None, "R1"),
        # principal item 1: digits and symbols are content too
        ("find drake's latest video on youtube", "drake 4111111111111111", "youtube_search", "R2"),
        ("search youtube for drake 2026", "drake 2026", "youtube_search", "R1"),
        ("search youtube for drake", "drake & co", "youtube_search", "R2"),
        ("search youtube for drakes latest", "drake's latest", "youtube_search", "R2"),
        ("search youtube for drake's latest", "drake's latest", "youtube_search", "R1"),
    ],
)
def test_the_content_tier_is_computed_from_the_goal(
    goal: str, text: str | None, site: str | None, tier: str
) -> None:
    step = _open(text=text, site=site)
    if site is None:
        step = Step(
            id="o1",
            subgoal="g1",
            verb="open",
            target=Target(role="App", name="notepad"),
            risk="R1",
        )
    assert open_tier(goal, step) == tier


def test_a_confirmed_open_shows_the_app_the_host_and_the_content() -> None:
    confirmer = FakeConfirmer(answers=["no"])
    gate = Gate(confirmer=confirmer, own_pid=4242, task_id="t")
    step = Step(
        id="o1",
        subgoal="g1",
        verb="open",
        target=Target(role="App", name="chrome"),
        risk="R2",
        params={"site": "youtube_search", "text": "drake newest"},
    )
    gate.decide(step, action_key="k", expect=None, target_pid=1, previous_attempt=None)
    ask = confirmer.asked[0]
    assert ask.target_name == "chrome"
    assert ask.params["opens"] == "www.youtube.com"
    assert ask.params["text"] == "drake newest"


# ---- tester gaps (sign-off 2026-11-06) ------------------------------------------------------


@pytest.mark.parametrize(
    ("goal", "text", "tier"),
    [
        (
            "search YouTube for Beyonc\u00e9 live",
            unicodedata.normalize("NFD", "Beyonc\u00e9 live"),
            "R1",
        ),
        ("search \uff39\uff4f\uff55\uff34\uff55\uff42\uff45 for drake", "drake", "R1"),
        ("search YouTube for drake", "drake's", "R2"),
        ("search YouTube for drake", "drake\u200blatest", "R2"),
        ("search YouTube for my code", "4821", "R2"),
        ("search YouTube for drake", "drake 4821", "R2"),
        ("search YouTube for drake", "drake \u0664\u0668\u0662\u0661", "R2"),
        ("search YouTube for drake", "+++", "R2"),
        ("search YouTube for DRAKE!", "drake?", "R2"),
    ],
)
def test_the_content_tier_with_unicode_and_punctuation(goal: str, text: str, tier: str) -> None:
    assert open_tier(goal, _open(text=text)) == tier


def test_key_params_of_a_non_open_step_without_a_window_are_the_pre_open_form() -> None:
    step = Step(
        id="s1",
        subgoal="g1",
        verb="type",
        target=Target(role="Edit", name="M"),
        text="hi",
        risk="R1",
        params={"scope": "Chat"},
    )
    assert key_params(step, None) == {"scope": "Chat", "text": "hi"}
