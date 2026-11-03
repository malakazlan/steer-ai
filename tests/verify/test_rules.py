"""T1 soundness rules from the verifier edge-case review (VF-01..40): a new RuntimeId is
necessary, never sufficient."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import steerai.verify.t1 as t1_mod
from steerai.errors import ErrorCode
from steerai.verify.predicates import (
    Appeared,
    Disappeared,
    TitleMatches,
    ValueEquals,
    WindowExists,
    WindowGone,
    Within,
    name_matches,
)
from steerai.verify.t1 import evaluate, label_of_record, verify
from steerai.world.model import Snapshot
from tests.verify.fakes import (
    BASE_ROWS,
    BUBBLE_OK,
    BUBBLE_OUT,
    CHAT_AMIR,
    CHAT_SARA,
    COMPOSER,
    CONVERSATION,
    FakeHandle,
    FakeLive,
    after_with,
    after_without,
    before,
    build,
)

CONVERSATION_SCOPE = Within(landmark="Conversation")
HELLO = Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE)
NEW_BUBBLE = (30, "Text", "hello there", (42, 33), CONVERSATION[0])


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    monkeypatch.setattr(t1_mod, "time", SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep))
    return now


def _rows_without(*ids: int) -> list[Any]:
    return [r for r in BASE_ROWS if r[0] not in ids]


# ---- VF-06 / VF-24 / VF-25: matching is full-string, plain text, format characters stripped ----


def test_scoped_match_is_full_string_case_insensitive_not_substring() -> None:
    assert name_matches("Hello There", "hello there", scoped=True) is True
    assert name_matches("Sara: hello there", "hello there", scoped=True) is False
    assert name_matches("> hello there", "hello there", scoped=True) is False


def test_match_strips_format_characters_and_never_treats_patterns_as_regex() -> None:
    assert name_matches("hel\u200blo", "hello", scoped=False) is True
    assert name_matches("abc", "a.c", scoped=False) is False
    assert name_matches("a.c", "a.c", scoped=False) is True
    assert name_matches("(1)", "(1)", scoped=False) is True


# ---- VF-02 / VF-13: count-aware matching --------------------------------------------------------


def test_appeared_needs_the_match_count_to_rise_by_exactly_the_expected_delta() -> None:
    # Positional ids: the last slot is "new" but carries the old "ok" text, and one old element
    # vanished: the shift guard catches it first (no label). A genuinely new second "ok" with
    # all old rows surviving is a real effect and passes.
    shifted_rows = [r for r in BASE_ROWS if r[0] not in (BUBBLE_OK[0], BUBBLE_OUT[0])] + [
        (9, "Text", "are we still on for friday?", (42, 31), CONVERSATION[0]),
        (10, "Text", "ok", (42, 34), CONVERSATION[0]),
    ]
    verdict = evaluate(
        Appeared(role="Text", name_pattern="ok", within=CONVERSATION_SCOPE),
        before(),
        build(shifted_rows, snapshot_id="after", taken_at=1.0),
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_shifted"


def test_two_new_matches_for_one_expected_is_a_duplicate_effect(clock: Any) -> None:
    twice = after_with(NEW_BUBBLE, (31, "Text", "hello there", (42, 35), CONVERSATION[0]))
    verdict = evaluate(HELLO, before(), twice)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "duplicate_effect"
    polled = verify(HELLO, before(), twice, lambda **_: twice)
    assert polled.code is ErrorCode.VERIFY_FAILED
    assert polled.evidence["reason"] == "duplicate_effect"


def test_expected_count_can_be_raised_explicitly() -> None:
    twice = after_with(NEW_BUBBLE, (31, "Text", "hello there", (42, 35), CONVERSATION[0]))
    assert (
        evaluate(
            Appeared(
                role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE, expected=2
            ),
            before(),
            twice,
        ).holds
        is True
    )


# ---- VF-03 / VF-12: scope churn guard -----------------------------------------------------------


def test_rerendered_scope_never_passes() -> None:
    fresh = [
        r
        if r[4] != CONVERSATION[0] and r[0] != CONVERSATION[0]
        else (r[0], r[1], r[2], (43, r[3][1]), r[4])
        for r in BASE_ROWS
    ] + [(30, "Text", "hello there", (43, 33), CONVERSATION[0])]
    verdict = evaluate(HELLO, before(), build(fresh, snapshot_id="after", taken_at=1.0))
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_churn"


def test_many_new_siblings_is_churn_not_an_effect() -> None:
    extra = [(30 + i, "Text", f"row {i}", (42, 60 + i), CONVERSATION[0]) for i in range(3)]
    verdict = evaluate(HELLO, before(), after_with(NEW_BUBBLE, *extra))
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_churn"


def test_one_unrelated_new_sibling_is_tolerated() -> None:
    separator = (31, "Text", "Today", (42, 61), CONVERSATION[0])
    assert evaluate(HELLO, before(), after_with(separator, NEW_BUBBLE)).holds is True


# ---- VF-04: position rule ----------------------------------------------------------------------


def test_new_element_must_come_after_the_last_before_element_in_scope() -> None:
    # Tree order is the element id order: the "new" row realised above the surviving rows is a
    # scroll, not an effect.
    rows = [
        (0, "Pane", "Amir Khan - Chat", (42, 1), None),
        (1, "Group", "Conversation", (42, 30), 0),
        (2, "Text", "hello there", (42, 33), 1),
        (3, "Text", "ok", (42, 31), 1),
        (4, "Text", "are we still on for friday?", (42, 32), 1),
    ]
    earlier = build(
        [
            rows[0],
            rows[1],
            (2, "Text", "ok", (42, 31), 1),
            (3, "Text", "are we still on for friday?", (42, 32), 1),
        ],
        snapshot_id="before",
    )
    verdict = evaluate(HELLO, earlier, build(rows, snapshot_id="after", taken_at=1.0))
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_shifted"


def test_a_scroll_that_drops_rows_while_adding_one_is_a_shift() -> None:
    rows = [r for r in BASE_ROWS if r[0] != BUBBLE_OK[0]] + [NEW_BUBBLE]
    verdict = evaluate(HELLO, before(), build(rows, snapshot_id="after", taken_at=1.0))
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_shifted"


# ---- VF-05: optimistic placeholders -------------------------------------------------------------


@pytest.mark.parametrize("name", ["hello there · Sending…", "hello there (pending)"])
def test_pending_placeholder_keeps_polling(clock: Any, name: str) -> None:
    pending = after_with((30, "Text", name, (42, 33), CONVERSATION[0]))
    verdict = evaluate(HELLO, before(), pending)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "no_match"
    polled = verify(HELLO, before(), pending, lambda **_: pending)
    assert polled.code is ErrorCode.VERIFY_TIMEOUT


def test_failed_placeholder_is_positive_contradiction(clock: Any) -> None:
    failed_rows = [
        (30, "Text", "hello there", (42, 33), CONVERSATION[0]),
        (31, "Text", "Failed to send · Retry", (42, 34), 30),
    ]
    failed = after_with(*failed_rows)
    verdict = evaluate(HELLO, before(), failed)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "failed_status"
    polled = verify(HELLO, before(), failed, lambda **_: failed)
    assert polled.code is ErrorCode.VERIFY_FAILED


# ---- VF-21 / VF-40: zero-area never counts, offscreen does ------------------------------------


def test_zero_area_elements_never_satisfy_appeared_but_offscreen_ones_do() -> None:
    snapshot = after_with(NEW_BUBBLE)
    flat = replace(
        snapshot,
        elements=[
            e if e.id != 30 else replace(e, box=(0, 300, 100, 300)) for e in snapshot.elements
        ],
    )
    assert evaluate(HELLO, before(), flat).evidence["reason"] == "no_match"
    offscreen = replace(
        snapshot,
        elements=[e if e.id != 30 else replace(e, offscreen=True) for e in snapshot.elements],
    )
    assert evaluate(HELLO, before(), offscreen).holds is True


# ---- VF-14 / VF-16: unstable snapshots and the third outcome ------------------------------------


def test_unstable_snapshot_on_either_side_never_passes_and_gives_no_label() -> None:
    unstable_after = build(
        [*BASE_ROWS, NEW_BUBBLE], snapshot_id="after", taken_at=1.0, stable=False
    )
    verdict = evaluate(HELLO, before(), unstable_after)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "unstable"
    assert (
        label_of_record(
            verdict, predicate=HELLO, action_key="k", before=before(), after=unstable_after
        )
        is None
    )
    unstable_before = build(BASE_ROWS, snapshot_id="before", stable=False)
    assert evaluate(HELLO, unstable_before, after_with(NEW_BUBBLE)).evidence["reason"] == "unstable"


def test_no_label_from_ambiguity_churn_or_observe_failure(clock: Any) -> None:
    twin = (20, "Group", "Conversation", (42, 60), 0)
    ambiguous = evaluate(
        HELLO,
        build([*BASE_ROWS, twin], snapshot_id="before"),
        build([*BASE_ROWS, twin, NEW_BUBBLE], snapshot_id="after", taken_at=1.0),
    )
    assert (
        label_of_record(
            ambiguous, predicate=HELLO, action_key="k", before=before(), after=after_with()
        )
        is None
    )

    def observe(**_: Any) -> Snapshot:
        raise OSError("gone")

    broken = verify(HELLO, before(), after_with(), observe)
    assert (
        label_of_record(
            broken, predicate=HELLO, action_key="k", before=before(), after=after_with()
        )
        is None
    )
    timed_out = verify(HELLO, before(), after_with(), lambda **_: after_with())
    label = label_of_record(
        timed_out, predicate=HELLO, action_key="k", before=before(), after=after_with()
    )
    assert label is not None
    assert label.holds is False
    assert label.code == "verify_timeout"


# ---- VF-17 / VF-30: contradictions need positive evidence -------------------------------------


def test_disappeared_still_present_only_times_out(clock: Any) -> None:
    verdict = verify(
        Disappeared(
            role="ListItem", name_pattern="Amir · hi bro · 2h", within=Within(landmark="Chats")
        ),
        before(),
        after_with(),
        lambda **_: after_with(),
    )
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT


def test_value_mismatch_is_only_failed_when_the_value_is_empty(clock: Any) -> None:
    # The before value is not in the snapshot (UIA `Current*` reads the provider now), so a
    # stable non-empty mismatch is uncertain, never a contradiction (principal re-review B1).
    stale = FakeLive(states={COMPOSER[3]: FakeHandle(value="draft")})
    verdict = verify(
        ValueEquals(target_id=COMPOSER[0], text="hello"),
        before(),
        after_with(),
        lambda **_: after_with(),
        live=stale,
    )
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.evidence["reason"] == "value_differs"


def test_value_empty_after_typing_is_failed(clock: Any) -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="")})
    verdict = verify(
        ValueEquals(target_id=COMPOSER[0], text="hello"),
        before(),
        after_with(),
        lambda **_: after_with(),
        live=live,
    )
    assert verdict.code is ErrorCode.VERIFY_FAILED


# ---- VF-11: delete must not be a collapse or a filter ------------------------------------------


def test_disappeared_needs_the_siblings_to_survive() -> None:
    verdict = evaluate(
        Disappeared(
            role="ListItem", name_pattern="Amir · hi bro · 2h", within=Within(landmark="Chats")
        ),
        before(),
        after_without(CHAT_AMIR[0], CHAT_SARA[0]),
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "siblings_changed"


def test_disappeared_needs_the_scope_container_to_survive() -> None:
    gone_list = build(_rows_without(2, 3, 4), snapshot_id="after", taken_at=1.0)
    verdict = evaluate(
        Disappeared(
            role="ListItem", name_pattern="Amir · hi bro · 2h", within=Within(landmark="Chats")
        ),
        before(),
        gone_list,
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_churn"


# ---- VF-01: process identity ---------------------------------------------------------------------


def test_a_different_process_behind_the_same_hwnd_is_app_crashed(clock: Any) -> None:
    restarted = replace(after_with(NEW_BUBBLE), pid=999)
    started = replace(before(), pid=777)
    verdict = verify(HELLO, started, restarted, lambda **_: restarted)
    assert verdict.holds is False
    assert verdict.code is ErrorCode.APP_CRASHED
    assert (
        label_of_record(verdict, predicate=HELLO, action_key="k", before=started, after=restarted)
        is None
    )


# ---- VF-09 / VF-31: titles --------------------------------------------------------------------


def test_title_already_matching_before_is_not_an_effect() -> None:
    verdict = evaluate(TitleMatches(pattern="Amir Khan - Chat"), before(), after_with())
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "already_true"


def test_title_match_strips_counters_and_keeps_digits() -> None:
    changed = build(
        BASE_ROWS, snapshot_id="after", taken_at=1.0, title="(2) invoice-2026.pdf - Notepad"
    )
    assert (
        evaluate(TitleMatches(pattern="invoice-2026.pdf - Notepad"), before(), changed).holds
        is True
    )
    assert (
        evaluate(TitleMatches(pattern="invoice-2027.pdf - Notepad"), before(), changed).holds
        is False
    )
    assert evaluate(TitleMatches(pattern="notepad"), before(), changed).holds is True


# ---- VF-18 / VF-22: windows need a before list -----------------------------------------------


def test_window_exists_for_open_requires_a_window_new_since_before() -> None:
    old = SimpleNamespace(title="Save As", hwnd=0x100, pid=50)
    new = SimpleNamespace(title="Save As", hwnd=0x200, pid=50)
    assert (
        evaluate(
            WindowExists(name_pattern="Save As"),
            before(),
            after_with(),
            windows=[old],
            windows_before=[old],
        ).holds
        is False
    )
    assert (
        evaluate(
            WindowExists(name_pattern="Save As"),
            before(),
            after_with(),
            windows=[old, new],
            windows_before=[old],
        ).holds
        is True
    )


def test_window_gone_needs_presence_before_and_absence_after() -> None:
    compose = SimpleNamespace(title="Untitled - Message", hwnd=0x300, pid=50)
    assert (
        evaluate(
            WindowGone(name_pattern="Untitled - Message"),
            before(),
            after_with(),
            windows=[],
            windows_before=[compose],
        ).holds
        is True
    )
    assert (
        evaluate(
            WindowGone(name_pattern="Untitled - Message"),
            before(),
            after_with(),
            windows=[compose],
            windows_before=[compose],
        ).holds
        is False
    )
    assert (
        evaluate(
            WindowGone(name_pattern="Untitled - Message"),
            before(),
            after_with(),
            windows=[],
            windows_before=[],
        ).evidence["reason"]
        == "absent_before"
    )


# ---- VF-20: secure targets ----------------------------------------------------------------------


def test_value_equals_on_a_secure_target_is_refused() -> None:
    plain = before()
    secure_before = replace(
        plain,
        elements=[e if e.id != COMPOSER[0] else replace(e, secure=True) for e in plain.elements],
    )
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="hunter2")})
    verdict = evaluate(
        ValueEquals(target_id=COMPOSER[0], text="hunter2"), secure_before, after_with(), live=live
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "secure_target"
    assert "hunter2" not in str(verdict.evidence)


# ---- VF-37: the deadline bounds the whole verify including observes ----------------------------


def test_slow_observe_is_not_followed_by_a_sleep_and_never_overshoots(clock: Any) -> None:
    def slow(**_: Any) -> Snapshot:
        clock["t"] += 0.6
        return after_with()

    verdict = verify(HELLO, before(), after_with(), slow, deadline_s=3.0, poll_s=0.25)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert clock["t"] <= 3.0 + 0.6
    # an observe is never started when less than its last duration remains
    assert verdict.polls <= 4
