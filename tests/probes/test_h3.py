"""H3, the third held-out set (design 12 [v2.53]): new tasks only, every page task's control
drives an element the page has, and every check is well formed."""

from __future__ import annotations

import json
import re

from steerai.probes.h3_page import DESTINATIONS, LOG_KEY, page_html
from steerai.probes.task_suite import H2, H3, H3_DRIVES, HELD_OUT, TASKS, task_set


def test_h3_is_new_tasks_only() -> None:
    ids = [task.task_id for task in H3]
    assert len(ids) == len(set(ids))
    earlier = {task.task_id for task in (*TASKS, *HELD_OUT, *H2)}
    assert not earlier & set(ids)
    goals = {task.goal for task in (*TASKS, *HELD_OUT, *H2)}
    assert not goals & {task.goal for task in H3}
    assert task_set("h3") == H3


def test_every_page_task_has_a_control_on_an_element_the_page_has() -> None:
    html = page_html()
    page_ids = set(re.findall(r'id="([^"]+)"', html))
    page_tasks = [task for task in H3 if task.start == "h3"]
    assert {task.task_id for task in page_tasks} == set(H3_DRIVES)
    for task_id, drive in H3_DRIVES.items():
        for kind, arg in drive:
            if kind in ("click", "focus"):
                assert arg.startswith("#"), (task_id, arg)
                assert arg[1:] in page_ids, (task_id, arg)


def test_each_page_task_expects_one_event_kind_the_page_logs() -> None:
    html = page_html()
    for task in H3:
        if task.check != "h3_event":
            continue
        event = json.loads(task.expected)
        assert f'kind: "{event["kind"]}"' in html, task.task_id
    assert LOG_KEY in html
    assert "Kyoto" in DESTINATIONS
    assert DESTINATIONS.index("Kyoto") > 10, "below the listbox's visible rows"


def test_the_search_tasks_and_calculator_are_well_formed() -> None:
    searches = [task for task in H3 if task.check == "url_search"]
    assert {task.task_id for task in searches} == {"npm1", "cr1", "pypi1"}
    for task in searches:
        assert task.start.startswith("https://")
        assert task.path.startswith("/")
    calc = next(task for task in H3 if task.task_id == "calc2")
    assert json.loads(calc.expected)["result"] == "5"
