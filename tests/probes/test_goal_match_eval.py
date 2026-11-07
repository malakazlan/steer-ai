"""The goal-match evaluation's counting, with a scripted matcher (no model call)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from steerai.goal_match import Judgement, Verdict
from steerai.orchestrator.contracts import Step
from steerai.probes.goal_match_eval import evaluate, load_cases
from tests.orchestrator.test_plan_io import _doc


class _Scripted:
    """Serves every own step, catches every injected one."""

    def judge(
        self,
        goal: str,  # noqa: ARG002 - the protocol's shape
        steps: Sequence[Step],
        done: Sequence[Step] = (),  # noqa: ARG002 - the protocol's shape
    ) -> Judgement:
        verdicts: dict[str, Verdict] = {
            s.id: ("unrelated" if s.id.startswith("inj") else "serves") for s in steps
        }
        return Judgement(verdicts, model="scripted", latency_ms=1.0)


def test_a_perfect_matcher_scores_no_false_ask_and_full_catch(tmp_path: Path) -> None:
    (tmp_path / "a.plan.json").write_text(json.dumps(_doc()), encoding="utf-8")
    (tmp_path / "copy.plan.json").write_text(json.dumps(_doc()), encoding="utf-8")
    cases = load_cases([tmp_path], seed=1)
    assert len(cases) == 1, "identical plan files count once"
    report = evaluate(_Scripted(), cases, runs=2)
    assert "false-ask rate: 0/6 = 0.0%" in report
    assert "catch rate: 4/4 = 100.0%" in report
