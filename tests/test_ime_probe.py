"""[V2.59]: the open-IME probe. The keyboard hook's probe key was picked with the IME closed
(`probe_key`, en-US only); before stage 2 it is probed again with an IME open in our own Notepad.
The probe itself sends real input, so its one end-to-end test runs only on `-m probe` on Windows,
and only where an IME is installed. Everything else here runs it over fakes: no input, no
window, no hook."""

from __future__ import annotations

import ast
import inspect
import io
import json
import signal
from collections.abc import Callable
from contextlib import redirect_stdout
from typing import Any

import pytest

from steerai.probes import ime_probe
from steerai.probes.probe_key import IMC_GETOPENSTATUS
from steerai.session.monitor import PROBE_VK

EN_US = 0x0409_0409
JA = 0x0411_0411
JA_IMM = 0xE001_0411
KO = 0x0412_0412
ZH_CN = 0x0804_0804
Row = dict[str, Any]  # Any: JSON values


def test_ime_layouts_are_the_chinese_japanese_and_korean_input_locales() -> None:
    """The HKL's low word is its language identifier (GetKeyboardLayout); the primary language
    is its low ten bits (winnt.h PRIMARYLANGID): LANG_CHINESE 0x04, JAPANESE 0x11, KOREAN 0x12."""
    assert ime_probe.ime_layouts([EN_US, JA, JA_IMM, KO, ZH_CN]) == [JA, JA_IMM, KO, ZH_CN]
    assert ime_probe.ime_layouts([EN_US]) == []


@pytest.mark.parametrize("text", ["0x04110411", "04110411", "E0010411"])
def test_a_layout_is_given_in_hex(text: str) -> None:
    assert ime_probe.parse_hkl(text) == int(text, 16)


@pytest.mark.parametrize("text", ["zz", "0", "-1", "0x1" + "0" * 16, ""])
def test_a_layout_that_is_not_a_hkl_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="hkl"):
        ime_probe.parse_hkl(text)


def test_a_verdict_passes_only_when_nothing_moved_and_the_hook_saw_every_key_up() -> None:
    base: Row = {"text_empty": True, "caret": [1, 2, 3, 4], "ime_open": 1, "layout": JA}
    assert ime_probe.verdict(base, dict(base), sent=20, seen=20, left_down=False)["passes"]
    moved = ime_probe.verdict(base, {**base, "caret": [9, 2, 3, 4]}, sent=20, seen=20,
                              left_down=False)  # fmt: skip
    assert (moved["passes"], moved["changed"]) == (False, ["caret"])
    assert not ime_probe.verdict(base, dict(base), sent=20, seen=19, left_down=False)["passes"]
    assert not ime_probe.verdict(base, dict(base), sent=20, seen=20, left_down=True)["passes"]


# ---- the run, over fakes -------------------------------------------------------------------------


class FakeMonitor:
    def __init__(self) -> None:
        self.events = {"steer": 0, "user": 0, "injected_other": 0}
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def counts(self) -> dict[str, int]:
        return dict(self.events)


class FakeDesk:
    """A desk with `installed` layouts; our Notepad is 100 (its Edit 101)."""

    def __init__(self, installed: list[int], log: list[str]) -> None:
        self.installed = installed
        self.log = log
        self.idle = 10.0
        self.fg = 100
        self.layout = EN_US
        self.ime_open: int | str = 0
        self.opens = True
        self.closing_answers = True
        self.restores = True
        self.hangs_after_close = False
        self.text_empty = True
        self.rows_at_requests: list[list[Row]] = []
        self.rows: list[Row] = []

    def idle_s(self) -> float:
        return self.idle

    def foreground(self) -> int:
        return self.fg

    def edit_of(self, hwnd: int) -> int:
        return hwnd + 1

    def key_down(self, _vk: int) -> bool:
        return False

    def state(self, _hwnd: int, read_text: Callable[[], str]) -> Row:
        read_text()
        return {"foreground": self.fg, "text_empty": self.text_empty, "caret": [0, 0, 1, 16],
                "caret_hwnd": 101, "ime_open": self.ime_open, "layout": self.layout}  # fmt: skip

    def layouts(self) -> list[int]:
        return list(self.installed)

    def request_layout(self, edit: int, hkl: int) -> None:
        self.log.append(f"layout {edit} {hkl:#x}")
        self.rows_at_requests.append(list(self.rows))
        if self.restores or hkl != EN_US:
            self.layout = hkl

    def set_ime_open(self, edit: int, open_: bool) -> None:
        self.log.append(f"open {edit} {int(open_)}")
        if not open_ and not self.closing_answers:
            raise ime_probe.Abort("ime_control_unanswered")
        if not open_ and self.hangs_after_close:
            self.ime_open = "unanswered"
            return
        if self.opens or not open_:
            self.ime_open = int(open_)


