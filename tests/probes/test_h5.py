"""H5, the fifth held-out set (packet h5-2026-11-11): committed before the fixes it certifies
(H4 classes B and C), never run until they merge. Five fixture shapes new to every earlier page,
each with a trusted-input control, and five sites new to every earlier set."""

from __future__ import annotations

import json

from steerai.probes.h5_page import EXPORTS, LOG_KEY, SPEEDS, ZONES, page_html
from steerai.probes.task_suite import (
    H2,
    H3,
    H4,
    H5,
    H5_DRIVES,
    HELD_OUT,
    TASKS,
    h2_event,
    passed,
    task_set,
)

EARLIER = (*TASKS, *HELD_OUT, *H2, *H3, *H4)


def test_h5_is_new_tasks_only() -> None:
    assert task_set("h5") == H5
    earlier_ids = {t.task_id for t in EARLIER}
    earlier_goals = {t.goal for t in EARLIER}
    earlier_starts = {t.start for t in EARLIER if t.start.startswith("http")}
    for task in H5:
        assert task.task_id not in earlier_ids
        assert task.goal not in earlier_goals
        if task.start.startswith("http"):
            assert task.start not in earlier_starts, task.task_id


def test_every_page_task_has_a_control_on_an_element_the_page_has() -> None:
    html = page_html()
    for task in H5:
        if task.start != "h5":
            continue
        drive = H5_DRIVES[task.task_id]
        assert drive, task.task_id
        for _kind, arg in drive:
            if arg.startswith("#"):
                assert f'id="{arg[1:]}"' in html, (task.task_id, arg)


def test_each_page_task_expects_one_event_kind_the_page_logs() -> None:
    html = page_html()
    for task in H5:
        if task.check != "h5_event":
            continue
        event = json.loads(task.expected)
        assert f'kind: "{event["kind"]}"' in html, task.task_id


def test_the_shapes_use_the_packet_idioms() -> None:
    html = page_html()
    assert "<fieldset><legend>Shipping speed</legend>" in html, "r1 radios in a fieldset"
    assert html.count('type="radio" name="speed"') == len(SPEEDS)
    assert 'role="switch"' in html, "r2 is an ARIA switch, not a CheckBox"
    assert '<optgroup label="Europe">' in html, "r3 groups its options"
    assert "Europe/Berlin" in html
    assert 'aria-haspopup="menu"' in html, "r4 is a menu button"
    assert html.count('<li role="menuitem"') == len(EXPORTS)
    assert 'id="newsletter" checked' in html, "r5 starts checked: the uncheck leg"
    assert "<details" not in html, "no H4 disclosure"
    assert 'role="tab"' not in html, "no H4 tabs"
    assert 'role="listbox"' not in html, "no H3 listbox"
    assert "size=" not in html, "no H4 list select"


def test_the_grouped_zones_cover_three_continents() -> None:
    assert set(ZONES) == {"America", "Asia", "Europe"}
    assert all(len(v) == 3 for v in ZONES.values())


def test_r3_and_r5_are_judged_on_the_final_state() -> None:
    by_id = {t.task_id: t for t in H5}
    r3 = by_id["r3"]
    assert r3.check == "h5_final"
    state = json.loads(r3.expected)
    assert state["state"]["zone"] == "Europe/Berlin"
    assert state["untrusted"] is False
    r5 = by_id["r5"]
    assert json.loads(r5.expected) == {"kind": "newsletter", "on": False}


def test_the_log_key_is_h5s_own() -> None:
    assert LOG_KEY == "steer-h5"
    assert f'"{LOG_KEY}"' in page_html()


def test_a_page_task_passes_only_on_its_own_event() -> None:
    by_id = {t.task_id: t for t in H5}
    r1 = by_id["r1"]

    def judged(entries: list[dict[str, object]]) -> bool:
        return passed(r1, "done", h2_event(json.dumps(entries)))

    assert judged([{"kind": "speed", "value": "Express"}]) is True
    assert judged([{"kind": "speed", "value": "Standard"}]) is False
    assert judged([{"kind": "dark", "on": True}]) is False
    assert judged([{"kind": "speed", "value": "Express", "untrusted": True}]) is False
