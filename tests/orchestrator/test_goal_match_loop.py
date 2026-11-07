"""Goal-match in the loop (5.13 step 2 [v2.27], #81 part B): one judgement per plan, before any
step; a step judged anything but `serves` asks with a reason token; no matcher means `off`,
traced; the trace carries verdict tokens only."""

from __future__ import annotations

from collections.abc import Sequence

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
