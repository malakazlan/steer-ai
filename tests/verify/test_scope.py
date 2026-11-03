"""Scope ladder (design 5.10 [v0.3]) and transient exclusion ([v0.4]), pure over elements."""

from __future__ import annotations

from steerai.verify.predicates import Within
from steerai.verify.scope import resolve_scope
from tests.verify.fakes import (
    BASE_ROWS,
    BUBBLE_OK,
    BUBBLE_OUT,
    COMPOSER,
    CONVERSATION,
    DELETE,
    SEND,
    TITLE,
    TOAST_AREA,
    before,
    build,
)


def test_landmark_resolves_to_the_named_container_subtree() -> None:
    scope = resolve_scope(before(), Within(landmark="Conversation"))
    assert scope.level == 1
    assert scope.container_id == CONVERSATION[0]
    assert scope.ids == {CONVERSATION[0], BUBBLE_OK[0], BUBBLE_OUT[0]}
    assert scope.strict is False


def test_landmark_is_matched_by_normalised_name_not_substring() -> None:
    assert resolve_scope(before(), Within(landmark=" conversation ")).level == 1
    assert resolve_scope(before(), Within(landmark="Conv")).level == 3


def test_ambiguous_landmark_resolves_to_nothing() -> None:
    twin = (20, "Group", "Conversation", (42, 60), 0)
    scope = resolve_scope(build([*BASE_ROWS, twin]), Within(landmark="Conversation"))
    assert scope.level == 0
    assert scope.ids == set()
    assert scope.reason == "ambiguous_landmark"


def test_landmark_must_be_a_container_role() -> None:
    scope = resolve_scope(before(), Within(landmark="Send"))
    assert scope.level == 3


def test_anchor_resolves_to_its_nearest_container_ancestor() -> None:
    scope = resolve_scope(before(), Within(anchor_id=SEND[0]))
    assert scope.level == 2
    assert scope.container_id == COMPOSER[4]
    assert COMPOSER[0] in scope.ids
    assert SEND[0] in scope.ids
    assert BUBBLE_OK[0] not in scope.ids


def test_anchor_that_is_itself_a_container_scopes_to_its_own_subtree() -> None:
    scope = resolve_scope(before(), Within(anchor_id=CONVERSATION[0]))
    assert scope.level == 2
    assert scope.container_id == CONVERSATION[0]


def test_unknown_anchor_falls_through_to_window_wide_strict() -> None:
    scope = resolve_scope(before(), Within(anchor_id=999))
    assert scope.level == 3
    assert scope.strict is True
    assert TITLE[0] in scope.ids
    assert DELETE[0] in scope.ids


def test_no_scope_is_window_wide_strict() -> None:
    scope = resolve_scope(before(), None)
    assert scope.level == 3
    assert scope.strict is True


def test_transient_subtrees_are_excluded_from_every_level() -> None:
    toast = (21, "Text", "Message sent", (42, 51), TOAST_AREA[0])
    tooltip = (22, "ToolTip", "Send (Enter)", (42, 52), 0)
    progress = (23, "ProgressBar", "Sending", (42, 53), CONVERSATION[0])
    snapshot = build([*BASE_ROWS, toast, tooltip, progress])
    window_wide = resolve_scope(snapshot, None)
    assert toast[0] not in window_wide.ids
    assert TOAST_AREA[0] not in window_wide.ids
    assert tooltip[0] not in window_wide.ids
    assert progress[0] not in window_wide.ids
    conversation = resolve_scope(snapshot, Within(landmark="Conversation"))
    assert progress[0] not in conversation.ids


def test_excluding_an_anchor_removes_its_subtree() -> None:
    scope = resolve_scope(before(), None, excluding=COMPOSER[4])
    assert COMPOSER[0] not in scope.ids
    assert SEND[0] not in scope.ids
    assert BUBBLE_OK[0] in scope.ids
