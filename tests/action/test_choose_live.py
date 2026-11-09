"""`choose` on a real Chrome page (widget-verbs W4 test plan), through the loop with the real
semantic executor, its UIA option source and live reads: a native `<select>` inside a `<label>`
is chosen and the page's own status line says so; choosing the option already chosen sends
nothing; a datalist input reads LeafNode and is refused `editable_combobox` with nothing sent; a
select whose onchange submits its form stops the task `navigated` (W4-12). A listbox elsewhere
on the page holds an option of the same name, which is never chosen (W4-13). Semantic UIA only:
no input is injected."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from steerai.action.execution import UiaBindings
from steerai.action.executor import Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.options import UiaOptions
from steerai.action.physical import PhysicalExecutor
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import Ask, Gate
from steerai.orchestrator.ledger import Ledger
from steerai.orchestrator.loop import Session
from steerai.orchestrator.trace import ListTrace
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, stop_chrome
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid
from steerai.verify.predicates import TitleMatches
from steerai.world.model import Snapshot, WorldModel

pytestmark = pytest.mark.windows
PAGE = """<!doctype html><title>Choose page</title>
<label>Size <select id="size">
  <option>Small</option><option>Medium</option><option>Large</option>
</select></label>
<label>City <input id="city" list="cities"></label>
<datalist id="cities"><option value="Lahore"><option value="Karachi"></datalist>
<ul role="listbox" aria-label="Elsewhere"><li role="option" id="decoy">Large</li></ul>
<form action="done.html" method="get">
  <label>Plan <select name="plan" onchange="this.form.submit()">
    <option>Basic</option><option>Pro</option>
  </select></label>
</form>
<p id="status">ready</p>
<script>
const status = document.getElementById("status");
document.getElementById("size").addEventListener("change", (e) => {
  status.textContent = "size " + e.target.value;
});
document.getElementById("city").addEventListener("input", () => {
  status.textContent = "city typed";
});
document.getElementById("decoy").addEventListener("click", () => {
  status.textContent = "decoy clicked";
});
</script>"""
DONE = "<!doctype html><title>Plan saved</title><p>Plan saved</p>"
WIDGETS = {("ComboBox", "Size"), ("ComboBox", "City"), ("ComboBox", "Plan")}


class NoAsk:
    def ask(self, ask: Ask) -> str:
        raise AssertionError(f"an R1 choose never asks: {ask.verb}")


def _ready(model: WorldModel, hwnd: int, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        if {(e.role, e.name) for e in snapshot.elements} >= WIDGETS:
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(f"the choosers were never exposed; {snapshot.node_count} nodes")
        time.sleep(0.5)


def _status(model: WorldModel, hwnd: int) -> str:
    snapshot = model.observe(hwnd, cap_s=5.0)
    return next(
        e.name
        for e in snapshot.elements
        if e.role == "Text" and e.name.split(" ")[0] in ("ready", "size", "city", "decoy")
    )


def _choose(name: str, option: str, step_id: str) -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb="choose",
        target=Target(role="ComboBox", name=name),
        params={"option": option},
    )


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_choose_picks_a_real_select_option(tmp_path: Path) -> None:
    page = tmp_path / "choose.html"
    page.write_text(PAGE, encoding="utf-8")
    (tmp_path / "done.html").write_text(DONE, encoding="utf-8")
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-choose-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9347, page.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        client = UiaClient()
        model = WorldModel(client)
        _ready(model, window.hwnd)
        session = Session(
            task_id="task-choose",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(options=UiaOptions(client)),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=NoAsk(), own_pid=0, task_id="task-choose"),
            ledger=Ledger(tmp_path, task_id="task-choose", started_at=time.time()),
            live=UiaLiveReads(client, UiaBindings.load()),
            trace=ListTrace(),
        )
        chosen = session.run_step(_choose("Size", "Large", "s1"))
        assert chosen.ok is True, chosen.to_dict()
        assert chosen.action is not None
        assert [s.label for s in chosen.action.steps] == [
            "ExpandCollapsePattern.Expand",
            "SelectionItemPattern.Select",
            "ExpandCollapsePattern.Collapse",
        ]
        assert _status(model, window.hwnd) == "size Large", "the decoy option is never chosen"

        again = session.run_step(_choose("Size", "Large", "s2"))
        assert again.ok is True, again.to_dict()
        assert again.action is not None
        assert again.action.skipped is True
        assert [s.label for s in again.action.steps] == ["AlreadyInState"]

        editable = session.run_step(_choose("City", "Lahore", "s3"))
        assert editable.ok is False
        assert editable.evidence["detail"] == "editable_combobox"
        assert editable.action is not None
        assert editable.action.steps == []
        assert _status(model, window.hwnd) == "size Large"

        task = session.run_task(
            Plan(
                goal="choose the Pro plan, then the Small size",
                subgoals=(Subgoal(id="g1", desc="d", condition=TitleMatches(pattern="Never")),),
                steps=(_choose("Plan", "Pro", "s4"), _choose("Size", "Small", "s5")),
            )
        )
        assert task.status == "stopped", task
        assert task.evidence["detail"] == "navigated"
        assert [r.step_id for r in task.results] == ["s4"], "nothing more runs on the new page"
    finally:
        stop_chrome(proc, profile_dir)
