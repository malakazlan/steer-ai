"""H3, the third held-out set (design 12 [v2.53]): new tasks only, every page task's control
drives an element the page has, and every check is well formed."""

from __future__ import annotations

import json
import re

from steerai.probes.h3_page import DESTINATIONS, LOG_KEY, page_html
from steerai.probes.task_suite import H2, H3, H3_DRIVES, HELD_OUT, TASKS, passed, task_set


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


def test_the_search_tasks_check_their_measured_titles() -> None:
    searches = [task for task in H3 if task.check == "url_search"]
    assert {task.task_id for task in searches} == {"npm1", "cr1", "pypi1"}
    for task in searches:
        assert task.start.startswith("https://")
        assert task.path.startswith("/")
        assert task.title, task.task_id


def test_a_results_page_with_another_title_fails() -> None:
    pypi = next(task for task in H3 if task.task_id == "pypi1")
    good = json.dumps({"href": "https://pypi.org/search/?q=requests", "title": pypi.title})
    challenge = json.dumps({"href": "https://pypi.org/search/?q=requests", "title": "Search"})
    assert passed(pypi, "done", good)
    assert not passed(pypi, "done", challenge)


def test_n2_is_judged_on_the_final_state() -> None:
    n2 = next(task for task in H3 if task.task_id == "n2")
    assert n2.check == "h3_final"
    state = {"Follows": True, "Mentions": True, "Replies": True}
    done = json.dumps({"kinds": ["notifications"], "state": state, "untrusted": False})
    stray = json.dumps({"kinds": ["billing", "notifications"], "state": state, "untrusted": False})
    assert passed(n2, "done", done)
    assert not passed(n2, "done", stray)


def test_the_near_copies_are_out() -> None:
    assert not [task for task in H3 if task.start == "calculator"]
    assert 'role="tab"' not in page_html()
