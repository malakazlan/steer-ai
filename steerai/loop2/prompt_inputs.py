"""The decide port's prompt inputs (adapter slice 4b; packet 2.9; §7.7; §8.6).

`controller_inputs` turns the runner's `TurnView` into what `controller.decide` takes:
- the listing through `v2_listing`, numbered by task handle ([V2.38] RT-17), and the window
  title: both inside the per-call screen fence;
- the request and the subgoal definitions, checked against the task fence; the definitions sit
  inside its subgoals block;
- the action log as the runner wrote it ([V2.46]): action entries and history events, closed
  tokens and task handles only.
`make_decide` is `Ports.decide`: the inputs through `controller.decide`, its provider calls on the
decide worker (`DecideCalls`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from steerai.loop2.controller import FocusState, ListingRow, decide, focus_state, v2_listing
from steerai.loop2.decide_port import DecideCalls
from steerai.loop2.history import HistoryRecord
from steerai.loop2.listing import LISTING_CAP, Listing, select_listing, words_of
from steerai.loop2.loop import Decided, TurnView
from steerai.loop2.task import Status, Subgoal, known_params


@dataclass(frozen=True)
class ControllerInputs:
    """One turn's inputs to `controller.decide`, before the fences are applied."""

    task_goal: str
    subgoal_lines: tuple[str, ...]
    action_log: tuple[HistoryRecord, ...]
    issued: frozenset[int]
    subgoal_status: tuple[tuple[str, Status], ...]
    window_title: str
    listing_rows: tuple[ListingRow, ...]
    listed_ids: frozenset[int]
    first_turn: bool
    known_subgoals: Mapping[str, frozenset[str]] | None
    unstable: bool
    growing: bool
    young: bool
    focus: FocusState | None
    """[V2.52] (b): the header line's focus state; None when a listing row carries it."""
    listing: Listing | None
    """[V2.54]: the rows selected under the cap, with the turn's counts."""
    listing_note: str | None
    """[V2.54]: the header line saying what was cut; None when nothing was."""
    changes_note: str | None
    """[V2.56]: the header line when this turn has no comparison or its states were unread."""


CHANGES_NOTE = {
    "no_comparison": "changes: no comparison this turn",
    "states_unread": "changes: states unread",
}
"""[V2.56]: the header line for a turn without a full comparison."""


def controller_inputs(view: TurnView, *, goal: str, cap: int = LISTING_CAP) -> ControllerInputs:
    observed = view.observed
    snapshot = observed.snapshot
    open_params = (
        span.text for s in view.subgoals if s.id not in view.proven for span in s.params.values()
    )
    keep = view.keep | view.new | view.changed  # [V2.56]: both feed always-keep
    listing = select_listing(
        snapshot, cap=cap, focus=observed.focus, keep=keep, goal_words=words_of(open_params)
    )
    rows = v2_listing(
        snapshot,
        handles=view.handles,
        focus=observed.focus,
        listing=listing,
        new=view.new,
        changed=view.changed,
    )
    return ControllerInputs(
        task_goal=goal,
        subgoal_lines=tuple(_subgoal_line(s) for s in view.subgoals),
        action_log=view.log,
        issued=view.issued,
        subgoal_status=tuple(
            (s.id, "done" if s.id in view.proven else s.status) for s in view.subgoals
        ),
        window_title=snapshot.title,
        listing_rows=tuple(rows),
        listed_ids=frozenset(r.handle for r in rows if r.handle is not None),
        first_turn=view.first_turn,
        known_subgoals=None if view.first_turn else known_params(view.subgoals),
        unstable=not snapshot.stable,
        growing=observed.growing,
        young=observed.young,
        focus=focus_state(snapshot, observed.focus),
        listing=listing,
        listing_note=listing.note(),
        changes_note=CHANGES_NOTE.get(view.changes),
    )


def _subgoal_line(subgoal: Subgoal) -> str:
    """`<id> closes=<kind> commit=<commit>: <desc>; <param>="<words>"`: the id and tokens are
    closed; the description (model text) and the params (the request's own words) are data,
    which `build_prompt` keeps inside the task fence."""
    head = f"{subgoal.id} closes={subgoal.closes} commit={subgoal.commit}: {subgoal.desc}"
    params = "; ".join(f'{name}="{span.text}"' for name, span in sorted(subgoal.params.items()))
    return f"{head}; {params}" if params else head


def make_decide(calls: DecideCalls, *, goal: str, task_fence: str) -> Callable[[TurnView], Decided]:
    """`Ports.decide` for one task: each turn's inputs through `controller.decide`, with the
    turn's summed usage. `task_fence` is fixed for the task, so the stable blocks stay
    byte-identical across calls; the screen fence is fresh per call (the controller's)."""

    def port(view: TurnView) -> Decided:
        inputs = controller_inputs(view, goal=goal)
        fitted: list[tuple[frozenset[int], int]] = []
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
                issued=inputs.issued,
                focus=inputs.focus,
                listing_note=inputs.listing_note,
                changes_note=inputs.changes_note,
                on_fit=lambda shown, cut: fitted.append((shown, cut)),
                listing_note_after=None if inputs.listing is None else inputs.listing.note,
                keep_cap=LISTING_CAP if inputs.listing is None else inputs.listing.cap,
            )
        )
        listed, cut = fitted[-1] if fitted else (inputs.listed_ids, 0)
        tokens = None if usage is None else usage.input_tokens
        listing = None if inputs.listing is None else inputs.listing.turn(listed, cut, tokens)
        return Decided(decision, usage, listing)

    return port
