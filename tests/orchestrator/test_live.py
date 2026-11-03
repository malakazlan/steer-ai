"""End-to-end task on the Chrome inbox page through the real world model, executors, gate and
DPAPI ledger: type, confirm, send, verify; run again and resume as done already."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from steerai.action.execution import UiaBindings
from steerai.action.executor import Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.physical import PhysicalExecutor
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import Ask, Gate
from steerai.orchestrator.ledger import Ledger
from steerai.orchestrator.loop import Session
from steerai.orchestrator.trace import ListTrace
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.verify.predicates import Appeared, Within
from steerai.world.model import Snapshot, WorldModel

pytestmark = pytest.mark.windows
PAGE = Path(__file__).resolve().parents[2] / "steerai" / "probes" / "pages" / "inbox.html"
MESSAGE = "hello there from the orchestrator"


@dataclass
class YesConfirmer:
    asked: list[Ask] = field(default_factory=list)

    def ask(self, ask: Ask) -> str:
        self.asked.append(ask)
        return f"yes:{ask.decision_id}"


def _observe_until_exposed(model: WorldModel, hwnd: int, *, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        if any(e.role == "Edit" and e.name == "Message" for e in snapshot.elements):
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(f"composer never exposed; {snapshot.node_count} nodes")
        time.sleep(0.5)


def _plan(content_role: str) -> Plan:
    condition = Appeared(
        role=content_role, name_pattern=MESSAGE, within=Within(landmark="Conversation")
    )
    return Plan(
        goal="send a message in the inbox page",
        subgoals=(Subgoal(id="g1", desc="message sent", condition=condition),),
        steps=(
            Step(
                id="focus",
                subgoal="g1",
                verb="focus",
                target=Target(role="Edit", name="Message"),
                risk="R0",
            ),
            Step(
                id="type",
                subgoal="g1",
                verb="type",
                target=Target(role="Edit", name="Message"),
                text=MESSAGE,
                risk="R1",
            ),
            Step(
                id="send",
                subgoal="g1",
                verb="send",
                target=Target(role="Button", name="Send"),
                risk="R2",
                params={
                    "text": MESSAGE,
                    "composer_role": "Edit",
                    "composer_name": "Message",
                    "scope": "Conversation",
                    "content_role": content_role,
                },
            ),
        ),
    )


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
def test_a_send_task_runs_end_to_end_and_resumes_as_done(tmp_path: Path) -> None:
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-orch-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9338, PAGE.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(proc.pid, title_contains="Google Chrome")
        acquire_foreground(window.hwnd)
        client = UiaClient()
        bindings = UiaBindings.load()
        model = WorldModel(client)
        start = _observe_until_exposed(model, window.hwnd)
        content_role = next(
            e.role for e in start.elements if e.name == "ok" and e.parent_id is not None
        )
        confirmer = YesConfirmer()
        trace = ListTrace()
        ledger = Ledger(tmp_path, task_id="task-live-1", started_at=time.time())
        session = Session(
            task_id="task-live-1",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=confirmer, own_pid=0, task_id="task-live-1"),
            ledger=ledger,
            live=UiaLiveReads(client, bindings),
            trace=trace,
        )
        plan = _plan(content_role)
        started = time.perf_counter()
        result = session.run_task(plan)
        elapsed = time.perf_counter() - started
        by_id = {r.step_id: r for r in result.results}
        assert by_id["type"].ok is True, by_id["type"].to_dict()
        assert by_id["send"].ok is True, (by_id["send"].to_dict(), elapsed)
        assert by_id["send"].gate == "confirm"
        assert by_id["send"].label_kind == "t1"
        assert result.ticked == {"g1"}
        assert len(confirmer.asked) == 1
        assert MESSAGE not in "\n".join(trace.lines)

        again = session.run_step(plan.steps[2])
        assert again.gate == "done_already"
        assert again.ok is True

        resumed = Ledger(tmp_path, task_id="task-live-1", started_at=ledger.started_at)
        fresh = Session(
            task_id="task-live-1",
            hwnd=window.hwnd,
            observer=model,
            semantic=Executor(),
            physical=PhysicalExecutor(),
            gate=Gate(confirmer=confirmer, own_pid=0, task_id="task-live-1"),
            ledger=resumed,
            live=UiaLiveReads(client, bindings),
        )
        assert fresh.run_step(plan.steps[2]).gate == "done_already"
        assert len(confirmer.asked) == 1, "resume never asked and never sent again"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        shutil.rmtree(profile_dir, ignore_errors=True)
