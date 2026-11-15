"""The first observation's shape in the redacted trace (3d shakedown, 2026-11-15: the first run saw
the page, later runs read blank even with the window in front). Closed tokens and counts only:
never a name, a title or page text."""

from __future__ import annotations

import json

import pytest

from steerai.loop2.first_facts import class_token, first_observation_record
from steerai.loop2.runner import run_task
from steerai.orchestrator.trace import ListTrace
from steerai.world.cover import CoverFacts
from tests.loop2.test_live_world import Platform, _world
from tests.loop2.world_fakes import DOC, FIELD, GO, PAGE, element, observed, snapshot, task

BOUND = 16


# ---- the record, pure ---------------------------------------------------------------------------


def test_a_page_reads_as_one_document_with_its_nodes() -> None:
    record = first_observation_record(
        observed(PAGE),
        foreground=True,
        cover=None,
        window_class="Chrome_WidgetWin_1",
        start_to_read_ms=12.6,
        reads=3,
    )
    assert record == {
        "event": "first_observation",
        "documents": "1",
        "doc_nodes": str(len([e for e in PAGE if e.parent_id == DOC.id])),
        "nodes": str(len(PAGE)),
        "stable": "yes",
        "foreground": "yes",
        "cover": "none",
        "window_class": "Chrome_WidgetWin_1",
        "start_to_first_read_ms": "13",
        "reads": "3",
    }


def test_a_frame_with_no_document_reads_as_none() -> None:
    frame = [e for e in PAGE if DOC.id not in (e.id, e.parent_id)]
    record = first_observation_record(
        observed(frame),
        foreground=False,
        cover=None,
        window_class="Chrome_WidgetWin_1",
        start_to_read_ms=None,
        reads=40,
    )
    assert (record["documents"], record["doc_nodes"], record["foreground"]) == ("0", "none", "no")
    assert record["start_to_first_read_ms"] == "none"


def test_an_empty_document_reads_as_zero_nodes_under_it() -> None:
    record = first_observation_record(
        observed([DOC]),
        foreground=None,
        cover=None,
        window_class="",
        start_to_read_ms=0.0,
        reads=1,
    )
    assert (record["doc_nodes"], record["foreground"], record["window_class"]) == (
        "0",
        "unread",
        "class_unread",
    )


def test_a_covered_start_has_no_read_and_names_the_cover() -> None:
    record = first_observation_record(
        None,
        foreground=True,
        cover="covered",
        window_class="Chrome_WidgetWin_1",
        start_to_read_ms=None,
        reads=0,
    )
    assert record["cover"] == "covered"
    assert (record["documents"], record["doc_nodes"], record["nodes"]) == ("none", "none", "none")


def test_an_unstable_read_says_so() -> None:
    record = first_observation_record(
        observed(PAGE).__class__(snapshot=snapshot(PAGE, stable=False)),
        foreground=True,
        cover=None,
        window_class="x",
        start_to_read_ms=1.0,
        reads=1,
    )
    assert record["stable"] == "no"


@pytest.mark.parametrize(
    ("name", "token"),
    [
        ("Chrome_WidgetWin_1", "Chrome_WidgetWin_1"),
        ("Notepad", "Notepad"),
        ("", "class_unread"),
        ("has space", "class_other"),
        ("x" * 65, "class_other"),
        ("café", "class_other"),
    ],
)
def test_the_class_is_a_closed_token(name: str, token: str) -> None:
    assert class_token(name) == token


def test_no_value_carries_page_text() -> None:
    named = element(70, "Text", "zqsentinelname", (9, 70), DOC.id)
    record = first_observation_record(
        observed([*PAGE, named]),
        foreground=True,
        cover=None,
        window_class="Chrome_WidgetWin_1",
        start_to_read_ms=1.0,
        reads=1,
    )
    assert "zqsentinelname" not in json.dumps(record)


# ---- the record in the live world's trace ----------------------------------------------------


def _first(trace: ListTrace) -> dict[str, str]:
    records = [json.loads(line) for line in trace.lines]
    found = [r for r in records if r.get("event") == "first_observation"]
    assert len(found) == 1, "one record, for the first observation only"
    return {str(k): str(v) for k, v in found[0].items()}


def test_the_first_observation_is_traced_once_with_its_shape() -> None:
    trace = ListTrace()
    platform = Platform()
    platform.window_class_name = "Chrome_WidgetWin_1"
    world, _ = _world(platform, trace=trace)
    world.bring_to_front()
    run_task(task(), world.ports(task()))
    world.close()
    record = _first(trace)
    assert record["documents"] == "1"
    assert record["nodes"] == str(len(PAGE))
    assert record["foreground"] == "yes"
    assert record["cover"] == "none"
    assert record["window_class"] == "Chrome_WidgetWin_1"
    assert int(record["reads"]) >= 1
    assert int(record["start_to_first_read_ms"]) >= 0


def test_a_covered_first_observation_is_traced_with_its_cover() -> None:
    trace = ListTrace()
    platform = Platform()
    world, _ = _world(platform, trace=trace)
    platform.cover = CoverFacts(
        minimized=True, cloaked=False, rect=(0, 0, 10, 10), above=(), screen=(0, 0, 100, 100)
    )
    run_task(task(), world.ports(task()))
    world.close()
    record = _first(trace)
    assert (record["cover"], record["reads"], record["documents"]) == ("minimized", "0", "none")


def test_the_trace_never_holds_a_name_from_the_page() -> None:
    trace = ListTrace()
    world, _ = _world(Platform(), trace=trace)
    run_task(task(), world.ports(task()))
    world.close()
    assert FIELD.name not in json.dumps(_first(trace))
    assert GO.name not in json.dumps(_first(trace))
