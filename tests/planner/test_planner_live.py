"""Opt-in: the real planner on the real Google page through the runner (`live` marker; needs an
Anthropic credential in the environment and Chrome). Records the planner's latency and tokens
for section 9 (unmeasured until this has run ten times)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from steerai.planner.claude_planner import ClaudePlanner
from steerai.planner.contracts import PlanRequest
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid
from steerai.world.model import WorldModel

pytestmark = [pytest.mark.live, pytest.mark.windows]
GOAL = "search Google for steer ai computer use agent"
_HAS_CREDENTIAL = bool(
    os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
)


@pytest.mark.skipif(not _HAS_CREDENTIAL, reason="no Anthropic credential in the environment")
@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
def test_the_planner_writes_a_valid_plan_for_the_google_page(tmp_path: Path) -> None:
    profile = Path(tempfile.mkdtemp(prefix="steer-plan-live-"))
    proc = launch_chrome(
        CHROME_DEFAULT, profile, 9361, "https://www.google.com/?hl=en", window_size=(1100, 760)
    )
    try:
        window = wait_window_for_pid(proc.pid, title_contains="Google Chrome")
        model = WorldModel(UiaClient())
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            snapshot = model.observe(window.hwnd, cap_s=5.0)
            if any(e.role == "ComboBox" and e.name == "Search" for e in snapshot.elements):
                break
            time.sleep(1.0)
        request = PlanRequest(goal=GOAL, screen_lines=snapshot.lines, window_title=snapshot.title)
        outcome = ClaudePlanner.from_env().plan(request)
        record = outcome.trace_record()
        (tmp_path / "planner-live.json").write_text(json.dumps(record), encoding="utf-8")
        sys.stderr.write(f"planner live: {json.dumps(record)}\n")
        verbs = [s.verb for s in outcome.plan.steps]
        assert "type" in verbs, verbs
        assert any(s.verb in ("submit", "click") for s in outcome.plan.steps), verbs
        typed = next(s for s in outcome.plan.steps if s.verb == "type")
        assert typed.text is not None
        assert "steer ai computer use agent" in typed.text
        assert typed.text_provenance == "verbatim"
        assert all(not s.preapproved_by_request for s in outcome.plan.steps)
        assert outcome.input_tokens is not None
        assert outcome.latency_ms > 0
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