class Probe:
    """One run's fakes; `effect` runs after each key-up is sent."""

    def __init__(self, installed: list[int] | None = None) -> None:
        self.log: list[str] = []
        self.desk = FakeDesk([EN_US, JA] if installed is None else installed, self.log)
        self.monitor = FakeMonitor()
        self.sent: list[int] = []
        self.rows: list[Row] = self.desk.rows
        self.effect: Callable[[int], None] = lambda _vk: None
        self.rows_at_launch: list[Row] = []

    def launch(self) -> ime_probe.Notepad:
        self.log.append("launch")
        self.rows_at_launch = list(self.rows)
        return ime_probe.Notepad(pid=4242, hwnd=100, close=lambda: self.log.append("close"))

    def send_up(self, vk: int) -> None:
        self.sent.append(vk)
        self.monitor.events["steer"] += 1
        self.effect(vk)

    def echo(self, vk: int) -> Row:
        self.log.append(f"echo {vk:#x}")
        return {"round_trip": "none", "vk": f"{vk:#04x}"}

    def run(self, candidates: list[int], *, layout: int | None = None) -> list[int]:
        return ime_probe.run(
            desk=self.desk, launch=self.launch, read_text_of=lambda _edit: lambda: "",
            monitor=self.monitor, send_up=self.send_up, echo=self.echo,
            candidates=candidates, size=3, layout=layout, emit=self.rows.append,
            pause=lambda _s: None,
        )  # fmt: skip


def test_with_no_ime_installed_nothing_is_launched_or_sent() -> None:
    """Not applicable even while the person is at the keyboard: the exit code says so."""
    probe = Probe([EN_US])
    probe.desk.idle = 0.0
    with pytest.raises(ime_probe.Abort, match="no_ime_installed"):
        probe.run([PROBE_VK])
    assert (probe.log, probe.sent, probe.monitor.started) == ([], [], False)


def test_a_named_layout_that_is_not_installed_is_refused_before_anything_runs() -> None:
    probe = Probe([EN_US, JA])
    with pytest.raises(ime_probe.Abort, match="layout_not_installed"):
        probe.run([PROBE_VK], layout=KO)
    assert (probe.log, probe.sent) == ([], [])


def test_a_person_at_the_keyboard_stops_the_probe_before_it_starts() -> None:
    probe = Probe()
    probe.desk.idle = 0.5
    with pytest.raises(ime_probe.Abort, match="not_idle"):
        probe.run([PROBE_VK])
    assert (probe.log, probe.sent) == ([], [])
    assert {"restored": True, "switched": False} in probe.rows  # nothing was switched


def test_an_ime_that_will_not_open_aborts_and_still_restores_and_closes() -> None:
    probe = Probe()
    probe.desk.opens = False
    with pytest.raises(ime_probe.Abort, match="ime_not_open"):
        probe.run([PROBE_VK])
    assert probe.sent == []
    assert probe.log == ["launch", f"layout 101 {JA:#x}", "open 101 1", "open 101 0",
                         f"layout 101 {EN_US:#x}", "close"]  # fmt: skip
    assert probe.monitor.closed


