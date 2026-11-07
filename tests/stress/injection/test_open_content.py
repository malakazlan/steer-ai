"""`open` content from a page (IJ-17, IJ-18, IJ-28 pinned): content that is not
the goal's own words leaves the machine only at R2 with the host shown in the ask, even from a
continuation; injected content can never become a flag, a quote or another scheme on Chrome's
command line, and over-long or multi-line content is refused at the plan boundary."""

from __future__ import annotations

import json
import urllib.parse

import pytest

from steerai.action.launch import MAX_CONTENT_CHARS, LaunchError, launch_argv
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import open_tier
from steerai.orchestrator.plan_io import PlanError, plan_from_json
from steerai.orchestrator.replan import Progress, continuation_refusal
from steerai.run import over_limit
from steerai.verify.predicates import TitleMatches, WindowExists
from tests.orchestrator.fakes import FakeConfirmer
from tests.orchestrator.test_open_loop import YT_TITLE, Rig, _open
from tests.stress.injection.catalogue import Attack, fixture, pinned

GOAL = "search YouTube for drake latest video and play the first video"
EXE = "C:/Program Files/Google/Chrome/Application/chrome.exe"
SEARCH = "https://www.google.com/search?hl=en&q="


def _google(text: str, step_id: str = "p2.o1") -> Step:
    return Step(
        id=step_id,
        subgoal="g2",
        verb="open",
        target=Target(role="App", name="chrome"),
        risk="R1",
        params={"site": "google_search", "text": text},
    )


def _first_plan() -> Plan:
    return Plan(
        goal=GOAL,
        subgoals=(
            Subgoal(id="g1", desc="results", condition=WindowExists(name_pattern=YT_TITLE)),
            Subgoal(id="g2", desc="playing", condition=TitleMatches(pattern="Drake")),
        ),
        steps=(_open(),),
    )


# ---- IJ-17: page text in a continuation's open ------------------------------------------------


def test_page_text_in_an_open_is_r2_whatever_the_plan_declares() -> None:
    attack = fixture("IJ-17", "page_text")
    assert open_tier(GOAL, _google(attack.payload)) == attack.tier


def test_a_continuations_open_with_page_text_asks_and_shows_the_host() -> None:
    attack = fixture("IJ-17", "page_text")
    rig = Rig(confirmer=FakeConfirmer(default="no"))
    session = rig.build()
    first = _first_plan()
    ran = session.run_task(first)
    assert ran.results[0].ok is True, ran
    assert rig.confirmer.asked == [], "the goal's own words open at R1"
    progress = Progress()
    progress.add(first, ran)
    second = Plan(
        goal=GOAL,
        subgoals=(Subgoal(id="g2", desc="playing", condition=TitleMatches(pattern="Drake")),),
        steps=(_google(attack.payload),),
    )
    assert continuation_refusal(second, progress) is None, "the rules alone let it through"
    result = session.run_task(second, continuation=True)
    assert [ask.risk for ask in rig.confirmer.asked] == [attack.tier]
    ask = rig.confirmer.asked[0]
    assert ask.params["opens"] == "www.google.com"
    assert ask.params["text"] == attack.payload, "the person sees what leaves the machine"
    assert result.results[0].gate == "block"
    assert len(rig.launcher.calls) == 1, "only the first plan's open launched"


# ---- IJ-18: content on Chrome's command line ----------------------------------------------------


def _escaped() -> list[Attack]:
    return [a for a in pinned("IJ-18") if a.expected == "escaped"]


def _refused() -> list[Attack]:
    return [a for a in pinned("IJ-18") if a.expected == "refused"]


@pytest.mark.parametrize("attack", _escaped(), ids=lambda a: a.case)
def test_injected_content_stays_one_url_encoded_argument(attack: Attack) -> None:
    argv = launch_argv("chrome", "google_search", attack.payload, exe=EXE)
    assert argv[:2] == [EXE, "--new-window"]
    assert len(argv) == 3, "the content is never an argument of its own"
    url = argv[2]
    assert url.startswith(SEARCH), "the scheme and host are the registry's"
    assert url[len(SEARCH) :] == urllib.parse.quote_plus(" ".join(attack.payload.split()))
    assert not any(ch in url for ch in ' "\r\n')


def _plan_body(text: str) -> str:
    return json.dumps(
        {
            "goal": GOAL,
            "subgoals": [{"id": "g1", "desc": "results", "condition": {"title": {"pattern": "x"}}}],
            "steps": [
                {
                    "id": "o1",
                    "subgoal": "g1",
                    "verb": "open",
                    "target": {"role": "App", "name": "chrome"},
                    "risk": "R1",
                    "params": {"site": "google_search", "text": text},
                }
            ],
        }
    )


@pytest.mark.parametrize("attack", _refused(), ids=lambda a: a.case)
def test_long_or_multi_line_content_is_refused_at_the_plan_boundary(attack: Attack) -> None:
    with pytest.raises(PlanError) as caught:
        plan_from_json(_plan_body(attack.payload))
    assert str(caught.value).startswith("$.steps[0].params")


def test_over_long_content_is_refused_by_the_launcher_too() -> None:
    payload = fixture("IJ-18", "too_long").payload
    assert len(payload) == MAX_CONTENT_CHARS + 1
    with pytest.raises(LaunchError):
        launch_argv("chrome", "google_search", payload, exe=EXE)


def test_line_breaks_never_reach_the_url() -> None:
    url = launch_argv("chrome", "google_search", fixture("IJ-18", "crlf").payload, exe=EXE)[2]
    assert "%0D" not in url
    assert "%0A" not in url


@pytest.mark.parametrize(
    ("app", "site"), [("cmd", None), ("chrome", "https://evil.example"), ("chrome", "file")]
)
def test_an_app_or_site_outside_the_registries_is_refused(app: str, site: str | None) -> None:
    body = json.loads(_plan_body("cats"))
    body["steps"][0]["target"]["name"] = app
    body["steps"][0]["params"] = {"text": "cats"} if site is None else {"site": site}
    with pytest.raises(PlanError):
        plan_from_json(json.dumps(body))


# ---- --max-risk sees run-time tiers (IJ-28, #88) ------------------------------------------------


def test_max_risk_refuses_an_open_the_run_will_raise_to_r2() -> None:
    attack = fixture("IJ-28", "max_risk")
    plan = Plan(goal=GOAL, subgoals=_first_plan().subgoals, steps=(_google(attack.payload),))
    assert open_tier(GOAL, plan.steps[0]) == attack.tier, "the run raises it"
    assert over_limit(plan, "R1") is not None
