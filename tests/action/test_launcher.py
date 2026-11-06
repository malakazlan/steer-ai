"""The launcher executor (packet open-multiwindow-2026-11-06, scope item 3): the kill blocker
before the launch, an argument list with no shell, then a new top-level window of the app's
process. Pure: `popen`, the window list, the process-name reader and the clock are injected."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from steerai.action.dispositions import Delivery
from steerai.action.launcher import OPEN_WINDOW_S, Launcher, OpenAction
from steerai.errors import ErrorCode
from steerai.probes.windows import WindowInfo

CHROME_OLD = WindowInfo(hwnd=0x10, title="Inbox - Google Chrome", pid=500)
NOTEPAD = WindowInfo(hwnd=0x20, title="Untitled - Notepad", pid=600)
NEW_CHROME = WindowInfo(hwnd=0x30, title="YouTube - Google Chrome", pid=500)
OTHER_APP = WindowInfo(hwnd=0x40, title="Calculator", pid=700)


@dataclass
class Machine:
    windows: list[WindowInfo] = field(default_factory=lambda: [CHROME_OLD, NOTEPAD])
    names: dict[int, str] = field(
        default_factory=lambda: {500: "chrome.exe", 600: "notepad.exe", 700: "calc.exe"}
    )
    launched: list[list[str]] = field(default_factory=list)
    appear_after: int = 1
    appears: list[WindowInfo] = field(default_factory=lambda: [NEW_CHROME])
    polls: int = 0
    clock: float = 0.0
    fail_launch: Exception | None = None

    def popen(self, argv: list[str]) -> Any:
        if self.fail_launch is not None:
            raise self.fail_launch
        self.launched.append(argv)
        return object()

    def list_windows(self) -> list[WindowInfo]:
        self.polls += 1
        if self.polls > self.appear_after:
            return [*self.windows, *self.appears]
        return list(self.windows)

    def process_name(self, pid: int) -> str:
        return self.names.get(pid, "")

    def sleep(self, s: float) -> None:
        self.clock += s


def _launcher(machine: Machine, *, blocker: Any = None) -> Launcher:
    return Launcher(
        popen=machine.popen,
        list_windows=machine.list_windows,
        process_name=machine.process_name,
        resolve_exe=lambda _candidates: "C:/Program Files/Google/Chrome/Application/chrome.exe",
        monotonic=lambda: machine.clock,
        sleep=machine.sleep,
        blocker=blocker,
    )


def _open(app: str = "chrome", site: str | None = "youtube", text: str | None = None) -> OpenAction:
    return OpenAction(app=app, site=site, text=text)


def test_a_launch_finds_the_new_window_of_the_apps_process() -> None:
    machine = Machine()
    before = machine.list_windows()
    result = _launcher(machine).execute(_open(), before)
    assert machine.launched == [
        [
            "C:/Program Files/Google/Chrome/Application/chrome.exe",
            "--new-window",
            "https://www.youtube.com/",
        ]
    ]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED, "the title is the loop's to verify"
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.evidence["hwnd"] == NEW_CHROME.hwnd
    assert result.evidence["launched"] == "chrome.exe"
    assert result.steps[-1].label == "launch"


def test_a_window_of_another_process_is_not_the_apps() -> None:
    machine = Machine(appears=[OTHER_APP], appear_after=0)
    result = _launcher(machine).execute(_open(), [CHROME_OLD, NOTEPAD])
    assert "hwnd" not in result.evidence
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence["detail"] == "no_new_window"
    assert machine.clock >= OPEN_WINDOW_S


def test_an_existing_window_never_counts_as_the_new_one() -> None:
    machine = Machine(appears=[], appear_after=0)
    result = _launcher(machine).execute(_open(), [CHROME_OLD, NOTEPAD])
    assert result.evidence["detail"] == "no_new_window"
    assert len(machine.launched) == 1, "never re-launched"


def test_two_new_windows_of_the_app_are_ambiguous() -> None:
    twin = WindowInfo(hwnd=0x31, title="Restore pages? - Google Chrome", pid=500)
    machine = Machine(appears=[NEW_CHROME, twin], appear_after=0)
    result = _launcher(machine).execute(_open(), [CHROME_OLD, NOTEPAD])
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence["detail"] == "several_new_windows"


def test_a_minimized_new_window_is_not_drivable() -> None:
    hidden = WindowInfo(hwnd=0x32, title="YouTube - Google Chrome", pid=500, minimized=True)
    machine = Machine(appears=[hidden], appear_after=0)
    result = _launcher(machine).execute(_open(), [CHROME_OLD, NOTEPAD])
    assert result.evidence["detail"] == "no_new_window"


def test_the_kill_switch_refuses_the_launch_and_nothing_starts() -> None:
    machine = Machine()
    result = _launcher(machine, blocker=lambda: "killed").execute(_open(), [CHROME_OLD])
    assert machine.launched == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"


def test_a_registry_refusal_is_not_delivered_and_names_the_reason() -> None:
    machine = Machine()
    result = _launcher(machine).execute(_open(app="calculator", site=None), [CHROME_OLD])
    assert machine.launched == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "launch_refused"


def test_a_launch_the_os_refuses_is_not_delivered() -> None:
    machine = Machine(fail_launch=OSError("The system cannot find the file specified"))
    result = _launcher(machine).execute(_open(), [CHROME_OLD])
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.evidence["detail"] == "launch_failed"


@pytest.mark.parametrize("text", ["drake latest video", "a & b"])
def test_the_content_never_enters_the_evidence(text: str) -> None:
    machine = Machine()
    result = _launcher(machine).execute(_open(site="youtube_search", text=text), [CHROME_OLD])
    assert text not in str(result.to_dict())


def test_the_kill_switch_during_the_poll_stops_it_with_nothing_adopted() -> None:
    """OW-07: the blocker is asked on every tick after the launch, not only before it."""
    machine = Machine(appears=[], appear_after=0)
    asks = {"n": 0}

    def blocker() -> str | None:
        asks["n"] += 1
        return "killed" if asks["n"] > 3 else None

    result = _launcher(machine, blocker=blocker).execute(_open(), [CHROME_OLD])
    assert len(machine.launched) == 1
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN, "the process was started"
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"
    assert "hwnd" not in result.evidence


def test_the_real_launch_passes_only_the_allowlisted_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import steerai.action.launcher as launcher_module  # noqa: PLC0415

    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr("steerai.action.launcher.subprocess.Popen", fake_popen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    launcher_module._real_popen(["C:/x.exe"])
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen.get("shell") in (None, False)
