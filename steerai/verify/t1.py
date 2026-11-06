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
_PENDING_TOKENS = ("sending", "pending", "queued")
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
        "siblings_changed",
        "anchor_vanished",
        "anchor_transient",
        "target_not_found",
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
    after_id: str | None = None
    """The snapshot the verdict was decided on (the last poll, not the caller's after)."""


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
            holds=False,
            code=None,
            level=0,
            polls=0,
            evidence={"level": 0, "reason": "unstable"},
            after_id=after.id,
        )
    verdict = _EVALUATORS[type(predicate)](predicate, before, after, ctx)
    return replace(verdict, after_id=after.id)


# ---- structural predicates (pure) ---------------------------------------------------------------


_PAGE_COUNTER = re.compile(r"^\(\d{1,4}\)\s+")
"""A leading notification counter a site puts before a page's name ("(3) Inbox"); the
`open` title rule (`steerai.action.launch`), never a regular expression from a plan."""


def _scoped(scope: Scope, element: Element, role: str, pattern: str, *, page: bool = False) -> bool:
    if not (element.id in scope.ids and element.role == role and element.has_area):
        return False
    if name_matches(element.name, pattern, scoped=not scope.strict):
        return True
    # a page's name may carry a notification counter in front, "(1) ": the `open`
    # title rule, for the replacing form only (probe yt-click 2026-11-06)
    counted = _PAGE_COUNTER.match(element.name)
    return (
        page
        and counted is not None
        and name_matches(element.name[counted.end() :], pattern, scoped=not scope.strict)
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


def _translate(before: Snapshot, after: Snapshot, element_id: int | None) -> int | None:
    """A before-snapshot element id as the after snapshot's id for the same element (RuntimeId,
    role and name), or None when it vanished. Element ids are positional, so an insertion
    earlier in tree order shifts every later id onto another element."""
    if element_id is None:
        return None
    try:
        return resolve_in(before, element_id, after).id
    except SteerError:
        return None


class _AnchorVanishedError(Exception):
    pass


def _translate_within(before: Snapshot, after: Snapshot, within: Within | None) -> Within | None:
    if within is None or within.anchor_id is None:
        return within
    translated = _translate(before, after, within.anchor_id)
    if translated is None:
        raise _AnchorVanishedError
    return Within(anchor_id=translated)


@dataclass(frozen=True, slots=True)
class _Appearance:
    """What the after snapshot shows for an `Appeared` check, all in scope and of the role."""

    in_role: list[Element]
    new_role: list[Element]
    new_matches: list[Element]
    count_before: int
    count_after: int
    surviving_ids: list[int]
    vanished: int
    """Elements of the role in the before scope whose RuntimeId is gone after."""


def _appearance(
    predicate: Appeared, before: Snapshot, after: Snapshot, scope: Scope
) -> _Appearance:
    scope_before = resolve_scope(before, predicate.within, excluding=predicate.excluding_anchor)
    before_ids = {e.runtime_id for e in before.elements if e.runtime_id}
    after_ids = {e.runtime_id for e in after.elements if e.runtime_id}
    # the replacing form counts top-level elements of the role only: a page's embedded frames
    # (ads, sign-in promos) are neither predecessors nor churn
    skip_after = _nested_ids(after, predicate.role) if predicate.replaces else set()
    skip_before = _nested_ids(before, predicate.role) if predicate.replaces else set()
    in_role = [
        e
        for e in after.elements
        if e.id in scope.ids and e.role == predicate.role and e.id not in skip_after
    ]
    new_role = [e for e in in_role if e.runtime_id and e.runtime_id not in before_ids]
    page = predicate.replaces
    new_matches = [
        e for e in new_role if _scoped(scope, e, predicate.role, predicate.name_match, page=page)
    ]
    # The effect's own subtree (a bubble's timestamp and status texts) is not unrelated churn.
    owned = _descendant_ids(after, [e.id for e in new_matches])
    return _Appearance(
        in_role=in_role,
        new_role=[e for e in new_role if e.id not in owned],
        new_matches=new_matches,
        count_before=sum(
            1
            for e in before.elements
            if _scoped(scope_before, e, predicate.role, predicate.name_match, page=page)
        ),
        count_after=sum(
            1
            for e in after.elements
            if _scoped(scope, e, predicate.role, predicate.name_match, page=page)
        ),
        surviving_ids=[e.id for e in in_role if e.runtime_id in before_ids],
        vanished=sum(
            1
            for e in before.elements
            if e.id in scope_before.ids
            and e.id not in skip_before
            and e.role == predicate.role
            and bool(e.runtime_id)
            and e.runtime_id not in after_ids
        ),
    )


def _nested_ids(snapshot: Snapshot, role: str) -> set[int]:
    """Ids of elements of `role` that sit under another element of `role`."""
    by_id = {e.id: e for e in snapshot.elements}
    nested: set[int] = set()
    for element in snapshot.elements:
        if element.role != role:
            continue
        parent = element.parent_id
        while parent is not None:
            ancestor = by_id.get(parent)
            if ancestor is None:
                break
            if ancestor.role == role:
                nested.add(element.id)
                break
            parent = ancestor.parent_id
    return nested


def _after_scope(predicate: Appeared, before: Snapshot, after: Snapshot) -> Scope:
    """The predicate's scope in the after snapshot, anchors resolved by identity."""
    try:
        within = _translate_within(before, after, predicate.within)
        excluding = _translate(before, after, predicate.excluding_anchor)
    except _AnchorVanishedError:
        return Scope(level=0, reason="anchor_vanished")
    if predicate.excluding_anchor is not None and excluding is None:
        return Scope(level=0, reason="anchor_vanished")
    return resolve_scope(after, within, excluding=excluding)


def _appeared(predicate: Appeared, before: Snapshot, after: Snapshot) -> Verdict:
    scope = _after_scope(predicate, before, after)
    if scope.level == 0:
        return _miss(scope, scope.reason or "no_scope")
    if not _container_survived(scope, after, before):
        return _miss(scope, "scope_churn")
    seen = _appearance(predicate, before, after, scope)
    if not seen.new_matches:
        renamed = _renamed_in_place(predicate, before, after, scope) if predicate.replaces else []
        if len(renamed) == 1:
            in_place = {"level": scope.level, "matched": 1, "renamed": 1}
            return Verdict(holds=True, code=None, level=scope.level, polls=0, evidence=in_place)
        present = any(
            _scoped(scope, e, predicate.role, predicate.name_match, page=predicate.replaces)
            for e in after.elements
        )
        return _miss(scope, "not_new" if present else "no_match")
    delta = seen.count_after - seen.count_before
    if predicate.replaces:
        # the navigation form: the predecessor's vanishing is the effect, its order is moot
        shifted = seen.vanished > 1
        replaced = (seen.vanished == 0, "not_replaced", False)
    else:
        shifted = seen.vanished > 0 or _out_of_order(predicate, seen)
        replaced = (False, "not_replaced", False)
    # (condition, reason, positive contradiction), checked in order; the first hit decides.
    # No-label conditions come first so a re-rendered scope never yields a failed label.
    rules = (
        (len(seen.new_role) > predicate.expected + CHURN_TOLERANCE, "scope_churn", False),
        (shifted, "scope_shifted", False),
        replaced,
        (
            len(seen.new_matches) > predicate.expected or delta > predicate.expected,
            "duplicate_effect",
            True,
        ),
        (delta < predicate.expected, "count_unchanged", False),
        (
            # a page is not a message: a replacing Document's content is never a status
            not predicate.replaces
            and any(
                _has_status(e, after, _PENDING_TOKENS, predicate.name_pattern)
                for e in seen.new_matches
            ),
            "pending_status",
            False,
        ),
        (
            not predicate.replaces
            and any(
                _has_status(e, after, _FAILED_TOKENS, predicate.name_pattern)
                for e in seen.new_matches
            ),
            "failed_status",
            True,
        ),
    )
    for condition, reason, contradiction in rules:
        if condition:
            return _contradiction(scope, reason) if contradiction else _miss(scope, reason)
    evidence: dict[str, Any] = {"level": scope.level, "matched": len(seen.new_matches)}
    if predicate.replaces:
        evidence["replaced"] = seen.vanished
    return Verdict(holds=True, code=None, level=scope.level, polls=0, evidence=evidence)


def _renamed_in_place(
    predicate: Appeared, before: Snapshot, after: Snapshot, scope: Scope
) -> list[Element]:
    """A single-page app navigates by renaming its top-level page element: same RuntimeId, a
    name that did not match before and matches now (live replan run 2026-11-06: YouTube's
    results Document became the watch page's). Only the replacing form asks this."""
    whole_before = resolve_scope(before, None)
    nested_before = _nested_ids(before, predicate.role)
    was = {
        e.runtime_id: e
        for e in before.elements
        if e.runtime_id and e.role == predicate.role and e.id not in nested_before
    }
    nested_after = _nested_ids(after, predicate.role)
    return [
        e
        for e in after.elements
        if e.id not in nested_after
        and e.runtime_id in was
        and _scoped(scope, e, predicate.role, predicate.name_match, page=True)
        and not _scoped(
            whole_before, was[e.runtime_id], predicate.role, predicate.name_match, page=True
        )
    ]


def _descendant_ids(snapshot: Snapshot, roots: list[int]) -> set[int]:
    children: dict[int | None, list[int]] = {}
    for element in snapshot.elements:
        children.setdefault(element.parent_id, []).append(element.id)
    ids: set[int] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        for child in children.get(node, []):
            if child not in ids:
                ids.add(child)
                stack.append(child)
    return ids


def _out_of_order(predicate: Appeared, seen: _Appearance) -> bool:
    """An effect lands at the growing end of its list: after every surviving element for an
    appending list, before every one for a prepending list (Sent folders)."""
    if not seen.surviving_ids:
        return False
    new_ids = [e.id for e in seen.new_matches]
    if predicate.order == "prepend":
        return max(new_ids) > min(seen.surviving_ids)
    return min(new_ids) < max(seen.surviving_ids)


def _has_status(
    element: Element, snapshot: Snapshot, tokens: tuple[str, ...], pattern: str
) -> bool:
    """A status token in a descendant of the element (a bubble's "Sending..." or "Failed");
    a descendant that is the message text itself ("please retry") is never a status."""
    children: dict[int | None, list[Element]] = {}
    for candidate in snapshot.elements:
        children.setdefault(candidate.parent_id, []).append(candidate)
    stack = list(children.get(element.id, []))
    while stack:
        node = stack.pop()
        stack.extend(children.get(node.id, []))
        if name_matches(node.name, pattern, scoped=True):
            continue
        name = node.name.casefold()
        if any(token in name for token in tokens):
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
    try:
        scope_after = resolve_scope(after, _translate_within(before, after, predicate.within))
    except _AnchorVanishedError:
        return _miss(Scope(level=0), "anchor_vanished")
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
    # The before value is not in the snapshot (`Current*` reads the provider now, VF-30 B1):
    # a stable non-empty mismatch stays uncertain until the value is cached at before time.
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


def title_holds(pattern: str, title: str) -> bool:
    """Presence only (no before): the counter-stripped, casefolded pattern is in the title."""
    return _title_key(pattern) in _title_key(title)


def _title_matches(predicate: TitleMatches, before: Snapshot, after: Snapshot) -> Verdict:
    wanted = _title_key(predicate.pattern)
    title = _title_key(after.title)
    if wanted in _title_key(before.title):
        return _plain("already_true")
    if wanted in title:
        return Verdict(holds=True, code=None, level=3, polls=0, evidence={})
    return _plain("title_unchanged" if title == _title_key(before.title) else "title_differs")


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
    if ctx.windows_before is None:
        raise ValueError("WindowExists needs the windows list taken before the step")
    seen = {w.hwnd for w in ctx.windows_before}
    matched = [
        w
        for w in _matching_windows(
            _windows(ctx, "WindowExists"), predicate.name_pattern, ctx.own_pid
        )
        if w.hwnd not in seen
    ]
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
        # A part with a typed contradiction (a duplicate effect, an empty value) decides the
        # conjunction on its own: it cannot un-happen while a sibling part is still pending.
        settled = next((v for v in verdicts if not v.holds and v.state is not None), None)
        return settled if settled is not None else replace(failing, state=None)
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
    undecidable = next((v for v in verdicts if v.evidence.get("reason") in NO_LABEL_REASONS), None)
    if undecidable is not None:
        return undecidable
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
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Verdict:
    """Evaluate against `after`; on a miss re-observe (`observe(stable_window=1)`) until the
    predicate holds, the contradiction settles (two equal typed observations), or the deadline,
    which bounds the whole call including the observes."""
    if poll_s <= 0:
        raise ValueError("poll_s must be > 0")
    if deadline_s < 0:
        raise ValueError("deadline_s must be >= 0")
    ctx = Context(live=live, windows=windows, windows_before=windows_before, own_pid=own_pid)
    now = monotonic or time.monotonic
    pause = sleep or time.sleep
    start = now()
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
        remaining = deadline_s - (now() - start)
        if remaining <= _EPSILON_S or remaining < last_observe_s:
            return replace(verdict, code=ErrorCode.VERIFY_TIMEOUT, polls=polls)
        if last_observe_s < poll_s:
            pause(max(0.0, min(poll_s - last_observe_s, remaining - last_observe_s)))
        polls += 1
        started = now()
        try:
            after = observe(stable_window=1)
            verdict = _evaluate(predicate, before, after, ctx)
        except (OSError, SteerError) as exc:
            code = exc.code if isinstance(exc, SteerError) else ErrorCode.VERIFY_TIMEOUT
            return _no_verdict(code, polls, "observe_failed")
        last_observe_s = now() - started
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
        after_id=verdict.after_id or after.id,
        level=verdict.level,
        code=verdict.code.value if verdict.code else None,
    )


