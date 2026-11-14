"""The v2 loop for step 3 (V2 §5), behind `--loop v2`: ports, the resolver, the action risk and
`run_task`.

Interface stub for step 3c. The §8 invariant tests in `tests/loop2/test_invariants_loop.py` are
written first against these shapes and fail with `NotImplementedError` until 3c implements them.
Every outside effect goes through `Ports`, so the tests drive the loop with fakes.

[V2.11]: the Task is built before the first call (goal, budgets, no subgoals); the first valid
Decision fills the subgoals once and code freezes them. When nothing was dispatched the loop
refuses with a token and a strike; the task ends only through strikes, a stop, the budgets or
the gate, and in step 3 a commit ends it `blocked` with `commit_not_in_step`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from steerai.loop2._stub import stub
from steerai.loop2.decision import ActionDoc, Decision
from steerai.loop2.guards import StrikeReason
from steerai.loop2.query import SearchFacts
from steerai.loop2.task import Task
from steerai.orchestrator.contracts import Risk
from steerai.world.effects import Observed
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.watch import Watched

Status = Literal["done", "blocked", "asked", "paused", "stopped", "cannot"]
Provenance = Literal["goal_span", "generated"]


@dataclass(frozen=True)
class Dispatch:
    """One action the loop sends: the model's action, the resolved element and its risk."""

    action: ActionDoc
    runtime_id: tuple[int, ...]
    risk: Risk
    provenance: Provenance | None
    """Set by code, never by the model, for a `type` ([V2.11] decision 5): `param` gives
    `goal_span`; free text is always `generated`, even when it matches the request."""


@dataclass(frozen=True)
class TurnView:
    """What one controller call is given: the latest observation and closed history tokens."""

    observed: Observed
    history: tuple[str, ...]
    """Code-written tokens only (§8.6): never screen text or model text."""
    first_turn: bool


@dataclass(frozen=True)
class Ports:
    decide: Callable[[TurnView], Decision]
    observe: Callable[[], Observed]
    act: Callable[[Dispatch], None]
    watch: Callable[[Observed, Dispatch], Watched]
    read_back: Callable[[Dispatch], bool | None]
    """A `type` or a state verb's read-back: True verified, False a mismatch, None unread."""
    write_ahead: Callable[[Dispatch], str]
    interrupted: Callable[[], str | None]
    """A takeover, the secure desktop or a lock, as a token; None to go on."""
    clock: Callable[[], float]


@dataclass(frozen=True)
class TaskOutcome:
    status: Status
    reason: str | None
    """A closed token: `commit_not_in_step`, `max_actions`, `takeover` and so on."""
    dispatched: tuple[Dispatch, ...]
    model_calls: int
    strikes: tuple[tuple[StrikeReason, ...], ...]
    """One entry per strike, with every reason that held in that turn."""


@stub
def resolve(target: int, listed: Snapshot, latest: Snapshot) -> Element | None:
    """The element `target` named in `listed`, if the latest snapshot still has its RuntimeId
    with the same role and name (§8.1); None otherwise."""
    raise NotImplementedError("3c: resolver")


@stub
def action_risk(
    action: ActionDoc, element: Element, *, goal: str, search: SearchFacts | None
) -> Risk:
    """§7.2: the max of the declared commit, deny words, a secure field and text provenance;
    raise-only, so no model field lowers it (§8.2). `search` is the field's facts for an Enter
    or a click that may be a query."""
    raise NotImplementedError("3c: action risk")


@stub
def run_task(task: Task, ports: Ports) -> TaskOutcome:
    """Run `task` to an outcome. Between batch actions the watch's after snapshot is reused only
    when it is a full-tier read, stable and within v1's R0/R1 freshness limit
    (`REUSE_FRESHNESS_S`, its age `clock() - snapshot.taken_at`); otherwise the loop
    re-observes ([V2.11] decision 6)."""
    raise NotImplementedError("3c: the v2 loop")
