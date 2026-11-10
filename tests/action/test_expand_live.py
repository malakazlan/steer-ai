"""`expand` / `collapse` on a real disclosure Button (W5, the post-H3 ruling): an `aria-expanded`
button that shows or hides a panel (the H3 n5 accordion shape), through the loop with the real
semantic executor and live reads. The page writes its own state into a status line, so each pass
is judged by the page as well as by T1's ExpandCollapseState read; the panel's text is in the tree
while it is open. An already-expanded button is skipped with nothing sent; a plain button (Invoke
only) is refused `no_ExpandCollapsePattern` with nothing sent and no physical fallback.

The window is brought to the front before the first observe: a Chrome started by a background
process opens behind the foreground window, and a covered Chromium window keeps a partial
accessibility tree with no Document (`layer_b._in_front`, measured 2026-11-08). That is the only
input injected: at most a tagged zero-distance mouse move inside `acquire_foreground`. The
expands and collapses are semantic UIA only."""

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
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.world.model import Snapshot, WorldModel

pytestmark = pytest.mark.windows
ANSWER = "Orders ship within two days."
PAGE = f"""<!doctype html><title>Disclosure page</title>
<button id="acc" aria-expanded="false" aria-controls="panel">Shipping questions</button>
<div id="panel" hidden><p>{ANSWER}</p></div>
<button id="help" aria-expanded="true" aria-controls="help-panel">Help section</button>
<div id="help-panel"><p>Help is here.</p></div>
<button id="plain">Contact us</button>
<p id="status">shipping closed, help open</p>
<script>
function wire(button, panel) {{
  button.addEventListener("click", () => {{
    const open = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", open ? "true" : "false");
    panel.hidden = !open;
    show();
  }});
}}
function state(id) {{
  return document.getElementById(id).getAttribute("aria-expanded") === "true" ? "open" : "closed";
}}
function show() {{
  document.getElementById("status").textContent =
    "shipping " + state("acc") + ", help " + state("help");
}}
wire(document.getElementById("acc"), document.getElementById("panel"));
wire(document.getElementById("help"), document.getElementById("help-panel"));
</script>"""


WIDGETS = (
    ("Button", "Shipping questions"),
    ("Button", "Help section"),
    ("Button", "Contact us"),
    ("Text", "shipping closed, help open"),
)


class NoAsk:
    def ask(self, ask: Ask) -> str:
        raise AssertionError(f"an R1 {ask.verb} never asks")


def _ready(model: WorldModel, hwnd: int, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        shown = {(e.role, e.name) for e in snapshot.elements}
        if shown >= set(WIDGETS):
            return snapshot
        if time.monotonic() >= deadline:
            seen = sorted(f"{role} {name!r}" for role, name in shown if role == "Button")
            raise AssertionError(f"buttons never exposed; {snapshot.node_count} nodes, {seen}")
        time.sleep(0.5)


def _page(model: WorldModel, hwnd: int) -> tuple[str, bool]:
    """The page's own status line, and whether the shipping answer is in the tree."""
    snapshot = model.observe(hwnd, cap_s=5.0)
    status = next(
        e.name for e in snapshot.elements if e.role == "Text" and e.name[:9] == "shipping "
    )
    return status, any(e.role == "Text" and e.name == ANSWER for e in snapshot.elements)


def _step(verb: str, name: str, step_id: str) -> Step:
    return Step(id=step_id, subgoal="g1", verb=verb, target=Target(role="Button", name=name))


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_expand_and_collapse_a_disclosure_button(tmp_path: Path) -> None:
    page = tmp_path / "disclosure.html"
    page.write_text(PAGE, encoding="utf-8")
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-expand-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9347, page.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        acquire_foreground(window.hwnd)
        client = UiaClient()
        model = WorldModel(client)
        _ready(model, window.hwnd)
        assert _page(model, window.hwnd) == ("shipping closed, help open", False)
        session = Session(
            task_id="task-expand",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=NoAsk(), own_pid=0, task_id="task-expand"),
            ledger=Ledger(tmp_path, task_id="task-expand", started_at=time.time()),
            live=UiaLiveReads(client, UiaBindings.load()),
            trace=ListTrace(),
        )
        opened = session.run_step(_step("expand", "Shipping questions", "s1"))
        assert opened.ok is True, opened.to_dict()
        assert opened.action is not None
        assert opened.action.skipped is False
        assert [s.label for s in opened.action.steps] == ["ExpandCollapsePattern.Expand"]
        assert _page(model, window.hwnd) == ("shipping open, help open", True)
        closed = session.run_step(_step("collapse", "Shipping questions", "s2"))
        assert closed.ok is True, closed.to_dict()
        assert closed.action is not None
        assert [s.label for s in closed.action.steps] == ["ExpandCollapsePattern.Collapse"]
        assert _page(model, window.hwnd) == ("shipping closed, help open", False)
        again = session.run_step(_step("expand", "Shipping questions", "s3"))
        assert again.ok is True, again.to_dict()
        assert again.action is not None
        assert [s.label for s in again.action.steps] == ["ExpandCollapsePattern.Expand"]
        assert _page(model, window.hwnd) == ("shipping open, help open", True)
        skipped = session.run_step(_step("expand", "Help section", "s4"))
        assert skipped.ok is True, skipped.to_dict()
        assert skipped.action is not None
        assert skipped.action.skipped is True
        assert [s.label for s in skipped.action.steps] == ["AlreadyInState"]
        refused = session.run_step(_step("expand", "Contact us", "s5"))
        assert refused.ok is False, refused.to_dict()
        assert refused.evidence["detail"] == "no_ExpandCollapsePattern", refused.to_dict()
        assert refused.action is not None
        assert refused.action.steps == []
        assert "click_fallback" not in refused.evidence
        assert _page(model, window.hwnd) == ("shipping open, help open", True)
    finally:
        stop_chrome(proc, profile_dir)
