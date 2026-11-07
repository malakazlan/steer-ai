"""`present` (#124, 5.10 [v2.36]): a state, judged on the after snapshot alone, holding for
exactly one visible element of the role and full name in a named landmark, never inside the
clicked target's own container; refused above R1 (hunter PS-02/05/06/13/30/35/39/40)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from steerai.verify.predicates import All, Present, Within, presents
from steerai.verify.strength import Params, strength
from steerai.verify.t1 import evaluate, redact
from steerai.world.model import Snapshot
from tests.verify.fakes import (
    BASE_ROWS,
    CHAT_AMIR,
    CHAT_LIST,
    CONVERSATION,
    TITLE,
    WINDOW,
    Row,
    build,
)

DOC: Row = (30, "Document", "Steer probe inbox", (42, 2), WINDOW[0])
"""The page: every top-level region of the fake inbox moves under it, as in a browser."""
PAGE = Within(landmark="Steer probe inbox")
HEADER_TEXT = Present(role="Text", name_pattern="Amir Khan", within=PAGE)


def _rows(*extra: Row, drop: tuple[int, ...] = ()) -> list[Row]:
    moved = [
        (r[0], r[1], r[2], r[3], DOC[0] if r[4] == WINDOW[0] else r[4])
        for r in BASE_ROWS
        if r[0] not in drop
    ]
    return [WINDOW, DOC, *moved[1:], *extra]


def before() -> Snapshot:
    return build(_rows(), snapshot_id="before")


def after_with(*extra: Row) -> Snapshot:
    return build(_rows(*extra), snapshot_id="after", taken_at=1.0)


def _bound(predicate: Present = HEADER_TEXT, *, content: tuple[str, ...] = ()) -> Present:
    return replace(predicate, target_id=CHAT_AMIR[0], excluding=content)


def _without(*ids: int, extra: tuple[Row, ...] = ()) -> Snapshot:
    return build(_rows(*extra, drop=ids), snapshot_id="after", taken_at=1.0)


def test_present_holds_on_the_after_snapshot_alone() -> None:
    """A state: already true before is no reason to fail (the chat was open)."""
    verdict = evaluate(_bound(), before(), after_with())
    assert verdict.holds is True
    assert verdict.evidence == {"level": 1, "matched": 1}


def test_present_is_false_when_no_element_matches() -> None:
    verdict = evaluate(_bound(), before(), _without(TITLE[0]))
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "absent"


def test_present_is_full_string_never_a_substring() -> None:
    renamed = (TITLE[0], "Text", "Amir Khan, online", TITLE[3], TITLE[4])
    assert evaluate(_bound(), before(), _without(TITLE[0], extra=(renamed,))).holds is False


def test_present_ignores_case_in_scope() -> None:
    lower = replace(HEADER_TEXT, name_pattern="amir khan")
    assert evaluate(_bound(lower), before(), after_with()).holds is True


def test_two_matches_are_ambiguous() -> None:
    bubble = (20, "Text", "Amir Khan", (42, 70), 8)
    verdict = evaluate(_bound(), before(), after_with(bubble))
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "ambiguous_match"


def test_the_clicked_rows_container_never_counts() -> None:
    """The row's own name Text inside the chat list proves nothing about the open chat."""
    row_text = (20, "Text", "Amir Khan", (42, 71), CHAT_AMIR[0])
    verdict = evaluate(_bound(), before(), _without(TITLE[0], extra=(row_text,)))
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "absent"


def test_unbound_presence_counts_the_row_text() -> None:
    """The exclusion comes from the bound target: a subgoal condition carries none, which is
    why a present condition ticks only through its own click (plan boundary)."""
    row_text = (20, "Text", "Amir Khan", (42, 71), CHAT_AMIR[0])
    assert evaluate(HEADER_TEXT, before(), _without(TITLE[0], extra=(row_text,))).holds is True


def test_a_vanished_target_container_is_a_miss() -> None:
    verdict = evaluate(_bound(), before(), _without(CHAT_LIST[0], CHAT_AMIR[0], 4))
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "target_container_gone"


def test_a_missing_landmark_never_falls_back_to_window_wide() -> None:
    elsewhere = replace(HEADER_TEXT, within=Within(landmark="Somewhere else"))
    verdict = evaluate(_bound(elsewhere), before(), after_with())
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "landmark_absent"


def test_an_ambiguous_landmark_never_holds() -> None:
    header = replace(HEADER_TEXT, within=Within(landmark="Header"))
    twin = (20, "Group", "Header", (42, 72), DOC[0])
    verdict = evaluate(_bound(header), before(), after_with(twin))
    assert verdict.holds is False
    assert verdict.evidence["level"] == 0


def test_an_offscreen_or_zero_area_match_never_counts() -> None:
    snapshot = after_with()
    elements = [replace(e, offscreen=True) if e.id == TITLE[0] else e for e in snapshot.elements]
    assert evaluate(_bound(), before(), replace(snapshot, elements=elements)).holds is False
    flat = [replace(e, box=(0, 0, 0, 0)) if e.id == TITLE[0] else e for e in snapshot.elements]
    assert evaluate(_bound(), before(), replace(snapshot, elements=flat)).holds is False


