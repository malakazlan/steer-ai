"""T1 step check (design 5.10): before-anchored evaluation, polling with the readback contract,
labels of record."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import steerai.verify.t1 as t1_mod
from steerai.errors import ErrorCode
from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    Focused,
    State,
    TitleMatches,
    ValueEquals,
    WindowExists,
    Within,
)
from steerai.verify.t1 import Label, Verdict, evaluate, label_of_record, title_holds, verify
from steerai.world.model import Snapshot
from tests.verify.fakes import (
    BASE_ROWS,
    BUBBLE_OUT,
    CHAT_AMIR,
    COMPOSER,
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

NEW_BUBBLE = (30, "Text", "hello there", (42, 33), CONVERSATION[0])
CONVERSATION_SCOPE = Within(landmark="Conversation")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    monkeypatch.setattr(t1_mod, "time", SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep))
    return now


def _windows(*names: str) -> list[Any]:
    return [SimpleNamespace(title=n, hwnd=0x100 + i, pid=50 + i) for i, n in enumerate(names)]


# ---- appeared / disappeared / count ------------------------------------------------------------


def test_appeared_holds_for_a_new_element_matching_in_scope() -> None:
    verdict = evaluate(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
        before(),
        after_with(NEW_BUBBLE),
    )
    assert verdict.holds is True
    assert verdict.level == 1
    assert verdict.evidence == {"level": 1, "matched": 1}


def test_appeared_needs_a_new_runtime_id_not_just_presence() -> None:
    old = (30, "Text", "hello there", (42, 33), CONVERSATION[0])
    verdict = evaluate(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
        build([*BASE_ROWS, old], snapshot_id="before"),
        build([*BASE_ROWS, old], snapshot_id="after"),
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "not_new"


def test_appeared_ignores_a_new_element_outside_the_scope() -> None:
    toast = (31, "Text", "hello there", (42, 51), TOAST_AREA[0])
    verdict = evaluate(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
        before(),
        after_with(toast),
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "no_match"


def test_appeared_window_wide_is_strict_and_skips_transient_areas() -> None:
    toast = (31, "Text", "hello there", (42, 51), TOAST_AREA[0])
    assert (
        evaluate(
            Appeared(role="Text", name_pattern="hello there"), before(), after_with(toast)
        ).holds
        is False
    )
    assert (
        evaluate(
            Appeared(role="Text", name_pattern="hello there"), before(), after_with(NEW_BUBBLE)
        ).holds
        is True
    )
    assert (
        evaluate(
            Appeared(role="Text", name_pattern="hello"), before(), after_with(NEW_BUBBLE)
        ).holds
        is False
    )


def test_appeared_excluding_the_composer_subtree() -> None:
    draft = (31, "Text", "hello there", (42, 43), COMPOSER[4])
    predicate = Appeared(role="Text", name_pattern="hello there", excluding_anchor=COMPOSER[0])
    assert evaluate(predicate, before(), after_with(draft)).holds is False
    assert evaluate(predicate, before(), after_with(NEW_BUBBLE)).holds is True


def test_appeared_with_an_ambiguous_landmark_never_holds() -> None:
    twin = (20, "Group", "Conversation", (42, 60), 0)
    verdict = evaluate(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
        build([*BASE_ROWS, twin], snapshot_id="before"),
        build([*BASE_ROWS, twin, NEW_BUBBLE], snapshot_id="after"),
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "ambiguous_landmark"


def test_appeared_on_different_windows_is_refused() -> None:
    other = build(BASE_ROWS, snapshot_id="after", hwnd=0x99)
    with pytest.raises(ValueError, match="different windows"):
        evaluate(Appeared(role="Text", name_pattern="x"), before(), other)


def test_disappeared_holds_when_the_runtime_id_is_gone() -> None:
    verdict = evaluate(
        Disappeared(
            role="ListItem",
            name_pattern="Amir \u00b7 hi bro \u00b7 2h",
            within=Within(landmark="Chats"),
        ),
        before(),
        after_without(CHAT_AMIR[0]),
    )
    assert verdict.holds is True
    assert verdict.evidence == {"level": 1, "matched": 1}


def test_disappeared_is_false_when_nothing_matched_before() -> None:
    verdict = evaluate(Disappeared(role="ListItem", name_pattern="Nobody"), before(), before())
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "absent_before"


def test_disappeared_is_false_while_any_match_remains() -> None:
    verdict = evaluate(
        Disappeared(
            role="ListItem",
            name_pattern="Amir \u00b7 hi bro \u00b7 2h",
            within=Within(landmark="Chats"),
        ),
        before(),
        after_with(),
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "still_present"


def test_count_delta_compares_scoped_counts() -> None:
    minus = CountDelta(role="ListItem", within=Within(landmark="Chats"), delta=-1)
    assert evaluate(minus, before(), after_without(CHAT_AMIR[0])).holds is True
    assert evaluate(minus, before(), after_with()).holds is False
    plus = CountDelta(role="Text", within=CONVERSATION_SCOPE, delta=1)
    assert evaluate(plus, before(), after_with(NEW_BUBBLE)).holds is True
    assert (
        evaluate(plus, before(), after_with(NEW_BUBBLE, (32, "Text", "x", (42, 34), 8))).holds
        is False
    )


# ---- live reads: value, focus, state -----------------------------------------------------------


def test_value_equals_reads_the_resolved_target_live() -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="hello there")})
    verdict = evaluate(
        ValueEquals(target_id=COMPOSER[0], text="hello there"), before(), after_with(), live=live
    )
    assert verdict.holds is True
    assert verdict.evidence == {}


def test_value_equals_normalises_line_endings_and_null_values() -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="a\r\nb")})
    assert (
        evaluate(
            ValueEquals(target_id=COMPOSER[0], text="a\nb"), before(), after_with(), live=live
        ).holds
        is True
    )
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value=None)})
    assert (
        evaluate(
            ValueEquals(target_id=COMPOSER[0], text=""), before(), after_with(), live=live
        ).holds
        is True
    )


def test_value_equals_without_live_reads_is_refused() -> None:
    with pytest.raises(ValueError, match="live"):
        evaluate(ValueEquals(target_id=COMPOSER[0], text="x"), before(), after_with())


def test_value_equals_on_a_vanished_target_is_false_with_a_token() -> None:
    live = FakeLive()
    verdict = evaluate(
        ValueEquals(target_id=COMPOSER[0], text="x"),
        before(),
        after_without(COMPOSER[0]),
        live=live,
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "target_not_found"
    assert verdict.state == "target_not_found"


def test_value_equals_target_identity_is_rechecked_in_the_after_snapshot() -> None:
    renamed = build(
        [r if r[0] != COMPOSER[0] else (12, "Edit", "Subject", (42, 41), 11) for r in BASE_ROWS],
        snapshot_id="after",
    )
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="x")})
    verdict = evaluate(ValueEquals(target_id=COMPOSER[0], text="x"), before(), renamed, live=live)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "target_not_found"


def test_value_equals_on_a_text_role_uses_the_name() -> None:
    verdict = evaluate(
        ValueEquals(target_id=BUBBLE_OUT[0], text="are we still on for friday?"),
        before(),
        after_with(),
        live=FakeLive(),
    )
    assert verdict.holds is True


def test_focused_and_state_read_live() -> None:
    live = FakeLive(focused=SEND[3], states={DELETE[3]: FakeHandle(toggle=1, selected=True)})
    assert evaluate(Focused(target_id=SEND[0]), before(), after_with(), live=live).holds is True
    assert evaluate(Focused(target_id=DELETE[0]), before(), after_with(), live=live).holds is False
    assert (
        evaluate(State(target_id=DELETE[0], wanted="on"), before(), after_with(), live=live).holds
        is True
    )
    assert (
        evaluate(State(target_id=DELETE[0], wanted="off"), before(), after_with(), live=live).holds
        is False
    )
    assert (
        evaluate(
            State(target_id=DELETE[0], wanted="selected"), before(), after_with(), live=live
        ).holds
        is True
    )


def test_state_on_an_element_without_the_pattern_is_false() -> None:
    live = FakeLive(states={DELETE[3]: FakeHandle()})
    verdict = evaluate(State(target_id=DELETE[0], wanted="on"), before(), after_with(), live=live)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "no_pattern"


# ---- title and windows ------------------------------------------------------------------------


def test_title_matches_ignores_counters_and_case() -> None:
    earlier = build(BASE_ROWS, snapshot_id="before", title="Chats")
    assert evaluate(TitleMatches(pattern="Amir Khan - Chat"), earlier, after_with()).holds is True
    counted = build(BASE_ROWS, snapshot_id="after", title="(3) Amir Khan - Chat")
    assert evaluate(TitleMatches(pattern="amir khan - chat"), earlier, counted).holds is True
    assert evaluate(TitleMatches(pattern="Sara"), earlier, after_with()).holds is False


def test_window_exists_consults_the_window_list_and_skips_our_own_process() -> None:
    windows = _windows("Save As", "Amir Khan - Chat")

    def check(pattern: str, *, own_pid: int | None = None) -> bool:
        return evaluate(
            WindowExists(name_pattern=pattern),
            before(),
            after_with(),
            windows=windows,
            windows_before=[],
            own_pid=own_pid,
        ).holds

    assert check("Save As") is True
    assert check("save as") is True
    assert check("Save") is False
    own = [SimpleNamespace(title="Steer", hwnd=0x1, pid=4242)]
    assert (
        evaluate(
            WindowExists(name_pattern="Steer"),
            before(),
            after_with(),
            windows=own,
            windows_before=[],
            own_pid=4242,
        ).holds
        is False
    )


def test_window_exists_without_a_window_list_is_refused() -> None:
    with pytest.raises(ValueError, match="windows"):
        evaluate(WindowExists(name_pattern="x"), before(), after_with(), windows_before=[])


# ---- conjunctions ------------------------------------------------------------------------------


def test_all_and_any_of_combine_verdicts() -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="")})
    send = All(
        (
            Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
            AnyOf(
                (
                    ValueEquals(target_id=COMPOSER[0], text=""),
                    Disappeared(role="Edit", name_pattern="Message"),
                )
            ),
        )
    )
    assert evaluate(send, before(), after_with(NEW_BUBBLE), live=live).holds is True
    assert evaluate(send, before(), after_with(), live=live).holds is False
    assert evaluate(send, before(), after_with(), live=live).evidence["reason"] == "no_match"


# ---- verify: polling, settled contradictions, deadline -----------------------------------------


def _observer(snapshots: list[Snapshot]) -> Any:
    calls = {"n": 0}

    def observe(**_: Any) -> Snapshot:
        calls["n"] += 1
        return snapshots[min(calls["n"] - 1, len(snapshots) - 1)]

    observe.calls = calls  # type: ignore[attr-defined]
    return observe


def test_verify_uses_the_given_after_snapshot_before_observing(clock: Any) -> None:
    observe = _observer([after_with()])
    verdict = verify(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
        before(),
        after_with(NEW_BUBBLE),
        observe,
    )
    assert verdict.holds is True
    assert verdict.code is None
    assert observe.calls["n"] == 0
    assert verdict.polls == 0


def test_verify_polls_until_the_effect_lands(clock: Any) -> None:
    observe = _observer([after_with(), after_with(), after_with(NEW_BUBBLE)])
    verdict = verify(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
        before(),
        after_with(),
        observe,
    )
    assert verdict.holds is True
    assert verdict.polls == 3
    assert clock["t"] == pytest.approx(0.75)


def test_verify_times_out_at_the_deadline_without_polling_past_it(clock: Any) -> None:
    observe = _observer([after_with()])
    verdict = verify(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
        before(),
        after_with(),
        observe,
        deadline_s=3.0,
        poll_s=0.25,
    )
    assert verdict.holds is False
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert clock["t"] <= 3.0 + 1e-9
    assert verdict.polls == 12


def test_verify_settles_an_empty_value_inside_the_deadline(clock: Any) -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="")})
    verdict = verify(
        ValueEquals(target_id=COMPOSER[0], text="hello there"),
        before(),
        after_with(),
        _observer([after_with()]),
        live=live,
    )
    assert verdict.holds is False
    assert verdict.code is ErrorCode.VERIFY_FAILED
    assert verdict.polls == 1
    assert verdict.evidence["reason"] == "value_empty"


def test_verify_never_settles_appeared_or_count(clock: Any) -> None:
    verdict = verify(
        CountDelta(role="Text", within=CONVERSATION_SCOPE, delta=1),
        before(),
        after_with(),
        _observer([after_with()]),
    )
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT


def test_verify_only_times_out_when_a_disappeared_target_stays(clock: Any) -> None:
    verdict = verify(
        Disappeared(
            role="ListItem",
            name_pattern="Amir \u00b7 hi bro \u00b7 2h",
            within=Within(landmark="Chats"),
        ),
        before(),
        after_with(),
        _observer([after_with()]),
    )
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT


def test_verify_reobserves_with_a_single_stability_fetch(clock: Any) -> None:
    seen: list[dict[str, Any]] = []

    def observe(**kwargs: Any) -> Snapshot:
        seen.append(kwargs)
        return after_with(NEW_BUBBLE)

    verify(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
        before(),
        after_with(),
        observe,
    )
    assert seen == [{"stable_window": 1}]


def test_verify_rejects_bad_timing() -> None:
    with pytest.raises(ValueError, match="poll_s"):
        verify(Focused(target_id=1), before(), after_with(), _observer([after_with()]), poll_s=0)
    with pytest.raises(ValueError, match="deadline_s"):
        verify(
            Focused(target_id=1), before(), after_with(), _observer([after_with()]), deadline_s=-1
        )


def test_verify_treats_an_observe_error_as_timeout_evidence(clock: Any) -> None:
    def observe(**_: Any) -> Snapshot:
        raise OSError("UIA gone")

    verdict = verify(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE),
        before(),
        after_with(),
        observe,
    )
    assert verdict.holds is False
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.evidence["reason"] == "observe_failed"


# ---- labels of record ---------------------------------------------------------------------------


def test_label_of_record_carries_the_action_key_and_redacts_the_pattern() -> None:
    predicate = Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE)
    verdict = Verdict(
        holds=True,
        code=None,
        level=1,
        polls=2,
        evidence={"level": 1, "matched": 1},
        after_id="after-poll-2",
    )
    label = label_of_record(
        verdict,
        predicate=predicate,
        action_key="k-1",
        before=before(),
        after=after_with(NEW_BUBBLE),
    )
    assert isinstance(label, Label)
    assert label.kind == "t1"
    assert label.holds is True
    assert label.action_key == "k-1"
    assert label.before_id == "before"
    assert label.after_id == "after-poll-2", "the deciding snapshot, not the caller's after"
    assert label.predicate["kind"] == "Appeared"
    assert label.predicate["role"] == "Text"
    assert label.predicate["within"] == {"kind": "landmark"}
    text = str(label.to_dict())
    assert "hello there" not in text
    assert "conversation" not in text, "landmark names are contact names in chat apps"


def test_label_of_record_is_never_made_from_a_thrown_verify() -> None:
    verdict = Verdict(
        holds=False,
        code=ErrorCode.VERIFY_TIMEOUT,
        level=0,
        polls=2,
        evidence={"reason": "observe_failed"},
    )
    label = label_of_record(
        verdict, predicate=Focused(target_id=1), action_key="k", before=before(), after=after_with()
    )
    assert label is None


def test_a_title_pattern_ignores_leading_and_trailing_counters() -> None:
    """VF-31 pin: the `title ~` rule strips a counter at either end and keeps inner digits
    (the page-name counter is a separate rule; an unpinned shadowing once went unseen)."""
    assert title_holds("Inbox", "Inbox (3) - Gmail")
    assert title_holds("Inbox - Gmail", "(12) Inbox - Gmail")
    assert title_holds("Inbox - Gmail", "Inbox - Gmail (4)")
    assert not title_holds("Inbox 3", "Inbox - Gmail")
