"""H6, the sixth held-out set (packet interaction-model-2026-11-12 §6): sealed before the
interaction model's verb admission merges, never run until the gallery is 3/3 per row."""

from __future__ import annotations

import json

from steerai.probes.h6_page import INVOICES, LOG_KEY, SORTS, page_html
from steerai.probes.task_suite import (
    H2,
    H3,
    H4,
    H5,
    H6,
    H6_DRIVES,
    HELD_OUT,
    TASKS,
    h2_event,
    passed,
    task_set,
)

EARLIER = (*TASKS, *HELD_OUT, *H2, *H3, *H4, *H5)


def test_h6_is_new_tasks_only() -> None:
    assert task_set("h6") == H6
    assert len(H6) == 10
    ids = {t.task_id for t in EARLIER}
    goals = {t.goal for t in EARLIER}
    starts = {t.start for t in EARLIER if t.start.startswith("http")}
    for task in H6:
        assert task.task_id not in ids
        assert task.goal not in goals
        if task.start.startswith("http"):
            assert task.start not in starts, task.task_id


def test_every_page_task_has_a_control_on_an_element_the_page_has() -> None:
    html = page_html()
    for task in H6:
        if task.start != "h6":
            continue
        drive = H6_DRIVES[task.task_id]
        assert drive, task.task_id
        for _kind, selector in drive:
            assert f'id="{selector[1:].split(" ")[0]}"' in html, (task.task_id, selector)


def test_each_page_task_expects_one_event_kind_the_page_logs() -> None:
    html = page_html()
    for task in H6:
        if task.check == "h6_event":
            assert f'kind: "{json.loads(task.expected)["kind"]}"' in html, task.task_id


def test_the_shapes_are_the_packet_idioms_and_none_of_the_gallerys_markup() -> None:
    html = page_html()
    assert 'role="toolbar"' in html
    assert 'aria-pressed="false">Mute notifications' in html
    assert 'type="checkbox" role="switch"' in html, "a switch on an input"
    assert 'role="tree"' in html
    assert 'role="treeitem" id="reports" aria-expanded="false"' in html
    assert html.count('<div role="row"') == len(INVOICES), "a div grid"
    assert html.count('role="menuitemradio"') == len(SORTS)
    assert "<table" not in html, "the gallery's grid is a table"
    assert LOG_KEY == "steer-h6"


def test_the_goals_name_their_controls_for_the_harness_rule() -> None:
    """H5 r5: a goal that never names its control makes the scripted person decline."""
    names = {
        "u1": "Mute notifications",
        "u2": "Auto-save",
        "u3": "2024",
        "u4": "Invoice 2042",
        "u5": "Newest first",
    }
    for task in H6:
        if task.task_id in names:
            assert names[task.task_id].casefold() in task.goal.casefold(), task.task_id


def test_a_page_task_passes_only_on_its_own_event() -> None:
    u4 = next(t for t in H6 if t.task_id == "u4")

    def judged(entries: list[dict[str, object]]) -> bool:
        return passed(u4, "done", h2_event(json.dumps(entries)))

    assert judged([{"kind": "invoice", "value": "Invoice 2042"}]) is True
    assert judged([{"kind": "invoice", "value": "Invoice 2041"}]) is False
    assert judged([{"kind": "invoice", "value": "Invoice 2042", "untrusted": True}]) is False


def test_the_sites_are_judged_on_their_measured_pages() -> None:
    by_id = {t.task_id: t for t in H6}
    page = json.dumps(
        {
            "href": "https://pkg.go.dev/search?q=cobra",
            "title": "cobra - Search Results - Go Packages",
        }
    )
    assert passed(by_id["gd1"], "done", page) is True
    same = json.dumps({"href": "https://cocoapods.org/?q=alamofire", "title": "CocoaPods.org"})
    assert passed(by_id["cp1"], "done", same) is True
    entry = json.dumps(
        {
            "href": "https://en.wiktionary.org/wiki/serendipity",
            "title": "serendipity - Wiktionary, the free dictionary",
        }
    )
    assert passed(by_id["wt1"], "done", entry) is True
