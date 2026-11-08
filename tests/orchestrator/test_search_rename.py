"""A search submit on a single-page app (5.10 [v2.39]; principal APPROVE-WITH-CHANGES): the page
renames its Document in place to the code-owned frame around the approved query, which counts at
R2 only for a `submit` that is R2 from its floor alone, with the bare replacing decorated form
naming exactly the typed, goal-verbatim text. Everything else stays `renamed_at_risk`. Live:
YouTube "lofi hip hop" stopped `unverified_effect` 2/2 (task suite, 2026-11-08)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import deny_tier, rename_counts
from steerai.verify.predicates import All, Appeared, Within
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_edge_cases import FocusLive, _rebuild
from tests.orchestrator.test_loop import HELLO, Harness

GOAL = f"search Google for {HELLO}"
PARAMS = {"text": HELLO, "composer_role": "Edit", "composer_name": "Message", "scope": "Page"}
REPLACING = Appeared(role="Document", name_pattern=HELLO, decoration="google_search", replaces=True)


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


def _counts(step: Step, **kw: Any) -> bool:
    args: dict[str, Any] = {"risk": "R2", "deny": "R0", "goal": GOAL}
    args.update(kw)
    return rename_counts(step, step.expect, **args)


# ---- the pure rule ---------------------------------------------------------------------------


def test_a_floor_only_search_submit_with_the_framed_query_counts() -> None:
    assert _counts(_submit())


@pytest.mark.parametrize(
    ("label", "step", "kw"),
    [
        ("a send", _submit(verb="send"), {}),
        ("an R3 step", _submit(risk="R3"), {"risk": "R3"}),
        ("a deny word or #84 id", _submit(), {"deny": "R2"}),
        ("a raised ceiling", _submit(), {"risk": "R3"}),
        ("a shorter pattern", _submit(expect=replace(REPLACING, name_pattern="hello")), {}),
        ("text not in the goal", _submit(), {"goal": "search Google for something else"}),
        ("an all wrapper", _submit(expect=All(parts=(REPLACING, REPLACING))), {}),
        ("no decoration", _submit(expect=replace(REPLACING, decoration=None)), {}),
        (
            "not the replacing form",
            _submit(expect=Appeared(role="Document", name_pattern=HELLO, within=Within("Page"))),
            {},
        ),
        ("another role", _submit(expect=replace(REPLACING, role="Text", replaces=False)), {}),
    ],
)
def test_every_other_case_stays_renamed_at_risk(label: str, step: Step, kw: Any) -> None:
    assert not _counts(step, **kw), label


def test_the_deny_tier_ignores_the_verb_floor_but_reads_names_and_ids() -> None:
    assert deny_tier(_submit()) == "R0"
    assert deny_tier(_submit(target=Target(role="Button", name="Post"))) == "R2"
    assert deny_tier(_submit(), element_ids=["deleteThread"]) == "R3"


# ---- through the loop ------------------------------------------------------------------------


def _renaming(h: Harness, before: str = "Home", after: str = f"{HELLO} - Google Search") -> None:
    h.world.rows.append((40, "Document", before, (42, 90), 0))
    started = h.clock["t"]
    observe = h.world.observe

    class Page:
        def observe(self, hwnd: int, **kwargs: Any) -> Any:
            old = (40, "Document", before, (42, 90), 0)
            if h.clock["t"] - started >= 0.2 and old in h.world.rows:
                h.world.rows.remove(old)
                h.world.rows.append((40, "Document", after, (42, 90), 0))
            return observe(hwnd, **kwargs)

    h.session._observer = Page()


def _harness() -> Harness:
    h = Harness()
    h.world.composer_value = HELLO
    _rebuild(h, live=FocusLive(h.world, focused=COMPOSER[3]))
    h.session._goal = GOAL
    return h


def test_the_framed_rename_verifies_the_search_submit() -> None:
    h = _harness()
    _renaming(h)
    result = h.session.run_step(_submit())
    assert result.ok is True, result.to_dict()
    assert result.evidence["reason"] == "renamed_submit"
    assert result.label_kind == "t1"


def test_a_counter_before_the_framed_name_still_counts() -> None:
    h = _harness()
    _renaming(h, after=f"(1) {HELLO} - Google Search")
    assert h.session.run_step(_submit()).ok is True


def test_the_same_query_twice_never_verifies() -> None:
    """SE-19: the page already carries the framed name, so nothing is renamed."""
    h = _harness()
    framed = f"{HELLO} - Google Search"
    _renaming(h, before=framed, after=framed)
    result = h.session.run_step(_submit())
    assert result.ok is False
    assert result.code is ErrorCode.VERIFY_TIMEOUT


def test_a_counter_only_change_never_verifies() -> None:
    h = _harness()
    framed = f"{HELLO} - Google Search"
    _renaming(h, before=framed, after=f"(2) {framed}")
    assert h.session.run_step(_submit()).ok is False


@pytest.mark.parametrize("after", [f"{HELLO} and more - Google Search", f"{HELLO[:-1]} - Goo"])
def test_a_superstring_or_trimmed_title_never_verifies(after: str) -> None:
    """SE-20, SE-21: full-string, never a pass."""
    h = _harness()
    _renaming(h, after=after)
    assert h.session.run_step(_submit()).ok is False


def test_two_renamed_documents_never_verify() -> None:
    h = _harness()
    _renaming(h)
    h.world.rows.append((41, "Document", "Other", (42, 91), 0))
    started = h.clock["t"]
    inner = h.session._observer

    class Twice:
        def observe(self, hwnd: int, **kwargs: Any) -> Any:
            old = (41, "Document", "Other", (42, 91), 0)
            if h.clock["t"] - started >= 0.2 and old in h.world.rows:
                h.world.rows.remove(old)
                h.world.rows.append((41, "Document", f"{HELLO} - Google Search", (42, 91), 0))
            return inner.observe(hwnd, **kwargs)

    h.session._observer = Twice()
    assert h.session.run_step(_submit()).ok is False


def test_a_text_the_goal_does_not_hold_stays_renamed_at_risk() -> None:
    h = _harness()
    h.session._goal = "search Google for something else"
    _renaming(h)
    result = h.session.run_step(_submit())
    assert result.ok is False
    assert result.evidence["reason"] == "renamed_at_risk"
    assert result.label_kind is None


def test_a_crash_after_enter_resumes_done_without_a_second_enter() -> None:
    h = _harness()
    _renaming(h)
    presses: list[Any] = []
    h.physical.on_execute = lambda _e, a: presses.append(a) if a.verb == "press" else None
    assert h.session.run_step(_submit()).ok is True
    key = h.ledger.key_for(_submit())
    h.ledger.executions[key]["outcome"] = None  # "crash" before the outcome was recorded
    h.ledger.executions[key]["phase"] = "acting"
    h.ledger.mark(_submit(), "acting")
    result = h.session.run_step(_submit())
    assert result.gate == "done_already", result.to_dict()
    assert len(presses) == 1, "never a second Enter"
