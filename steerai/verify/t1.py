"""T1 step check (design 5.10, [v1.2]): did the step actually work, decided model-free against
the before snapshot, polled until a deadline, and recorded as a label of record.

A new RuntimeId is necessary evidence, never sufficient (verifier review VF-02..06, 13):
`Appeared` also needs the count of full-string matches in scope to rise by exactly the expected
number, the scope container to survive with bounded churn, the new element to sit after every
surviving element of its role (a scroll realising old rows is not an effect), no element of that
role to vanish in the same diff, and no failure status under the new element. Matching is always
full-string. `Disappeared` needs the target's siblings and container to survive (a collapse or a
filter is not a delete). An unstable snapshot on either side yields no verdict.

`evaluate` is pure over two snapshots except for the live predicates (`ValueEquals`, `Focused`,
`State`), which read the resolved target through an injected `LiveReads`. `verify` evaluates the
orchestrator's after snapshot first and re-observes only on a miss, one stability fetch per poll,
never past the deadline (an observe is not started with less time left than the last one took).
`verify_failed` needs positive contradiction evidence (a duplicate effect, a failure status, a
value that is empty or unchanged, a definite toggle or focus); "not there yet" only times out.
Outcomes are three-valued: pass, fail, or no label (`label_of_record` returns None).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from steerai.action.sanitize import normalise_newlines
from steerai.errors import ErrorCode, SteerError
from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    Focused,
    Predicate,
    State,
    TitleMatches,
    ValueEquals,
    WindowExists,
    WindowGone,
    Within,
    name_matches,
)
from steerai.verify.scope import Scope, resolve_scope
from steerai.world.elements import Element
from steerai.world.model import Snapshot, resolve_in

DEADLINE_S = 3.0
POLL_S = 0.25
CHURN_TOLERANCE = 1
"""Unrelated new elements of the template role allowed next to the effect (a date separator);
above it the scope re-rendered (VF-03). Unmeasured; one is the smallest useful value."""
_EPSILON_S = 1e-6
_TEXT_ROLES: frozenset[str] = frozenset({"Text"})
_FAILED_TOKENS = ("failed", "retry", "not delivered", "not sent")
_COUNTER = re.compile(r"^\s*\(\d+\)\s*|\s*\(\d+\)\s*$")
_UNSET = object()
NO_LABEL_REASONS: frozenset[str] = frozenset(
    {
        "unstable",
        "ambiguous_landmark",
        "scope_churn",
        "scope_shifted",
        "observe_failed",
        "evaluate_failed",
        "secure_target",
    }
)


class LiveReads(Protocol):
    """COM reads the pure snapshot cannot answer; `handle` is the snapshot's live element."""

    def value(self, handle: Any) -> str | None: ...
    def toggle_state(self, handle: Any) -> int | None: ...
    def is_selected(self, handle: Any) -> bool | None: ...
    def focused_runtime_id(self) -> tuple[int, ...]: ...


class WindowLike(Protocol):
    @property
    def title(self) -> str: ...
    @property
    def hwnd(self) -> int: ...
    @property
    def pid(self) -> int: ...


@dataclass(frozen=True, slots=True)
class Verdict:
    holds: bool
    code: ErrorCode | None
    level: int
    polls: int
    evidence: dict[str, Any]
    """Whitelisted tokens and counts only: never a name, pattern or value."""
    state: Hashable | None = None
    """The typed observation behind a contradiction, for settle detection; never recorded.
    None means the predicate can only time out from this observation."""


@dataclass(frozen=True, slots=True)
class Context:
    live: LiveReads | None = None
    windows: Sequence[WindowLike] | None = None
    windows_before: Sequence[WindowLike] | None = None
    own_pid: int | None = None


