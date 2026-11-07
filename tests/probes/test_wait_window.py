"""`wait_window_for_pid` says what the launched process did when no window came (issue #39): the
first Chrome start of a CI job showed no window at all three times, and nothing said whether
the process had exited, relaunched or was still starting. No desktop: the list and clock are
injected."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from steerai.probes import windows


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(
        windows, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep)
    )
    monkeypatch.setattr(windows, "list_top_windows", list)
    monkeypatch.setattr(windows, "_windows_of", lambda _image: "; chrome.exe windows: none")
    return fake


@pytest.mark.parametrize(
    ("returncode", "state"), [(None, "running"), (0, "exit=0"), (21, "exit=21")]
)
def test_the_timeout_says_whether_the_process_is_still_there(
    clock: _Clock, returncode: int | None, state: str
) -> None:
    process = SimpleNamespace(poll=lambda: returncode)
    with pytest.raises(TimeoutError) as timed_out:
        windows.wait_window_for_pid(4242, timeout_s=30.0, image="chrome.exe", process=process)
    message = str(timed_out.value)
    assert state in message
    assert "after 30.0 s" in message
    assert "chrome.exe windows: none" in message


def test_without_a_process_the_message_is_unchanged_but_timed(clock: _Clock) -> None:
    with pytest.raises(TimeoutError) as timed_out:
        windows.wait_window_for_pid(4242, timeout_s=5.0)
    assert "no visible window for pid 4242 within 5.0 s" in str(timed_out.value)
    assert "exit=" not in str(timed_out.value)
    assert "running" not in str(timed_out.value)


def test_the_hidden_window_counts_tell_no_window_from_a_hidden_one() -> None:
    """Schema gate item 2: the visible list drops invisible, untitled and cloaked windows."""
    assert windows.hidden_counts([]) == (
        "; all top windows 0: invisible 0, untitled 0, cloaked 0, cloak unreadable 0"
    )
    rows = [
        (False, 13, False),
        (True, 0, False),
        (True, 13, True),
        (True, 13, None),
        (True, 9, False),
    ]
    assert windows.hidden_counts(rows) == (
        "; all top windows 5: invisible 1, untitled 1, cloaked 1, cloak unreadable 1"
    )
