"""The Phase 1 suite's pure parts: ten tasks, the person who approves only each task's exact
phrases, the exact independent checks, the inbox send rule and the bar (#139 review)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from steerai.probes.task_suite import (
    TASKS,
    Person,
    inbox_send,
    passed,
    query_of,
    seconds_line,
    verdict,
)


def _task(task_id: str) -> Any:
    return next(t for t in TASKS if t.task_id == task_id)


def _page(href: str, title: str = "") -> str:
    return json.dumps({"href": href, "title": title})


def _ask(text: str | None, kind: str = "yes") -> Any:
    return SimpleNamespace(
        decision_id="d1", params={"text": text}, verb="send", risk="R2", kind=kind
    )


def test_ten_distinct_tasks_with_exact_approvals() -> None:
    assert len(TASKS) == 10
    assert len({t.task_id for t in TASKS}) == 10
    asking = {"g1", "g2", "yt1", "yt2", "wk1", "wk2", "ib1"}
    assert {t.task_id for t in TASKS if t.approve} == asking


def test_the_person_approves_only_the_exact_phrases() -> None:
    reply = Person(_task("ib1").approve)
    assert reply.ask(_ask("ok thanks")) == "yes:d1"
    assert reply.ask(_ask("OK  thanks")) == "yes:d1", "case and spacing folded"
    assert reply.ask(_ask("ok")) == "no:d1", "a substring of the goal is not its phrase"
    search = Person(_task("g2").approve)
    assert search.ask(_ask("windows ui automation")) == "yes:d1"
    assert search.ask(_ask("for")) == "no:d1"
    assert search.ask(_ask(None)) == "no:d1"
    assert search.ask(_ask("windows ui automation", kind="which")) == "no:d1"
    assert search.ask_go("go:1") == "go"


def _named_ask(name: str, text: str | None = None) -> Any:
    return SimpleNamespace(
        decision_id="d1",
        params={"text": text},
        verb="click",
        risk="R2",
        kind="yes",
        target_name=name,
    )


def test_a_text_less_ask_is_approved_when_its_name_is_in_the_goal() -> None:
    """The scripted person's goal rule (#200): a button or box named verbatim in the goal is
    what the goal asked for; a text ask still needs the task's exact phrase."""
    person = Person(frozenset(), goal="switch to the annual billing period")
    assert person.ask(_named_ask("Annual")) == "yes:d1"
    assert person.ask(_named_ask("annual  billing")) == "yes:d1", "case and spacing folded"
    assert person.ask(_named_ask("Delete")) == "no:d1"
    assert person.ask(_named_ask("")) == "no:d1", "an unnamed target is never in the goal"
    assert person.ask(_named_ask("Annual", text="annual")) == "no:d1", "text asks need text"
    assert person.ask(_named_ask("Annual", text="billing")) == "no:d1"
    assert person.log[-1]["approved"] is False
    listed = Person(frozenset(), frozenset({"Delete"}), goal="switch to the annual billing period")
    assert listed.ask(_named_ask("Delete")) == "yes:d1", "approve_targets still apply"
    unknown = Person(frozenset())
    assert unknown.ask(_named_ask("Annual")) == "no:d1", "no goal, no goal rule"


def test_query_of_reads_google_and_youtube() -> None:
    assert query_of("https://www.google.com/search?q=windows+ui+automation&hl=en") == (
        "windows ui automation"
    )
    assert query_of("https://www.youtube.com/results?search_query=lofi+hip+hop") == "lofi hip hop"
    assert query_of("https://www.youtube.com/") == ""


def test_a_search_passes_only_on_its_results_path_with_its_words() -> None:
    g2 = _task("g2")
    assert passed(g2, "done", _page("https://www.google.com/search?q=Windows+UI+Automation"))
    assert not passed(g2, "done", _page("https://www.google.com/?q=windows+ui+automation"))
    assert not passed(g2, "stopped", _page("https://www.google.com/search?q=windows+ui+automation"))
    yt1 = _task("yt1")
    assert passed(yt1, "done", _page("https://www.youtube.com/results?search_query=lofi+hip+hop"))
    assert not passed(yt1, "done", _page("https://www.youtube.com/watch?search_query=lofi+hip+hop"))


def test_playing_the_first_video_needs_a_watch_page_naming_the_artist() -> None:
    yt2 = _task("yt2")
    assert passed(yt2, "done", _page("https://www.youtube.com/watch?v=a", "Drake - Janice STFU"))
    assert not passed(yt2, "done", _page("https://www.youtube.com/watch?v=a", "Lofi radio"))
    assert not passed(yt2, "done", _page("https://www.youtube.com/results?x=1", "drake"))


