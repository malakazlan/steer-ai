"""An offscreen target on a lazy-loading feed, end to end on real Chrome (principal re-review of
#184, item 1): the "Load more" button sits below the fold; scrolling to it brings a sentinel
into view that appends rows 300 ms later, as an infinite feed does. The click step must scroll
before the gate, wait out the lazy load in its quiet look, click, and verify the row the click
adds, never counting the lazily loaded rows as the click's effect."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from steerai.action.execution import UiaBindings
from steerai.action.executor import Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.physical import PhysicalExecutor
from steerai.orchestrator.contracts import Step, StepResult, Target
from steerai.orchestrator.gate import Ask, Gate
from steerai.orchestrator.ledger import Ledger
from steerai.orchestrator.loop import Session
from steerai.orchestrator.trace import ListTrace
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, stop_chrome
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid
from steerai.verify.predicates import Appeared
from steerai.world.model import Snapshot, WorldModel

pytestmark = pytest.mark.windows
PAGE = """<!doctype html><title>Feed page</title>
<ul id="feed" aria-label="Feed items"></ul>
<div id="sentinel" style="height:1px"></div>
<button id="more">Load more</button>
<script>
const feed = document.getElementById("feed");
let n = 0;
function add(count) {
  for (let i = 0; i < count; i++) {
    n += 1;
    const li = document.createElement("li");
    li.textContent = "Feed item " + n;
    li.style.height = "60px";
    feed.appendChild(li);
  }
}
add(60);
let loaded = false;
new IntersectionObserver((entries) => {
  if (!loaded && entries.some((e) => e.isIntersecting)) {
    loaded = true;
    setTimeout(() => add(5), 300);
  }
}).observe(document.getElementById("sentinel"));
document.getElementById("more").addEventListener("click", () => {
  const li = document.createElement("li");
  li.textContent = "Loaded by the button";
  feed.appendChild(li);
});
</script>"""


class NoAsk:
    def ask(self, ask: Ask) -> str:
        raise AssertionError(f"an R1 click never asks: {ask.verb}")


def _ready(model: WorldModel, hwnd: int, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        if any(e.role == "Button" and e.name == "Load more" for e in snapshot.elements):
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(f"button never exposed; {snapshot.node_count} nodes")
        time.sleep(0.5)


def run_load_more(tmp_path: Path, port: int = 9345) -> tuple[StepResult, bool]:
    """One run: (the step's result, whether the button was offscreen before the step)."""
    page = tmp_path / "feed.html"
    page.write_text(PAGE, encoding="utf-8")
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-feed-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, port, page.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        client = UiaClient()
        model = WorldModel(client)
        start = _ready(model, window.hwnd)
        button = next(e for e in start.elements if e.role == "Button" and e.name == "Load more")
        session = Session(
            task_id="task-feed",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=NoAsk(), own_pid=0, task_id="task-feed"),
            ledger=Ledger(tmp_path, task_id="task-feed", started_at=time.time()),
            live=UiaLiveReads(client, UiaBindings.load()),
            trace=ListTrace(),
        )
        step = Step(
            id="m1",
            subgoal="g1",
            verb="click",
            target=Target(role="Button", name="Load more"),
            risk="R1",
            expect=Appeared(role="ListItem", name_pattern="Loaded by the button"),
        )
        return session.run_step(step), button.offscreen
    finally:
        stop_chrome(proc, profile_dir)


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_an_offscreen_button_on_a_lazy_feed_is_scrolled_waited_clicked_and_verified(
    tmp_path: Path,
) -> None:
    result, was_offscreen = run_load_more(tmp_path)
    assert was_offscreen, "the premise: the button starts below the fold"
    assert result.ok is True, result.to_dict()
    assert result.evidence["scrolled"] == "delivered_verified"
