"""Goal-match in the loop (5.13 step 2 [v2.27], #81 part B): one judgement per plan, before any
step; a step judged anything but `serves` asks with a reason token; no matcher means `off`,
traced; the trace carries verdict tokens only."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from steerai.goal_match import Judgement, Verdict
from steerai.orchestrator.contracts import Step
from tests.orchestrator.test_loop import Harness, _click, _plan, _type


class _Matcher:
    def __init__(self, verdicts: dict[str, Verdict], failure: str | None = None) -> None:
        self.verdicts = verdicts
        self.failure = failure
        self.calls: list[tuple[str, list[str], list[str]]] = []

    def judge(self, goal: str, steps: Sequence[Step], done: Sequence[Step] = ()) -> Judgement:
        self.calls.append((goal, [s.id for s in steps], [s.id for s in done]))
        return Judgement(dict(self.verdicts), model="fake", latency_ms=5.0, failure=self.failure)


def _with(h: Harness, matcher: _Matcher | None) -> Harness:
    h.session._goal_matcher = matcher
    return h


def test_a_step_judged_unrelated_asks_with_its_reason() -> None:
    h = _with(Harness(default="no"), _Matcher({"s1": "unrelated"}))
    result = h.session.run_task(_plan(_click()))
    assert [ask.reasons for ask in h.confirmer.asked] == [("goal_unrelated",)]
    assert result.results[0].gate == "block"
    assert result.results[0].evidence["ask_reasons"] == ["goal_unrelated"]


def test_steps_that_serve_run_without_an_ask_and_the_plan_is_judged_once() -> None:
    matcher = _Matcher({"s1": "serves", "s2": "serves"})
    h = _with(Harness(), matcher)
    h.session.run_task(_plan(_click(), _type()))
    assert h.confirmer.asked == []
    assert matcher.calls == [("send hello there to Amir", ["s1", "s2"], [])]


def test_a_failed_judgement_or_a_step_without_a_verdict_asks_as_unknown() -> None:
    h = _with(Harness(default="no"), _Matcher({}, failure="TimeoutError"))
    h.session.run_task(_plan(_click()))
    assert [ask.reasons for ask in h.confirmer.asked] == [("goal_unknown",)]
    line = next(line for line in h.trace.lines if '"kind": "goal_match"' in line)
    assert '"failure": "TimeoutError"' in line, "the fake reports its own token"


def test_no_matcher_is_off_and_traced_so() -> None:
    h = _with(Harness(), None)
    h.session.run_task(_plan(_click()))
    assert h.confirmer.asked == []
    assert any('"goal_match": "off"' in line for line in h.trace.lines)


def test_the_trace_carries_verdict_tokens_never_names_or_the_goal() -> None:
    h = _with(Harness(), _Matcher({"s1": "serves"}))
    h.session.run_task(_plan(_click(name="Chat options")))
    line = next(line for line in h.trace.lines if '"kind": "goal_match"' in line)
    assert '"verdicts": {"s1": "serves"}' in line
    assert "Chat options" not in line
    assert "hello there" not in line


def test_a_continuation_is_judged_with_the_steps_already_done() -> None:
    matcher = _Matcher({"s2": "serves", "p2.s1": "serves"})
    h = _with(Harness(), matcher)
    h.session.run_task(_plan(_type()))
    h.session.run_task(_plan(_click(step_id="p2.s1")), continuation=True)
    assert matcher.calls[1][2] == ["s2"]


def test_a_type_of_the_persons_own_words_still_asks_when_judged_unrelated() -> None:
    """Provenance passes ("hello there" is in the goal); the destination is the matcher's to
    judge (hunter GM-10)."""
    h = _with(Harness(default="no"), _Matcher({"s2": "unrelated"}))
    h.session.run_task(_plan(_type()))
    assert [ask.reasons for ask in h.confirmer.asked] == [("goal_unrelated",)]


def test_a_preset_plan_is_not_judged_again_and_is_traced_from_the_cache() -> None:
    """A trusted cache entry's verdicts stand for its own plan: no matcher call (5.12 [v2.34])."""
    matcher = _Matcher({"s1": "unrelated"})
    h = _with(Harness(), matcher)
    plan = _plan(_click())
    h.session.preset(plan, {"s1": "serves"})
    h.session.run_task(plan)
    assert matcher.calls == []
    assert h.confirmer.asked == []
    line = next(line for line in h.trace.lines if '"kind": "goal_match"' in line)
    assert '"model": "cache"' in line


def test_after_a_preset_a_different_plan_is_judged_live() -> None:
    """A continuation is never covered by the cached verdicts (hunter PC-12)."""
    matcher = _Matcher({"s2": "serves", "p2.s1": "serves"})
    h = _with(Harness(), matcher)
    first = _plan(_type())
    h.session.preset(first, {"s2": "serves"})
    h.session.run_task(first)
    h.session.run_task(_plan(_click(step_id="p2.s1")), continuation=True)
    assert [goal for goal, _ids, _done in matcher.calls] == ["send hello there to Amir"]


def test_a_failed_judgement_is_reported_and_writes_no_cache_entry(tmp_path: Path) -> None:
    from steerai.memory.plan_cache import CacheKey, PlanCache  # noqa: PLC0415 - this test only
    from steerai.run import _CacheRun, _settle_cache  # noqa: PLC0415
    from tests.memory.test_plan_cache import _fake_protect, _fake_unprotect  # noqa: PLC0415

    h = _with(Harness(), _Matcher({}, failure="timeout"))
    plan = _plan(_type())
    result = h.session.run_task(plan)
    assert h.session.judged[1] == "timeout"
    cache = PlanCache(tmp_path, protect=_fake_protect, unprotect=_fake_unprotect)
    key = CacheKey.of(plan.goal, app_kind="notepad.exe", prompt_version="p", matcher="m")
    done = replace(result, status="done")
    _settle_cache(
        _CacheRun(cache, key, "notepad.exe", data=b"plan"),
        h.session,
        plan,
        done,
        replanned=False,
        trace=h.trace,
    )
    assert cache.get(key).event == "miss"
