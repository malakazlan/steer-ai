"""Verbs by capability through the real Session on the gallery (packet interaction-model-2026-11-12
§2; principal item 7: the gallery is the measuring instrument). Each step runs the whole loop:
resolve, admission, gate, act, readback, and the page's own log proves a trusted event."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from steerai.action.execution import UiaBindings
from steerai.action.executor import Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.physical import PhysicalExecutor
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import Ask, Gate
from steerai.orchestrator.ledger import Ledger
from steerai.orchestrator.loop import Session
from steerai.orchestrator.trace import ListTrace
from steerai.probes.chrome_a11y import (
    CHROME_DEFAULT,
    CdpSession,
    launch_chrome,
    page_ws_url,
    stop_chrome,
)
from steerai.probes.gallery_page import LOG_KEY, page_html
from steerai.probes.layer_b_pages import PageServer
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.world.model import WorldModel

pytestmark = pytest.mark.windows
PORT = 9353


class Person:
    """Approves every ask and records which ones came: only the menu button named "Share" asks
    (the control-word class reads a Button's name, W5; a view change still asks, fail-safe)."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    def ask(self, ask: Ask) -> str:
        self.asked.append((ask.verb, ask.target_name))
        return f"yes:{ask.decision_id}"


def _step(step_id: str, verb: str, role: str, name: str) -> Step:
    return Step(id=step_id, subgoal="g1", verb=verb, target=Target(role=role, name=name), risk="R1")


def _log(port: int) -> list[dict[str, object]]:
    cdp = CdpSession(page_ws_url(port))
    try:
        result = cdp.call(
            "Runtime.evaluate",
            {"expression": f'localStorage.getItem("{LOG_KEY}") || "[]"', "returnByValue": True},
        )
    finally:
        cdp.close()
    return list(json.loads(str(result["result"]["value"])))


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_the_new_capability_targets_run_through_the_session(tmp_path: Path) -> None:
    profile = Path(tempfile.mkdtemp(prefix="steer-capverbs-"))
    with PageServer({"/page.html": page_html()}) as server:
        proc = launch_chrome(
            CHROME_DEFAULT,
            profile,
            PORT,
            f"http://127.0.0.1:{server.port}/page.html",
            window_size=(1100, 900),
        )
        try:
            window = wait_window_for_pid(
                proc.pid, title_contains="Steer gallery", image="chrome.exe", process=proc
            )
            acquire_foreground(window.hwnd)
            client = UiaClient()
            model = WorldModel(client)
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                if any(e.name == "Dark mode" for e in model.observe(window.hwnd).elements):
                    break
                time.sleep(0.5)
            person = Person()
            session = Session(
                task_id="task-capverbs",
                hwnd=window.hwnd,
                observer=model,
                semantic=Executor(),
                physical=PhysicalExecutor(),
                gate=Gate(confirmer=person, own_pid=0, task_id="task-capverbs"),
                ledger=Ledger(tmp_path, task_id="task-capverbs", started_at=time.time()),
                live=UiaLiveReads(client, UiaBindings.load()),
                trace=ListTrace(),
            )
            steps = [
                _step("s1", "check", "Button", "Dark mode"),
                _step("s2", "check", "Button", "Bold"),
                _step("s3", "select", "DataItem", "Order 1001"),
                _step("s4", "expand", "Button", "Share"),
                _step("s5", "check", "CheckBox", "Show word count"),
                _step("s6", "expand", "Button", "Share"),
                _step("s7", "select", "RadioButton", "Large font"),
            ]
            for step in steps:
                result = session.run_step(step)
                assert result.ok is True, (step.id, result.to_dict())
                time.sleep(0.3)
            assert {verb for verb, _name in person.asked} <= {"expand"}, person.asked
            rows = [(entry.get("row"), entry.get("untrusted")) for entry in _log(PORT)]
            assert ("g03", None) in rows, rows  # the switch, trusted
            assert ("g02", None) in rows, rows  # aria-pressed, trusted
            assert ("g21", None) in rows, rows  # the grid row
            assert ("g18", None) in rows, rows  # the menu checkbox
            assert ("g19", None) in rows, rows  # the menu radio
        finally:
            stop_chrome(proc, profile)
