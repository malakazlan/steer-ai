"""Fingerprint boundaries: dynamic-only screens, empty landmark sets, thresholds of 0 and 1,
equality at the threshold, case-insensitive process, title as a landmark, minimum landmarks."""

import pytest

from steerai.world.elements import Element
from steerai.world.fingerprint import Fingerprint, landmarks, similarity

BOX = (0, 0, 10, 10)


def _el(idx: int, role: str, name: str = "", automation_id: str = "") -> Element:
    return Element(id=idx, depth=1, role=role, name=name, box=BOX, automation_id=automation_id)


def _fp(process: str, *marks: tuple[str, str], title: str = "t") -> Fingerprint:
    return Fingerprint(process=process, title_pattern=title, marks=frozenset(marks))


# --- landmarks --------------------------------------------------------------------------------


def test_landmarks_of_an_empty_list_is_empty() -> None:
    assert landmarks([]) == frozenset()


def test_landmarks_of_only_dynamic_roles_is_empty() -> None:
    dynamic = [
        _el(1, "ListItem", "row"),
        _el(2, "DataItem", "cell"),
        _el(3, "Text", "hello"),
        _el(4, "TreeItem", "node"),
        _el(5, "Image", "pic"),
        _el(6, "Window", "Inbox"),
    ]
    assert landmarks(dynamic) == frozenset()


def test_landmarks_skip_dynamic_roles_even_with_an_automation_id() -> None:
    assert landmarks([_el(1, "ListItem", "row", automation_id="row-1")]) == frozenset()


def test_landmarks_skip_unnamed_elements_without_automation_id() -> None:
    assert landmarks([_el(1, "Button"), _el(2, "Group")]) == frozenset()


def test_landmarks_prefer_automation_id_over_name() -> None:
    marks = landmarks([_el(1, "Button", "Send", automation_id="send")])
    assert marks == frozenset({("Button", "#send")})


def test_landmarks_collapse_duplicate_controls_into_one_mark() -> None:
    marks = landmarks([_el(i, "Button", "Delete") for i in range(1, 11)])
    assert marks == frozenset({("Button", "Delete")})


def test_landmarks_use_the_raw_name_not_the_escaped_one() -> None:
    assert landmarks([_el(1, "Button", "a\nb")]) == frozenset({("Button", "a\nb")})


# --- similarity -------------------------------------------------------------------------------


def test_similarity_with_one_empty_side_is_zero() -> None:
    assert similarity(frozenset({("Button", "a")}), frozenset()) == 0.0
    assert similarity(frozenset(), frozenset({("Button", "a")})) == 0.0


def test_similarity_of_identical_sets_is_one() -> None:
    marks = frozenset({("Button", "a"), ("Edit", "b")})
    assert similarity(marks, marks) == 1.0


def test_similarity_of_disjoint_sets_is_zero() -> None:
    assert similarity(frozenset({("Button", "a")}), frozenset({("Button", "b")})) == 0.0


def test_similarity_is_symmetric() -> None:
    a = frozenset({("Button", "a"), ("Edit", "b"), ("List", "c")})
    b = frozenset({("Button", "a"), ("Edit", "x")})
    assert similarity(a, b) == similarity(b, a) == pytest.approx(0.25)


# --- Fingerprint.matches -----------------------------------------------------------------------


def test_two_empty_fingerprints_never_match() -> None:
    # Edge case WM-04: fewer than MIN_MARKS landmarks is not enough evidence for a screen match.
    empty = _fp("app.exe")
    assert not empty.matches(empty, threshold=0.0)


def test_matches_at_exactly_the_threshold() -> None:
    a = _fp("app.exe", ("B", "1"), ("B", "2"), ("B", "3"), ("B", "4"), ("B", "5"))
    b = _fp("app.exe", ("B", "1"), ("B", "2"), ("B", "3"), ("B", "4"))
    assert similarity(a.marks, b.marks) == 0.8
    assert a.matches(b, threshold=0.8)
    assert not a.matches(b, threshold=0.8000001)


def test_matches_with_threshold_one_requires_identical_marks() -> None:
    a = _fp("app.exe", ("B", "1"), ("B", "2"), ("B", "3"), ("B", "4"))
    b = _fp("app.exe", ("B", "1"), ("B", "2"), ("B", "3"), ("B", "5"))
    assert a.matches(a, threshold=1.0)
    assert not a.matches(b, threshold=1.0)


def test_matches_with_threshold_above_one_never_matches() -> None:
    a = _fp("app.exe", ("B", "1"))
    assert not a.matches(a, threshold=1.0000001)


def test_matches_compares_process_case_insensitively() -> None:
    marks = (("B", "1"), ("B", "2"), ("B", "3"), ("B", "4"))
    assert _fp("Chrome.EXE", *marks).matches(_fp("chrome.exe", *marks), threshold=1.0)


def test_matches_rejects_a_different_process_even_with_identical_marks() -> None:
    a = _fp("chrome.exe", ("B", "1"))
    b = _fp("msedge.exe", ("B", "1"))
    assert not a.matches(b, threshold=0.0)


def test_matches_with_an_unknown_process_never_matches() -> None:
    # Edge case WM-09: an unknown process is a failure state, not a wildcard.
    marks = (("B", "1"), ("B", "2"), ("B", "3"), ("B", "4"))
    assert not _fp("", *marks).matches(_fp("", *marks), threshold=0.0)
    assert not _fp("app.exe", *marks).matches(_fp("", *marks), threshold=0.0)


def test_title_is_a_landmark_so_dialogs_with_the_same_buttons_differ() -> None:
    # Edge case WM-04: "Delete file?" vs "Save changes?" share Yes/No/Cancel but not the title.
    buttons = [_el(1, "Button", "Yes"), _el(2, "Button", "No"), _el(3, "Button", "Cancel")]
    delete = landmarks(buttons, title="Delete file?")
    save = landmarks(buttons, title="Save changes?")
    assert ("title", "delete file?") in delete
    assert similarity(delete, save) == 0.6
    assert not _fp("app.exe", *delete).matches(_fp("app.exe", *save), threshold=0.8)
