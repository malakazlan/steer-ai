"""`select` on a real Chrome tablist and radio group (W3 v1 test plan): an ARIA tablist whose
click handler flips aria-selected and shows its panel, and native radios in a fieldset with a
legend, through the loop with the real semantic executor and live reads. The page writes its own
state into a status line, so each pass is judged by the page as well as by T1's IsSelected read.
Semantic UIA only: no input is injected."""

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
PAGE = """<!doctype html><title>Select page</title>
<div role="tablist" aria-label="Sections">
  <button role="tab" id="overview" aria-selected="true" aria-controls="p-overview">Overview</button>
  <button role="tab" id="details" aria-selected="false" aria-controls="p-details">Details</button>
</div>
<div role="tabpanel" id="p-overview">Overview panel</div>
<div role="tabpanel" id="p-details" hidden>Details panel</div>
<fieldset><legend>Delivery speed</legend>
  <label><input type="radio" name="speed" value="standard" checked> Standard</label>
  <label><input type="radio" name="speed" value="express"> Express</label>
</fieldset>
<p id="status">ready</p>
<script>
const tabs = Array.from(document.querySelectorAll("[role=tab]"));
function show() {
  const tab = tabs.find((t) => t.getAttribute("aria-selected") === "true").id;
  const speed = document.querySelector("input[name=speed]:checked").value;
  document.getElementById("status").textContent = "tab " + tab + ", speed " + speed;
}
for (const tab of tabs) {
  tab.addEventListener("click", () => {
    for (const other of tabs) {
      const on = other === tab;
      other.setAttribute("aria-selected", on ? "true" : "false");
      document.getElementById(other.getAttribute("aria-controls")).hidden = !on;
    }
    show();
  });
}
for (const radio of document.querySelectorAll("input[name=speed]")) {
  radio.addEventListener("change", show);
}
</script>"""


WIDGETS = (
    ("TabItem", "Overview"),
    ("TabItem", "Details"),
    ("RadioButton", "Standard"),
    ("RadioButton", "Express"),
)


class NoAsk:
    def ask(self, ask: Ask) -> str:
        raise AssertionError(f"an R1 select never asks: {ask.verb}")


def _ready(model: WorldModel, hwnd: int, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        # the browser's own tab strip holds TabItems too: count the page's by name
        shown = {(e.role, e.name) for e in snapshot.elements}
        if shown >= set(WIDGETS):
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(f"tabs and radios never exposed; {snapshot.node_count} nodes")
        time.sleep(0.5)


def _status(model: WorldModel, hwnd: int) -> str:
    snapshot = model.observe(hwnd, cap_s=5.0)
    return next(e.name for e in snapshot.elements if e.role == "Text" and e.name[:4] == "tab ")


def _step(role: str, name: str, step_id: str) -> Step:
    return Step(id=step_id, subgoal="g1", verb="select", target=Target(role=role, name=name))


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_select_picks_a_real_tab_and_a_real_radio(tmp_path: Path) -> None:
    page = tmp_path / "select.html"
    page.write_text(PAGE, encoding="utf-8")
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-select-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9345, page.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        client = UiaClient()
        model = WorldModel(client)
        _ready(model, window.hwnd)
        session = Session(
            task_id="task-select",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=NoAsk(), own_pid=0, task_id="task-select"),
            ledger=Ledger(tmp_path, task_id="task-select", started_at=time.time()),
            live=UiaLiveReads(client, UiaBindings.load()),
            trace=ListTrace(),
        )
        tab = session.run_step(_step("TabItem", "Details", "s1"))
        assert tab.ok is True, tab.to_dict()
        assert tab.action is not None
        assert tab.action.skipped is False
        assert [s.label for s in tab.action.steps] == ["SelectionItemPattern.Select"]
        assert _status(model, window.hwnd) == "tab details, speed standard"
        radio = session.run_step(_step("RadioButton", "Express", "s2"))
        assert radio.ok is True, radio.to_dict()
        assert radio.action is not None
        assert [s.label for s in radio.action.steps] == ["SelectionItemPattern.Select"]
        assert _status(model, window.hwnd) == "tab details, speed express"
        again = session.run_step(_step("RadioButton", "Express", "s3"))
        assert again.ok is True, again.to_dict()
        assert again.action is not None
        assert again.action.skipped is True
        assert _status(model, window.hwnd) == "tab details, speed express"
    finally:
        stop_chrome(proc, profile_dir)
