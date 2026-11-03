"""Rules from the principal's re-review of the T1 verifier (B1..B7 and the non-blocking items)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import steerai.verify.t1 as t1_mod
from steerai.errors import ErrorCode
from steerai.verify.predicates import (
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    TitleMatches,
    ValueEquals,
    Within,
)
from steerai.verify.strength import Params, strength
from steerai.verify.t1 import evaluate, label_of_record, redact, verify
from steerai.world.model import Snapshot
from tests.verify.fakes import (
    BASE_ROWS,
    CHAT_AMIR,
    COMPOSER,
    COMPOSER_AREA,
    CONVERSATION,
    SEND,
    FakeHandle,
    FakeLive,
    after_with,
    before,
    build,
)

CONVERSATION_SCOPE = Within(landmark="Conversation")
HELLO = Appeared(role="Text", name_pattern="hello there", within=CONVERSATION_SCOPE)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    monkeypatch.setattr(t1_mod, "time", SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep))
    return now


# ---- B2: anchors resolve by identity, not by positional id ------------------------------------


def _shifted_rows(*inserted: tuple[int, str, str, tuple[int, ...], int | None]) -> list[Any]:
    """BASE_ROWS with `inserted` rows placed in the conversation: every positional id from the
    composer area onwards shifts by the number of insertions, as a real snapshot would."""
    shift = len(inserted)
    rows: list[Any] = []
    for row in BASE_ROWS:
        if row[0] >= COMPOSER_AREA[0]:
            parent = row[4] + shift if row[4] is not None and row[4] >= COMPOSER_AREA[0] else row[4]
            rows.append((row[0] + shift, row[1], row[2], row[3], parent))
        else:
            rows.append(row)
    for offset, extra in enumerate(inserted):
        rows.insert(COMPOSER_AREA[0] + offset, (COMPOSER_AREA[0] + offset, *extra[1:]))
    return rows


def test_excluding_anchor_follows_the_composer_after_an_insertion() -> None:
    after = build(
        _shifted_rows((0, "Text", "hello there", (42, 33), CONVERSATION[0])),
        snapshot_id="after",
        taken_at=1.0,
    )
    predicate = Appeared(role="Text", name_pattern="hello there", excluding_anchor=COMPOSER[0])
    verdict = evaluate(predicate, before(), after)
    assert verdict.holds is True, verdict


def test_excluding_anchor_still_excludes_the_moved_composer_area() -> None:
    rows = _shifted_rows((0, "Text", "Today", (42, 60), CONVERSATION[0]))
    draft = (40, "Text", "hello there", (42, 43), COMPOSER_AREA[0] + 1)
    after = build([*rows, draft], snapshot_id="after", taken_at=1.0)
    predicate = Appeared(role="Text", name_pattern="hello there", excluding_anchor=COMPOSER[0])
    verdict = evaluate(predicate, before(), after)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "no_match"


def test_vanished_anchor_is_no_label() -> None:
    gone = build(
        [r for r in BASE_ROWS if r[0] not in (COMPOSER[0],)], snapshot_id="after", taken_at=1.0
    )
    predicate = Appeared(role="Text", name_pattern="hello there", excluding_anchor=COMPOSER[0])
    verdict = evaluate(predicate, before(), gone)
    assert verdict.evidence["reason"] == "anchor_vanished"
    assert (
        label_of_record(verdict, predicate=predicate, action_key="k", before=before(), after=gone)
        is None
    )
    scoped = Appeared(role="Text", name_pattern="hello there", within=Within(anchor_id=SEND[0]))
    gone_send = build([r for r in BASE_ROWS if r[0] != SEND[0]], snapshot_id="after", taken_at=1.0)
    assert evaluate(scoped, before(), gone_send).evidence["reason"] == "anchor_vanished"
    count = CountDelta(role="Text", within=Within(anchor_id=SEND[0]), delta=1)
    assert evaluate(count, before(), gone_send).evidence["reason"] == "anchor_vanished"


# ---- B1: the before value cannot be read after the fact ----------------------------------------


def test_stable_non_empty_mismatch_never_settles(clock: Any) -> None:
    live = FakeLive(states={COMPOSER[3]: FakeHandle(value="draft")})
    verdict = verify(
        ValueEquals(target_id=COMPOSER[0], text="hello"),
        before(),
        after_with(),
        lambda **_: after_with(),
        live=live,
    )
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.polls == 12


# ---- B3: no-label reasons come first, siblings_changed is no label, AnyOf undecidable ---------


def test_rerendered_scope_holding_the_text_twice_is_churn_not_a_failed_label() -> None:
    fresh = [
        r
        if r[4] != CONVERSATION[0] and r[0] != CONVERSATION[0]
        else (r[0], r[1], r[2], (43, r[3][1]), r[4])
        for r in BASE_ROWS
    ] + [
        (30, "Text", "hello there", (43, 33), CONVERSATION[0]),
        (31, "Text", "hello there", (43, 35), CONVERSATION[0]),
    ]
    after = build(fresh, snapshot_id="after", taken_at=1.0)
    verdict = evaluate(HELLO, before(), after)
    assert verdict.evidence["reason"] == "scope_churn"
    assert (
        label_of_record(verdict, predicate=HELLO, action_key="k", before=before(), after=after)
        is None
    )


def test_siblings_changed_is_no_label() -> None:
    predicate = Disappeared(
        role="ListItem", name_pattern="Amir · hi bro · 2h", within=Within(landmark="Chats")
    )
    after = build(
        [r for r in BASE_ROWS if r[0] not in (CHAT_AMIR[0], 4)], snapshot_id="after", taken_at=1.0
    )
    verdict = evaluate(predicate, before(), after)
    assert verdict.evidence["reason"] == "siblings_changed"
    assert (
        label_of_record(verdict, predicate=predicate, action_key="k", before=before(), after=after)
        is None
    )


def test_any_of_with_an_undecidable_branch_is_no_label() -> None:
    twin = (20, "Group", "Conversation", (42, 60), 0)
    ambiguous_before = build([*BASE_ROWS, twin], snapshot_id="before")
    ambiguous_after = build([*BASE_ROWS, twin], snapshot_id="after", taken_at=1.0)
    predicate = AnyOf((HELLO, TitleMatches(pattern="Nothing")))
    verdict = evaluate(predicate, ambiguous_before, ambiguous_after)
    assert verdict.evidence["reason"] == "ambiguous_landmark"
    assert (
        label_of_record(
            verdict,
            predicate=predicate,
            action_key="k",
            before=ambiguous_before,
            after=ambiguous_after,
        )
        is None
    )


# ---- B4: the verdict names the deciding snapshot -------------------------------------------------


def test_verdict_and_label_cite_the_poll_that_decided(clock: Any) -> None:
    polls = iter(
        [
            after_with(),
            build(
                [*BASE_ROWS, (30, "Text", "hello there", (42, 33), CONVERSATION[0])],
                snapshot_id="poll-2",
                taken_at=2.0,
            ),
        ]
    )
    verdict = verify(HELLO, before(), after_with(), lambda **_: next(polls))
    assert verdict.holds is True
    assert verdict.after_id == "poll-2"
    label = label_of_record(
        verdict, predicate=HELLO, action_key="k", before=before(), after=after_with()
    )
    assert label is not None
    assert label.after_id == "poll-2"


# ---- B5: the label never carries the landmark name ---------------------------------------------


def test_redact_keeps_only_the_scope_kind_for_landmarks() -> None:
    spec = redact(Appeared(role="Text", name_pattern="x", within=Within(landmark="Amir Khan")))
    assert spec["within"] == {"kind": "landmark"}, "no name and no digest of it"
    assert "amir" not in str(spec).casefold()
    anchored = redact(CountDelta(role="Text", within=Within(anchor_id=5), delta=1))
    assert anchored["within"] == {"kind": "anchor", "anchor_id": 5}


def test_vanished_live_target_is_no_label(clock: Any) -> None:
    # The live analogue of a vanished anchor: a re-keyed composer is not a failed step.
    predicate = ValueEquals(target_id=COMPOSER[0], text="hello")
    gone = build([r for r in BASE_ROWS if r[0] != COMPOSER[0]], snapshot_id="after", taken_at=1.0)
    verdict = verify(predicate, before(), gone, lambda **_: gone, live=FakeLive())
    assert verdict.evidence["reason"] == "target_not_found"
    assert (
        label_of_record(verdict, predicate=predicate, action_key="k", before=before(), after=gone)
        is None
    )


def test_the_message_text_itself_is_never_a_status_token() -> None:
    # A bubble row whose Text child is the message: "please retry" is content, not a status.
    retry = Appeared(role="ListItem", name_pattern="please retry", within=CONVERSATION_SCOPE)
    bubble = after_with(
        (30, "ListItem", "please retry", (42, 33), CONVERSATION[0]),
        (31, "Text", "please retry", (42, 34), 30),
    )
    assert evaluate(retry, before(), bubble).holds is True
    sending = Appeared(
        role="ListItem", name_pattern="sending you the file", within=CONVERSATION_SCOPE
    )
    bubble = after_with(
        (30, "ListItem", "sending you the file", (42, 33), CONVERSATION[0]),
        (31, "Text", "sending you the file", (42, 34), 30),
        (32, "Text", "Sending…", (42, 35), 30),
    )
    assert evaluate(sending, before(), bubble).evidence["reason"] == "pending_status"


def test_window_exists_needs_the_before_list() -> None:
    from steerai.verify.predicates import WindowExists  # noqa: PLC0415 - local to this check

    windows = [SimpleNamespace(title="Save As", hwnd=0x100, pid=50)]
    with pytest.raises(ValueError, match="before"):
        evaluate(WindowExists(name_pattern="Save As"), before(), after_with(), windows=windows)


# ---- B6: the sleep leaves room for the next observe --------------------------------------------


def test_sleep_shrinks_so_the_next_observe_fits_inside_the_deadline(clock: Any) -> None:
    def observe(**_: Any) -> Snapshot:
        clock["t"] += 0.2
        return after_with()

    verdict = verify(HELLO, before(), after_with(), observe, deadline_s=1.0, poll_s=0.25)
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert clock["t"] <= 1.0 + 1e-9


# ---- B7: pending placeholders under the new element are a miss ---------------------------------


def test_pending_status_under_the_new_element_keeps_polling(clock: Any) -> None:
    pending = after_with(
        (30, "Text", "hello there", (42, 33), CONVERSATION[0]),
        (31, "Group", "meta", (42, 34), 30),
        (32, "Text", "Sending…", (42, 35), 31),
    )
    verdict = evaluate(HELLO, before(), pending)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "pending_status"
    assert verify(HELLO, before(), pending, lambda **_: pending).code is ErrorCode.VERIFY_TIMEOUT


def test_failed_status_is_found_in_a_deeper_descendant(clock: Any) -> None:
    failed = after_with(
        (30, "Text", "hello there", (42, 33), CONVERSATION[0]),
        (31, "Group", "meta", (42, 34), 30),
        (32, "Text", "Not delivered · Retry", (42, 35), 31),
    )
    assert evaluate(HELLO, before(), failed).evidence["reason"] == "failed_status"


# ---- non-blocking: newest-first lists, title never settles, target labels -----------------------


def test_prepending_list_accepts_a_new_element_at_the_top() -> None:
    rows = [
        (0, "Pane", "Mail", (42, 1), None),
        (1, "List", "Sent", (42, 30), 0),
        (2, "ListItem", "hello there", (42, 33), 1),
        (3, "ListItem", "older", (42, 31), 1),
    ]
    earlier = build([rows[0], rows[1], (2, "ListItem", "older", (42, 31), 1)], snapshot_id="before")
    after = build(rows, snapshot_id="after", taken_at=1.0)
    appending = Appeared(
        role="ListItem", name_pattern="hello there", within=Within(landmark="Sent")
    )
    assert evaluate(appending, earlier, after).evidence["reason"] == "scope_shifted"
    prepending = Appeared(
        role="ListItem", name_pattern="hello there", within=Within(landmark="Sent"), order="prepend"
    )
    assert evaluate(prepending, earlier, after).holds is True


def test_title_unchanged_only_times_out(clock: Any) -> None:
    verdict = verify(TitleMatches(pattern="Sara"), before(), after_with(), lambda **_: after_with())
    assert verdict.code is ErrorCode.VERIFY_TIMEOUT
    assert verdict.evidence["reason"] == "title_unchanged"


def test_a_clicked_targets_label_is_not_content_for_appeared() -> None:
    params = Params(target_id=7, target_name="Delete chat", target_role="Button")
    weak = strength(Appeared(role="Text", name_pattern="Delete chat"), "R3", params)
    assert weak.ok is False
    assert weak.reason == "pattern_not_a_parameter"
    strong = strength(Disappeared(role="Button", name_pattern="Delete chat"), "R3", params)
    assert strong.ok is True


def test_count_delta_scope_follows_the_anchor_after_an_insertion() -> None:
    after = build(
        _shifted_rows((0, "Text", "hello there", (42, 33), CONVERSATION[0])),
        snapshot_id="after",
        taken_at=1.0,
    )
    count = CountDelta(role="Button", within=Within(anchor_id=SEND[0]), delta=1)
    verdict = evaluate(count, before(), after)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "count_differs"
    assert verdict.evidence["before"] == 1
    assert verdict.evidence["after"] == 1, "the composer area still holds one button"
