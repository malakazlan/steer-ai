"""The gallery task set (packet interaction-model-2026-11-12 §5): one goal per actionable row,
each judged on the event the gallery page logs."""

from __future__ import annotations

import json

from steerai.probes.gallery_page import page_html
from steerai.probes.task_suite import GALLERY, h2_event, passed, task_set


def test_the_gallery_set_is_its_rows() -> None:
    assert task_set("gallery") == GALLERY
    assert [t.task_id for t in GALLERY][:3] == ["g01", "g02", "g03"]
    assert len({t.task_id for t in GALLERY}) == len(GALLERY)


def test_every_goal_expects_a_row_the_page_logs() -> None:
    html = page_html()
    for task in GALLERY:
        row = json.loads(task.expected)["row"]
        assert f'{{row: "{row}"' in html or f'"{row}"' in html, task.task_id


def test_a_row_passes_only_on_its_own_trusted_event() -> None:
    g03 = next(t for t in GALLERY if t.task_id == "g03")

    def judged(entries: list[dict[str, object]]) -> bool:
        return passed(g03, "done", h2_event(json.dumps(entries)))

    assert judged([{"row": "g03", "on": True}]) is True
    assert judged([{"row": "g03", "on": False}]) is False
    assert judged([{"row": "g02", "on": True}]) is False
    assert judged([{"row": "g03", "on": True, "untrusted": True}]) is False
