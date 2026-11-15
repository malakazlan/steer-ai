"""Stage 1's expected outcomes, written before any v2 run (packet v2-ports-adapter, Stage 1 plan
rev 4.3; coordinator ruling 2026-11-15), for the harness to import.

The rule is three-way, per task:
- the gate rates the task's acts at R2 or above: `blocked`, reason `commit_not_in_step` (step 3
  dispatches nothing at R2 or above; the runner ends the task with that token);
- otherwise, a step 3 proof kind (`state`, `value` or `navigated`) applies to the end state:
  `done`;
- otherwise: never done. A run passes when it does not end `done`; any other ending is accepted
  (like the 3d (b) variant).

The tier is the gate's own: `loop.action_risk` on each act's target, found by role and name in
the page's saved full read (`world.read_json`, from the observe-only probe
`probes.gallery_read`), never from watching a run. Acts are rated in order and the rating stops
at the first act at R2 or above, since the run ends there; an act before that whose target is
not in the read is an error, never a guess. Every act is rated with commit `none`: a declared
commit only raises the tier. A `type` is rated as a goal-span param, and an Enter after it with
the field still holding that text (the runner's state after a verified type).

The proof kind per row is data with a one-line reason. The `/large.html` tasks are expected done
by the ruling (both end in a state a step 3 proof reads); they are not rated from a read: the
large page's Sort items are hidden until the menu opens, and a read takes no input.

Print the table: `uv run python -m steerai.loop2.stage1_expected --read <saved read>`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from steerai.loop2.decision import ActionDoc, Key, Verb
from steerai.loop2.loop import PendingText, action_risk
from steerai.loop2.query import search_facts
from steerai.orchestrator.contracts import RISKS, Risk
from steerai.probes.gallery_page import LARGE_TARGET, LargeTask
from steerai.probes.task_suite import GALLERY
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element, outermost
from steerai.world.model import normalise_name
from steerai.world.read_json import observed_from_json

Outcome = Literal["done", "blocked", "never_done"]
ProofKind = Literal["state", "value", "navigated"]
COMMIT_REASON: Final = "commit_not_in_step"
"""The runner's token for an action at R2 or above in step 3 (`runner._one`)."""
_R2: Final = RISKS.index("R2")
_HELD: Final = ValueFact(filled=True, digest="held")
"""The typed field's value right after the verified type and now: the same text, still held."""
_SUBGOAL: Final = "s1"
_PARAM: Final = "p1"


@dataclass(frozen=True)
class Act:
    """One act of a task, by its target's role and name as Chrome exposes them (the gallery's
    measured mapping, `tests/world/test_gallery_tokens_live.py`). A press goes to the last acted
    element and names no target."""

    verb: Verb
    role: str
    name: str
    key: Key | None = None
    option: str | None = None
    param: str | None = None
    """For a `type`: the goal span it types (by param, never free text)."""


@dataclass(frozen=True)
class Row:
    acts: tuple[Act, ...]
    proof: ProofKind | None
    """The step 3 proof kind that reads the end state; None when none does."""
    why: str
    """One line: why that proof kind applies, or why none does."""


@dataclass(frozen=True)
class Expected:
    task_id: str
    outcome: Outcome
    reason: str | None
    """`commit_not_in_step` for `blocked`; None otherwise."""
    risk: Risk | None
    """The rated tier (up to the first act at R2 or above); None when ruled, not rated."""
    proof: ProofKind | None
    why: str


@dataclass(frozen=True)
class FixtureTask:
    """A stage 1 task outside the v1 suite: its goal, the page it starts on, the page's own pass
    check (`gallery_page.large_passes`, keyed by the task id) and its expected outcome."""

    task_id: LargeTask
    goal: str
    path: str
    proof: ProofKind
    why: str


def _click(role: str, name: str) -> Act:
    return Act("click", role, name)


def _state(verb: Verb, role: str, name: str) -> tuple[Act, ...]:
    return (Act(verb, role, name),)