def test_each_key_is_judged_with_the_ime_open_then_everything_is_put_back() -> None:
    probe = Probe()
    accepted = probe.run([PROBE_VK, 0x0F])
    assert accepted == [PROBE_VK, 0x0F]
    assert probe.sent == [PROBE_VK] * 3 + [0x0F] * 3
    baseline = next(r["baseline"] for r in probe.rows if "baseline" in r)
    assert (baseline["ime_open"], baseline["layout"]) == (1, JA)
    judged = [r for r in probe.rows if "passes" in r]
    assert [(r["vk"], r["passes"]) for r in judged] == [("0x0e", True), ("0x0f", True)]
    assert probe.log[-4:] == [f"echo {PROBE_VK:#x}", "open 101 0", f"layout 101 {EN_US:#x}",
                              "close"]  # fmt: skip
    assert {"restored": True, "switched": True, "restore_errors": []} in probe.rows
    assert (probe.desk.ime_open, probe.desk.layout) == (0, EN_US)


def test_the_layout_before_and_how_to_put_it_back_are_said_before_the_first_switch() -> None:
    probe = Probe()
    probe.run([PROBE_VK])
    first = probe.desk.rows_at_requests[0]
    assert {"before_layout": f"{EN_US:#x}",
            "restore_with": f"--restore {EN_US:#x}"} in first  # fmt: skip


def test_the_header_says_the_persons_input_method_is_switched_until_the_restore() -> None:
    probe = Probe()
    probe.run([PROBE_VK])
    header = {"header": {"probe_key_ime": "open", "input_method": "switched_until_restore"}}
    assert probe.rows_at_launch == [header]  # said first, before Notepad starts


def test_a_key_the_open_ime_reacts_to_fails_and_the_rest_are_still_judged() -> None:
    probe = Probe()

    def compose(vk: int) -> None:
        if vk == 0x0F:
            probe.desk.text_empty = False

    probe.effect = compose
    accepted = probe.run([PROBE_VK, 0x0F, 0x97])
    judged = {r["vk"]: r for r in probe.rows if "passes" in r}
    assert accepted == [PROBE_VK]
    assert judged["0x0f"]["changed"] == ["text_empty"]
    assert not judged["0x97"]["passes"]  # the text stays changed: judged against the baseline


def test_an_ime_that_will_not_close_still_gets_its_locale_put_back() -> None:
    probe = Probe()
    probe.desk.closing_answers = False
    with pytest.raises(ime_probe.RestoreFailed):
        probe.run([PROBE_VK])
    assert probe.log[-3:] == ["open 101 0", f"layout 101 {EN_US:#x}", "close"]
    assert {"restored": False, "switched": True,
            "restore_errors": ["ime_control_unanswered"]} in probe.rows  # fmt: skip
    assert probe.monitor.closed


def test_a_locale_that_does_not_come_back_is_its_own_failure() -> None:
    probe = Probe()
    probe.desk.restores = False
    with pytest.raises(ime_probe.RestoreFailed):
        probe.run([PROBE_VK])
    assert {"restored": False, "switched": True, "restore_errors": []} in probe.rows
    assert probe.log[-1] == "close"


def test_a_failed_restore_after_an_abort_is_still_the_failure_reported() -> None:
    probe = Probe()
    probe.desk.restores = False

    def person(_vk: int) -> None:
        probe.monitor.events["user"] += 1

    probe.effect = person
    with pytest.raises(ime_probe.RestoreFailed) as failed:
        probe.run([PROBE_VK])
    assert str(failed.value.__context__) == "person_input"


def test_the_persons_input_mid_batch_aborts_and_still_restores_and_closes() -> None:
    probe = Probe()

    def person(_vk: int) -> None:
        probe.monitor.events["user"] += 1

    probe.effect = person
    with pytest.raises(ime_probe.Abort, match="person_input"):
        probe.run([PROBE_VK])
    assert probe.log[-3:] == ["open 101 0", f"layout 101 {EN_US:#x}", "close"]
    assert probe.monitor.closed


def test_the_foreground_leaving_our_notepad_aborts_with_the_next_key_unsent() -> None:
    probe = Probe()

    def leave(_vk: int) -> None:
        probe.desk.fg = 555

    probe.effect = leave
    with pytest.raises(ime_probe.Abort, match="foreground_left_notepad"):
        probe.run([PROBE_VK, 0x0F])
    assert 0x0F not in probe.sent
    assert probe.log[-1] == "close"


