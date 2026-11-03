"""T1 step check (design 5.10): did the step actually work, decided model-free against the
before snapshot, polled until a deadline, and recorded as a label of record.

`evaluate` is pure over two snapshots except for the three live predicates (`ValueEquals`,
`Focused`, `State`), which read the resolved target through an injected `LiveReads`; value,
focus and toggle state are not cached properties, and a persisted before snapshot has no
handles. `verify` evaluates the orchestrator's after snapshot first and re-observes only on a
miss, one stability fetch per poll, never past the deadline. A predicate whose typed
observation repeats unchanged is a settled contradiction (`verify_failed`, the readback
contract); `Appeared` and `CountDelta` never settle, they only time out. A false success is
the worst error (it teaches the cache), so unclear means fail.
"""

from __future__ import annotations

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
    Within,
    name_matches,
)
from steerai.verify.scope import Scope, resolve_scope
from steerai.world.elements import Element
from steerai.world.fingerprint import normalise_title
from steerai.world.model import Snapshot, diff, resolve_in

DEADLINE_S = 3.0
POLL_S = 0.25
_EPSILON_S = 1e-6
_TEXT_ROLES: frozenset[str] = frozenset({"Text"})
_UNSET = object()


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
    """The typed observation behind the verdict, for settle detection; never recorded."""


@dataclass(frozen=True, slots=True)
class Context:
    live: LiveReads | None = None
    windows: Sequence[WindowLike] | None = None
    own_pid: int | None = None


def evaluate(  # noqa: PLR0913 - keyword-only live context, each part optional
    predicate: Predicate,
    before: Snapshot,
    after: Snapshot,
    *,
    live: LiveReads | None = None,
    windows: Sequence[WindowLike] | None = None,
    own_pid: int | None = None,
) -> Verdict:
    if before.hwnd != after.hwnd:
        raise ValueError(
            f"snapshots are of different windows ({before.hwnd:#x} vs {after.hwnd:#x})"
        )
    return _evaluate(predicate, before, after, Context(live=live, windows=windows, own_pid=own_pid))