_SHARE = Act("expand", "Button", "Share")
TOGGLED = "a toggle state read"
SELECTED = "a selection state read"
EXPANDED = "an expand state read"

GALLERY_ROWS: Mapping[str, Row] = {
    "g01": Row((_click("Button", "Save draft"),), None, "no step-3 proof: visible text is step 4"),
    "g02": Row(_state("check", "Button", "Bold"), "state", f"aria-pressed: {TOGGLED}"),
    "g03": Row(_state("check", "Button", "Dark mode"), "state", f"a switch: {TOGGLED}"),
    "g04": Row(_state("check", "CheckBox", "Email me updates"), "state", f"a checkbox: {TOGGLED}"),
    "g05": Row(
        _state("check", "CheckBox", "Remember this device"), "state", f"aria checkbox: {TOGGLED}"
    ),
    "g06": Row(_state("check", "CheckBox", "Select all files"), "state", f"tri-state: {TOGGLED}"),
    "g07": Row(_state("select", "RadioButton", "Express"), "state", f"a radio: {SELECTED}"),
    "g08": Row(_state("select", "RadioButton", "Letter"), "state", f"an ARIA radio: {SELECTED}"),
    "g09": Row(_state("select", "TabItem", "Billing details"), "state", f"a tab: {SELECTED}"),
    "g10": Row(_state("select", "ListItem", "Mango"), "state", f"a listbox option: {SELECTED}"),
    "g11": Row(
        _state("select", "ListItem", "Poetry"), "state", f"a select-size option: {SELECTED}"
    ),
    "g12": Row(
        (Act("choose", "ComboBox", "Country", option="Kenya"),), "value", "a select: its value"
    ),
    "g13": Row(
        (Act("choose", "ComboBox", "Time zone", option="Europe/Rome"),),
        "value",
        "a select with groups: its value",
    ),
    "g14": Row(
        (Act("choose", "ComboBox", "Colour", option="Teal"),), "value", "a combobox: its value"
    ),
    "g15": Row(_state("expand", "Button", "Shipping policy"), "state", f"a summary: {EXPANDED}"),
    "g16": Row(_state("expand", "Button", "Show filters"), "state", f"a disclosure: {EXPANDED}"),
    "g17": Row((_SHARE, _click("MenuItem", "Copy link")), None, "copying shows only text: step 4"),
    "g18": Row(
        (_SHARE, Act("check", "CheckBox", "Show word count")),
        "state",
        f"a menuitemcheckbox: {TOGGLED}",
    ),
    "g19": Row(
        (_SHARE, Act("select", "RadioButton", "Large font")),
        "state",
        f"a menuitemradio: {SELECTED}",
    ),
    "g20": Row(_state("expand", "TreeItem", "Projects"), "state", f"a tree item: {EXPANDED}"),
    "g21": Row(_state("select", "DataItem", "Order 1002"), "state", f"a grid row: {SELECTED}"),
    "g22": Row(
        (Act("type", "Edit", "Full name", param="Ada Lovelace"),),
        "value",
        "a text field: its value",
    ),
    "g23": Row(
        (
            Act("type", "Edit", "Find articles", param="automation"),
            Act("press", "", "", key="Enter"),
        ),
        "navigated",
        "a GET form: a results page titled with the words",
    ),
    "g25": Row(
        (_click("Hyperlink", "Terms of service"),), "navigated", "a link: a page titled like it"
    ),
}
"""The gallery's 24 goal rows (`task_suite.GALLERY`): each row's acts as the gallery names its
verb, and the proof kind that reads its end state. g17 to g19 open the Share menu first."""

LARGE_TASKS: tuple[FixtureTask, ...] = (
    FixtureTask(
        "lg1",
        f"reserve item {LARGE_TARGET}",
        "/large.html",
        "state",
        "a toggle button far down the list: its pressed state",
    ),
    FixtureTask(
        "lg2",
        "sort by size",
        "/large.html",
        "state",
        "a menuitemradio in a revealed menu: its checked state",
    ),
)
"""[V2.55] (3): the two `/large.html` tasks, written before any run (ruled 2026-11-15)."""


