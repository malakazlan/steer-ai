"""Replanning's pure parts (design 5.3 / 5.11 [v2.1], packet replan-2026-11-06): which ended
runs may be planned again, what the planner is told about the task's progress, which
continuation plans are refused, and the task's result merged over its plans.

A replan is an allow-list: a run that ended `unfinished`, or that stopped on a target missing
before the gate ran. Anything with an effect that may have happened unverified at R2/R3 stops
`unverified_effect` instead, so a new plan never acts on top of an effect nobody checked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from steerai.action.dispositions import Delivery
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import (
    OPENED_PREFIX,
    START_WINDOW,
    Plan,
    Step,
    StepResult,
    Subgoal,
)
from steerai.orchestrator.gate import effective_risk
from steerai.orchestrator.loop import TaskResult
from steerai.orchestrator.plan_io import encode_predicate
from steerai.planner.contracts import DoneStep, FailedStep, OpenSubgoal
from steerai.verify.predicates import Predicate, field_values

MAX_REPLANS = 2
"""Every replan counts, a continuation after `unfinished` too (design 5.3 [v2.1]: stricter than
"two failed replans")."""

Trigger = Literal["unfinished", "element_not_found", "unverified_effect"]
_REPLAN_DETAILS = frozenset({"no_match"})
"""Missing-target details that replan. `candidate_gone` only comes from a person target, which
never replans (hunter RP-05)."""


@dataclass
class Progress:
    """The task's plans in order and what each run returned."""

    plans: list[Plan] = field(default_factory=list)
    runs: list[TaskResult] = field(default_factory=list)

    def add(self, plan: Plan, result: TaskResult) -> None:
        self.plans.append(plan)
        self.runs.append(result)

    @property
    def last(self) -> TaskResult:
        return self.runs[-1]

    @property
    def steps(self) -> dict[str, Step]:
        return {s.id: s for plan in self.plans for s in plan.steps}

    @property
    def ticked(self) -> set[str]:
        return set().union(*(run.ticked for run in self.runs))

    def subgoals(self) -> dict[str, Subgoal]:
        """Every subgoal id of every plan, at its latest definition."""
        return {s.id: s for plan in self.plans for s in plan.subgoals}

    def results(self) -> list[StepResult]:
        return [r for run in self.runs for r in run.results]

    def remaining(self) -> list[OpenSubgoal]:
        ticked = self.ticked
        return [
            OpenSubgoal(id=s.id, desc=s.desc)
            for s in self.subgoals().values()
            if s.id not in ticked
        ]

    def done_steps(self) -> list[DoneStep]:
        """Verb, target role and name of the steps verified (T1) or found done; never text,
        params, the chosen row or evidence (schema gate item 5)."""
        steps = self.steps
        done = []
        for result in self.results():
            # a failed verify still carries a T1 label: only an ok step is done (review item 1)
            if not result.ok or (result.label_kind != "t1" and result.gate != "done_already"):
                continue
            target = steps[result.step_id].target
            if target is not None:
                done.append(
                    DoneStep(verb=steps[result.step_id].verb, role=target.role, name=target.name)
                )
        return done

    def failure(self) -> FailedStep | None:
        """The last step that failed in the last run, by id and taxonomy code: an earlier
        plan's failure is never reported again."""
        for result in reversed(self.last.results):
            code = result.code or result.verify_code
            if not result.ok and code is not None:
                return FailedStep(step_id=result.step_id, code=code.value)
        return None

    def rewritten(self, plan: Plan) -> set[str]:
        """The ids whose condition the new plan changed (the preview marks them, Q3)."""
        before = self.subgoals()
        return {
            s.id
            for s in plan.subgoals
            if s.id in before
            and encode_predicate(s.condition) != encode_predicate(before[s.id].condition)
        }

    def merged(self) -> TaskResult:
        """One result for the task: every plan's step results in run order, the union of
        ticks, the last run's stop; `done` only when the union covers every subgoal id of
        every plan (schema gate item 6)."""
        last = self.last
        ticked = self.ticked
        status = last.status
        code = last.code
        evidence = dict(last.evidence)
        if status == "done" and not set(self.subgoals()) <= ticked:
            status, code, evidence = "stopped", ErrorCode.VERIFY_FAILED, {"detail": "unfinished"}
        return TaskResult(
            status=status, results=self.results(), ticked=ticked, code=code, evidence=evidence
        )

    def ended(
        self, status: Literal["stopped", "paused", "blocked"], code: ErrorCode, detail: str
    ) -> TaskResult:
        """The task ends on the way to another plan, with everything that did run."""
        return TaskResult(
            status=status,
            results=self.results(),
            ticked=self.ticked,
            code=code,
            evidence={"detail": detail},
        )


