"""`check` / `uncheck` on real Chrome checkboxes (W2 v2 test plan): a native box and a mixed
"Select all" box, through the loop with the real semantic executor and live reads. The page
writes its own state into a status line, so each pass is judged by the page as well as by T1's
ToggleState read."""

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
from steerai.orchestrator.gate import Ask, Gate
from steerai.orchestrator.ledger import Ledger
from steerai.orchestrator.loop import Session
from steerai.orchestrator.trace import ListTrace
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, stop_chrome
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid
from steerai.world.model import Snapshot, WorldModel

pytestmark = pytest.mark.windows
PAGE = """<!doctype html><title>Checkbox page</title>
<label><input type="checkbox" id="alerts"> Email alerts</label>
<label><input type="checkbox" id="all"> Select all</label>
<p id="status">ready</p>
<script>
const all = document.getElementById("all");
all.indeterminate = true;
function show() {
  document.getElementById("status").textContent =
    "alerts " + document.getElementById("alerts").checked + ", all " + all.checked;
}
for (const id of ["alerts", "all"]) document.getElementById(id).addEventListener("change", show);
</script>"""


class NoAsk:
    def ask(self, ask: Ask) -> str:
        raise AssertionError(f"an R1 check never asks: {ask.verb}")


def _ready(model: WorldModel, hwnd: int, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        if sum(e.role == "CheckBox" for e in snapshot.elements) == 2:
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(f"checkboxes never exposed; {snapshot.node_count} nodes")
        time.sleep(0.5)


def _status(model: WorldModel, hwnd: int) -> str:
    snapshot = model.observe(hwnd, cap_s=5.0)
    return next(e.name for e in snapshot.elements if e.role == "Text" and e.name[:6] == "alerts")


def _step(verb: str, name: str, step_id: str) -> Step:
    return Step(id=step_id, subgoal="g1", verb=verb, target=Target(role="CheckBox", name=name))


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_check_and_uncheck_set_real_checkboxes(tmp_path: Path) -> None:
    page = tmp_path / "checkboxes.html"
    page.write_text(PAGE, encoding="utf-8")
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-check-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9343, page.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        client = UiaClient()
        model = WorldModel(client)
        _ready(model, window.hwnd)
        session = Session(
            task_id="task-check",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=NoAsk(), own_pid=0, task_id="task-check"),
            ledger=Ledger(tmp_path, task_id="task-check", started_at=time.time()),
            live=UiaLiveReads(client, UiaBindings.load()),
            trace=ListTrace(),
        )
        checked = session.run_step(_step("check", "Email alerts", "c1"))
        assert checked.ok is True, checked.to_dict()
        assert checked.action is not None
        assert checked.action.evidence == {"toggles": 1}
        assert _status(model, window.hwnd) == "alerts true, all false"
        # a mixed box reads ToggleState 2; one Toggle gives 1, so `uncheck` needs a second
        unchecked = session.run_step(_step("uncheck", "Select all", "c2"))
        assert unchecked.ok is True, unchecked.to_dict()
        assert unchecked.action is not None
        assert unchecked.action.evidence == {"toggles": 2}
        assert _status(model, window.hwnd) == "alerts true, all false"
        again = session.run_step(_step("check", "Email alerts", "c3"))
        assert again.ok is True, again.to_dict()
        assert again.action is not None
        assert again.action.skipped is True
    finally:
        stop_chrome(proc, profile_dir)