def test_an_article_passes_only_on_its_exact_path() -> None:
    wk1 = _task("wk1")
    assert passed(wk1, "done", _page("https://en.wikipedia.org/wiki/Alan_Turing"))
    assert not passed(wk1, "done", _page("https://en.wikipedia.org/wiki/Alan_Turing_Institute"))


def test_notepad_needs_the_exact_text() -> None:
    assert passed(_task("np1"), "done", "hello from steer")
    assert not passed(_task("np1"), "done", "hello from stee")


def test_the_inbox_send_is_exactly_one_trusted_expected_send() -> None:
    right = {"chat": "chat-amir-khan", "trusted": True, "expected": True}
    stray = {"chat": "chat-amir-khan", "trusted": True, "expected": False}
    assert inbox_send(json.dumps([right])) == "chat-amir-khan"
    assert inbox_send(json.dumps([stray, right])) == "sends:2", "a stray send fails the run"
    assert inbox_send(json.dumps([stray])) == "bad_send"
    assert inbox_send("") == "sends:0"
    with pytest.raises(ValueError, match="not a list"):
        inbox_send("{}")
    assert passed(_task("ib1"), "done", "chat-amir-khan")
    assert not passed(_task("ib1"), "done", "sends:2")


def test_the_bar_needs_every_task_and_every_run() -> None:
    rows: list[dict[str, Any]] = [{"kind": "header", "git": "abc"}]
    rows += [{"task": t.task_id, "passed": True, "seconds": 10.0} for t in TASKS]
    lines, ok = verdict(rows)
    assert ok
    assert lines[-1] == "all: 10/10"
    rows[4] = {"task": rows[4]["task"], "passed": False, "invalid": "page_targets:2"}
    lines, ok = verdict(rows)
    assert not ok
    assert "(1 invalid)" in lines[3]
    assert not verdict(rows[:9])[1]
    assert not verdict([])[1]


def test_the_seconds_line_reports_each_task() -> None:
    rows = [{"task": "np1", "seconds": s} for s in (7.0, 8.0, 12.0)]
    assert seconds_line(rows) == "seconds p50/p95: np1 8.0/12.0"


def test_a_person_who_stays_active_gives_an_invalid_row_and_the_suite_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import steerai.probes.task_suite as suite  # noqa: PLC0415 - this test only
    from steerai.probes.layer_b_pages import InvalidRun  # noqa: PLC0415

    def active() -> None:
        raise InvalidRun("person_active")

    monkeypatch.setattr(suite, "wait_until_idle", active)
    row = suite.one_run(TASKS[0], 1, suite._Planner())
    assert row == {"task": "np1", "run": 1, "passed": False, "invalid": "person_active"}


def test_the_goal_rule_matches_whole_tokens_of_at_least_three_characters() -> None:
    """H4 review (#204): a plain substring approved Button "Go" under any Google goal and
    "Post" under a Post Malone goal. The target's folded tokens must appear as a contiguous
    token sequence in the goal, and the target must be at least three characters."""
    google = Person(frozenset(), goal="search Google for steer ai computer use agent")
    assert google.ask(_named_ask("Go")) == "no:d1", "a substring of 'Google', not a token"
    assert google.ask(_named_ask("ai")) == "no:d1", "a token, but under three characters"
    assert google.ask(_named_ask("Google")) == "yes:d1"
    assert google.ask(_named_ask("computer use")) == "yes:d1", "a contiguous token run"
    assert google.ask(_named_ask("use computer")) == "no:d1", "the tokens out of order"
    malone = Person(frozenset(), goal="play the latest Post Malone video")
    assert malone.ask(_named_ask("Post")) == "yes:d1", "a whole token of the goal"
    assert malone.ask(_named_ask("Pos")) == "no:d1", "a prefix of a token"
    signup = Person(frozenset(), goal="tick the I agree to the terms box on the sign-up form")
    assert signup.ask(_named_ask("I agree to the terms")) == "yes:d1"
    assert signup.ask(_named_ask("sign-up")) == "yes:d1", "punctuation inside a token is kept"