def evaluate(  # noqa: PLR0913 - keyword-only live context, each part optional
    predicate: Predicate,
    before: Snapshot,
    after: Snapshot,
    *,
    live: LiveReads | None = None,
    windows: Sequence[WindowLike] | None = None,
    windows_before: Sequence[WindowLike] | None = None,
    own_pid: int | None = None,
) -> Verdict:
    ctx = Context(live=live, windows=windows, windows_before=windows_before, own_pid=own_pid)
    return _evaluate(predicate, before, after, ctx)


def _evaluate(predicate: Predicate, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    if before.hwnd != after.hwnd:
        raise ValueError(
            f"snapshots are of different windows ({before.hwnd:#x} vs {after.hwnd:#x})"
        )
    if before.pid and after.pid and before.pid != after.pid:
        raise SteerError(ErrorCode.APP_CRASHED, "a different process owns the window now")
    if not before.stable or not after.stable:
        return Verdict(
            holds=False, code=None, level=0, polls=0, evidence={"level": 0, "reason": "unstable"}
        )
    return _EVALUATORS[type(predicate)](predicate, before, after, ctx)


# ---- structural predicates (pure) ---------------------------------------------------------------


def _scoped(scope: Scope, element: Element, role: str, pattern: str) -> bool:
    return (
        element.id in scope.ids
        and element.role == role
        and element.has_area
        and name_matches(element.name, pattern, scoped=not scope.strict)
    )


def _miss(scope: Scope, reason: str, **extra: Any) -> Verdict:
    return Verdict(
        holds=False,
        code=None,
        level=scope.level,
        polls=0,
        evidence={"level": scope.level, "reason": reason, **extra},
    )


def _contradiction(scope: Scope, reason: str) -> Verdict:
    return replace(_miss(scope, reason), state=reason)


def _container_survived(scope: Scope, source: Snapshot, other: Snapshot) -> bool:
    """The scoped container (levels 1 and 2) keeps its RuntimeId across the two snapshots."""
    if scope.container_id is None:
        return True
    container = source.element(scope.container_id)
    if not container.runtime_id:
        return False
    return any(e.runtime_id == container.runtime_id for e in other.elements)


@dataclass(frozen=True, slots=True)
class _Appearance:
    """What the after snapshot shows for an `Appeared` check, all in scope and of the role."""

    in_role: list[Element]
    new_role: list[Element]
    new_matches: list[Element]
    count_before: int
    count_after: int
    surviving_ids: list[int]
    vanished: bool


def _appearance(
    predicate: Appeared, before: Snapshot, after: Snapshot, scope: Scope
) -> _Appearance:
    scope_before = resolve_scope(before, predicate.within, excluding=predicate.excluding_anchor)
    before_ids = {e.runtime_id for e in before.elements if e.runtime_id}
    after_ids = {e.runtime_id for e in after.elements if e.runtime_id}
    in_role = [e for e in after.elements if e.id in scope.ids and e.role == predicate.role]
    new_role = [e for e in in_role if e.runtime_id and e.runtime_id not in before_ids]
    return _Appearance(
        in_role=in_role,
        new_role=new_role,
        new_matches=[
            e for e in new_role if _scoped(scope, e, predicate.role, predicate.name_pattern)
        ],
        count_before=sum(
            1
            for e in before.elements
            if _scoped(scope_before, e, predicate.role, predicate.name_pattern)
        ),
        count_after=sum(
            1 for e in after.elements if _scoped(scope, e, predicate.role, predicate.name_pattern)
        ),
        surviving_ids=[e.id for e in in_role if e.runtime_id in before_ids],
        vanished=any(
            e.id in scope_before.ids
            and e.role == predicate.role
            and bool(e.runtime_id)
            and e.runtime_id not in after_ids
            for e in before.elements
        ),
    )


def _appeared(predicate: Appeared, before: Snapshot, after: Snapshot) -> Verdict:
    scope = resolve_scope(after, predicate.within, excluding=predicate.excluding_anchor)
    if scope.level == 0:
        return _miss(scope, scope.reason or "no_scope")
    if not _container_survived(scope, after, before):
        return _miss(scope, "scope_churn")
    seen = _appearance(predicate, before, after, scope)
    if not seen.new_matches:
        present = any(
            _scoped(scope, e, predicate.role, predicate.name_pattern) for e in after.elements
        )
        return _miss(scope, "not_new" if present else "no_match")
    delta = seen.count_after - seen.count_before
    first_new = min(e.id for e in seen.new_matches)
    shifted = seen.vanished or (bool(seen.surviving_ids) and first_new < max(seen.surviving_ids))
    # (condition, reason, positive contradiction), checked in order; the first hit decides.
    rules = (
        (
            len(seen.new_matches) > predicate.expected or delta > predicate.expected,
            "duplicate_effect",
            True,
        ),
        (delta < predicate.expected, "count_unchanged", False),
        (len(seen.new_role) > predicate.expected + CHURN_TOLERANCE, "scope_churn", False),
        (shifted, "scope_shifted", False),
        (any(_has_failed_status(e, after) for e in seen.new_matches), "failed_status", True),
    )
    for condition, reason, contradiction in rules:
        if condition:
            return _contradiction(scope, reason) if contradiction else _miss(scope, reason)
    return Verdict(
        holds=True,
        code=None,
        level=scope.level,
        polls=0,
        evidence={"level": scope.level, "matched": len(seen.new_matches)},
    )


def _has_failed_status(element: Element, snapshot: Snapshot) -> bool:
    for child in snapshot.elements:
        if child.parent_id == element.id:
            name = child.name.casefold()
            if any(token in name for token in _FAILED_TOKENS):
                return True
    return False


def _disappeared(predicate: Disappeared, before: Snapshot, after: Snapshot) -> Verdict:
    scope = resolve_scope(before, predicate.within)
    if scope.level == 0:
        return _miss(scope, scope.reason or "no_scope")
    matched = [
        e for e in before.elements if _scoped(scope, e, predicate.role, predicate.name_pattern)
    ]
    if not matched:
        return _miss(scope, "absent_before")
    after_ids = {e.runtime_id for e in after.elements if e.runtime_id}
    by_id = {e.id: e for e in before.elements}
    matched_ids = {e.id for e in matched}
    parents = [by_id[e.parent_id] for e in matched if e.parent_id in by_id]
    siblings = [
        e
        for e in before.elements
        if e.parent_id in {q.id for q in parents} and e.id not in matched_ids and e.runtime_id
    ]
    rules = (
        (
            not _container_survived(scope, before, after)
            or after.node_count * 2 < before.node_count
            or any(q.runtime_id not in after_ids for q in parents),
            "scope_churn",
        ),
        (any(not e.runtime_id or e.runtime_id in after_ids for e in matched), "still_present"),
        (any(e.runtime_id not in after_ids for e in siblings), "siblings_changed"),
    )
    for condition, reason in rules:
        if condition:
            return _miss(scope, reason)
    return Verdict(
        holds=True,
        code=None,
        level=scope.level,
        polls=0,
        evidence={"level": scope.level, "matched": len(matched)},
    )


def _count_delta(predicate: CountDelta, before: Snapshot, after: Snapshot) -> Verdict:
    scope_before = resolve_scope(before, predicate.within)
    scope_after = resolve_scope(after, predicate.within)
    if scope_before.level == 0 or scope_after.level == 0:
        return _miss(scope_after, "ambiguous_landmark")
    count_before = sum(
        1 for e in before.elements if e.id in scope_before.ids and e.role == predicate.role
    )
    count_after = sum(
        1 for e in after.elements if e.id in scope_after.ids and e.role == predicate.role
    )
    holds = count_after - count_before == predicate.delta
    evidence: dict[str, Any] = {
        "level": scope_after.level,
        "before": count_before,
        "after": count_after,
    }
    if not holds:
        evidence["reason"] = "count_differs"
    return Verdict(holds=holds, code=None, level=scope_after.level, polls=0, evidence=evidence)


# ---- live predicates -----------------------------------------------------------------------------


def _target(before: Snapshot, after: Snapshot, target_id: int) -> Element | None:
    try:
        return resolve_in(before, target_id, after)
    except SteerError:
        return None


def _plain(reason: str, *, settles: bool = False) -> Verdict:
    return Verdict(
        holds=False,
        code=None,
        level=3,
        polls=0,
        evidence={"reason": reason},
        state=reason if settles else None,
    )


def _live(ctx: Context, name: str) -> LiveReads:
    if ctx.live is None:
        raise ValueError(f"{name} needs live reads")
    return ctx.live


def _read_value(element: Element, snapshot: Snapshot, live: LiveReads) -> str:
    if element.role in _TEXT_ROLES:
        return normalise_newlines(element.name)
    raw = live.value(snapshot.handle(element.id))
    return normalise_newlines("" if raw is None else str(raw))


def _value_equals(
    predicate: ValueEquals, before: Snapshot, after: Snapshot, ctx: Context
) -> Verdict:
    live = _live(ctx, "ValueEquals")
    if before.element(predicate.target_id).secure:
        return _plain("secure_target")
    element = _target(before, after, predicate.target_id)
    if element is None:
        return _plain("target_not_found", settles=True)
    value = _read_value(element, after, live)
    if value == normalise_newlines(predicate.text):
        return Verdict(holds=True, code=None, level=3, polls=0, evidence={})
    if value == "":
        return _plain("value_empty", settles=True)
    if len(before.handles) == len(before.elements):
        try:
            unchanged = _read_value(before.element(predicate.target_id), before, live) == value
        except (OSError, SteerError):
            unchanged = False
        if unchanged:
            return _plain("value_unchanged", settles=True)
    return _plain("value_differs")


def _focused(predicate: Focused, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    live = _live(ctx, "Focused")
    element = _target(before, after, predicate.target_id)
    if element is None:
        return _plain("target_not_found", settles=True)
    focused = live.focused_runtime_id()
    if focused and focused == element.runtime_id:
        return Verdict(holds=True, code=None, level=3, polls=0, evidence={})
    return replace(_plain("not_focused"), state=focused)


def _state(predicate: State, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    live = _live(ctx, "State")
    element = _target(before, after, predicate.target_id)
    if element is None:
        return _plain("target_not_found", settles=True)
    handle = after.handle(element.id)
    observed: int | bool | None
    if predicate.wanted == "selected":
        observed = live.is_selected(handle)
        holds = observed is True
    else:
        observed = live.toggle_state(handle)
        holds = observed == (1 if predicate.wanted == "on" else 0)
    if observed is None:
        return _plain("no_pattern", settles=True)
    if holds:
        return Verdict(holds=True, code=None, level=3, polls=0, evidence={})
    return replace(_plain("state_differs"), state=observed)


# ---- window-level predicates ---------------------------------------------------------------------


def _title_key(title: str) -> str:
    """Leading and trailing counters stripped, casefolded; digits are content (VF-31)."""
    return _COUNTER.sub("", _COUNTER.sub("", title)).strip().casefold()


def _title_matches(predicate: TitleMatches, before: Snapshot, after: Snapshot) -> Verdict:
    wanted = _title_key(predicate.pattern)
    title = _title_key(after.title)
    if wanted in _title_key(before.title):
        return _plain("already_true")
    if wanted in title:
        return Verdict(holds=True, code=None, level=3, polls=0, evidence={})
    if title == _title_key(before.title):
        return _plain("title_unchanged", settles=True)
    return _plain("title_differs")


def _windows(ctx: Context, name: str) -> Sequence[WindowLike]:
    if ctx.windows is None:
        raise ValueError(f"{name} needs the top-level windows list")
    return ctx.windows


def _matching_windows(
    windows: Sequence[WindowLike], pattern: str, own_pid: int | None
) -> list[WindowLike]:
    return [
        w
        for w in windows
        if (own_pid is None or w.pid != own_pid) and name_matches(w.title, pattern, scoped=True)
    ]


def _window_exists(predicate: WindowExists, ctx: Context) -> Verdict:
    matched = _matching_windows(_windows(ctx, "WindowExists"), predicate.name_pattern, ctx.own_pid)
    reason = "no_window"
    if ctx.windows_before is not None:
        seen = {w.hwnd for w in ctx.windows_before}
        matched = [w for w in matched if w.hwnd not in seen]
        reason = "not_new"
    if matched:
        return Verdict(holds=True, code=None, level=3, polls=0, evidence={"matched": len(matched)})
    return _plain(reason)


def _window_gone(predicate: WindowGone, ctx: Context) -> Verdict:
    if ctx.windows_before is None:
        raise ValueError("WindowGone needs the windows list taken before the step")
    was = _matching_windows(ctx.windows_before, predicate.name_pattern, ctx.own_pid)
    if not was:
        return _plain("absent_before")
    now = {w.hwnd for w in _windows(ctx, "WindowGone")}
    if any(w.hwnd in now for w in was):
        return _plain("still_present")
    return Verdict(holds=True, code=None, level=3, polls=0, evidence={"matched": len(was)})


# ---- conjunctions --------------------------------------------------------------------------------


def _combined_state(verdicts: list[Verdict]) -> Hashable | None:
    states = tuple(v.state for v in verdicts)
    return None if any(s is None for s in states) else states


def _all(predicate: All, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    verdicts = [_evaluate(part, before, after, ctx) for part in predicate.parts]
    failing = next((v for v in verdicts if not v.holds), None)
    if failing is not None:
        return replace(failing, state=_combined_state(verdicts))
    return Verdict(
        holds=True,
        code=None,
        level=min(v.level for v in verdicts),
        polls=0,
        evidence={"parts": len(verdicts)},
    )


def _any_of(predicate: AnyOf, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    verdicts = [_evaluate(part, before, after, ctx) for part in predicate.parts]
    holding = next((v for v in verdicts if v.holds), None)
    if holding is not None:
        return holding
    return replace(verdicts[-1], state=_combined_state(verdicts))


_EVALUATORS: dict[type, Callable[[Any, Snapshot, Snapshot, Context], Verdict]] = {
    Appeared: lambda p, b, a, _c: _appeared(p, b, a),
    Disappeared: lambda p, b, a, _c: _disappeared(p, b, a),
    CountDelta: lambda p, b, a, _c: _count_delta(p, b, a),
    ValueEquals: _value_equals,
    Focused: _focused,
    State: _state,
    TitleMatches: lambda p, b, a, _c: _title_matches(p, b, a),
    WindowExists: lambda p, _b, _a, c: _window_exists(p, c),
    WindowGone: lambda p, _b, _a, c: _window_gone(p, c),
    All: _all,
    AnyOf: _any_of,
}


# ---- verify: poll until the deadline -------------------------------------------------------------


def verify(  # noqa: PLR0913 - keyword-only context and timing knobs with measured defaults
    predicate: Predicate,
    before: Snapshot,
    after: Snapshot,
    observe: Callable[..., Snapshot],
    *,
    live: LiveReads | None = None,
    windows: Sequence[WindowLike] | None = None,
    windows_before: Sequence[WindowLike] | None = None,
    own_pid: int | None = None,
    deadline_s: float = DEADLINE_S,
    poll_s: float = POLL_S,
) -> Verdict:
    """Evaluate against `after`; on a miss re-observe (`observe(stable_window=1)`) until the
    predicate holds, the contradiction settles (two equal typed observations), or the deadline,
    which bounds the whole call including the observes."""
    if poll_s <= 0:
        raise ValueError("poll_s must be > 0")
    if deadline_s < 0:
        raise ValueError("deadline_s must be >= 0")
    ctx = Context(live=live, windows=windows, windows_before=windows_before, own_pid=own_pid)
    start = time.monotonic()
    polls = 0
    previous: object = _UNSET
    last_observe_s = 0.0
    try:
        verdict = _evaluate(predicate, before, after, ctx)
    except SteerError as exc:
        return _no_verdict(exc.code, polls, "evaluate_failed")
    while not verdict.holds:
        if verdict.state is not None and previous is not _UNSET and previous == verdict.state:
            return replace(verdict, code=ErrorCode.VERIFY_FAILED, polls=polls)
        previous = verdict.state
        remaining = deadline_s - (time.monotonic() - start)
        if remaining <= _EPSILON_S or remaining < last_observe_s:
            return replace(verdict, code=ErrorCode.VERIFY_TIMEOUT, polls=polls)
        if last_observe_s < poll_s:
            time.sleep(min(poll_s - last_observe_s, remaining))
        polls += 1
        started = time.monotonic()
        try:
            after = observe(stable_window=1)
            verdict = _evaluate(predicate, before, after, ctx)
        except (OSError, SteerError) as exc:
            code = exc.code if isinstance(exc, SteerError) else ErrorCode.VERIFY_TIMEOUT
            return _no_verdict(code, polls, "observe_failed")
        last_observe_s = time.monotonic() - started
    return replace(verdict, polls=polls)


def _no_verdict(code: ErrorCode, polls: int, reason: str) -> Verdict:
    return Verdict(holds=False, code=code, level=0, polls=polls, evidence={"reason": reason})


# ---- labels of record ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Label:
    """A T1 verdict as a label of record (5.10 [v0.3]): model-free, redacted, keyed by action."""

    kind: Literal["t1"]
    holds: bool
    action_key: str
    predicate: dict[str, Any]
    before_id: str
    after_id: str
    level: int
    code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "holds": self.holds,
            "action_key": self.action_key,
            "predicate": dict(self.predicate),
            "before_id": self.before_id,
            "after_id": self.after_id,
            "level": self.level,
            "code": self.code,
        }


def label_of_record(
    verdict: Verdict, *, predicate: Predicate, action_key: str, before: Snapshot, after: Snapshot
) -> Label | None:
    """Pass or fail as a label of record, or None when the verify could not decide: an unstable
    or churned scope, an ambiguous landmark, a failed observe, a secure target, a crashed app.
    The cache and the training set never learn from a None (VF-14/16)."""
    if verdict.evidence.get("reason") in NO_LABEL_REASONS:
        return None
    if verdict.code not in (None, ErrorCode.VERIFY_FAILED, ErrorCode.VERIFY_TIMEOUT):
        return None
    return Label(
        kind="t1",
        holds=verdict.holds,
        action_key=action_key,
        predicate=redact(predicate),
        before_id=before.id,
        after_id=after.id,
        level=verdict.level,
        code=verdict.code.value if verdict.code else None,
    )


def redact(predicate: Predicate) -> dict[str, Any]:
    """The predicate's shape without its patterns or values."""
    spec: dict[str, Any] = {"kind": type(predicate).__name__}
    match predicate:
        case Appeared() | Disappeared() | CountDelta():
            spec["role"] = predicate.role
            if predicate.within is not None:
                spec["within"] = _within_spec(predicate.within)
            if isinstance(predicate, CountDelta):
                spec["delta"] = predicate.delta
            if isinstance(predicate, Appeared):
                if predicate.excluding_anchor is not None:
                    spec["excluding_anchor"] = predicate.excluding_anchor
                if predicate.expected != 1:
                    spec["expected"] = predicate.expected
        case ValueEquals() | Focused() | State():
            spec["target_id"] = predicate.target_id
            if isinstance(predicate, State):
                spec["wanted"] = predicate.wanted
        case All() | AnyOf():
            spec["parts"] = [redact(part) for part in predicate.parts]
        case TitleMatches() | WindowExists() | WindowGone():
            pass
    return spec


def _within_spec(within: Within) -> str:
    return within.landmark if within.landmark is not None else f"anchor:{within.anchor_id}"
