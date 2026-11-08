"""A button Chromium exposes without InvokePattern (#149; principal D-149 required Windows test):
a local page's `<button aria-haspopup="dialog" aria-expanded="false">` reads with ExpandCollapse
and no Invoke, like GitHub's header search button; Steer's click step reaches it through the
physical fallback and verifies the dialog it opens."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from steerai.action.execution import UIA_INVOKE_PATTERN, UiaBindings
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
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.verify.predicates import Appeared
from steerai.world.model import Snapshot, WorldModel

pytestmark = pytest.mark.windows
NAME = "Open the search panel"
PAGE = f"""<!doctype html><title>Fallback page</title>
<button id="b" aria-haspopup="dialog" aria-expanded="false">{NAME}</button>
<div id="d" role="dialog" aria-label="Search panel" hidden><p>Panel opened</p></div>
<script>
document.getElementById("b").addEventListener("click", () => {{
  document.getElementById("d").hidden = false;
  document.getElementById("b").setAttribute("aria-expanded", "true");
}});
</script>"""


class NoAsk:
    def ask(self, ask: Ask) -> str:
        raise AssertionError(f"an R0 click never asks: {ask.verb}")


def _button(model: WorldModel, hwnd: int, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        if any(e.role == "Button" and e.name == NAME for e in snapshot.elements):
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(f"button never exposed; {snapshot.node_count} nodes")
        time.sleep(0.5)


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_a_button_without_invoke_is_clicked_through_the_physical_fallback(tmp_path: Path) -> None:
    page = tmp_path / "fallback.html"
    page.write_text(PAGE, encoding="utf-8")
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-fallback-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9341, page.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        acquire_foreground(window.hwnd)
        client = UiaClient()
        bindings = UiaBindings.load()
        model = WorldModel(client)
        start = _button(model, window.hwnd)
        button = next(e for e in start.elements if e.role == "Button" and e.name == NAME)
        assert not start.handle(button.id).GetCurrentPattern(UIA_INVOKE_PATTERN), (
            "the premise: Chromium exposes no Invoke on this button"
        )
        session = Session(
            task_id="task-fallback",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=NoAsk(), own_pid=0, task_id="task-fallback"),
            ledger=Ledger(tmp_path, task_id="task-fallback", started_at=time.time()),
            live=UiaLiveReads(client, bindings),
            trace=ListTrace(),
        )
        step = Step(
            id="c1",
            subgoal="g1",
            verb="click",
            target=Target(role="Button", name=NAME),
            risk="R0",
            expect=Appeared(role="Text", name_pattern="Panel opened"),
        )
        result = session.run_step(step)
        assert result.ok is True, result.to_dict()
        assert result.evidence["click_fallback"] == "physical"
        assert result.evidence["semantic_detail"] == "no_InvokePattern"
    finally:
        stop_chrome(proc, profile_dir)
