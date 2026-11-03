"""Screen fingerprints: stable landmarks compared by similarity, never equality (design 5.7)."""

import pytest

from steerai.world.elements import Element
from steerai.world.fingerprint import Fingerprint, landmarks, similarity

BOX = (0, 0, 10, 10)


def _el(idx: int, role: str, name: str = "", automation_id: str = "") -> Element:
    return Element(id=idx, depth=1, role=role, name=name, box=BOX, automation_id=automation_id)


INBOX = [
    _el(1, "Window", "Inbox - Chat"),
    _el(2, "Edit", "Search chats"),
    _el(3, "Button", "New message"),
    _el(4, "List", "Chat list"),
    _el(5, "ListItem", "Amir · hi bro · 2h"),
    _el(6, "ListItem", "Sara Ahmed · invoice attached · 1d"),
    _el(7, "Text", "yes, 6pm works"),
    _el(8, "Button", "", automation_id="composer-send"),
]


def test_landmarks_keep_containers_and_controls_drop_dynamic_rows_and_text() -> None:
    marks = landmarks(INBOX)
    assert ("Edit", "Search chats") in marks
    assert ("Button", "New message") in marks
    assert ("List", "Chat list") in marks
    assert ("Button", "#composer-send") in marks
    assert not any(role in ("ListItem", "Text") for role, _ in marks)


def test_window_title_is_not_a_landmark() -> None:
    assert not any(name == "Inbox - Chat" for _, name in landmarks(INBOX))


def test_similarity_is_jaccard_and_ignores_dynamic_content() -> None:
    later = [*INBOX[:4], _el(5, "ListItem", "Bilal · see you · 5d"), *INBOX[6:]]
    assert similarity(landmarks(INBOX), landmarks(later)) == pytest.approx(1.0)
    different = [_el(1, "Window", "Settings"), _el(2, "Button", "Sign out")]
    assert similarity(landmarks(INBOX), landmarks(different)) < 0.2


def test_similarity_of_two_empty_sets_is_zero() -> None:
    assert similarity(frozenset(), frozenset()) == 0.0


def test_fingerprint_matches_by_threshold() -> None:
    fp = Fingerprint(process="chrome.exe", title_pattern="Inbox", marks=landmarks(INBOX))
    # same screen, different rows: rows are not landmarks, so similarity is 1.0
    later = [*INBOX[:4], *INBOX[6:]]
    same_app = Fingerprint(process="chrome.exe", title_pattern="Inbox", marks=landmarks(later))
    three_of_four = Fingerprint(
        process="chrome.exe", title_pattern="Inbox", marks=landmarks(INBOX[:7])
    )
    other = Fingerprint(process="notepad.exe", title_pattern="x", marks=landmarks(INBOX))
    assert fp.matches(same_app, threshold=0.8)
    assert not fp.matches(three_of_four, threshold=0.8)  # Jaccard 3/4 = 0.75
    assert fp.matches(three_of_four, threshold=0.7)
    assert not fp.matches(other, threshold=0.8)