def _evaluate(predicate: Predicate, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    return _EVALUATORS[type(predicate)](predicate, before, after, ctx)


# ---- structural predicates (pure) ---------------------------------------------------------------


def _scoped(scope: Scope, element: Element, role: str, pattern: str) -> bool:
    return (
        element.id in scope.ids
        and element.role == role
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


def _appeared(predicate: Appeared, before: Snapshot, after: Snapshot) -> Verdict:
    scope = resolve_scope(after, predicate.within, excluding=predicate.excluding_anchor)
    if scope.level == 0:
        return _miss(scope, scope.reason or "no_scope")
    new = [
        e
        for e in diff(before, after).appeared
        if _scoped(scope, e, predicate.role, predicate.name_pattern)
    ]
    if new:
        return Verdict(
            holds=True,
            code=None,
            level=scope.level,
            polls=0,
            evidence={"level": scope.level, "matched": len(new)},
        )
    present = any(_scoped(scope, e, predicate.role, predicate.name_pattern) for e in after.elements)
    return _miss(scope, "not_new" if present else "no_match")


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
    remaining = [e for e in matched if not e.runtime_id or e.runtime_id in after_ids]
    if remaining:
        return replace(_miss(scope, "still_present"), state="present")
    return Verdict(
        holds=True,
        code=None,
        level=scope.level,
        polls=0,
        evidence={"level": scope.level, "matched": len(matched)},
        state="absent",
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
    return Verdict(
        holds=holds,
        code=None,
        level=scope_after.level,
        polls=0,
        evidence={
            "level": scope_after.level,
            "before": count_before,
            "after": count_after,
            **({} if holds else {"reason": "count_differs"}),
        },
    )


# ---- live predicates -----------------------------------------------------------------------------


def _target(before: Snapshot, after: Snapshot, target_id: int) -> Element | None:
    try:
        return resolve_in(before, target_id, after)
    except SteerError:
        return None


def _not_found() -> Verdict:
    return Verdict(
        holds=False,
        code=None,
        level=3,
        polls=0,
        evidence={"reason": "target_not_found"},
        state="target_not_found",
    )


def _value_equals(
    predicate: ValueEquals, before: Snapshot, after: Snapshot, ctx: Context
) -> Verdict:
    if ctx.live is None:
        raise ValueError("ValueEquals needs live reads")
    element = _target(before, after, predicate.target_id)
    if element is None:
        return _not_found()
    if element.role in _TEXT_ROLES:
        value = element.name
    else:
        raw = ctx.live.value(after.handle(element.id))
        value = "" if raw is None else str(raw)
    value = normalise_newlines(value)
    holds = value == normalise_newlines(predicate.text)
    evidence = {} if holds else {"reason": "value_differs"}
    return Verdict(holds=holds, code=None, level=3, polls=0, evidence=evidence, state=value)


def _focused(predicate: Focused, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    if ctx.live is None:
        raise ValueError("Focused needs live reads")
    element = _target(before, after, predicate.target_id)
    if element is None:
        return _not_found()
    focused = ctx.live.focused_runtime_id()
    holds = bool(focused) and focused == element.runtime_id
    evidence = {} if holds else {"reason": "not_focused"}
    return Verdict(holds=holds, code=None, level=3, polls=0, evidence=evidence, state=focused)


def _state(predicate: State, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    if ctx.live is None:
        raise ValueError("State needs live reads")
    element = _target(before, after, predicate.target_id)
    if element is None:
        return _not_found()
    handle = after.handle(element.id)
    observed: int | bool | None
    if predicate.wanted == "selected":
        observed = ctx.live.is_selected(handle)
        holds = observed is True
    else:
        observed = ctx.live.toggle_state(handle)
        holds = observed == (1 if predicate.wanted == "on" else 0)
    if observed is None:
        return Verdict(
            holds=False,
            code=None,
            level=3,
            polls=0,
            evidence={"reason": "no_pattern"},
            state="no_pattern",
        )
    evidence = {} if holds else {"reason": "state_differs"}
    return Verdict(holds=holds, code=None, level=3, polls=0, evidence=evidence, state=observed)


# ---- window-level predicates ---------------------------------------------------------------------


def _title_matches(predicate: TitleMatches, after: Snapshot) -> Verdict:
    title = normalise_title(after.title)
    holds = title == normalise_title(predicate.pattern)
    evidence = {} if holds else {"reason": "title_differs"}
    return Verdict(holds=holds, code=None, level=3, polls=0, evidence=evidence, state=title)


def _window_exists(predicate: WindowExists, ctx: Context) -> Verdict:
    if ctx.windows is None:
        raise ValueError("WindowExists needs the top-level windows list")
    matched = [
        w
        for w in ctx.windows
        if (ctx.own_pid is None or w.pid != ctx.own_pid)
        and name_matches(w.title, predicate.name_pattern, scoped=True)
    ]
    holds = bool(matched)
    evidence = {"matched": len(matched)} if holds else {"reason": "no_window"}
    return Verdict(
        holds=holds,
        code=None,
        level=3,
        polls=0,
        evidence=evidence,
        state="present" if holds else "absent",
    )


# ---- conjunctions --------------------------------------------------------------------------------


def _all(predicate: All, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    verdicts = [_evaluate(part, before, after, ctx) for part in predicate.parts]
    failing = next((v for v in verdicts if not v.holds), None)
    states = tuple(v.state for v in verdicts)
    state = None if any(s is None for s in states) else states
    if failing is not None:
        return replace(failing, state=state)
    return Verdict(
        holds=True,
        code=None,
        level=min(v.level for v in verdicts),
        polls=0,
        evidence={"parts": len(verdicts)},
        state=state,
    )


def _any_of(predicate: AnyOf, before: Snapshot, after: Snapshot, ctx: Context) -> Verdict:
    verdicts = [_evaluate(part, before, after, ctx) for part in predicate.parts]
    holding = next((v for v in verdicts if v.holds), None)
    if holding is not None:
        return holding
    states = tuple(v.state for v in verdicts)
    state = None if any(s is None for s in states) else states
    return replace(verdicts[-1], state=state)


_EVALUATORS: dict[type, Callable[[Any, Snapshot, Snapshot, Context], Verdict]] = {
    Appeared: lambda p, b, a, _c: _appeared(p, b, a),
    Disappeared: lambda p, b, a, _c: _disappeared(p, b, a),
    CountDelta: lambda p, b, a, _c: _count_delta(p, b, a),
    ValueEquals: _value_equals,
    Focused: _focused,
    State: _state,
    TitleMatches: lambda p, _b, a, _c: _title_matches(p, a),
    WindowExists: lambda p, _b, _a, c: _window_exists(p, c),
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
    own_pid: int | None = None,
    deadline_s: float = DEADLINE_S,
    poll_s: float = POLL_S,
) -> Verdict:
    """Evaluate against `after`; on a miss re-observe (`observe(stable_window=1)`) every `poll_s`
    until the predicate holds, the observation settles on a contradiction, or the deadline."""
    if poll_s <= 0:
        raise ValueError("poll_s must be > 0")
    if deadline_s < 0:
        raise ValueError("deadline_s must be >= 0")
    ctx = Context(live=live, windows=windows, own_pid=own_pid)
    start = time.monotonic()
    polls = 0
    previous: object = _UNSET
    verdict = _evaluate(predicate, before, after, ctx)
    while not verdict.holds:
        if verdict.state is not None and previous is not _UNSET and previous == verdict.state:
            return replace(
                verdict,
                code=ErrorCode.VERIFY_FAILED,
                polls=polls,
                evidence={**verdict.evidence, "reason": "settled"},
            )
        previous = verdict.state
        remaining = deadline_s - (time.monotonic() - start)
        if remaining <= _EPSILON_S:
            return replace(
                verdict,
                code=ErrorCode.VERIFY_TIMEOUT,
                polls=polls,
                evidence={**verdict.evidence, "reason": "timeout"},
            )
        time.sleep(min(poll_s, remaining))
        polls += 1
        try:
            after = observe(stable_window=1)
        except (OSError, SteerError) as exc:
            extra = {"code": exc.code.value} if isinstance(exc, SteerError) else {}
            return replace(
                verdict,
                code=ErrorCode.VERIFY_TIMEOUT,
                polls=polls,
                evidence={**verdict.evidence, "reason": "observe_failed", **extra},
            )
        verdict = _evaluate(predicate, before, after, ctx)
    return replace(verdict, polls=polls)


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
) -> Label:
    reason = verdict.evidence.get("reason")
    if reason == "observe_failed":
        raise ValueError("no label from a verify that could not observe: observe_failed")
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
            if isinstance(predicate, Appeared) and predicate.excluding_anchor is not None:
                spec["excluding_anchor"] = predicate.excluding_anchor
        case ValueEquals() | Focused() | State():
            spec["target_id"] = predicate.target_id
            if isinstance(predicate, State):
                spec["wanted"] = predicate.wanted
        case All() | AnyOf():
            spec["parts"] = [redact(part) for part in predicate.parts]
        case TitleMatches() | WindowExists():
            pass
    return spec


def _within_spec(within: Within) -> str:
    return within.landmark if within.landmark is not None else f"anchor:{within.anchor_id}"
