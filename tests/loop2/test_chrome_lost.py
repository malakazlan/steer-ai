"""#459: a Chrome that goes away mid-run (its DevTools connection closes without a close frame)
is an infrastructure event: the DevTools client raises `ChromeLost` (an `OSError`), the stage
harness ends the run as a void with the closed cause `chrome_lost`, and the set goes on, never a
traceback exit. The live edges are faked; the harness's own run function is the real one."""

from __future__ import annotations

import dataclasses
import json
import re
import socket
import struct
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.utils import accept_key

from steerai.loop2 import stage1
from steerai.loop2.stage1 import CHROME_LOST, StageTask, accept, classify, run_task_runs
from steerai.probes import (
    chrome_a11y,
    instant_search,
    layer_b,
    layer_b_pages,
    suite_chunks,
    windows,
)
from steerai.probes.chrome_a11y import CdpSession, ChromeLost
from tests.loop2.test_stage1 import DONE, _final, _ok, _record

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


BLOCKED_LINE = _final("blocked", "controller_stop")
"""Steer did not claim done: a lost Chrome is then the machine's, a void."""


def test_a_lost_chrome_when_steer_did_not_claim_done_is_a_void_with_its_cause() -> None:
    lost = dataclasses.replace(_record(BLOCKED_LINE), log_read=False, check=None, lost=CHROME_LOST)
    got = classify(DONE, lost)
    assert (got.verdict, got.reason) == ("void", CHROME_LOST)


def test_a_done_that_cannot_be_read_because_chrome_is_gone_stays_a_fail() -> None:
    """The coordinator's ruling: a done we cannot verify is where a false done would hide; it
    fails closed (`done_log_unread`), its cause kept visible, never a void that drops out."""
    lost = dataclasses.replace(_record(_final()), log_read=False, check=None, lost=CHROME_LOST)
    got = classify(DONE, lost)
    assert (got.verdict, got.reason) == ("fail", f"done_log_unread:{CHROME_LOST}")
    report = accept([DONE], [got], controls=(5, 5), runs_per_task=3, label="t").report
    assert report["fail_reasons"] == {f"done_log_unread:{CHROME_LOST}": 1}


class _ReadyThenLostSocket(_ClosedSocket):
    """Answers the page's first call (its ready state), then its peer is gone."""

    def __init__(self) -> None:
        self.sent = 0

    def send(self, _message: str) -> None:
        self.sent += 1
        if self.sent > 1:
            raise ConnectionClosedError(None, None)

    def recv(self, timeout: float | None = None) -> str:  # noqa: ARG002 - the socket's shape
        return json.dumps({"id": 1, "result": {"result": {"value": "complete"}}})


def _fake_live_edges(monkeypatch: pytest.MonkeyPatch, session: CdpSession) -> None:
    """Every live edge of a stage 1 run faked: no Chrome, no child, no input. The child ends
    without claiming done, and `session` is the page's DevTools connection."""

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
        out.write_text(json.dumps(BLOCKED_LINE) + "\n", encoding="utf-8")
        return Child()

    monkeypatch.setattr(layer_b_pages, "PageServer", Server)
    monkeypatch.setattr(instant_search, "_free_port", lambda: 9)
    monkeypatch.setattr(instant_search, "_wait_ready", lambda _cdp: None)
    monkeypatch.setattr(instant_search, "SETTLE_S", 0.0)
    monkeypatch.setattr(chrome_a11y, "launch_chrome", lambda *_a, **_k: Proc())
    monkeypatch.setattr(chrome_a11y, "stop_chrome", lambda *_a, **_k: None)
    monkeypatch.setattr(windows, "wait_window_for_pid", lambda *_a, **_k: Window())
    monkeypatch.setattr(layer_b, "_ours", lambda *_a, **_k: (session, 1))
    monkeypatch.setattr(suite_chunks, "kill_tree", lambda _pid: None)
    monkeypatch.setattr(stage1, "_start", start)
    monkeypatch.setattr(stage1, "_tree_reading", lambda: (5000, (900.0, 950.0, 12)))
    monkeypatch.setattr(stage1, "PAGE_SETTLE_S", 0.0)


