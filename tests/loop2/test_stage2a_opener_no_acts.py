"""#451: the 2a opener pass refuses a task with no acts row before it opens anything, as the 2b
pass does: a missing acts row loads no page (it failed closed before, but loaded the page)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2 import stage2a
from steerai.loop2.stage2_expected import OPENERS
from steerai.probes import memory_guard


def test_a_task_with_no_acts_row_opens_no_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    task = next(t for t in stage2a.local_tasks() if t.task_id in OPENERS)
    task_id = task.task_id
    opened: list[str] = []

    def opened_page(task: Any) -> Any:  # Any: a Task, then the context manager
        opened.append(task.task_id)
        raise AssertionError("a page was opened")

    monkeypatch.setattr(stage2a, "local_tasks", lambda: (task,))
    monkeypatch.setattr(stage2a, "load_acts", lambda _text: {})
    monkeypatch.setattr(stage2a, "_opened", opened_page)
    monkeypatch.setattr(memory_guard, "free_physical_mb", lambda: 10**6)
    assert stage2a.save_opened(tmp_path) == 0
    assert opened == []
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert rows == [{"task": task_id, "invalid": "no_acts"}]
    assert list(tmp_path.iterdir()) == []