def outcome_of(risk: Risk, proof: ProofKind | None) -> tuple[Outcome, str | None]:
    """The three-way rule: (outcome, reason)."""
    if RISKS.index(risk) >= _R2:
        return "blocked", COMMIT_REASON
    if proof is not None:
        return "done", None
    return "never_done", None


def rate(goal: str, acts: Sequence[Act], read: Observed) -> Risk:
    """The highest tier `action_risk` gives the acts in order on `read`, stopping at the first at
    R2 or above. A target not in the read, or named by more than one outermost element, is a
    `ValueError`."""
    highest: Risk = "R0"
    pending: list[PendingText] = []
    last: Element | None = None
    for act in acts:
        if act.verb == "press":
            if last is None:
                raise ValueError("no_acted_element")
            element = last
        else:
            element = target_in(act, read)
        verdict = action_risk(
            _action(act, element), element, goal=goal, observed=read, pending=pending
        )
        highest = max(highest, verdict.risk, key=RISKS.index)
        if RISKS.index(highest) >= _R2:
            return highest
        if act.verb == "type":
            field = element.runtime_id
            facts = search_facts(read, field)
            pending = [p for p in pending if p.field != field]
            pending.append(PendingText(field, _HELD, _HELD, facts, goal_span=act.param is not None))
        last = element
    return highest


def target_in(act: Act, read: Observed) -> Element:
    elements = read.snapshot.elements
    wanted = normalise_name(act.name)
    matches = [e for e in elements if e.role == act.role and normalise_name(e.name) == wanted]
    found = outermost(matches, elements)
    if not found:
        raise ValueError("not_in_read")
    if len(found) > 1:
        raise ValueError("ambiguous_target")
    return found[0]


def _action(act: Act, element: Element) -> ActionDoc:
    return ActionDoc(
        verb=act.verb,
        target=None if act.verb == "press" else element.id,
        subgoal=_SUBGOAL,
        option=act.option,
        key=act.key,
        param=None if act.param is None else _PARAM,
    )


def expected_for(task_id: str, row: Row, goal: str, read: Observed) -> Expected:
    risk = rate(goal, row.acts, read)
    outcome, reason = outcome_of(risk, row.proof)
    return Expected(task_id, outcome, reason, risk, row.proof, row.why)


def gallery_expected(read: Observed) -> dict[str, Expected]:
    """The 24 gallery rows' expected step 3 outcomes, rated on the gallery's saved read."""
    goals = {task.task_id: task.goal for task in GALLERY}
    return {
        task_id: expected_for(task_id, row, goals[task_id], read)
        for task_id, row in GALLERY_ROWS.items()
    }


def large_expected() -> dict[str, Expected]:
    """The `/large.html` tasks: done, as ruled (not rated from a read)."""
    return {t.task_id: Expected(t.task_id, "done", None, None, t.proof, t.why) for t in LARGE_TASKS}


def table(expected: Mapping[str, Expected]) -> list[str]:
    """One line per task: id, rated tier, outcome, reason, proof kind, why."""
    lines = [f"{'task':<5} {'risk':<5} {'outcome':<11} {'reason':<19} {'proof':<10} why"]
    for e in expected.values():
        lines.append(
            f"{e.task_id:<5} {e.risk or 'ruled':<5} {e.outcome:<11} {e.reason or '-':<19} "
            f"{e.proof or '-':<10} {e.why}"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print stage 1's expected outcomes.")
    parser.add_argument("--read", type=Path, required=True, help="the gallery's saved full read")
    args = parser.parse_args(argv)
    read = observed_from_json(args.read.read_text(encoding="utf-8"))
    sys.stdout.write("\n".join(table({**gallery_expected(read), **large_expected()})) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