def replan_trigger(result: TaskResult, steps: Mapping[str, Step]) -> Trigger | None:
    """Why this run may be planned again, `unverified_effect` when it must not because an
    R2/R3 effect may have happened unchecked, or None (schema gate item 1, Q2)."""
    if result.status != "stopped":
        return None
    detail = result.evidence.get("detail")
    trigger: Trigger
    if detail == "unfinished":
        trigger = "unfinished"
    elif _missing_target(result, steps):
        trigger = "element_not_found"
    else:
        return None
    for step_result in result.results:
        if step_result.ok:
            continue
        if _maybe_landed(step_result, steps[step_result.step_id]):
            return "unverified_effect"
    for step_result in result.results:
        step = steps[step_result.step_id]
        if not step_result.ok and (step.verb == "open" or _person(step)):
            return None
    return trigger


def continuation_refusal(plan: Plan, progress: Progress, *, desktop: bool = False) -> str | None:
    """The path of the first rule a continuation plan breaks, or None: the task's goal, the
    remaining ids kept and no ticked id reused (Q3), step ids new to the task (item 2), and no
    second open of an (app, site) pair (item 4)."""
    if plan.goal != progress.plans[0].goal:
        return "goal"
    if _subgoals_refused(plan, progress):
        return "subgoals"
    used = set(progress.steps)
    opened = {_opens(s) for s in progress.steps.values() if s.verb == "open"}
    verified = {
        r.step_id for r in progress.results() if r.ok and progress.steps[r.step_id].verb == "open"
    }
    opened_here: set[str] = set()
    for index, step in enumerate(plan.steps):
        if step.id in used:
            return f"steps[{index}].id"
        if _bad_window(step.window, opened_here | verified, desktop=desktop):
            return f"steps[{index}].window"
        if step.verb == "open" and _opens(step) in opened:
            return f"steps[{index}]"
        if step.verb == "open":
            opened_here.add(step.id)
    return None


def _subgoals_refused(plan: Plan, progress: Progress) -> bool:
    """Q3: every remaining id kept, no ticked id reused; a value's text is the goal's, so a
    continuation never rewrites it to screen text (FV-04)."""
    ids = {s.id for s in plan.subgoals}
    ticked = progress.ticked
    remaining = set(progress.subgoals()) - ticked
    if not remaining <= ids or ids & ticked:
        return True
    before = progress.subgoals()
    opened = _open_only(progress)
    return any(
        s.id in before
        and (
            _texts(before[s.id].condition) != _texts(s.condition)
            or _weakened(before[s.id].condition, s.condition, open_only=s.id in opened)
        )
        for s in plan.subgoals
    )


def _texts(condition: Predicate) -> list[str]:
    return sorted(value.text for value in field_values(condition))


_WINDOW_KINDS = frozenset({"WindowExists", "TitleMatches"})


def _kind(condition: Predicate) -> str:
    """A condition's shape: its type, and for All / AnyOf the sorted shapes of its parts."""
    parts = getattr(condition, "parts", None)
    if parts is None:
        return type(condition).__name__
    return f"{type(condition).__name__}({','.join(sorted(_kind(p) for p in parts))})"


def _weakened(before: Predicate, after: Predicate, *, open_only: bool) -> bool:
    """IJ-10 (#86): a continuation written from a page never changes a remaining condition's
    kind, so an `appeared` diff cannot become a title the page controls. A subgoal reached
    only by an `open` may move between its window and its title (5.3 [v2.6])."""
    old, new = _kind(before), _kind(after)
    if old == new:
        return False
    return not (open_only and {old, new} <= _WINDOW_KINDS)


def _open_only(progress: Progress) -> set[str]:
    """Subgoals every earlier step of which is an `open`."""
    verbs: dict[str, set[str]] = {}
    for step in progress.steps.values():
        verbs.setdefault(step.subgoal, set()).add(step.verb)
    return {subgoal for subgoal, seen in verbs.items() if seen == {"open"}}


def _bad_window(window: str | None, opens: set[str], *, desktop: bool) -> bool:
    """`start` is never the shell's desktop (SW-05); `opened:<id>` names an `open` earlier in
    this plan or a verified `open` of an earlier link (SW-19)."""
    if window is None:
        return False
    if window == START_WINDOW:
        return desktop
    return window.removeprefix(OPENED_PREFIX) not in opens


def _missing_target(result: TaskResult, steps: Mapping[str, Step]) -> bool:
    if result.code is not ErrorCode.ELEMENT_NOT_FOUND or not result.results:
        return False
    last = result.results[-1]
    step = steps[last.step_id]
    return (
        last.gate == "unreached"
        and last.evidence.get("detail") in _REPLAN_DETAILS
        and step.verb != "open"
        and not _person(step)
    )


def _maybe_landed(result: StepResult, step: Step) -> bool:
    """Dispatched at R2/R3 (the tier the run used, raised by a row or a deny word) and not
    verified; nothing delivered is not an effect (hunter RP-10, RP-31)."""
    if result.gate == "done_already" or result.action is None:
        return False
    if result.action.delivery is Delivery.NOT_DELIVERED:
        return False
    risk = str(result.evidence.get("risk", effective_risk(step)))
    return risk in ("R2", "R3")


def _person(step: Step) -> bool:
    return step.target is not None and step.target.match == "person"


def _opens(step: Step) -> tuple[str, str | None]:
    name = "" if step.target is None else step.target.name
    return name, step.params.get("site")
