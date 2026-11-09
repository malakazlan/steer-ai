"""An Enter submit on a search box whose suggestion is the active option (D-151 v2), on a real
Chrome page through the loop with the real executors and live reads.

The page is the APG combobox with automatic selection: DOM focus stays in the input, typing
shows a listbox, and `aria-activedescendant` moves to the first option, which Chrome reports as
the UIA focus. Enter would accept that option, so the submit is refused `focus_lost` /
`active_option` after the person's yes, and nothing is pressed: the page logs a keydown Enter in
its status line, and an Enter in the field would also submit the form to another page.

The test injects typing, so it needs the person idle; it runs in a throwaway profile."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from steerai.action.dispositions import Delivery
from steerai.action.execution import UiaBindings
from steerai.action.executor import Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.physical import PhysicalExecutor
from steerai.errors import ErrorCode
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
TITLE = "Active option page"
TYPED = "flexbox"
PAGE = """<!doctype html><title>Active option page</title>
<form action="results.html" method="get">
  <input id="q" name="q" type="text" role="combobox" aria-label="Search" autocomplete="off"
         aria-autocomplete="list" aria-expanded="false" aria-controls="suggestions">
  <ul id="suggestions" role="listbox" aria-label="Suggestions" hidden>
    <li role="option" id="opt-0" aria-selected="false">Site search</li>
    <li role="option" id="opt-1" aria-selected="false">Flexbox basics</li>
    <li role="option" id="opt-2" aria-selected="false">Float layout</li>
  </ul>
</form>
<p id="status">ready</p>
<script>
const input = document.getElementById("q");
const list = document.getElementById("suggestions");
const first = document.getElementById("opt-0");
let pending = 0;
input.addEventListener("input", () => {
  first.textContent = "Site search for " + input.value;
  list.hidden = false;
  input.setAttribute("aria-expanded", "true");
  // suggestions arrive after the keystrokes, as on a real site; then the first is active
  clearTimeout(pending);
  pending = setTimeout(() => {
    first.setAttribute("aria-selected", "true");
    input.setAttribute("aria-activedescendant", "opt-0");
  }, 300);
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("status").textContent = "enter pressed";
});
</script>"""
RESULTS = "<!doctype html><title>flexbox results</title><p>Results</p>"
SEARCH = Target(role="ComboBox", name="Search")


@dataclass
class YesConfirmer:
    asked: list[Ask] = field(default_factory=list)

    def ask(self, ask: Ask) -> str:
        self.asked.append(ask)
        return f"yes:{ask.decision_id}"


def _observe_until(
    model: WorldModel,
    hwnd: int,
    ready: str,
    test: Callable[[Snapshot], bool],
    max_s: float = 25.0,
) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        if test(snapshot):
            return snapshot
        if time.monotonic() >= deadline:
            roles = sorted({e.role for e in snapshot.elements})
            raise AssertionError(f"{ready} never seen; {snapshot.node_count} nodes, {roles}")
        time.sleep(0.2)


def _has_search(snapshot: Snapshot) -> bool:
    return any(e.role == SEARCH.role and e.name == SEARCH.name for e in snapshot.elements)


def _status(snapshot: Snapshot) -> str:
    return next(
        e.name
        for e in snapshot.elements
        if e.role == "Text" and e.name in ("ready", "enter pressed")
    )


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
@pytest.mark.usefixtures("warm_chrome")
def test_enter_on_a_combobox_with_an_active_option_is_refused_as_active_option(
    tmp_path: Path,
) -> None:
    page = tmp_path / "active.html"
    page.write_text(PAGE, encoding="utf-8")
    (tmp_path / "results.html").write_text(RESULTS, encoding="utf-8")
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-active-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9349, page.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        acquire_foreground(window.hwnd)
        client = UiaClient()
        model = WorldModel(client)
        _observe_until(model, window.hwnd, "the search box", _has_search)
        confirmer = YesConfirmer()
        session = Session(
            task_id="task-active",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=confirmer, own_pid=0, task_id="task-active"),
            ledger=Ledger(tmp_path, task_id="task-active", started_at=time.time()),
            live=UiaLiveReads(client, UiaBindings.load()),
            trace=ListTrace(),
        )
        focused = session.run_step(
            Step(id="s1", subgoal="g1", verb="focus", target=SEARCH, risk="R0")
        )
        assert focused.ok is True, focused.to_dict()
        typed = session.run_step(
            Step(id="s2", subgoal="g1", verb="type", target=SEARCH, text=TYPED, risk="R1")
        )
        assert typed.ok is True, typed.to_dict()

        def option_focused(snapshot: Snapshot) -> bool:
            rid = client.focused_runtime_id()
            return any(e.runtime_id == rid and e.role == "ListItem" for e in snapshot.elements)

        # the precondition, measured: Chrome reports the active option as the UIA focus
        _observe_until(model, window.hwnd, "the active option as the UIA focus", option_focused)

        submitted = session.run_step(
            Step(
                id="s3",
                subgoal="g1",
                verb="submit",
                target=SEARCH,
                risk="R2",
                params={"text": TYPED},
                expect=Appeared(role="Document", name_pattern=TYPED, replaces=True),
            )
        )
        assert submitted.ok is False, submitted.to_dict()
        assert submitted.code is ErrorCode.FOCUS_LOST, submitted.to_dict()
        assert submitted.evidence["detail"] == "active_option", submitted.to_dict()
        assert submitted.action is not None
        assert submitted.action.delivery is Delivery.NOT_DELIVERED
        assert submitted.action.steps == []
        assert len(confirmer.asked) == 1, "the refusal comes after the person's yes"

        after = model.observe(window.hwnd, cap_s=5.0)
        assert TITLE in after.title, "the page did not navigate"
        assert _status(after) == "ready", "no Enter reached the page"
    finally:
        stop_chrome(proc, profile_dir)
