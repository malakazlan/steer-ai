"""`select` on a real listbox option (W3b, the post-H3 ruling): an ARIA `role=listbox` of twenty
options in a 120 px box, whose "Kyoto" is scrolled out of view (the H3 n7 shape), and a native
`<select size=4>`, through the loop with the real semantic executor and live reads. The listbox
admission reads the item's SelectionItem and the List's Selection (CanSelectMultiple false) live;
the offscreen option is scrolled into view before the gate ([v2.55]). The page writes its own
state into a status line, so each pass is judged by the page as well as by T1's IsSelected read.

The window is brought to the front before the first observe: a Chrome started by a background
process opens behind the foreground window, and a covered Chromium window keeps a partial
accessibility tree with no Document (`layer_b._in_front`, measured 2026-11-08; seen here as a
37-node tree for 25 s, 2 of 5 runs). That is the only input injected: at most a tagged
zero-distance mouse move inside `acquire_foreground`. The selects are semantic UIA only."""

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
CITIES = (
    "Accra",
    "Agra",
    "Baku",
    "Cairo",
    "Delhi",
    "Dhaka",
    "Hanoi",
    "Jaipur",
    "Kabul",
    "Kano",
    "Kiev",
    "Kobe",
    "Kyoto",
    "Lagos",
    "Lima",
    "Lyon",
    "Male",
    "Oslo",
    "Pune",
    "Rome",
)
OPTIONS = "".join(f'<li role="option" aria-selected="false">{city}</li>' for city in CITIES)
PAGE = f"""<!doctype html><title>Listbox page</title>
<p id="lb-label">Destinations</p>
<ul id="lb" role="listbox" aria-labelledby="lb-label" style="height:120px;overflow:auto">
{OPTIONS}
</ul>
<label>Size <select id="sz" size="4"><option>S</option><option>M</option><option>L</option>
<option>XL</option></select></label>
<p id="status">dest none, size none</p>
<script>
function show() {{
  const chosen = document.querySelector("#lb li[aria-selected=true]");
  const dest = chosen ? chosen.textContent.toLowerCase() : "none";
  const size = document.getElementById("sz").value || "none";
  document.getElementById("status").textContent = "dest " + dest + ", size " + size;
}}
for (const li of document.querySelectorAll("#lb li")) {{
  li.addEventListener("click", () => {{
    for (const o of document.querySelectorAll("#lb li")) o.setAttribute("aria-selected", o === li);
    show();
  }});
}}
document.getElementById("sz").addEventListener("change", show);
</script>"""


WIDGETS = (
    ("List", "Destinations"),
    ("ListItem", "Accra"),
    ("ListItem", "Kyoto"),
    ("List", "Size"),
    ("ListItem", "L"),
)


class NoAsk:
    def ask(self, ask: Ask) -> str:
        raise AssertionError(f"an R1 select never asks: {ask.verb}")


def _ready(model: WorldModel, hwnd: int, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        shown = {(e.role, e.name) for e in snapshot.elements}
        if shown >= set(WIDGETS):
            return snapshot
        if time.monotonic() >= deadline:
            seen = sorted(
                f"{role} {name!r}" for role, name in shown if role in ("List", "ListItem")
            )
            raise AssertionError(
                f"lists and options never exposed; {snapshot.node_count} nodes, {seen}"
            )
        time.sleep(0.5)


def _status(model: WorldModel, hwnd: int) -> str:
    snapshot = model.observe(hwnd, cap_s=5.0)
    return next(e.name for e in snapshot.elements if e.role == "Text" and e.name[:5] == "dest ")


def _step(name: str, step_id: str) -> Step:
    return Step(id=step_id, subgoal="g1", verb="select", target=Target(role="ListItem", name=name))


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_select_picks_a_scrolled_listbox_option_and_a_native_list_row(tmp_path: Path) -> None:
    page = tmp_path / "listbox.html"
    page.write_text(PAGE, encoding="utf-8")
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-listbox-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9346, page.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        acquire_foreground(window.hwnd)
        client = UiaClient()
        model = WorldModel(client)
        ready = _ready(model, window.hwnd)
        kyoto = next(e for e in ready.elements if e.role == "ListItem" and e.name == "Kyoto")
        assert kyoto.offscreen is True, "the n7 shape: Kyoto is scrolled out of view in its box"
        session = Session(
            task_id="task-listbox",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=NoAsk(), own_pid=0, task_id="task-listbox"),
            ledger=Ledger(tmp_path, task_id="task-listbox", started_at=time.time()),
            live=UiaLiveReads(client, UiaBindings.load()),
            trace=ListTrace(),
        )
        option = session.run_step(_step("Kyoto", "s1"))
        assert option.ok is True, option.to_dict()
        assert option.evidence["scrolled"] == "delivered_verified", option.to_dict()
        assert option.action is not None
        assert option.action.skipped is False
        assert [s.label for s in option.action.steps] == ["SelectionItemPattern.Select"]
        assert _status(model, window.hwnd) == "dest kyoto, size none"
        row = session.run_step(_step("L", "s2"))
        assert row.ok is True, row.to_dict()
        assert "scrolled" not in row.evidence, "the four rows of a size=4 select are on screen"
        assert row.action is not None
        assert [s.label for s in row.action.steps] == ["SelectionItemPattern.Select"]
        assert _status(model, window.hwnd) == "dest kyoto, size L"
        again = session.run_step(_step("L", "s3"))
        assert again.ok is True, again.to_dict()
        assert again.action is not None
        assert again.action.skipped is True
        assert _status(model, window.hwnd) == "dest kyoto, size L"
    finally:
        stop_chrome(proc, profile_dir)
