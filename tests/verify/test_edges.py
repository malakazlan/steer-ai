"""Boundary and gap tests from the verifier sign-off (VF-01..40 coverage map): rules the hunter
listed as fixed that had no direct test, numeric boundaries, the no-label paths, and the
re-review rules (3daa87d) at their edges."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import steerai.verify.t1 as t1_mod
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
from steerai.verify.scope import resolve_scope
from steerai.verify.strength import MIN_PATTERN_CHARS, Params, strength, template_for
from steerai.verify.t1 import (
    CHURN_TOLERANCE,
    NO_LABEL_REASONS,
    Verdict,
    evaluate,
    label_of_record,
    redact,
    verify,
)
from steerai.world.model import Snapshot
from tests.verify.fakes import (
    BASE_ROWS,
    BUBBLE_OK,
    BUBBLE_OUT,
    CHAT_AMIR,
    CHAT_LIST,
    COMPOSER,
    COMPOSER_AREA,
    CONVERSATION,
    DELETE,
    SEND,
    TOAST_AREA,
    FakeHandle,
    FakeLive,
    after_with,
    after_without,
    before,
    build,
)

CONVERSATION_SCOPE = Within(landmark="Conversation")
CHATS_SCOPE = Within(landmark="Chats")
HELLO = Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE)
NEW_BUBBLE = (30, "Text", "hello there", (42, 33), CONVERSATION[0])
TWIN_BUBBLE = (31, "Text", "hello there", (42, 35), CONVERSATION[0])
AMIR_GONE = Disappeared(role="ListItem", name_pattern="Amir · hi bro · 2h", within=CHATS_SCOPE)
NBSP = chr(0xA0)
ZWSP = chr(0x200B)
ZWJ = chr(0x200D)
LRE = chr(0x202A)
PDF = chr(0x202C)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    now: dict[str, Any] = {"t": 0.0, "sleeps": []}

    def sleep(seconds: float) -> None:
        now["t"] += seconds
        now["sleeps"].append(seconds)

    monkeypatch.setattr(t1_mod, "time", SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep))
    return now


def _rows_without(*ids: int) -> list[Any]:
    return [r for r in BASE_ROWS if r[0] not in ids]


def _after(rows: list[Any]) -> Snapshot:
    return build(rows, snapshot_id="after", taken_at=1.0)


def _renumbered(rows: list[Any], offset: int) -> list[Any]:
    """The same tree observed again with every positional id shifted by `offset`."""
    return [
        (r[0] + offset, r[1], r[2], r[3], None if r[4] is None else r[4] + offset) for r in rows
    ]


def _label(verdict: Verdict, predicate: Predicate, earlier: Snapshot, later: Snapshot) -> Any:
    return label_of_record(
        verdict, predicate=predicate, action_key="k", before=earlier, after=later
    )


# ---- Appeared: exclusion, churn boundary, empty scope, rule order (VF-02/03/04/13) --------------


def test_new_match_only_in_the_excluded_composer_is_not_new_when_an_old_twin_exists() -> None:
    old = (30, "Text", "hello there", (42, 33), CONVERSATION[0])
    draft = (31, "Text", "hello there", (42, 43), COMPOSER[4])
    predicate = Appeared(role="Text", name_pattern="hello there", excluding_anchor=COMPOSER[0])
    verdict = evaluate(
        predicate, build([*BASE_ROWS, old], snapshot_id="before"), _after([*BASE_ROWS, old, draft])
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "not_new"
    scoped = Appeared(
        role="Text",
        name_pattern="hello there",
        within=CONVERSATION_SCOPE,
        excluding_anchor=COMPOSER[0],
    )
    assert evaluate(scoped, before(), after_with(draft)).evidence["reason"] == "no_match"


def test_churn_tolerance_boundary_with_expected_two() -> None:
    twice = Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE, expected=2)
    limit = twice.expected + CHURN_TOLERANCE
    assert limit == 3
    separators = [(40 + i, "Text", f"Today {i}", (42, 70 + i), CONVERSATION[0]) for i in range(2)]
    at_limit = after_with(NEW_BUBBLE, TWIN_BUBBLE, separators[0])
    assert evaluate(twice, before(), at_limit).holds is True
    over = after_with(NEW_BUBBLE, TWIN_BUBBLE, *separators)
    verdict = evaluate(twice, before(), over)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_churn"


def test_first_message_in_an_empty_conversation_passes_the_position_rule() -> None:
    empty = build(_rows_without(BUBBLE_OK[0], BUBBLE_OUT[0]), snapshot_id="before")
    first = _after([*_rows_without(BUBBLE_OK[0], BUBBLE_OUT[0]), NEW_BUBBLE])
    verdict = evaluate(HELLO, empty, first)
    assert verdict.holds is True
    assert verdict.evidence == {"level": 1, "matched": 1}
    prepending = replace(HELLO, order="prepend")
    assert evaluate(prepending, empty, first).holds is True


@pytest.mark.parametrize("expected", [0, -1])
def test_appeared_expected_must_be_positive(expected: int) -> None:
    with pytest.raises(ValueError, match="expected"):
        Appeared(role="Text", name_pattern="hello there", expected=expected)


def test_appeared_order_must_be_append_or_prepend() -> None:
    with pytest.raises(ValueError, match="order"):
        Appeared(role="Text", name_pattern="hello there", order="sideways")  # type: ignore[arg-type]


def test_a_prepending_list_rejects_a_new_element_at_the_bottom() -> None:
    sent = Within(landmark="Sent")
    older = (2, "ListItem", "older", (42, 31), 1)
    rows = [(0, "Pane", "Mail", (42, 1), None), (1, "List", "Sent", (42, 30), 0), older]
    earlier = build(rows, snapshot_id="before")
    at_bottom = _after([*rows, (3, "ListItem", "hello there", (42, 33), 1)])
    prepending = Appeared(role="ListItem", name_pattern="hello there", within=sent, order="prepend")
    verdict = evaluate(prepending, earlier, at_bottom)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_shifted"
    assert _label(verdict, prepending, earlier, at_bottom) is None
    appending = replace(prepending, order="append")
    assert evaluate(appending, earlier, at_bottom).holds is True


def test_a_shift_outranks_a_duplicate_so_a_rerender_never_yields_a_failed_label() -> None:
    shifted_twice = _after([*_rows_without(BUBBLE_OK[0]), NEW_BUBBLE, TWIN_BUBBLE])
    verdict = evaluate(HELLO, before(), shifted_twice)
    assert verdict.evidence["reason"] == "scope_shifted"
    assert verdict.state is None
    assert _label(verdict, HELLO, before(), shifted_twice) is None


@pytest.mark.parametrize("status", ["queued", "Pending", "sending now"])
def test_every_pending_token_is_a_miss_that_can_only_time_out(clock: Any, status: str) -> None:
    pending = after_with(NEW_BUBBLE, (31, "Text", status, (42, 34), 30))
    verdict = evaluate(HELLO, before(), pending)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "pending_status"
    assert verdict.state is None
    polled = verify(HELLO, before(), pending, lambda **_: pending)
    assert polled.code is ErrorCode.VERIFY_TIMEOUT
    label = _label(polled, HELLO, before(), pending)
    assert label is not None
    assert label.holds is False


def test_pending_outranks_failed_when_both_tokens_sit_under_the_new_element() -> None:
    both = after_with(
        NEW_BUBBLE,
        (31, "Custom", "Sending", (42, 34), 30),
        (32, "Custom", "Failed", (42, 35), 30),
    )
    assert evaluate(HELLO, before(), both).evidence["reason"] == "pending_status"


def test_the_new_bubbles_own_children_never_count_as_churn() -> None:
    # A real chat bubble carries a timestamp and a status as `Text` children: its own subtree
    # is the effect, not unrelated churn (tester observation 2).
    two_children = after_with(
        NEW_BUBBLE, (31, "Text", "12:30", (42, 34), 30), (32, "Text", "Delivered", (42, 35), 30)
    )
    verdict = evaluate(HELLO, before(), two_children)
    assert verdict.holds is True
    unrelated = after_with(
        NEW_BUBBLE, (31, "Text", "row a", (42, 34), 8), (32, "Text", "row b", (42, 35), 8)
    )
    assert evaluate(HELLO, before(), unrelated).evidence["reason"] == "scope_churn"


def test_appeared_evaluates_the_full_element_list_not_the_pruned_lines() -> None:
    # VF-36: a bubble past the decision model's cap is still evidence.
    capped = replace(after_with(NEW_BUBBLE), cap=1)
    assert len(capped.lines) <= 2
    assert evaluate(HELLO, before(), capped).holds is True


# ---- anchors resolve by identity when every id moved (re-review B2) -----------------------------


def test_anchor_scope_survives_a_full_renumbering_of_the_after_snapshot() -> None:
    later = _after(_renumbered([*BASE_ROWS, NEW_BUBBLE], 100))
    anchored = Appeared(
        role="Text", name_pattern="hello there", within=Within(anchor_id=BUBBLE_OUT[0])
    )
    verdict = evaluate(anchored, before(), later)
    assert verdict.holds is True
    assert verdict.level == 2


def test_excluded_composer_survives_a_full_renumbering_of_the_after_snapshot() -> None:
    draft = (31, "Text", "hello there", (42, 43), COMPOSER_AREA[0])
    later = _after(_renumbered([*BASE_ROWS, draft], 100))
    predicate = Appeared(role="Text", name_pattern="hello there", excluding_anchor=COMPOSER[0])
    verdict = evaluate(predicate, before(), later)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "no_match"


def test_count_delta_anchor_survives_a_full_renumbering() -> None:
    later = _after(_renumbered([*BASE_ROWS, NEW_BUBBLE], 100))
    count = CountDelta(role="Text", within=Within(anchor_id=BUBBLE_OUT[0]), delta=1)
    verdict = evaluate(count, before(), later)
    assert verdict.holds is True
    assert verdict.evidence == {"level": 2, "before": 2, "after": 3}


def test_a_renamed_anchor_is_a_vanished_anchor() -> None:
    renamed = _after(
        [r if r[0] != COMPOSER[0] else (12, "Edit", "Subject", (42, 41), 11) for r in BASE_ROWS]
    )
    predicate = Appeared(role="Text", name_pattern="hello there", excluding_anchor=COMPOSER[0])
    verdict = evaluate(predicate, before(), renamed)
    assert verdict.evidence["reason"] == "anchor_vanished"
    assert verdict.level == 0
    assert "anchor_vanished" in NO_LABEL_REASONS


# ---- Disappeared: parent without RuntimeId, parent gone, ambiguous landmark (VF-11/26) ----------


def test_disappeared_under_a_parent_without_runtime_id_is_churn_and_no_label() -> None:
    bare_list = (CHAT_LIST[0], CHAT_LIST[1], CHAT_LIST[2], (), CHAT_LIST[4])
    rows = [bare_list if r[0] == CHAT_LIST[0] else r for r in BASE_ROWS]
    earlier = build(rows, snapshot_id="before")
    later = _after([r for r in rows if r[0] != CHAT_AMIR[0]])
    verdict = evaluate(AMIR_GONE, earlier, later)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_churn"
    assert _label(verdict, AMIR_GONE, earlier, later) is None


def test_disappeared_needs_the_parent_to_survive() -> None:
    # The list re-rendered with a new RuntimeId while the landmark container (Chats) survived.
    fresh_list = (CHAT_LIST[0], CHAT_LIST[1], CHAT_LIST[2], (43, 11), CHAT_LIST[4])
    rows = [fresh_list if r[0] == CHAT_LIST[0] else r for r in _rows_without(CHAT_AMIR[0])]
    verdict = evaluate(AMIR_GONE, before(), _after(rows))
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_churn"


def test_disappeared_and_count_delta_with_an_ambiguous_landmark_never_hold() -> None:
    twin = (20, "Group", "Chats", (42, 60), 0)
    earlier = build([*BASE_ROWS, twin], snapshot_id="before")
    later = _after([*_rows_without(CHAT_AMIR[0]), twin])
    gone = evaluate(AMIR_GONE, earlier, later)
    assert gone.holds is False
    assert gone.evidence["reason"] == "ambiguous_landmark"
    count = evaluate(CountDelta(role="ListItem", delta=-1, within=CHATS_SCOPE), earlier, later)
    assert count.holds is False
    assert count.evidence["reason"] == "ambiguous_landmark"
    assert count.level == 0


def test_count_delta_is_identity_free_so_a_rerendered_scope_still_counts() -> None:
    # Unspecified by 5.10 [v1.2]: the churn guard is a rule of appeared/disappeared, and the
    # delete template pairs count with disappeared, which catches the churn. Alone (R0/R1 only)
    # the count is a count.
    rows = [
        (r[0], r[1], r[2], (43, r[3][1]), r[4]) if r[4] == CHAT_LIST[0] or r[0] in (1, 2) else r
        for r in _rows_without(CHAT_AMIR[0])
    ]
    rerendered = _after(rows)
    alone = CountDelta(role="ListItem", delta=-1, within=CHATS_SCOPE)
    assert evaluate(alone, before(), rerendered).holds is True
    paired = template_for(
        "delete",
        Params(
            target_id=3, target_role="ListItem", target_name="Amir · hi bro · 2h", scope=CHATS_SCOPE
        ),
    )
    verdict = evaluate(paired, before(), rerendered)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_churn"


@pytest.mark.parametrize("delta", [-2, 2])
def test_count_delta_off_by_one_in_either_direction_misses(delta: int) -> None:
    verdict = evaluate(
        CountDelta(role="ListItem", delta=delta, within=CHATS_SCOPE),
        before(),
        after_without(CHAT_AMIR[0]),
    )
    assert verdict.holds is False
    assert verdict.evidence == {"level": 1, "before": 2, "after": 1, "reason": "count_differs"}


# ---- ValueEquals: the before handle is never read, handle-less after (VF-17/29/30, B1) ---------


class _TaggedLive(FakeLive):
    """Records every handle it is asked to read; a before handle is tagged "stale"."""

    def __init__(self, after_value: str) -> None:
        super().__init__()
        self._after_value = after_value
        self.seen: list[Any] = []

    def value(self, handle: Any) -> str | None:
        self.seen.append(handle)
        if handle[0] == "stale":
            raise OSError("the before handle must never be read")
        return self._after_value


def test_value_equals_never_reads_the_before_handle_and_a_mismatch_only_times_out(
    clock: Any,
) -> None:
    stale = replace(before(), handles=[("stale", *h) for h in before().handles])
    live = _TaggedLive(after_value="draft")
    predicate = ValueEquals(target_id=COMPOSER[0], text="hello")
    verdict = evaluate(predicate, stale, after_with(), live=live)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "value_differs"
    assert verdict.state is None
    polled = verify(predicate, stale, after_with(), lambda **_: after_with(), live=live)
    assert polled.code is ErrorCode.VERIFY_TIMEOUT
    assert polled.polls == 12
    assert all(handle[0] != "stale" for handle in live.seen)
    assert len(live.seen) == 14


def test_value_equals_on_a_handle_less_after_snapshot_gives_no_label(clock: Any) -> None:
    persisted = replace(after_with(), handles=[])
    predicate = ValueEquals(target_id=COMPOSER[0], text="x")
    with pytest.raises(SteerError, match="no live handles"):
        evaluate(predicate, before(), persisted, live=FakeLive())
    verdict = verify(predicate, before(), persisted, lambda **_: persisted, live=FakeLive())
    assert verdict.code is ErrorCode.ELEMENT_NOT_FOUND
    assert verdict.evidence["reason"] == "evaluate_failed"
    assert verdict.after_id is None
    assert _label(verdict, predicate, before(), persisted) is None


# ---- Focused / State identity and indeterminate toggles (VF-32) --------------------------------


def test_focused_and_state_recheck_the_target_identity_in_the_after_snapshot() -> None:
    renamed = _after(
        [r if r[0] != DELETE[0] else (7, "Button", "Archive chat", (42, 22), 5) for r in BASE_ROWS]
    )
    live = FakeLive(focused=DELETE[3], states={DELETE[3]: FakeHandle(toggle=1)})
    focus = evaluate(Focused(target_id=DELETE[0]), before(), renamed, live=live)
    assert focus.holds is False
    assert focus.evidence["reason"] == "target_not_found"
    state = evaluate(State(target_id=DELETE[0], wanted="on"), before(), renamed, live=live)
    assert state.holds is False
    assert state.evidence["reason"] == "target_not_found"


@pytest.mark.parametrize("wanted", ["on", "off"])
def test_indeterminate_toggle_is_neither_on_nor_off(clock: Any, wanted: str) -> None:
    live = FakeLive(states={DELETE[3]: FakeHandle(toggle=2)})
    predicate = State(target_id=DELETE[0], wanted=wanted)  # type: ignore[arg-type]
    verdict = evaluate(predicate, before(), after_with(), live=live)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "state_differs"
    polled = verify(predicate, before(), after_with(), lambda **_: after_with(), live=live)
    assert polled.code is ErrorCode.VERIFY_FAILED
    assert polled.polls == 1


def test_focused_settles_on_a_stable_other_focus_and_on_no_focus(clock: Any) -> None:
    elsewhere = FakeLive(focused=DELETE[3])
    verdict = verify(
        Focused(target_id=SEND[0]), before(), after_with(), lambda **_: after_with(), live=elsewhere
    )
    assert verdict.code is ErrorCode.VERIFY_FAILED
    assert verdict.polls == 1
    nowhere = FakeLive(focused=())
    verdict = verify(
        Focused(target_id=SEND[0]), before(), after_with(), lambda **_: after_with(), live=nowhere
    )
    assert verdict.code is ErrorCode.VERIFY_FAILED


# ---- verify timing boundaries (VF-37, B6) --------------------------------------------------------


def test_no_sleep_follows_a_slow_observe_and_the_last_observe_is_not_started(clock: Any) -> None:
    def slow(**_: Any) -> Snapshot:
        clock["t"] += 0.6
        return after_with()

    verdict = verify(HELLO, before(), after_with(), slow, deadline_s=1.0, poll_s=0.25)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.polls == 1
    assert clock["sleeps"] == [0.25]
    assert clock["t"] == pytest.approx(0.85)


def test_an_observe_starts_when_exactly_its_last_duration_remains(clock: Any) -> None:
    def slow(**_: Any) -> Snapshot:
        clock["t"] += 0.5
        return after_with()

    exact = verify(HELLO, before(), after_with(), slow, deadline_s=1.25, poll_s=0.25)
    assert exact.polls == 2
    assert exact.code is ErrorCode.VERIFY_TIMEOUT
    assert clock["t"] == pytest.approx(1.25)
    clock["t"] = 0.0
    short = verify(HELLO, before(), after_with(), slow, deadline_s=1.2, poll_s=0.25)
    assert short.polls == 1


def test_the_sleep_is_cut_so_the_next_observe_ends_exactly_at_the_deadline(clock: Any) -> None:
    def observe(**_: Any) -> Snapshot:
        clock["t"] += 0.1
        return after_with()

    verdict = verify(HELLO, before(), after_with(), observe, deadline_s=0.55, poll_s=0.25)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.polls == 2
    assert clock["sleeps"] == [pytest.approx(0.25), pytest.approx(0.1)]
    assert clock["t"] == pytest.approx(0.55)


def test_a_deadline_shorter_than_one_poll_still_observes_once(clock: Any) -> None:
    observe_calls = {"n": 0}

    def observe(**_: Any) -> Snapshot:
        observe_calls["n"] += 1
        return after_with()

    verdict = verify(HELLO, before(), after_with(), observe, deadline_s=0.1, poll_s=0.25)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.polls == 1
    assert observe_calls["n"] == 1
    assert clock["sleeps"] == [pytest.approx(0.1)]
    assert clock["t"] == pytest.approx(0.1)


def test_a_zero_deadline_evaluates_once_and_never_observes(clock: Any) -> None:
    def observe(**_: Any) -> Snapshot:
        raise AssertionError("observe must not be called")

    verdict = verify(HELLO, before(), after_with(), observe, deadline_s=0.0)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.polls == 0
    assert verdict.evidence["reason"] == "no_match"
    assert verdict.after_id == "after"
    assert verify(HELLO, before(), after_with(NEW_BUBBLE), observe, deadline_s=0.0).holds is True


def test_a_contradiction_on_the_given_after_snapshot_still_needs_one_poll_to_settle(
    clock: Any,
) -> None:
    twice = after_with(NEW_BUBBLE, TWIN_BUBBLE)
    verdict = verify(HELLO, before(), twice, lambda **_: twice)
    assert verdict.code is ErrorCode.VERIFY_FAILED
    assert verdict.polls == 1
    flip = iter([after_with(), twice, after_with(), twice])
    flapping = verify(HELLO, before(), twice, lambda **_: next(flip), deadline_s=1.0)
    assert flapping.code is ErrorCode.VERIFY_TIMEOUT


def test_a_timeout_verdict_and_its_label_cite_the_last_poll(clock: Any) -> None:
    polls = {"n": 0}

    def observe(**_: Any) -> Snapshot:
        polls["n"] += 1
        return build(BASE_ROWS, snapshot_id=f"poll-{polls['n']}", taken_at=float(polls["n"]))

    verdict = verify(HELLO, before(), after_with(), observe)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.after_id == "poll-12"
    label = _label(verdict, HELLO, before(), after_with())
    assert label is not None
    assert label.after_id == "poll-12"


# ---- verify: process identity on the first and later evaluates (VF-01) -------------------------


def test_pid_change_on_the_first_evaluate_is_app_crashed_without_an_observe(clock: Any) -> None:
    def observe(**_: Any) -> Snapshot:
        raise AssertionError("observe must not be called")

    started = replace(before(), pid=777)
    replaced = replace(after_with(NEW_BUBBLE), pid=999)
    verdict = verify(HELLO, started, replaced, observe)
    assert verdict.code is ErrorCode.APP_CRASHED
    assert verdict.evidence == {"reason": "evaluate_failed"}
    assert verdict.polls == 0
    assert verdict.level == 0
    assert verdict.after_id is None


def test_pid_change_during_a_poll_is_app_crashed_and_no_label(clock: Any) -> None:
    started = replace(before(), pid=777)
    replaced = replace(after_with(NEW_BUBBLE), pid=999)
    verdict = verify(HELLO, started, replace(after_with(), pid=777), lambda **_: replaced)
    assert verdict.code is ErrorCode.APP_CRASHED
    assert verdict.evidence["reason"] == "observe_failed"
    assert verdict.polls == 1
    assert _label(verdict, HELLO, started, replaced) is None


def test_a_zero_pid_on_either_side_is_not_a_crash() -> None:
    assert (
        evaluate(HELLO, replace(before(), pid=0), replace(after_with(NEW_BUBBLE), pid=5)).holds
        is True
    )
    assert (
        evaluate(HELLO, replace(before(), pid=5), replace(after_with(NEW_BUBBLE), pid=0)).holds
        is True
    )


# ---- All / AnyOf across polls (VF-10, B3) --------------------------------------------------------


def test_all_parts_settle_on_different_polls_and_share_one_observe_per_poll(clock: Any) -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="hello there")})
    send = All((HELLO, ValueEquals(target_id=COMPOSER[0], text="")))
    observes = {"n": 0}

    def observe(**_: Any) -> Snapshot:
        observes["n"] += 1
        if observes["n"] >= 2:
            live.states[COMPOSER[3]].value = ""
        return after_with(NEW_BUBBLE)

    verdict = verify(send, before(), after_with(), observe, live=live)
    assert verdict.holds is True
    assert verdict.polls == 2
    assert observes["n"] == 2
    assert verdict.evidence == {"parts": 2}
    assert verdict.level == 1


def test_all_reports_the_first_failing_part_and_its_level() -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="")})
    both = All((ValueEquals(target_id=COMPOSER[0], text=""), HELLO))
    verdict = evaluate(both, before(), after_with(), live=live)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "no_match"
    assert verdict.level == 1
    passing = All((HELLO, ValueEquals(target_id=COMPOSER[0], text="")))
    assert evaluate(passing, before(), after_with(NEW_BUBBLE), live=live).level == 1


def test_all_with_a_settled_contradiction_and_a_pending_part_fails_on_the_contradiction(
    clock: Any,
) -> None:
    # A part whose contradiction settled decides the All even while a sibling is pending: an
    # empty composer after a type cannot be undone by a bubble landing later.
    emptied = FakeLive(states={COMPOSER[3]: FakeHandle(value="")})
    both = All((ValueEquals(target_id=COMPOSER[0], text="hello there"), HELLO))
    verdict = verify(both, before(), after_with(), lambda **_: after_with(), live=emptied)
    assert verdict.code is ErrorCode.VERIFY_FAILED
    assert verdict.evidence["reason"] == "value_empty"
    assert verdict.polls == 1


def test_all_of_two_settled_contradictions_fails_after_one_poll(clock: Any) -> None:
    emptied = FakeLive(states={COMPOSER[3]: FakeHandle(value="")})
    twice = after_with(NEW_BUBBLE, TWIN_BUBBLE)
    both = All((HELLO, ValueEquals(target_id=COMPOSER[0], text="hello there")))
    verdict = verify(both, before(), twice, lambda **_: twice, live=emptied)
    assert verdict.code is ErrorCode.VERIFY_FAILED
    assert verdict.polls == 1
    assert verdict.evidence["reason"] == "duplicate_effect"


def test_send_template_duplicate_with_a_non_empty_composer_fails_after_one_poll(
    clock: Any,
) -> None:
    # A duplicate effect cannot un-happen: the settled contradiction of one part decides the
    # `All`, even when a sibling part has no typed state (tester observation 3).
    params = Params(
        target_id=SEND[0], text="hello there", composer_id=COMPOSER[0], scope=CONVERSATION_SCOPE
    )
    send = template_for("send", params)
    still_draft = FakeLive(states={COMPOSER[3]: FakeHandle(value="hello there")})
    twice = after_with(NEW_BUBBLE, TWIN_BUBBLE)
    verdict = verify(send, before(), twice, lambda **_: twice, live=still_draft)
    assert verdict.evidence["reason"] == "duplicate_effect"
    assert verdict.code is ErrorCode.VERIFY_FAILED
    assert verdict.polls == 1
    label = _label(verdict, send, before(), twice)
    assert label is not None
    assert label.code == "verify_failed"


def test_any_of_with_one_contradiction_and_one_pending_branch_times_out(clock: Any) -> None:
    emptied = FakeLive(states={COMPOSER[3]: FakeHandle(value="")})
    either = AnyOf((ValueEquals(target_id=COMPOSER[0], text="hello there"), HELLO))
    verdict = verify(either, before(), after_with(), lambda **_: after_with(), live=emptied)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.evidence["reason"] == "no_match"
    swapped = AnyOf((HELLO, ValueEquals(target_id=COMPOSER[0], text="hello there")))
    clock["t"] = 0.0
    verdict = verify(swapped, before(), after_with(), lambda **_: after_with(), live=emptied)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.evidence["reason"] == "value_empty"


def test_any_of_two_settled_contradictions_fails(clock: Any) -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value=""), DELETE[3]: FakeHandle(toggle=0)})
    either = AnyOf(
        (
            ValueEquals(target_id=COMPOSER[0], text="hello there"),
            State(target_id=DELETE[0], wanted="on"),
        )
    )
    verdict = verify(either, before(), after_with(), lambda **_: after_with(), live=live)
    assert verdict.code is ErrorCode.VERIFY_FAILED
    assert verdict.polls == 1


def test_any_of_undecidable_branch_outranks_a_settled_contradiction(clock: Any) -> None:
    twin = (20, "Group", "Conversation", (42, 60), 0)
    earlier = build([*BASE_ROWS, twin], snapshot_id="before")
    later = _after([*BASE_ROWS, twin])
    emptied = FakeLive(states={COMPOSER[3]: FakeHandle(value="")})
    either = AnyOf((HELLO, ValueEquals(target_id=COMPOSER[0], text="hello there")))
    verdict = verify(either, earlier, later, lambda **_: later, live=emptied)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.evidence["reason"] == "ambiguous_landmark"
    assert _label(verdict, either, earlier, later) is None


def test_any_of_returns_the_first_holding_branch_verdict(clock: Any) -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="")})
    either = AnyOf((HELLO, ValueEquals(target_id=COMPOSER[0], text="")))
    verdict = evaluate(either, before(), after_with(), live=live)
    assert verdict.holds is True
    assert verdict.evidence == {}
    assert verdict.level == 3


# ---- labels of record: every no-label reason, every non-verify code (VF-16) ---------------------


@pytest.mark.parametrize("reason", sorted(NO_LABEL_REASONS))
def test_every_no_label_reason_yields_none(reason: str) -> None:
    verdict = Verdict(
        holds=False, code=None, level=1, polls=0, evidence={"level": 1, "reason": reason}
    )
    assert _label(verdict, HELLO, before(), after_with()) is None
    timed = replace(verdict, code=ErrorCode.VERIFY_TIMEOUT)
    assert _label(timed, HELLO, before(), after_with()) is None


@pytest.mark.parametrize(
    "code",
    sorted(c for c in ErrorCode if c not in (ErrorCode.VERIFY_FAILED, ErrorCode.VERIFY_TIMEOUT)),
)
def test_every_code_outside_the_verify_pair_yields_none(code: ErrorCode) -> None:
    verdict = Verdict(
        holds=False, code=code, level=1, polls=0, evidence={"level": 1, "reason": "no_match"}
    )
    assert _label(verdict, HELLO, before(), after_with()) is None


@pytest.mark.parametrize("code", [None, ErrorCode.VERIFY_FAILED, ErrorCode.VERIFY_TIMEOUT])
def test_the_verify_pair_and_a_pass_are_labels(code: ErrorCode | None) -> None:
    verdict = Verdict(
        holds=code is None, code=code, level=1, polls=3, evidence={"level": 1, "reason": "no_match"}
    )
    label = _label(verdict, HELLO, before(), after_with())
    assert label is not None
    assert label.code == (code.value if code else None)
    assert label.holds is (code is None)
    assert label.level == 1
    assert label.after_id == "after"


def test_no_label_reason_wins_over_a_verify_code() -> None:
    verdict = Verdict(
        holds=False,
        code=ErrorCode.VERIFY_FAILED,
        level=1,
        polls=1,
        evidence={"reason": "secure_target"},
    )
    assert _label(verdict, HELLO, before(), after_with()) is None


# ---- redact: nested shapes leak nothing (SX-28, B5) ----------------------------------------------

SECRETS = {
    "appeared": "sekrit-appeared",
    "disappeared": "sekrit-disappeared",
    "value": "sekrit-value",
    "title": "sekrit-title",
    "exists": "sekrit-exists",
    "gone": "sekrit-gone",
    "landmark": "sekrit-landmark",
}


def _every_kind() -> Predicate:
    return All(
        (
            AnyOf(
                (
                    Appeared(
                        role="Text",
                        name_pattern=SECRETS["appeared"],
                        within=Within(landmark=SECRETS["landmark"]),
                        excluding_anchor=12,
                        expected=2,
                        order="prepend",
                    ),
                    Disappeared(
                        role="Edit",
                        name_pattern=SECRETS["disappeared"],
                        within=Within(anchor_id=13),
                    ),
                    WindowGone(name_pattern=SECRETS["gone"]),
                )
            ),
            All(
                (
                    ValueEquals(target_id=12, text=SECRETS["value"]),
                    TitleMatches(pattern=SECRETS["title"]),
                    WindowExists(name_pattern=SECRETS["exists"]),
                    Focused(target_id=5),
                    State(target_id=7, wanted="selected"),
                    CountDelta(
                        role="ListItem", delta=-1, within=Within(landmark=SECRETS["landmark"])
                    ),
                )
            ),
        )
    )


def test_redact_keeps_the_shape_of_every_kind_and_leaks_no_text() -> None:
    spec = redact(_every_kind())
    flat = str(spec).casefold()
    for secret in SECRETS.values():
        assert secret not in flat, secret
    assert spec["kind"] == "All"
    inner_any, inner_all = spec["parts"]
    assert [p["kind"] for p in inner_any["parts"]] == ["Appeared", "Disappeared", "WindowGone"]
    appeared = inner_any["parts"][0]
    assert appeared["within"]["kind"] == "landmark"
    assert len(appeared["within"]["hash"]) == 12
    assert int(appeared["within"]["hash"], 16) >= 0
    assert {k: v for k, v in appeared.items() if k != "within"} == {
        "kind": "Appeared",
        "role": "Text",
        "excluding_anchor": 12,
        "expected": 2,
        "order": "prepend",
    }
    assert inner_any["parts"][1] == {
        "kind": "Disappeared",
        "role": "Edit",
        "within": {"kind": "anchor", "anchor_id": 13},
    }
    assert inner_any["parts"][2] == {"kind": "WindowGone"}
    assert [p["kind"] for p in inner_all["parts"]] == [
        "ValueEquals",
        "TitleMatches",
        "WindowExists",
        "Focused",
        "State",
        "CountDelta",
    ]
    assert inner_all["parts"][0] == {"kind": "ValueEquals", "target_id": 12}
    assert inner_all["parts"][1] == {"kind": "TitleMatches"}
    assert inner_all["parts"][2] == {"kind": "WindowExists"}
    assert inner_all["parts"][3] == {"kind": "Focused", "target_id": 5}
    assert inner_all["parts"][4] == {"kind": "State", "target_id": 7, "wanted": "selected"}
    count = inner_all["parts"][5]
    assert count["within"] == appeared["within"], "the same landmark hashes the same"
    assert {k: v for k, v in count.items() if k != "within"} == {
        "kind": "CountDelta",
        "role": "ListItem",
        "delta": -1,
    }


def test_landmark_hash_is_stable_under_normalisation_and_distinct_across_names() -> None:
    def hash_of(landmark: str) -> str:
        spec = redact(Appeared(role="Text", name_pattern="x", within=Within(landmark=landmark)))
        return str(spec["within"]["hash"])

    assert hash_of("Conversation") == hash_of(f"  conversation{NBSP}")
    assert hash_of("Conversation") != hash_of("Chats")
    assert "conversation" not in str(redact(HELLO)).casefold()


def test_repr_of_every_kind_leaks_no_text() -> None:
    flat = repr(_every_kind())
    for secret in SECRETS.values():
        if secret != SECRETS["landmark"]:
            assert secret not in flat, secret


# ---- strength boundaries (VF-08/39) --------------------------------------------------------------


def test_any_of_two_strong_branches_is_strong() -> None:
    params = Params(
        target_id=13,
        text="hello there",
        file_name="invoice.pdf",
        composer_id=12,
        scope=CONVERSATION_SCOPE,
    )
    either = AnyOf(
        (
            Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
            Disappeared(role="ListItem", name_pattern="invoice.pdf"),
        )
    )
    assert strength(either, "R3", params).ok is True


def test_all_of_two_weak_structural_parts_reports_the_first_structural_reason() -> None:
    params = Params(target_id=13, text="hello there", composer_id=12, scope=CONVERSATION_SCOPE)
    both = All(
        (Appeared(role="Text", name_pattern="ok"), Appeared(role="Text", name_pattern="Today"))
    )
    verdict = strength(both, "R2", params)
    assert verdict.ok is False
    assert verdict.reason == "pattern_too_short"
    led_by_value = All(
        (ValueEquals(target_id=12, text=""), Appeared(role="Text", name_pattern="Today"))
    )
    assert strength(led_by_value, "R2", params).reason == "pattern_is_date_like"
    nested = All(
        (
            ValueEquals(target_id=12, text=""),
            AnyOf((Appeared(role="Text", name_pattern="ok"), Focused(target_id=1))),
        )
    )
    assert strength(nested, "R2", params).reason == "any_of_has_a_weak_branch"


@pytest.mark.parametrize(
    ("text", "ok", "reason"),
    [("ab", False, "pattern_too_short"), ("abc", True, None), ("abcd", True, None)],
)
def test_min_pattern_chars_boundary(text: str, ok: bool, reason: str | None) -> None:
    assert MIN_PATTERN_CHARS == 3
    params = Params(target_id=13, text=text, composer_id=12, scope=CONVERSATION_SCOPE)
    verdict = strength(
        Appeared(role="Text", name_pattern=text, within=CONVERSATION_SCOPE), "R2", params
    )
    assert verdict.ok is ok
    assert verdict.reason == reason


def test_short_pattern_is_measured_after_normalisation() -> None:
    params = Params(target_id=13, text="a b", composer_id=12, scope=CONVERSATION_SCOPE)
    verdict = strength(
        Appeared(role="Text", name_pattern=f"a   {NBSP}b", within=CONVERSATION_SCOPE),
        "R2",
        params,
    )
    assert verdict.ok is True


@pytest.mark.parametrize("pattern", ["amir khan", "  AMIR   KHAN ", f"Amir{NBSP}Khan"])
def test_recipient_comparison_ignores_case_and_whitespace(pattern: str) -> None:
    params = Params(
        target_id=13, text=pattern, composer_id=12, scope=CONVERSATION_SCOPE, recipient="Amir Khan"
    )
    verdict = strength(
        Appeared(role="Text", name_pattern=pattern, within=CONVERSATION_SCOPE), "R2", params
    )
    assert verdict.ok is False
    assert verdict.reason == "pattern_is_the_recipient"


@pytest.mark.parametrize("pattern", ["TODAY", "Tomorrow", "MAY", "12:30 PM", "23:59:59"])
def test_date_and_time_words_are_refused_in_any_case(pattern: str) -> None:
    params = Params(target_id=13, text=pattern, composer_id=12, scope=CONVERSATION_SCOPE)
    verdict = strength(
        Appeared(role="Text", name_pattern=pattern, within=CONVERSATION_SCOPE), "R2", params
    )
    assert verdict.ok is False
    assert verdict.reason in ("pattern_is_date_like", "pattern_is_time_like")


def test_a_sentence_containing_a_date_word_is_content() -> None:
    params = Params(target_id=13, text="see you today", composer_id=12, scope=CONVERSATION_SCOPE)
    predicate = Appeared(role="Text", name_pattern="see you today", within=CONVERSATION_SCOPE)
    assert strength(predicate, "R2", params).ok is True


def test_scope_name_is_refused_from_the_params_scope_even_without_within() -> None:
    params = Params(target_id=13, text="Conversation", composer_id=12, scope=CONVERSATION_SCOPE)
    verdict = strength(Appeared(role="Text", name_pattern="Conversation"), "R2", params)
    assert verdict.reason == "pattern_is_the_scope"


def test_anchor_scoped_pattern_equal_to_the_text_is_strong() -> None:
    params = Params(target_id=13, text="hello there", composer_id=12, scope=Within(anchor_id=12))
    verdict = strength(
        Appeared(role="Text", name_pattern="hello there", within=Within(anchor_id=12)), "R2", params
    )
    assert verdict.ok is True


def test_file_name_is_content_for_appeared_but_the_target_name_is_not() -> None:
    params = Params(target_id=3, file_name="invoice.pdf", target_name="Attach")
    assert strength(Appeared(role="ListItem", name_pattern="invoice.pdf"), "R2", params).ok is True
    weak = strength(Appeared(role="ListItem", name_pattern="Attach"), "R2", params)
    assert weak.reason == "pattern_not_a_parameter"
    assert strength(Disappeared(role="Button", name_pattern="Attach"), "R2", params).ok is True


# ---- templates: submit, composer_name branch, missing parameters --------------------------------


def test_submit_is_the_send_template() -> None:
    params = Params(target_id=13, text="hello there", composer_id=12, scope=CONVERSATION_SCOPE)
    assert template_for("submit", params) == template_for("send", params)


def test_send_template_with_a_composer_name_accepts_an_empty_or_a_gone_composer() -> None:
    params = Params(
        target_id=13,
        text="hello there",
        composer_id=12,
        scope=CONVERSATION_SCOPE,
        composer_name="Message",
        composer_role="Document",
    )
    predicate = template_for("send", params)
    assert predicate == All(
        (
            Appeared(
                role="Text",
                name_pattern="hello there",
                within=CONVERSATION_SCOPE,
                excluding_anchor=12,
            ),
            AnyOf(
                (
                    ValueEquals(target_id=12, text=""),
                    Disappeared(role="Document", name_pattern="Message"),
                )
            ),
        )
    )


def test_send_template_needs_a_scope_and_delete_needs_a_role_and_name() -> None:
    with pytest.raises(ValueError, match="scope"):
        template_for("send", Params(target_id=13, text="hello", composer_id=12))
    with pytest.raises(ValueError, match="target_role"):
        template_for("delete", Params(target_id=3, target_name="x"))
    with pytest.raises(ValueError, match="target_name"):
        template_for("delete", Params(target_id=3, target_role="ListItem"))
    with pytest.raises(ValueError, match="window_name"):
        template_for("open", Params())


# ---- scope: anchors inside transient subtrees ----------------------------------------------------


def test_anchor_inside_a_transient_container_scopes_to_nothing() -> None:
    toast = (21, "Text", "Message sent", (42, 51), TOAST_AREA[0])
    earlier = build([*BASE_ROWS, toast], snapshot_id="before")
    scope = resolve_scope(earlier, Within(anchor_id=toast[0]))
    assert scope.level == 0
    assert scope.reason == "anchor_transient"
    assert scope.ids == set()
    later = _after([*BASE_ROWS, toast, (22, "Text", "Delivered", (42, 52), TOAST_AREA[0])])
    hello = Appeared(role="Text", name_pattern="Delivered", within=Within(anchor_id=toast[0]))
    verdict = evaluate(hello, earlier, later)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "anchor_transient"
    assert _label(verdict, hello, earlier, later) is None


def test_anchor_inside_a_transient_role_climbs_past_it() -> None:
    tooltip = (22, "ToolTip", "Send (Enter)", (42, 52), COMPOSER[4])
    tip_text = (23, "Text", "Send (Enter)", (42, 53), 22)
    snapshot = build([*BASE_ROWS, tooltip, tip_text])
    scope = resolve_scope(snapshot, Within(anchor_id=tip_text[0]))
    assert scope.level == 2
    assert scope.container_id == COMPOSER[4]
    assert tooltip[0] not in scope.ids
    assert tip_text[0] not in scope.ids
    assert SEND[0] in scope.ids


def test_landmark_inside_a_transient_container_is_invisible() -> None:
    inner = (21, "Group", "Conversation", (42, 51), TOAST_AREA[0])
    scope = resolve_scope(build([*BASE_ROWS, inner]), CONVERSATION_SCOPE)
    assert scope.level == 1
    assert scope.container_id == CONVERSATION[0]


# ---- name_matches on degenerate inputs (VF-24) ---------------------------------------------------


def test_name_matches_with_only_format_characters_compares_as_empty() -> None:
    # The dataclasses refuse such a pattern at construction; the function alone treats format
    # characters as nothing on both sides.
    assert name_matches("", ZWSP + ZWJ, scoped=False) is True
    assert name_matches(ZWSP, "", scoped=True) is True
    assert name_matches("hello", ZWSP, scoped=False) is False
    assert name_matches(ZWSP, "hello", scoped=True) is False
    with pytest.raises(ValueError, match="pattern"):
        Appeared(role="Text", name_pattern=ZWSP + ZWJ)


def test_name_matches_keeps_combining_marks_and_strips_bidi_controls() -> None:
    assert name_matches("caf" + chr(0xE9), "cafe" + chr(0x301), scoped=False) is True
    assert name_matches(LRE + "hello" + PDF, "hello", scoped=False) is True
    assert name_matches("hello", "hellO", scoped=False) is False