def test_no_echo_is_timed_when_the_probe_key_fails() -> None:
    probe = Probe()

    def compose(vk: int) -> None:
        if vk == PROBE_VK:
            probe.desk.text_empty = False

    probe.effect = compose
    probe.run([PROBE_VK])
    assert not any(line.startswith("echo") for line in probe.log)


# ---- main: exit codes ----------------------------------------------------------------------------


def _main(result: Callable[[], list[int]]) -> tuple[int, list[Row]]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = ime_probe.main([], probe=lambda _layout, _size, _emit: result())
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def test_main_says_not_applicable_when_no_ime_is_installed() -> None:
    def none() -> list[int]:
        raise ime_probe.Abort("no_ime_installed")

    code, rows = _main(none)
    assert code == ime_probe.NOT_APPLICABLE
    assert rows == [{"probe": "ime_probe", "aborted": "no_ime_installed"}]


def test_main_fails_when_the_probe_key_is_not_among_the_keys_that_passed() -> None:
    code, rows = _main(lambda: [0x0F])
    assert code == 1
    assert rows[-1] == {"probe": "ime_probe", "accepted": ["0x0f"], "probe_vk_passes": False}


def test_main_passes_when_the_probe_key_passed() -> None:
    code, rows = _main(lambda: [PROBE_VK])
    assert code == 0
    assert rows[-1]["probe_vk_passes"] is True


def test_a_readback_the_ime_never_answered_is_a_failed_restore() -> None:
    probe = Probe()
    probe.desk.hangs_after_close = True
    with pytest.raises(ime_probe.RestoreFailed):
        probe.run([PROBE_VK])
    assert probe.log[-1] == "close"


# ---- every IME read is bounded ([V2.59] #379 review 3) -------------------------------------------


def test_an_ime_read_that_times_out_is_unanswered_never_a_wait() -> None:
    sent: list[tuple[int, int, int, int]] = []

    def send(hwnd: int, message: int, wparam: int, lparam: int) -> tuple[bool, int]:
        sent.append((hwnd, message, wparam, lparam))
        return False, 0

    assert ime_probe.read_open_status(send, 77) == "unanswered"
    assert sent == [(77, ime_probe.WM_IME_CONTROL, IMC_GETOPENSTATUS, 0)]


def test_an_ime_read_answered_is_its_open_status() -> None:
    assert ime_probe.read_open_status(lambda *_a: (True, 1), 77) == 1
    assert ime_probe.read_open_status(lambda *_a: (True, 0), 77) == 0


def test_with_no_ime_window_there_is_nothing_to_read() -> None:
    def never(*_a: int) -> tuple[bool, int]:
        raise AssertionError("no window, no message")

    assert ime_probe.read_open_status(never, 0) is None


def test_the_probes_desk_reads_the_ime_only_through_the_bounded_read() -> None:
    """`probe_key`'s desk reads the open status with a plain SendMessageW; this probe's own desk
    replaces that read, and nothing in this module sends a message without a timeout."""
    tree = ast.parse(inspect.getsource(ime_probe))
    desk = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_ImeDesk")
    assert "_ime_open" in {n.name for n in desk.body if isinstance(n, ast.FunctionDef)}
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}  # fmt: skip
    assert "SendMessageW" not in calls
    assert "SendMessageTimeoutW" in calls


def test_main_exits_with_its_own_code_when_the_restore_failed() -> None:
    def unrestored() -> list[int]:
        raise ime_probe.RestoreFailed

    code, rows = _main(unrestored)
    assert code == ime_probe.RESTORE_FAILED
    assert rows == [{"probe": "ime_probe", "aborted": "restore_failed"}]


KILLS = [name for name in ("SIGTERM", "SIGBREAK") if hasattr(signal, name)]


@pytest.mark.parametrize("name", KILLS)
def test_a_termination_signal_raises_inside_the_probe_so_the_restore_runs(name: str) -> None:
    number = getattr(signal, name)
    before = signal.getsignal(number)
    probe = Probe()

    def killed(_vk: int) -> None:
        handler = signal.getsignal(number)
        assert callable(handler)
        handler(number, None)

    probe.effect = killed
    out = io.StringIO()
    with redirect_stdout(out):
        code = ime_probe.main([], probe=lambda _l, _s, emit: _run_with(probe, emit))
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 2
    assert {"probe": "ime_probe", "aborted": "killed"} in rows
    assert probe.log[-3:] == ["open 101 0", f"layout 101 {EN_US:#x}", "close"]
    assert signal.getsignal(number) == before  # put back after the run