def test_a_site_run_waits_for_the_loaded_document_before_steer_looks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev set on feat/planner-submit-button (2026-11-11): two YouTube runs planned on a
    listing ~700 input tokens short of a rendered page and opened chrome instead of typing.
    A site run settles the document (readyState complete, as the controls and the H sets do)
    before Steer's first observe; a site that never loads is an invalid run, not a FAIL."""
    import time  # noqa: PLC0415 - this test only

    import steerai.probes.chrome_a11y as chrome  # noqa: PLC0415
    import steerai.probes.task_suite as suite  # noqa: PLC0415

    order: list[str] = []

    def noting(mark: str, result: Any = None) -> Any:  # Any: the stubs' own returns
        def stub(*_args: Any, **_kwargs: Any) -> Any:  # Any: each stub's real signature
            order.append(mark)
            return result

        return stub

    monkeypatch.setattr(suite, "_free_port", lambda: 9)
    monkeypatch.setattr(suite, "launch_chrome", noting("launch", SimpleNamespace(pid=1)))
    monkeypatch.setattr(suite, "wait_window_for_pid", noting("window", SimpleNamespace(hwnd=7)))
    monkeypatch.setattr(suite, "page_ws_url", noting("target"))
    monkeypatch.setattr(suite, "_settled", noting("settled"))
    monkeypatch.setattr(time, "sleep", noting("sleep"))
    monkeypatch.setattr(suite, "_in_front", noting("front"))
    monkeypatch.setattr(suite, "_steer", noting("steer", {"status": "done"}))
    monkeypatch.setattr(suite, "_read_page", noting("read", "{}"))
    monkeypatch.setattr(chrome, "stop_chrome", noting("stop"))
    suite._run_chrome(TASKS[0], 1, Path("unused"), suite._Planner(), "https://example.test/")
    assert order == [
        "launch",
        "window",
        "target",
        "settled",
        "sleep",
        "front",
        "steer",
        "read",
        "stop",
    ]


