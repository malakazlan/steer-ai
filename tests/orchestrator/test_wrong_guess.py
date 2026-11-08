"""What a wrong click guess does today (#140 review, design 5.3 [v2.42]): the click's check
fails, the plan's next steps still run, and an irreversible step after it stops
`precondition_unmet` because the click's subgoal never ticked; that stop is not a replan
trigger, so the task ends there. The prompt and the refusal say exactly this."""

from __future__ import annotations

from dataclasses import replace

from steerai.orchestrator.contracts import Plan, Subgoal
from steerai.orchestrator.replan import replan_trigger
from steerai.verify.predicates import TitleMatches
from tests.orchestrator.test_loop import APPEARED, Harness, _click, _send, _type


def test_a_wrong_guess_runs_on_and_stops_before_the_send_without_a_replan() -> None:
    h = Harness()
    wrong = TitleMatches(pattern="A page that never comes")
    click = _click(step_id="s1", expect=wrong)
    plan = Plan(
        goal="send hello there to Amir",
        subgoals=(
            Subgoal(id="g0", desc="search open", condition=wrong),
            Subgoal(id="g1", desc="sent", condition=APPEARED),
        ),
        steps=(replace(click, subgoal="g0"), _type(), _send()),
    )
    result = h.session.run_task(plan)
    assert [r.step_id for r in result.results] == ["s1", "s2"], "the type still ran"
    assert result.results[0].ok is False
    assert result.evidence["detail"] == "precondition_unmet"
    assert h.world.sent == []
    assert replan_trigger(result, {s.id: s for s in plan.steps}) is None
