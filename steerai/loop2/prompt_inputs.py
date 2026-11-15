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

import dataclasses
import math
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from steerai.loop2 import controller
from steerai.loop2.controller import (
    AUDIT_PROMPT_VERSION,
    TOKENS_PER_BYTE,
    ControllerError,
    DecideRecord,
    FocusState,
    ListingRow,
    build_audit_prompt,
    decide,
    focus_state,
    parse_audit,
    prompt_tokens,
    v2_listing,
)
from steerai.loop2.decide_port import DecideCalls
from steerai.loop2.history import HistoryRecord
from steerai.loop2.listing import LISTING_CAP, Listing, select_listing, words_of
from steerai.loop2.loop import Audited, AuditView, Decided, TokenUsage, TurnView
from steerai.loop2.task import Status, Subgoal, known_params
from steerai.world.effects import Observed

CALIBRATION_MARGIN: Final = 1.1
"""[V2.61]: a turn whose true input tokens beat the bound tightens the task's tokens per byte to
the turn's own ratio times this margin (the margin of `TOKENS_PER_BYTE` over the measured worst)."""


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


def make_decide(
    calls: DecideCalls,
    *,
    goal: str,
    task_fence: str,
    on_rejected: Callable[[dict[str, Any]], None] | None = None,
) -> Callable[[TurnView], Decided]:
    """`Ports.decide` for one task: each turn's inputs through `controller.decide`, with the
    turn's summed usage. `task_fence` is fixed for the task, so the stable blocks stay
    byte-identical across calls; the screen fence is fresh per call (the controller's).
    [V2.61]: the guard may spend one exact count a turn (`DecideCalls.count`); each turn's true
    input tokens are held against the bound of the bytes it sent, and a bound that was low is
    flagged and tightens the ratio for the rest of the task (`calibrated`)."""
    per_byte = TOKENS_PER_BYTE
    turns = 0

    def port(view: TurnView) -> Decided:
        nonlocal per_byte, turns
        turns += 1
        inputs = controller_inputs(view, goal=goal)
        fitted: list[tuple[frozenset[int], int]] = []
        sent: list[int] = []
        spent = False

        def exact(system: list[str], user: list[str]) -> int | None:
            nonlocal spent
            spent = True
            return calls.count(system, user)

        try:
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
                    tokens_per_byte=per_byte,
                    count_exact=exact,
                    on_sent=sent.append,
                    on_rejected=on_rejected,
                    turn=turns,
                )
            )
        except ControllerError as failed:
            per_byte, failed.estimate_low = calibrated(per_byte, sent, failed.usage)
            failed.exact_counted = spent
            raise
        per_byte, low = calibrated(per_byte, sent, usage)
        listed, cut = fitted[-1] if fitted else (inputs.listed_ids, 0)
        tokens = None if usage is None else usage.input_tokens
        first = sent[0] if sent else None
        listing = (
            None if inputs.listing is None else inputs.listing.turn(listed, cut, tokens, first)
        )
        return Decided(decision, usage, listing, exact_counted=spent, estimate_low=low)

    return port


def make_audit(calls: DecideCalls, *, goal: str, task_fence: str) -> Callable[[AuditView], Audited]:
    """`Ports.audit` ([V2.64] One): the request, the proof tokens and the final listing through
    `build_audit_prompt`, one call on the decide worker (kill-aware, its usage summed). A failed
    or unparsed call raises (`ControllerError` carries its usage): the runner vetoes."""

    def port(view: AuditView) -> Audited:
        observed = masked(view.observed, view.typed)
        snapshot = observed.snapshot
        listing = select_listing(  # #404 principal 2: decide's cap and keep set
            snapshot, cap=LISTING_CAP, focus=observed.focus, keep=view.keep, goal_words=view.words
        )
        rows = v2_listing(snapshot, handles=view.handles, focus=observed.focus, listing=listing)
        system, user = build_audit_prompt(
            goal, view.proofs, snapshot.title or "", rows,
            task_fence=task_fence, screen_fence=secrets.token_hex(16),
        )  # fmt: skip
        _within_bound(calls, system, user)  # over it: refused before any call
        try:
            doc, usage = calls.turn(lambda ask: parse_audit(ask(system, user)))
        except ControllerError as failed:
            failed.record = DecideRecord(AUDIT_PROMPT_VERSION, 1, False, True)  # a call was made
            raise
        return Audited(doc.sufficient, doc.reason, usage)

    return port


def _within_bound(calls: DecideCalls, system: list[str], user: list[str]) -> None:
    """#404 principal 2: the audit's prompt under decide's byte bound, with one exact count near
    the ceiling ([V2.61]); over it, or a count that failed, `prompt_too_large` with no record."""
    ceiling = controller.PROMPT_TOKEN_CEILING
    bound = prompt_tokens(system, user)
    if bound <= ceiling:
        return
    exact = calls.count(system, user) if bound <= ceiling * controller.EXACT_BAND else None
    if exact is None or exact > ceiling:
        raise ControllerError("prompt_too_large")


def calibrated(
    per_byte: float, sent: Sequence[int], usage: TokenUsage | None
) -> tuple[float, bool]:
    """[V2.61]: the tokens per byte for the next turn, and whether this turn's bound was low.
    The truth is every input category the provider reported over the turn's calls; the bound is
    each call's bytes at `per_byte`. Unknown usage, or no call, leaves the ratio as it was."""
    if usage is None or not sent:
        return per_byte, False
    truth = usage.input_tokens + usage.cache_write_5m + usage.cache_write_1h + usage.cache_read
    if truth <= sum(math.ceil(size * per_byte) for size in sent):
        return per_byte, False
    return max(per_byte, truth / sum(sent) * CALIBRATION_MARGIN), True


TYPED_TEXT: Final = "[typed_text]"
"""Packet rev 5 item 5: what a free text Steer typed reads as in the audit's view."""


def masked(observed: Observed, texts: Sequence[str]) -> Observed:
    """Packet rev 5 item 5: every free text Steer typed in this task, replaced in each element's
    name and in the window title by `TYPED_TEXT` (matched after NFKC folding and whitespace
    collapsing, ignoring case), so a page that echoes it whole never hands the audit model text.
    A part echo is not caught; the chain eval measures how often one escapes."""
    folded = sorted({_fold(t) for t in texts if t.strip()}, key=len, reverse=True)
    if not folded:
        return observed
    pattern = re.compile("|".join(re.escape(t) for t in folded), re.IGNORECASE)

    def mask(text: str) -> str:
        return pattern.sub(TYPED_TEXT, _fold_keep_case(text))

    snapshot = observed.snapshot
    elements = type(snapshot.elements)(
        dataclasses.replace(e, name=mask(e.name)) for e in snapshot.elements
    )
    title = None if snapshot.title is None else mask(snapshot.title)
    return dataclasses.replace(
        observed, snapshot=dataclasses.replace(snapshot, elements=elements, title=title)
    )


def _fold(text: str) -> str:
    return _fold_keep_case(text).casefold()


def _fold_keep_case(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())