def test_a_match_in_a_transient_area_never_counts() -> None:
    toast_title = (20, "Text", "Amir Khan", (42, 73), 14)
    verdict = evaluate(_bound(), before(), _without(TITLE[0], extra=(toast_title,)))
    assert verdict.holds is False


def test_an_unstable_snapshot_never_holds() -> None:
    assert evaluate(_bound(), before(), replace(after_with(), stable=False)).holds is False


def test_present_needs_a_within_and_a_clean_pattern() -> None:
    with pytest.raises(TypeError):
        Present(role="Text", name_pattern="Amir Khan")  # type: ignore[call-arg]  # the refusal under test
    with pytest.raises(ValueError, match="control"):
        Present(role="Text", name_pattern="Amir\u200bKhan", within=PAGE)
    with pytest.raises(ValueError, match="empty"):
        Present(role="Text", name_pattern="   ", within=PAGE)
    with pytest.raises(ValueError, match="role"):
        Present(role=" ", name_pattern="Amir Khan", within=PAGE)


def test_redact_keeps_the_role_and_the_scope_kind_only() -> None:
    spec = redact(_bound())
    assert spec == {"kind": "Present", "role": "Text", "within": {"kind": "landmark"}}
    assert "Amir" not in repr(_bound())


def test_strength_refuses_present_above_r1() -> None:
    params = Params(target_id=CHAT_AMIR[0], target_name="Amir Khan")
    assert strength(HEADER_TEXT, "R0", params).ok
    assert strength(HEADER_TEXT, "R1", params).ok
    for tier in ("R2", "R3"):
        verdict = strength(HEADER_TEXT, tier, params)
        assert not verdict.ok
        assert verdict.reason == "present_alone"


def test_presents_finds_every_part_at_any_depth() -> None:
    other = replace(HEADER_TEXT, name_pattern="Sara Ahmed")
    nested = All((HEADER_TEXT, All((other, HEADER_TEXT))))
    assert presents(nested) == [HEADER_TEXT, other, HEADER_TEXT]
    assert presents(HEADER_TEXT) == [HEADER_TEXT]


def test_an_exact_name_bubble_in_the_content_landmark_never_counts() -> None:
    """Bob's chat stays open after a dropped click and shows a bubble reading exactly "Amir
    Khan": the plan's content landmark (the send's scope) is excluded (principal item 1)."""
    bubble = (20, "Text", "Amir Khan", (42, 70), CONVERSATION[0])
    bob = (TITLE[0], "Text", "Bob", TITLE[3], TITLE[4])
    snapshot = _without(TITLE[0], extra=(bob, bubble))
    assert evaluate(_bound(), before(), snapshot).holds is True, "unexcluded: the hole"
    verdict = evaluate(_bound(content=("Conversation",)), before(), snapshot)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "absent"
    assert evaluate(_bound(content=("Conversation",)), before(), after_with()).holds is True


def test_an_unresolved_content_landmark_is_a_miss() -> None:
    verdict = evaluate(
        _bound(content=("Conversation",)), before(), _without(CONVERSATION[0], 9, 10)
    )
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "content_landmark_unresolved"
    twin = (20, "Group", "Conversation", (42, 74), DOC[0])
    verdict = evaluate(_bound(content=("Conversation",)), before(), after_with(twin))
    assert verdict.evidence["reason"] == "content_landmark_unresolved"


def test_the_container_is_found_by_runtime_id_when_its_name_changes() -> None:
    """A chat list whose name carries an unread count that clears on the click (item 2)."""
    renamed = (CHAT_LIST[0], "List", "Chat list (0 unread)", CHAT_LIST[3], CHAT_LIST[4])
    row_text = (20, "Text", "Amir Khan", (42, 71), CHAT_AMIR[0])
    snapshot = _without(CHAT_LIST[0], extra=(renamed, row_text))
    verdict = evaluate(_bound(), before(), snapshot)
    assert verdict.holds is True, verdict.evidence


def test_a_landmark_that_is_the_window_itself_never_holds() -> None:
    """The window holds the browser's tab strip and title (PS-14): never the scope (item 4)."""
    window = replace(HEADER_TEXT, within=Within(landmark="Amir Khan - Chat"))
    verdict = evaluate(_bound(window), before(), after_with())
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "landmark_is_window"


def test_excluding_names_are_normalised_and_never_empty() -> None:
    assert replace(HEADER_TEXT, excluding=(" Conversation ",)).excluding == ("conversation",)
    with pytest.raises(ValueError, match="empty"):
        replace(HEADER_TEXT, excluding=("  ",))


def test_every_predicate_kind_has_an_evaluator() -> None:
    """A kind with no evaluator would raise a KeyError out of `run_task` (hunter PS-31)."""
    import typing  # noqa: PLC0415 - this pin only

    from steerai.verify import predicates, t1  # noqa: PLC0415

    assert set(typing.get_args(predicates.Predicate)) == set(t1._EVALUATORS)