def _appeared_spec(predicate: Appeared) -> dict[str, Any]:
    """The `Appeared` options that shape a label, never its pattern."""
    spec: dict[str, Any] = {}
    if predicate.excluding_anchor is not None:
        spec["excluding_anchor"] = predicate.excluding_anchor
    if predicate.expected != 1:
        spec["expected"] = predicate.expected
    if predicate.order != "append":
        spec["order"] = predicate.order
    if predicate.decoration is not None:
        spec["decoration"] = predicate.decoration
    if predicate.replaces:
        spec["replaces"] = True
    return spec


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
                spec.update(_appeared_spec(predicate))
        case ValueEquals() | Focused() | State():
            spec["target_id"] = predicate.target_id
            if isinstance(predicate, State):
                spec["wanted"] = predicate.wanted
        case All() | AnyOf():
            spec["parts"] = [redact(part) for part in predicate.parts]
        case TitleMatches() | WindowExists() | WindowGone():
            pass
    return spec


def _within_spec(within: Within) -> dict[str, Any]:
    """The scope without its landmark name: panes are named after contacts, and a short
    name's digest is dictionary-recoverable, so only the kind is kept."""
    if within.landmark is None:
        return {"kind": "anchor", "anchor_id": within.anchor_id}
    return {"kind": "landmark"}
