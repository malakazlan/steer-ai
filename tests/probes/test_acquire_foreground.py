"""`acquire_foreground` rungs (design 5.9 [v0.6], PX-23, PX-24) against a fake user32, so the
ladder is checked without a desktop: already → direct → a tagged zero-distance mouse nudge →
AttachThreadInput, the last rung skipped when the foreground is missing or hung (attaching to a
thread that does not pump messages blocks for good). Never an ALT tap (issue #38): a lone ALT put
classic Notepad in menu mode 16 of 17 times (probe 2026-11-07), so its next keys were mnemonics."""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from steerai.probes import win_input

HWND = 0x10
OTHER = 0x99
OWN_THREAD = 11
FOREGROUND_THREAD = 22


class _FakeUser32:
    """The user32 entry points the ladder touches; `grant_at` names the rung at which
    `SetForegroundWindow` is honoured (None: never)."""

    def __init__(self, *, foreground: int, hung: bool = False, grant_at: str | None = None) -> None:
        self.foreground = foreground
        self.hung = hung
        self.grant_at = grant_at
        self.taps = 0
        self.attached = False
        self.set_calls: list[int] = []
        self.attach_calls: list[tuple[int, int, bool]] = []

        def get_foreground() -> int:
            return self.foreground

        def set_foreground(hwnd: int) -> int:
            self.set_calls.append(hwnd)
            if self.attached:
                rung = "attach-thread-input"
            elif self.taps:
                rung = "input-nudge"
            else:
                rung = "direct"
            if rung == self.grant_at:
                self.foreground = hwnd
            return 1

        def is_hung(_hwnd: int) -> int:
            return int(self.hung)

        def thread_of(_hwnd: int, _pid: Any) -> int:
            return FOREGROUND_THREAD

        def attach(own: int, other: int, flag: bool) -> int:
            self.attach_calls.append((own, other, bool(flag)))
            self.attached = bool(flag)
            return 1

        # Plain functions accept the `.argtypes` / `.restype` assignments the module makes.
        self.GetForegroundWindow = get_foreground
        self.SetForegroundWindow = set_foreground
        self.IsHungAppWindow = is_hung
        self.GetWindowThreadProcessId = thread_of
        self.AttachThreadInput = attach


@pytest.fixture
def user32(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _FakeUser32]:
    def install(**kwargs: Any) -> _FakeUser32:
        fake = _FakeUser32(**kwargs)

        def nudge() -> None:
            fake.taps += 1

        def key(vk: int) -> None:
            raise AssertionError(f"acquire_foreground pressed a key ({vk:#x}): never an ALT tap")

        def current_thread_id() -> int:
            return OWN_THREAD

        monkeypatch.setattr(win_input, "_user32", lambda: fake)
        monkeypatch.setattr(win_input, "nudge_in_place", nudge)
        monkeypatch.setattr(win_input, "key_tap", key)
        kernel32 = SimpleNamespace(GetCurrentThreadId=current_thread_id)
        monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel32, raising=False)
        return fake

    return install


def test_already_foreground_touches_nothing(user32: Callable[..., _FakeUser32]) -> None:
    fake = user32(foreground=HWND)
    assert win_input.acquire_foreground(HWND) == "already"
    assert fake.set_calls == []
    assert fake.taps == 0
    assert fake.attach_calls == []


def test_direct_call_when_windows_allows_it(user32: Callable[..., _FakeUser32]) -> None:
    fake = user32(foreground=OTHER, grant_at="direct")
    assert win_input.acquire_foreground(HWND) == "direct"
    assert fake.set_calls == [HWND]
    assert fake.taps == 0
    assert fake.attach_calls == []


def test_a_nudge_then_the_call_never_a_key(user32: Callable[..., _FakeUser32]) -> None:
    fake = user32(foreground=OTHER, grant_at="input-nudge")
    assert win_input.acquire_foreground(HWND) == "input-nudge"
    assert fake.taps == 1
    assert fake.set_calls == [HWND, HWND]
    assert fake.attach_calls == []


