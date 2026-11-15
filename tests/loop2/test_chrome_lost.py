"""#459: a Chrome that goes away mid-run (its DevTools connection closes without a close frame)
is an infrastructure event: the DevTools client raises `ChromeLost` (an `OSError`), the stage
harness ends the run as a void with the closed cause `chrome_lost`, and the set goes on, never a
traceback exit. The live edges are faked; the harness's own run function is the real one."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError

from steerai.loop2 import stage1
from steerai.loop2.stage1 import CHROME_LOST, StageTask, classify, run_task_runs
from steerai.probes import (
    chrome_a11y,
    instant_search,
    layer_b,
    layer_b_pages,
    suite_chunks,
    windows,
)
from steerai.probes.chrome_a11y import CdpSession, ChromeLost
from tests.loop2.test_stage1 import DONE, _final, _record

TASK = StageTask("3d-a", "search the catalog for red shoes", "3d", "/a", "done")


class _ClosedSocket:
    """A DevTools socket whose peer went away: no close frame received or sent."""

    def send(self, _message: str) -> None:
        raise ConnectionClosedError(None, None)

    def recv(self, timeout: float | None = None) -> str:  # noqa: ARG002 - the socket's shape
        raise ConnectionClosedError(None, None)

    def close(self) -> None:
        raise ConnectionClosedError(None, None)


def _lost_session() -> CdpSession:
    session = CdpSession.__new__(CdpSession)
    session._ws = _ClosedSocket()  # type: ignore[assignment]  # a fake socket
    session._next_id = 1
    return session


def test_a_call_on_a_closed_connection_is_chrome_lost_an_os_error() -> None:
    with pytest.raises(ChromeLost) as lost:
        _lost_session().call("Runtime.evaluate", {"expression": "1"})
    assert isinstance(lost.value, OSError)
    assert str(lost.value) == CHROME_LOST


def test_closing_a_lost_connection_does_not_raise() -> None:
    _lost_session().close()


def test_a_lost_chrome_is_a_void_with_its_closed_cause() -> None:
    got = classify(DONE, dataclasses.replace(_record(_final()), log_read=False, lost=CHROME_LOST))
    assert (got.verdict, got.reason) == ("void", CHROME_LOST)


def test_the_3d_run_voids_when_chrome_goes_away_before_the_log_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `_live_3d`: the page is ready, the child runs and ends, then the log read finds
    the DevTools connection closed. The run is a `chrome_lost` void; nothing is raised."""

    class Server:
        port = 1

        def __init__(self, _pages: Any) -> None:  # Any: the fixture's pages
            pass

        def __enter__(self) -> Server:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class Proc:
        pid = 4242

    class Window:
        hwnd = 16

    class Child:
        pid = 5151

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - the Child shape
            return 0

    def start(_argv: list[str], out: Path) -> Child:
        out.write_text(__import__("json").dumps(_final()) + "\n", encoding="utf-8")
        return Child()

    monkeypatch.setattr(layer_b_pages, "PageServer", Server)
    monkeypatch.setattr(instant_search, "_free_port", lambda: 9)
    monkeypatch.setattr(instant_search, "_wait_ready", lambda _cdp: None)
    monkeypatch.setattr(instant_search, "SETTLE_S", 0.0)
    monkeypatch.setattr(chrome_a11y, "launch_chrome", lambda *_a, **_k: Proc())
    monkeypatch.setattr(chrome_a11y, "stop_chrome", lambda *_a, **_k: None)
    monkeypatch.setattr(windows, "wait_window_for_pid", lambda *_a, **_k: Window())
    monkeypatch.setattr(layer_b, "_ours", lambda *_a, **_k: (_lost_session(), 1))
    monkeypatch.setattr(suite_chunks, "kill_tree", lambda _pid: None)
    monkeypatch.setattr(stage1, "_start", start)
    monkeypatch.setattr(stage1, "_tree_reading", lambda: (5000, (900.0, 950.0, 12)))
    record = stage1._live_3d(TASK, 1, out_dir=tmp_path, price=tmp_path / "price.json")
    assert record.lost == CHROME_LOST
    assert record.log_read is False
    assert classify(TASK, record).verdict == "void"


def test_the_set_goes_on_after_a_chrome_lost_void() -> None:
    """A void is re-run within the bound: the task still gets its scored runs."""
    records = iter([dataclasses.replace(_record(_final()), log_read=False, lost=CHROME_LOST),
                    _record(), _record(), _record()])  # fmt: skip
    runs, not_started = run_task_runs(DONE, runs=3, attempt=lambda _run: next(records))
    assert [r.verdict for r in runs] == ["void", "pass", "pass", "pass"]
    assert not_started == 0


def test_the_harness_cause_is_the_clients_own_token() -> None:
    assert CHROME_LOST == chrome_a11y.CHROME_LOST
