"""The runner end to end on the Chrome inbox page through the real command line: a person at
the console answers the ask (the decision id is read from stderr), the task is done, a rerun
is done already, a read-only limit refuses the plan, a bare Enter pauses."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from steerai.orchestrator.plan_io import plan_from_json
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import _send, _user32, build_keyboard_input
from steerai.probes.windows import wait_window_for_pid
from steerai.run import EXIT_BLOCKED, EXIT_DONE, EXIT_PAUSED
from steerai.session.monitor import DEFAULT_KILL_KEY, parse_kill_key
from steerai.world.model import WorldModel
from tests.orchestrator.test_live import _observe_until_exposed

pytestmark = pytest.mark.windows
PAGE = Path(__file__).resolve().parents[2] / "steerai" / "probes" / "pages" / "inbox.html"
MESSAGE = "hello there from the runner"
ROOT = Path(__file__).resolve().parents[2]
_MODIFIER_VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}


def _press_kill_key() -> None:
    mods, vk = parse_kill_key(DEFAULT_KILL_KEY)
    order = [m for m in ("ctrl", "shift", "alt", "win") if m in mods]
    time.sleep(0.3)  # the runner is inside its ask: the console is waiting
    _send(
        _user32(),
        *[build_keyboard_input(vk=_MODIFIER_VK[m], keyup=False) for m in order],
        build_keyboard_input(vk=vk, keyup=False),
        build_keyboard_input(vk=vk, keyup=True),
        *[build_keyboard_input(vk=_MODIFIER_VK[m], keyup=True) for m in reversed(order)],
    )


def _plan_doc(content_role: str) -> dict[str, Any]:
    conversation = {"landmark": "Conversation"}
    return {
        "goal": "send a message in the inbox page",
        "subgoals": [
            {
                "id": "g1",
                "desc": "message sent",
                "condition": {
                    "appeared": {"role": content_role, "name": MESSAGE, "within": conversation}
                },
            }
        ],
        "steps": [
            {
                "id": "focus",
                "subgoal": "g1",
                "verb": "focus",
                "target": {"role": "Edit", "name": "Message"},
                "risk": "R0",
            },
            {
                "id": "type",
                "subgoal": "g1",
                "verb": "type",
                "target": {"role": "Edit", "name": "Message"},
                "text": MESSAGE,
                "risk": "R1",
            },
            {
                "id": "send",
                "subgoal": "g1",
                "verb": "send",
                "target": {"role": "Button", "name": "Send"},
                "risk": "R2",
                "params": {
                    "text": MESSAGE,
                    "composer_role": "Edit",
                    "composer_name": "Message",
                    "scope": "Conversation",
                    "content_role": content_role,
                },
            },
        ],
    }


def _run_cli(
    args: list[str],
    *,
    answer: str | None,
    on_ask: Callable[[], None] | None = None,
    which: str | None = None,
) -> tuple[int, dict[str, Any], str]:
    """Run the CLI; a thread answers the first ASK line on stderr with `answer` (the decision
    id substituted for `{id}`), or with a bare Enter when `answer` is None; `on_ask` runs first
    (the kill chord in the kill variant), in which case nothing is typed."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed module, the test's own args
        [sys.executable, "-m", "steerai.run", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
    )
    err_lines: list[str] = []

    def pump() -> None:
        assert proc.stderr is not None
        assert proc.stdin is not None
        for line in proc.stderr:
            err_lines.append(line)
            if line.startswith("ASK "):
                if on_ask is not None:
                    on_ask()
                    continue
                decision_id = line.split()[1]
                is_which = line.split()[2] == "which"
                if is_which:
                    reply = "" if which is None else which.replace("{id}", decision_id)
                else:
                    reply = "" if answer is None else answer.replace("{id}", decision_id)
                with contextlib.suppress(OSError, ValueError):
                    proc.stdin.write(reply + "\n")
                    proc.stdin.flush()

    out_lines: list[str] = []

    def collect() -> None:
        assert proc.stdout is not None
        out_lines.extend(proc.stdout)

    threads = [
        threading.Thread(target=pump, daemon=True),
        threading.Thread(target=collect, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        # stdin stays open until the runner exits: `communicate()` would close it before the ask
        proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    finally:
        assert proc.stdin is not None
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.close()
    for thread in threads:
        thread.join(timeout=10)
    lines = [line for line in out_lines if line.strip()]
    assert len(lines) == 1, out_lines
    return proc.returncode, json.loads(lines[0]), "".join(err_lines)


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
def test_the_runner_sends_once_resumes_as_done_and_honours_the_risk_limit(tmp_path: Path) -> None:
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-run-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9339, PAGE.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(proc.pid, title_contains="Google Chrome", image="chrome.exe")
        start = _observe_until_exposed(WorldModel(UiaClient()), window.hwnd)
        content_role = next(
            e.role for e in start.elements if e.name == "ok" and e.parent_id is not None
        )
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(_plan_doc(content_role)), encoding="utf-8")
        plan_from_json(plan_path.read_text(encoding="utf-8"))  # the file is a valid plan
        ledger = tmp_path / "ledger"
        trace = tmp_path / "trace.jsonl"
        base = [
            "--plan",
            str(plan_path),
            "--hwnd",
            str(window.hwnd),
            "--ledger-dir",
            str(ledger),
            "--trace",
            str(trace),
            "--task-id",
            "run-live-1",
        ]

        code, out, err = _run_cli(base, answer="yes:{id}")
        assert code == EXIT_DONE, (out, err)
        assert out["status"] == "done"
        assert out["ticked"] == ["g1"]
        by_id = {r["step_id"]: r for r in out["results"]}
        assert by_id["send"]["gate"] == "confirm"
        assert by_id["send"]["ok"] is True
        assert "ASK " in err
        assert MESSAGE in err, "the console shows the text the person approves"
        assert MESSAGE not in trace.read_text(encoding="utf-8")
        assert "run-live-1" in err

        time.sleep(0.5)
        code, out, err = _run_cli(base, answer="yes:{id}")
        assert code == EXIT_DONE, (out, err)
        assert "ASK " not in err, "a rerun of the same task never asks or sends again"
        assert {r["step_id"]: r["gate"] for r in out["results"]}.get("send") in (
            "done_already",
            None,
        )

        code, out, err = _run_cli(
            [*base, "--task-id", "run-live-ro", "--max-risk", "R1"], answer=None
        )
        assert code == EXIT_BLOCKED, (out, err)
        assert out["detail"] == "risk_over_limit"
        assert out["step_id"] == "send"
        assert out["results"] == []

        code, out, err = _run_cli([*base, "--task-id", "run-live-2"], answer=None)
        assert code == EXIT_PAUSED, (out, err)
        assert out["status"] == "paused"
        assert out["code"] == "gate_pending_user"
        assert "--task-id run-live-2" in err

        # the kill switch at the ask: injected with Steer's tag from this process; the runner's
        # hot key fires, the console returns killed, the gate blocks, nothing is sent
        code, out, err = _run_cli(
            [*base, "--task-id", "run-live-kill"], answer=None, on_ask=_press_kill_key
        )
        assert code == EXIT_BLOCKED, (out, err)
        assert out["status"] == "blocked"
        assert out["detail"] == "killed"
        assert {r["step_id"]: r["gate"] for r in out["results"]}["send"] == "block"
        assert "ASK " in err
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        shutil.rmtree(profile_dir, ignore_errors=True)


def _person_plan_doc() -> dict[str, Any]:
    """A person click: two rows name "Amir" on the inbox page (Chrome exposes the rows' links as
    Hyperlinks, PT-33), so the runner must ask which and click the chosen one."""
    return {
        "goal": "open the chat with Amir",
        "subgoals": [
            {
                "id": "g1",
                "desc": "chat opened",
                "condition": {"title": {"pattern": "Nothing - Chat"}},
            }
        ],
        "steps": [
            {
                "id": "open",
                "subgoal": "g1",
                "verb": "click",
                "target": {"role": "Hyperlink", "name": "Amir", "match": "person"},
                "risk": "R1",
                "expect": {"title": {"pattern": "Nothing - Chat"}},
            }
        ],
    }


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
def test_a_person_target_asks_which_row_and_clicks_the_chosen_one(tmp_path: Path) -> None:
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-person-"))
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9341, PAGE.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(proc.pid, title_contains="Google Chrome", image="chrome.exe")
        _observe_until_exposed(WorldModel(UiaClient()), window.hwnd)
        plan_path = tmp_path / "person.json"
        plan_path.write_text(json.dumps(_person_plan_doc()), encoding="utf-8")
        base = [
            "--plan",
            str(plan_path),
            "--hwnd",
            str(window.hwnd),
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--trace",
            str(tmp_path / "trace.jsonl"),
            "--task-id",
            "person-live-1",
        ]
        code, out, err = _run_cli(base, answer=None, which="2:{id}")
        step = {r["step_id"]: r for r in out["results"]}["open"]
        assert step["evidence"]["candidates"] == 2, (out, err)
        assert step["evidence"]["chosen_index"] == 2
        assert step["action"] is not None, "the chosen row was clicked"
        assert "ASK " in err
        assert "which" in err
        assert '"Amir Khan"' in err, "the console shows the candidate name parts"
        assert code in (EXIT_DONE, 4), "the click lands; the title expectation is a stand-in"
        trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
        assert "Amir" not in trace

        code, out, err = _run_cli(base, answer=None, which=None)
        assert code == EXIT_PAUSED, (out, err)
        assert out["detail"] == "unanswered"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        shutil.rmtree(profile_dir, ignore_errors=True)