def test_attach_thread_input_is_the_last_rung_and_always_detaches(
    user32: Callable[..., _FakeUser32],
) -> None:
    fake = user32(foreground=OTHER, grant_at="attach-thread-input")
    assert win_input.acquire_foreground(HWND) == "attach-thread-input"
    assert fake.taps == 1
    assert fake.attach_calls == [
        (OWN_THREAD, FOREGROUND_THREAD, True),
        (OWN_THREAD, FOREGROUND_THREAD, False),
    ]


def test_hung_foreground_skips_attach_thread_input(user32: Callable[..., _FakeUser32]) -> None:
    # PX-24: attaching to a non-pumping thread blocks indefinitely; fail instead.
    fake = user32(foreground=OTHER, hung=True)
    with pytest.raises(OSError, match="could not bring hwnd 0x10 to the foreground"):
        win_input.acquire_foreground(HWND)
    assert fake.taps == 1, "the cheaper rungs were still tried"
    assert fake.attach_calls == []


def test_missing_foreground_skips_attach_thread_input(user32: Callable[..., _FakeUser32]) -> None:
    fake = user32(foreground=0)
    with pytest.raises(OSError, match="could not bring hwnd 0x10 to the foreground"):
        win_input.acquire_foreground(HWND)
    assert fake.attach_calls == []


def test_every_rung_refused_detaches_and_raises(user32: Callable[..., _FakeUser32]) -> None:
    fake = user32(foreground=OTHER)
    with pytest.raises(OSError, match="could not bring hwnd 0x10 to the foreground"):
        win_input.acquire_foreground(HWND)
    assert fake.attach_calls == [
        (OWN_THREAD, FOREGROUND_THREAD, True),
        (OWN_THREAD, FOREGROUND_THREAD, False),
    ]


# ---- in_menu_mode (GUITHREADINFO.flags, winuser.h) ---------------------------------------------


class _GuiThreads:
    """`GetWindowThreadProcessId` and `GetGUIThreadInfo`; `readable=False` makes the read fail."""

    def __init__(self, *, thread: int = 7, flags: int = 0, readable: bool = True) -> None:
        self.read: list[int] = []

        def thread_of(_hwnd: int, _pid: Any) -> int:
            return thread

        def gui_thread_info(thread_id: int, ref: Any) -> int:
            info = ref._obj  # the structure behind ctypes.byref
            assert info.cbSize == ctypes.sizeof(info), "GetGUIThreadInfo fails without cbSize"
            self.read.append(thread_id)
            if not readable:
                return 0
            info.flags = flags
            return 1

        # Plain functions accept the `.argtypes` / `.restype` assignments the module makes.
        self.GetWindowThreadProcessId = thread_of
        self.GetGUIThreadInfo = gui_thread_info


@pytest.mark.parametrize(
    ("flags", "menu"),
    [
        (0x0, False),
        (0x1, False),  # GUI_CARETBLINKING
        (0x2, False),  # GUI_INMOVESIZE
        (0x4, True),  # GUI_INMENUMODE
        (0x8, True),  # GUI_SYSTEMMENUMODE
        (0x10, True),  # GUI_POPUPMENUMODE
        (0x1 | 0x10, True),
    ],
)
def test_menu_mode_reads_the_menu_flags(
    monkeypatch: pytest.MonkeyPatch, flags: int, menu: bool
) -> None:
    threads = _GuiThreads(flags=flags)
    monkeypatch.setattr(win_input, "_user32", lambda: threads)
    assert win_input.in_menu_mode(0x30) is menu
    assert threads.read == [7]


def test_an_unreadable_thread_counts_as_in_a_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unclear means no typing."""
    threads = _GuiThreads(readable=False)
    monkeypatch.setattr(win_input, "_user32", lambda: threads)
    assert win_input.in_menu_mode(0x30) is True
    gone = _GuiThreads(thread=0)
    monkeypatch.setattr(win_input, "_user32", lambda: gone)
    assert win_input.in_menu_mode(0x30) is True
    assert gone.read == [], "thread 0 is never read: GetGUIThreadInfo(0) is the foreground thread"
