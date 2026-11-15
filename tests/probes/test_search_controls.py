"""Stage 2 plan §2.4: a positive control for every real-site task the controls can reach. The
Google and YouTube search checks had none: their control URL is built from the task's own data
(the check's results path and the query key `query_of` reads), and must pass that task's own
check. A task whose check names no fixed page (`youtube_watch`: "the first video" changes) has
no control and says so. Offline: no Chrome here; `controls()` itself opens real sites."""

from __future__ import annotations

import json

import pytest

from steerai.probes.task_suite import (
    CONTROLLED,
    HELD_OUT,
    TASKS,
    Task,
    control_url,
    passed,
    task_set,
)

ALL = (*TASKS, *HELD_OUT, *(t for name in ("h3", "h4", "h5", "h6") for t in task_set(name)))


def _task(task_id: str) -> Task:
    return next(t for t in ALL if t.task_id == task_id)


def _page(href: str, title: str = "") -> str:
    return json.dumps({"href": href, "title": title, "links": []})


@pytest.mark.parametrize(
    ("task_id", "url"),
    [
        ("g1", "https://www.google.com/search?q=steer+ai+computer+use+agent"),
        ("g2", "https://www.google.com/search?q=windows+ui+automation"),
        ("yt1", "https://www.youtube.com/results?search_query=lofi+hip+hop"),
    ],
)
def test_a_search_task_s_control_is_its_own_results_page(task_id: str, url: str) -> None:
    task = _task(task_id)
    assert control_url(task) == url
    assert passed(task, "done", _page(url)), "a control must pass its own task's check"


def test_a_watch_task_has_no_fixed_page_to_open() -> None:
    assert control_url(_task("yt2")) is None


def test_the_existing_controls_are_unchanged() -> None:
    assert control_url(_task("py1")) == "https://docs.python.org/3/search.html?q=pathlib"
    assert control_url(_task("wk1")) == "https://en.wikipedia.org/wiki/Alan_Turing"


def test_a_local_task_has_no_control() -> None:
    assert control_url(_task("np1")) is None


def test_every_real_site_task_either_has_a_control_or_its_check_is_named_uncontrolled() -> None:
    real = [t for t in ALL if t.start.startswith("http")]
    assert {t.task_id for t in real if control_url(t) is None} == {"yt2"}
    assert all(t.check in CONTROLLED for t in real if control_url(t) is not None)


def test_every_control_without_a_title_or_result_link_passes_its_own_check() -> None:
    """Offline: the title a `url_search` check may also need is the site's, read live."""
    for task in ALL:
        url = control_url(task)
        if url is not None and not task.title and not task.result_link:
            assert passed(task, "done", _page(url)), task.task_id


def test_controls_say_a_real_site_task_has_none_and_open_nothing_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator's ruling (c): yt2 has no control and the line says so; a local task is
    skipped as before. No Chrome starts for either."""
    from steerai.probes import task_suite  # noqa: PLC0415 - the module's own launcher

    def no_chrome(*_a: object, **_k: object) -> object:
        raise AssertionError("no control page for these tasks")

    monkeypatch.setattr(task_suite, "_launch_one", no_chrome)
    assert task_suite.controls([_task("yt2"), _task("np1")]) == [
        "yt2: control unavailable (youtube_watch)"
    ]


@pytest.mark.probe
@pytest.mark.windows
def test_the_search_controls_pass_on_the_live_sites() -> None:
    """Opens real sites: runs only on `-m probe`, and only after the lead's spacing ruling (stage
    2 plan §2.3). A site that does not load is an invalid control, never a pass."""
    from steerai.probes.task_suite import controls  # noqa: PLC0415 - live

    lines = controls([_task("g1"), _task("g2"), _task("yt1")])
    assert len(lines) == 3
    assert not any("FAILS" in line for line in lines), lines