def test_the_planner_wrapper_records_a_refused_plan_s_boundary_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dev wk2 (2026-11-11): `invalid_plan` at `$.steps[0].expect` twice, and the trace keeps
    the path only (the reason may quote the model's text, so Steer never traces it). The suite
    is the probe process: its wrapper keeps the boundary's path and reason from the error's
    cause for the row, and the error still propagates."""
    import steerai.probes.task_suite as suite  # noqa: PLC0415 - this test only
    import steerai.run as run_mod  # noqa: PLC0415
    from steerai.errors import ErrorCode  # noqa: PLC0415
    from steerai.orchestrator.plan_io import PlanError  # noqa: PLC0415
    from steerai.planner.contracts import PlannerError, PlanOutcome, PlanRequest  # noqa: PLC0415

    def refusing(request: PlanRequest) -> PlanOutcome:
        try:
            raise PlanError("$.steps[0].expect", "a click names its effect in expect")
        except PlanError as exc:
            raise PlannerError(
                ErrorCode.MODEL_ERROR, "invalid_plan", path=exc.path, attempt=2
            ) from exc

    monkeypatch.setattr(run_mod, "_planner", refusing)
    planner = suite._Planner()
    with pytest.raises(PlannerError):
        planner(PlanRequest(goal="g", screen_lines=()))
    assert planner.calls == 1
    assert planner.refusals == [(2, "$.steps[0].expect", "a click names its effect in expect")]


def test_the_planner_wrapper_keeps_the_screen_an_empty_plan_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#201: an empty plan is resampled once and both came back empty on g05 (2026-11-12), so
    the cause is what the model saw. The suite keeps that screen (a fixture or public page,
    never the person's) for `--keep`; the row carries its line count only."""
    import steerai.probes.task_suite as suite  # noqa: PLC0415 - this test only
    import steerai.run as run_mod  # noqa: PLC0415
    from steerai.errors import ErrorCode  # noqa: PLC0415
    from steerai.planner.contracts import PlannerError, PlanOutcome, PlanRequest  # noqa: PLC0415

    def empty(request: PlanRequest) -> PlanOutcome:
        raise PlannerError(ErrorCode.MODEL_ERROR, "no_steps", attempt=2)

    monkeypatch.setattr(run_mod, "_planner", empty)
    planner = suite._Planner()
    lines = ('1  CheckBox "Remember this device"   [0,0,10,10]',)
    with pytest.raises(PlannerError):
        planner(PlanRequest(goal="g", screen_lines=lines, window_title="Gallery"))
    assert planner.empty == [("Gallery", list(lines))]
    assert planner.refusals == []


def test_a_kept_run_writes_the_empty_plans_screen_and_the_row_keeps_no_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import steerai.probes.task_suite as suite  # noqa: PLC0415 - this test only

    def started(task: Any, number: int, tmp: Path, planner: Any) -> dict[str, Any]:
        planner.empty.append(("Gallery", ['1  CheckBox "Remember this device"']))
        return {
            "status": "error",
            "detail": "no_steps",
            "observed": "",
            "opens_recorded": 0,
            "empty_screens": suite.empty_lines(planner, 1),
        }

    monkeypatch.setattr(suite, "wait_until_idle", lambda: None)
    monkeypatch.setattr(suite, "_started", started)
    planner = suite._Planner()
    planner.empty.append(("Earlier", ["1  an earlier run's screen"]))
    row = suite.one_run(TASKS[0], 1, planner, keep=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["np1-1-empty-1.txt"]
    kept = (tmp_path / "np1-1-empty-1.txt").read_bytes()
    assert kept == b'Gallery\n1  CheckBox "Remember this device"\n', "LF, on every OS"
    assert row["empty_screens"] == [1]
    assert "Remember" not in json.dumps(row)


def test_a_shared_planner_keeps_only_this_runs_empty_screens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The suite shares one `_Planner` across runs: an earlier run's screen is never this
    run's file nor its count (principal on #240)."""
    import steerai.probes.task_suite as suite  # noqa: PLC0415 - this test only
    import steerai.run as run_mod  # noqa: PLC0415
    from steerai.errors import ErrorCode  # noqa: PLC0415
    from steerai.planner.contracts import PlannerError, PlanOutcome, PlanRequest  # noqa: PLC0415

    def empty(request: PlanRequest) -> PlanOutcome:
        raise PlannerError(ErrorCode.MODEL_ERROR, "no_steps", attempt=2)

    def run(_options: Any, deps: Any, **_kw: Any) -> int:
        with pytest.raises(PlannerError):
            deps.planner(PlanRequest(goal="g", screen_lines=("1  now",), window_title="Now"))
        return 1

    monkeypatch.setattr(run_mod, "_planner", empty)
    monkeypatch.setattr(run_mod, "run", run)

    class Deps:
        """Only what `_steer` replaces and the fake run reads."""

        def replace(self, **changes: Any) -> SimpleNamespace:
            return SimpleNamespace(**changes)

    monkeypatch.setattr(run_mod, "_real_deps", Deps)
    planner = suite._Planner()
    planner.empty.append(("Earlier", ["1  a", "2  b"]))
    row = suite._steer(TASKS[0], 2, 0x10, tmp_path, planner)
    assert row["empty_screens"] == [1]


def test_an_anubis_bot_check_is_an_invalid_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """H4 aw1 (2026-11-11): the Arch wiki's search path lands on Anubis's "Making sure you're
    not a bot!"; a run that ends there measured nothing (as PyPI's and Cloudflare's)."""
    import steerai.probes.task_suite as suite  # noqa: PLC0415 - this test only
    from steerai.probes.layer_b_pages import InvalidRun  # noqa: PLC0415

    def challenge(*_args: Any) -> str:
        return (
            '{"href": "https://wiki.archlinux.org/index.php?search=pacman", '
            '"title": "Making sure you\'re not a bot!"}'
        )

    monkeypatch.setattr(suite, "_read", challenge)
    with pytest.raises(InvalidRun, match="bot_check"):
        suite._read_page(9, "page")


def test_the_scripted_person_answers_a_which_ask_by_the_tasks_automation_id() -> None:
    """Packet h4-2026-11-11 round 2 item 5: the harness oracle picks the candidate line whose
    `automation_id` the task names; without one, or with no such line, `no`."""
    from steerai.orchestrator.identity import CandidateLine  # noqa: PLC0415 - this test only

    lines = (
        CandidateLine(1, "Edit", "Find crate by search query", "", automation_id="nav-search"),
        CandidateLine(2, "Edit", "Find crate by search query", "", automation_id="search"),
    )
    ask = SimpleNamespace(
        decision_id="w-1",
        verb="focus",
        risk="R0",
        params={},
        kind="which",
        target_name="Find crate by search query",
        candidates=lines,
    )
    person = Person(frozenset(), which={"Find crate by search query": "search"})
    assert person.ask(ask) == "2:w-1"
    assert person.log[-1]["approved"] is True
    assert Person(frozenset()).ask(ask) == "no:w-1"
    assert Person(frozenset(), which={"Find crate by search query": "footer"}).ask(ask) == "no:w-1"


def test_keep_creates_its_folder_before_the_first_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#249 item 5: `--keep` into a folder that did not exist crashed after the run in
    `shutil.copy`, and the run's row was lost."""
    import steerai.probes.task_suite as suite  # noqa: PLC0415 - this test only

    keep = tmp_path / "new" / "keep"
    seen: list[bool] = []

    def run(task: Any, number: int, planner: Any, kept: Path | None = None) -> dict[str, Any]:
        seen.append(kept is not None and kept.is_dir())
        return {"task": task.task_id, "run": number, "passed": False}

    monkeypatch.setattr(suite, "one_run", run)
    out = tmp_path / "rows.jsonl"
    suite.main(["--tasks", "np1", "--runs", "1", "--keep", str(keep), "--out", str(out)])
    assert seen == [True]
    assert '"task": "np1"' in out.read_text("utf-8")
