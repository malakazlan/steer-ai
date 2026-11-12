"""A tree item opened, closed and opened again through the real Session on the gallery
([v2.81]): the semantic executor refuses `expand_by_click`, the loop clicks the item's own row
once per step, T1 reads ExpandCollapseState, and the page's own log proves each open and close
was a trusted event on the folder and the child item was never selected (measured before the
fix: a click at an open item's centre selects its child, 3/3)."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from steerai.action.execution import UiaBindings
from steerai.action.executor import Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.physical import PhysicalExecutor
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import Gate
from steerai.orchestrator.ledger import Ledger
from steerai.orchestrator.loop import Session
from steerai.orchestrator.trace import ListTrace
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, stop_chrome
from steerai.probes.gallery_page import page_html
from steerai.probes.layer_b_pages import PageServer
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.world.model import WorldModel
from tests.orchestrator.test_capability_verbs_live import Person, _log

pytestmark = pytest.mark.windows
PORT = 9354


def _step(step_id: str, verb: str) -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb=verb,
        target=Target(role="TreeItem", name="Projects"),
        risk="R1",
    )


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_a_tree_item_opens_closes_and_opens_through_the_session(tmp_path: Path) -> None:
    profile = Path(tempfile.mkdtemp(prefix="steer-tree-"))
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
                if any(e.name == "Projects" for e in model.observe(window.hwnd).elements):
                    break
                time.sleep(0.5)
            session = Session(
                task_id="task-tree",
                hwnd=window.hwnd,
                observer=model,
                semantic=Executor(),
                physical=PhysicalExecutor(),
                gate=Gate(confirmer=Person(), own_pid=0, task_id="task-tree"),
                ledger=Ledger(tmp_path, task_id="task-tree", started_at=time.time()),
                live=UiaLiveReads(client, UiaBindings.load()),
                trace=ListTrace(),
            )
            for step_id, verb in (("s1", "expand"), ("s2", "collapse"), ("s3", "expand")):
                result = session.run_step(_step(step_id, verb))
                assert result.ok is True, (step_id, result.to_dict())
                assert result.evidence["click_fallback"] == "physical", result.to_dict()
                time.sleep(0.3)
            again = session.run_step(_step("s4", "expand"))
            assert again.ok is True
            assert again.action is not None
            assert again.action.skipped is True, "open already: nothing sent"
            events = [(e.get("row"), e.get("open"), e.get("untrusted")) for e in _log(PORT)]
            assert [e for e in events if e[0] == "g20"] == [
                ("g20", True, None),
                ("g20", False, None),
                ("g20", True, None),
            ], events
            assert all(e[0] != "g20-alpha" for e in events), "the child was never selected"
        finally:
            stop_chrome(proc, profile)
