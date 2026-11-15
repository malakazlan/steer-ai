"""The decide port's prompt inputs (adapter slice 4b; packet 2.9; §7.7; §8.6).

`controller_inputs` turns the runner's `TurnView` into what `controller.decide` takes:
- the listing through `v2_listing`, numbered by task handle ([V2.38] RT-17), and the window
  title: both inside the per-call screen fence;
- the request and the subgoal definitions, checked against the task fence; the definitions sit
  inside its subgoals block;
- the action log, from the runner's code-written history (`history_log`).
`make_decide` is `Ports.decide`: the inputs through `controller.decide`, its provider calls on the
decide worker (`DecideCalls`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from steerai.loop2.controller import ListingRow, LogEntry, decide, v2_listing
from steerai.loop2.decide_port import DecideCalls
from steerai.loop2.loop import Decided, TurnView
from steerai.loop2.task import Status, Subgoal, known_params


@dataclass(frozen=True)
class ControllerInputs:
    """One turn's inputs to `controller.decide`, before the fences are applied."""

    task_goal: str
    subgoal_lines: tuple[str, ...]
    action_log: tuple[LogEntry, ...]
    subgoal_status: tuple[tuple[str, Status], ...]
    window_title: str
    listing_rows: tuple[ListingRow, ...]
    listed_ids: frozenset[int]
    first_turn: bool
    known_subgoals: Mapping[str, frozenset[str]] | None
    unstable: bool
    growing: bool
    young: bool


def controller_inputs(view: TurnView, *, goal: str) -> ControllerInputs:
    observed = view.observed
    snapshot = observed.snapshot
    return ControllerInputs(
        task_goal=goal,
        subgoal_lines=tuple(_subgoal_line(s) for s in view.subgoals),
        action_log=history_log(view.history),
        subgoal_status=tuple(
            (s.id, "done" if s.id in view.proven else s.status) for s in view.subgoals
        ),
        window_title=snapshot.title,
        listing_rows=tuple(v2_listing(snapshot, handles=view.handles)),
        listed_ids=frozenset(view.handles.values()),
        first_turn=view.first_turn,
        known_subgoals=None if view.first_turn else known_params(view.subgoals),
        unstable=not snapshot.stable,
        growing=observed.growing,
        young=observed.young,
    )


def _subgoal_line(subgoal: Subgoal) -> str:
    """`<id> closes=<kind> commit=<commit>: <desc>; <param>="<words>"`: the id and tokens are
    closed; the description (model text) and the params (the request's own words) are data,
    which `build_prompt` keeps inside the task fence."""
    head = f"{subgoal.id} closes={subgoal.closes} commit={subgoal.commit}: {subgoal.desc}"
    params = "; ".join(f'{name}="{span.text}"' for name, span in sorted(subgoal.params.items()))
    return f"{head}; {params}" if params else head


def history_log(history: Sequence[str]) -> tuple[LogEntry, ...]:
    """The action log from the runner's history tokens. Pending [V2.46] (the next commit): no
    line yet."""
    del history
    return ()


def make_decide(calls: DecideCalls, *, goal: str, task_fence: str) -> Callable[[TurnView], Decided]:
    """`Ports.decide` for one task: each turn's inputs through `controller.decide`, with the
    turn's summed usage. `task_fence` is fixed for the task, so the stable blocks stay
    byte-identical across calls; the screen fence is fresh per call (the controller's)."""

    def port(view: TurnView) -> Decided:
        inputs = controller_inputs(view, goal=goal)
        (decision, _record), usage = calls.turn(
            lambda ask: decide(
                ask,
                task_goal=inputs.task_goal,
                subgoal_lines=inputs.subgoal_lines,
                action_log=inputs.action_log,
                subgoal_status=inputs.subgoal_status,
                window_title=inputs.window_title,
                listing_rows=inputs.listing_rows,
                listed_ids=inputs.listed_ids,
                first_turn=inputs.first_turn,
                task_fence=task_fence,
                known_subgoals=inputs.known_subgoals,
                unstable=inputs.unstable,
                growing=inputs.growing,
                young=inputs.young,
            )
        )
        return Decided(decision, usage)

    return port