def test_the_3d_run_voids_when_chrome_goes_away_before_the_log_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `_live_3d`: the page is ready, the child runs and ends, then the log read finds
    the DevTools connection closed. The run is a `chrome_lost` void; nothing is raised."""
    _fake_live_edges(monkeypatch, _lost_session())
    record = stage1._live_3d(TASK, 1, out_dir=tmp_path, price=tmp_path / "price.json")
    assert record.lost == CHROME_LOST
    assert record.log_read is False
    assert classify(TASK, record).verdict == "void"


@pytest.mark.parametrize("fixture", ["gallery", "large"])
def test_the_page_run_voids_when_chrome_goes_away_before_the_log_is_read(
    fixture: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#469: the real `_live_page` (gallery and large): the page loads, the child runs and ends,
    then the log read finds the connection closed. A `chrome_lost` void, never a crash."""
    session = CdpSession.__new__(CdpSession)
    session._ws = _ReadyThenLostSocket()  # type: ignore[assignment]  # a fake socket
    session._next_id = 1
    _fake_live_edges(monkeypatch, session)
    task = StageTask("g01", "click Save draft", fixture, "/page.html", "blocked")
    record = stage1._live_page(task, 1, out_dir=tmp_path, price=tmp_path / "price.json")
    assert (record.fixture_ok, record.log_read, record.lost) == (True, False, CHROME_LOST)
    assert classify(task, record).verdict == "void"


def test_a_done_log_unread_cause_that_is_not_chrome_lost_is_reported_as_other() -> None:
    """#469: `fail_reasons` keeps only the closed cause `chrome_lost`; any other text after
    `done_log_unread:` is `other`, so nothing the child wrote reaches the report."""
    runs = [_ok(DONE, 1), _ok(DONE, 2), _ok(DONE, 3, "fail", reason="done_log_unread:C:/x.txt")]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    assert report["fail_reasons"] == {"done_log_unread:other": 1}


def test_a_chrome_lost_void_counts_in_the_infrastructure_void_limit() -> None:
    """#469: a `chrome_lost` void is the machine's, never a site void: one in ten passes the
    1-in-10 limit, two in ten leave the set inconclusive."""
    lost = [_ok(DONE, r, "void", reason=CHROME_LOST) for r in (9, 10)]
    one = [*(_ok(DONE, r) for r in range(1, 10)), lost[1]]
    summary = accept([DONE], one, controls=(5, 5), runs_per_task=9, label="t")
    assert summary.gates["void"] is True
    assert (summary.report["void_rate"], summary.report["site_voids"]) == (0.1, {})
    assert summary.report["void_causes"] == {CHROME_LOST: 1}
    two = [*(_ok(DONE, r) for r in range(1, 9)), *lost]
    summary = accept([DONE], two, controls=(5, 5), runs_per_task=8, label="t")
    assert summary.gates["void"] is False
    assert summary.report["result"] == "inconclusive"


def _reset_after_first_frame(listener: socket.socket) -> None:
    """A WebSocket peer that accepts the handshake, reads the first frame, then aborts the TCP
    connection (SO_LINGER on, zero timeout: the close sends a reset, never a close frame)."""
    conn, _ = listener.accept()
    with conn:
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        found = re.search(rb"sec-websocket-key: *(\S+)", request, re.IGNORECASE)
        assert found is not None
        answer = accept_key(found.group(1).decode("ascii")).encode("ascii")
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + answer + b"\r\n\r\n"
        )
        conn.recv(4096)
        # Windows' linger is two u_shorts, POSIX's two ints (each `struct linger`; Windows:
        # https://learn.microsoft.com/en-us/windows/win32/api/winsock/ns-winsock-linger)
        linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)


def test_a_hard_reset_of_the_devtools_connection_is_chrome_lost() -> None:
    """#469: a TCP reset (no close frame, the socket error a killed browser can leave) reaches
    the caller as `ChromeLost`, not as a bare `OSError` on the generic unread path."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        peer = threading.Thread(target=_reset_after_first_frame, args=(listener,), daemon=True)
        peer.start()
        session = CdpSession(f"ws://127.0.0.1:{listener.getsockname()[1]}/devtools/page/x")
        with pytest.raises(ChromeLost) as lost:
            session.call("Runtime.evaluate", {"expression": "1"})
        session.close()
        peer.join(timeout=5.0)
    closed = lost.value.__cause__
    assert isinstance(closed, ConnectionClosedError)
    assert isinstance(closed.__cause__, OSError)  # the reset itself, not a clean end of stream
    assert not peer.is_alive()


def test_the_set_goes_on_after_a_chrome_lost_void() -> None:
    """A void is re-run within the bound: the task still gets its scored runs."""
    lost = dataclasses.replace(_record(BLOCKED_LINE), log_read=False, check=None, lost=CHROME_LOST)
    records = iter([lost, _record(), _record(), _record()])
    runs, not_started = run_task_runs(DONE, runs=3, attempt=lambda _run: next(records))
    assert [r.verdict for r in runs] == ["void", "pass", "pass", "pass"]
    assert not_started == 0


def test_the_harness_cause_is_the_clients_own_token() -> None:
    assert CHROME_LOST == chrome_a11y.CHROME_LOST
