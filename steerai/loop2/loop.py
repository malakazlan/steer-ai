"""The v2 loop for step 3 (V2 §5), behind `--loop v2`: ports, the resolver, the action risk and
`run_task`.

Interface stub for step 3c. The §8 invariant tests in `tests/loop2/test_invariants_loop.py` are
written first against these shapes and fail with `NotImplementedError` until 3c implements them.
Every outside effect goes through `Ports`, so the tests drive the loop with fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from steerai.loop2.decision import ActionDoc, Decision
from steerai.loop2.query import SearchFacts
from steerai.loop2.task import Task
from steerai.orchestrator.contracts import Risk
from steerai.world.effects import Observed
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.watch import Watched

Status = Literal["done", "blocked", "asked", "paused", "stopped", "cannot"]


@dataclass(frozen=True)
class Dispatch:
    """One action the loop sends: the model's action, the resolved element and its risk."""

    action: ActionDoc
    runtime_id: tuple[int, ...]
    risk: Risk


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
    """A `type` or a state verb's read-back: True verified, False not, None unread."""
    write_ahead: Callable[[Dispatch], str]
    interrupted: Callable[[], str | None]
    """A takeover, the secure desktop or a lock, as a token; None to go on."""
    clock: Callable[[], float]


@dataclass(frozen=True)
class TaskOutcome:
    status: Status
    reason: str | None
    """A closed token: `commit`, `max_actions`, `takeover`, `secure_field` and so on."""
    dispatched: tuple[Dispatch, ...]
    model_calls: int


def resolve(target: int, listed: Snapshot, latest: Snapshot) -> Element | None:
    """The element `target` named in `listed`, if the latest snapshot still has its RuntimeId
    with the same role and name (§8.1); None otherwise."""
    raise NotImplementedError("3c: resolver")


def action_risk(
    action: ActionDoc, element: Element, *, goal: str, search: SearchFacts | None
) -> Risk:
    """§7.2: the max of the declared commit, deny words, a secure field and text provenance;
    raise-only, so no model field lowers it (§8.2). `search` is the field's facts for an Enter
    or a click that may be a query."""
    raise NotImplementedError("3c: action risk")


def run_task(task: Task, ports: Ports) -> TaskOutcome:
    """Run `task` to an outcome. In step 3 no action at R2 or above is dispatched: the task ends
    `blocked` before the write-ahead."""
    raise NotImplementedError("3c: the v2 loop")
