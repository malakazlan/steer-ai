"""The runner's own desktop actions before the session (TK-29): bringing the window to the
foreground is an ALT tap and `SetForegroundWindow` on the person's desktop, so it waits for
them to be idle like a dispatch does."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from tests.orchestrator.test_loop import _type
from tests.orchestrator.test_run import FakeSession, _done, _options, _plan
from tests.orchestrator.test_run_monitor import FakeMonitor, MonitorWorld, _run


@dataclass
class ActiveMonitor(FakeMonitor):
    """The person is at the keyboard for the first two reads, then idle."""

    reads: int = 0

    def last_user_input(self) -> float:
        self.reads += 1
        return time.monotonic() if self.reads <= 2 else 0.0


def test_the_foreground_is_acquired_only_after_the_person_is_idle(tmp_path: Path) -> None:
    plan = _plan(_type())
    monitor = ActiveMonitor()
    world = MonitorWorld(session=FakeSession(_done(plan)), monitor=monitor)
    code, _, _ = _run(_options(tmp_path, plan), world)
    assert code == 0
    assert monitor.reads >= 3, (
        "the runner never asked whether the person is idle before the ALT tap and "
        "SetForegroundWindow (TK-29)"
    )
    assert "foreground" in world.calls