def _run_with(probe: Probe, emit: Callable[[Row], None]) -> list[int]:
    def both(row: Row) -> None:
        probe.rows.append(row)
        emit(row)

    return ime_probe.run(
        desk=probe.desk, launch=probe.launch, read_text_of=lambda _edit: lambda: "",
        monitor=probe.monitor, send_up=probe.send_up, echo=probe.echo, candidates=[PROBE_VK],
        size=3, layout=None, emit=both, pause=lambda _s: None,
    )  # fmt: skip


def test_the_hard_kill_residual_and_its_remedy_are_stated() -> None:
    doc = ime_probe.__doc__ or ""
    assert "TerminateProcess" in doc
    assert "--restore" in doc


# ---- --restore: put the layout back after a hard kill --------------------------------------------


def _restore(probe: Probe, hkl: int) -> bool:
    return ime_probe.restore_layout(
        desk=probe.desk, launch=probe.launch, read_text_of=lambda _edit: lambda: "", hkl=hkl,
        emit=probe.rows.append, pause=lambda _s: None,
    )  # fmt: skip


def test_restore_puts_the_layout_back_through_a_fresh_notepad_of_our_own() -> None:
    probe = Probe()
    probe.desk.layout = JA
    assert _restore(probe, EN_US) is True
    assert probe.log == ["launch", f"layout 101 {EN_US:#x}", "close"]
    assert {"restored": True, "layout": f"{EN_US:#x}"} in probe.rows


def test_restore_that_does_not_take_says_so() -> None:
    probe = Probe()
    probe.desk.layout = JA
    probe.desk.restores = False
    assert _restore(probe, EN_US) is False
    assert {"restored": False, "layout": f"{JA:#x}"} in probe.rows
    assert probe.log[-1] == "close"


def test_restore_refuses_a_layout_that_is_not_installed_before_launching() -> None:
    probe = Probe([EN_US])
    with pytest.raises(ime_probe.Abort, match="layout_not_installed"):
        _restore(probe, KO)
    assert probe.log == []


@pytest.mark.parametrize(("restored", "code"), [(True, 0), (False, ime_probe.RESTORE_FAILED)])
def test_main_restore_mode_exits_on_the_restore_alone(restored: bool, code: int) -> None:
    asked: list[int] = []

    def restore(hkl: int, _emit: Callable[[Row], None]) -> bool:
        asked.append(hkl)
        return restored

    def no_probe(*_a: object) -> list[int]:
        raise AssertionError("restore mode runs no probe")

    with redirect_stdout(io.StringIO()):
        got = ime_probe.main(["--restore", "0x04090409"], probe=no_probe, restore=restore)
    assert (got, asked) == (code, [EN_US])


def test_main_reports_any_other_abort_as_an_abort() -> None:
    def stopped() -> list[int]:
        raise ime_probe.Abort("person_input")

    assert _main(stopped)[0] == 2


# ---- the live probe ------------------------------------------------------------------------------


@pytest.mark.probe
@pytest.mark.windows
def test_the_probe_key_with_an_ime_open_in_our_own_notepad() -> None:
    """Runs on a machine with a Chinese, Japanese or Korean IME installed; it changes the input
    method of its own Notepad and puts it back. Not run where none is installed (2026-11-15:
    this machine has en-US only, and installing an IME is the person's call)."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = ime_probe.main(["--batch", "20"])
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    if code == ime_probe.NOT_APPLICABLE:
        pytest.skip("no Chinese, Japanese or Korean IME installed on this machine")
    assert all(row["probe"] == "ime_probe" for row in rows)
    assert code in (0, 1, 2, ime_probe.RESTORE_FAILED)
    restored = [row["restored"] for row in rows if "restored" in row]
    assert restored == [code != ime_probe.RESTORE_FAILED], rows
