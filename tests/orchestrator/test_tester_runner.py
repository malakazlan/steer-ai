"""Tester's red-first tests for the task runner (PR #16): the codec boundary (RN-01/04/05/26/
30/31/33), the console (RN-08/09/42/43), the risk limit (RN-02), stop codes and the post-ask
check (RN-10/13/14), the ledger lock and bindings (RN-12/16/17, Windows), window resolution
(RN-15/21/36/53) and the runner's one-JSON-line contract (RN-23/28/39/40/51)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.console import MAX_ANSWER_CHARS, ConsoleConfirmer, render_ask
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.ledger import Ledger, LedgerError
from steerai.orchestrator.loop import STOP_CODES
from steerai.orchestrator.plan_io import MAX_PARTS, PlanError, plan_from_bytes, plan_from_json
from steerai.orchestrator.trace import ListTrace
from steerai.probes.windows import WindowInfo
from steerai.run import (
    EXIT_BLOCKED,
    EXIT_DONE,
    EXIT_PAUSED,
    EXIT_USAGE,
    RunnerError,
    UsageError,
    main,
    over_limit,
    parse_args,
    resolve_window,
    run,
)
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import FakeWorld
from tests.orchestrator.test_console import DECISION, _ask, _console
from tests.orchestrator.test_edge_cases import _open_record, _rebuild
from tests.orchestrator.test_loop import Harness, _click, _send, _type
from tests.orchestrator.test_loop import _plan as _loop_plan
from tests.orchestrator.test_plan_io import _doc, _error
from tests.orchestrator.test_run import (
    CHROME,
    CONSOLE,
    NOTEPAD,
    FakeSession,
    World,
    _done,
    _options,
    _plan,
    _run,
)
from tests.orchestrator.test_runner_loop import _Titled

# ---- the codec: every decoder failure is a PlanError (RN-30/31) ----------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param('"n": ' + "1" * 4301, id="int-over-4300-digits"),
        pytest.param('"n": ' + "[" * 5000 + "]" * 5000, id="list-bomb"),
        pytest.param('"n": ' + '{"a":' * 5000 + "1" + "}" * 5000, id="object-bomb"),
    ],
)
def test_every_decoder_failure_is_a_plan_error(payload: str) -> None:
    text = '{"goal": "g", "subgoals": [], "steps": [], ' + payload + "}"
    assert len(text.encode()) < 262_144, "the bomb is well under the size cap"
    with pytest.raises(PlanError):
        plan_from_json(text)


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param({"count_delta": {"role": "ListItem", "delta": 0}}, id="delta-zero"),
        pytest.param({"appeared": {"role": "Text", "name": ""}}, id="empty-name"),
        pytest.param({"appeared": {"role": "", "name": "x"}}, id="empty-role"),
        pytest.param(
            {"appeared": {"role": "Text", "name": "x", "within": {"landmark": ""}}},
            id="empty-landmark",
        ),
        pytest.param({"title": {"pattern": "   "}}, id="blank-pattern"),
        pytest.param({"all": [{"title": {"pattern": "x"}}]}, id="all-one-part"),
        pytest.param({"any_of": []}, id="any-of-empty"),
    ],
)
def test_a_predicate_the_contract_refuses_is_a_plan_error_under_the_subgoal(
    condition: dict[str, Any],
) -> None:
    doc = _doc()
    doc["subgoals"][0]["condition"] = condition
    assert _error(doc).path.startswith("$.subgoals[0].condition")


@pytest.mark.parametrize(
    ("value", "ok"),
    [(-1, False), (0, False), (1, True), (10_000, True), (10_001, False)],
)
def test_expected_is_bounded_one_to_ten_thousand(value: int, ok: bool) -> None:
    doc = _doc()
    doc["subgoals"][0]["condition"]["appeared"]["expected"] = value
    if ok:
        plan_from_json(json.dumps(doc))
    else:
        assert _error(doc).path == "$.subgoals[0].condition.appeared.expected"


@pytest.mark.parametrize(
    ("delta", "ok"),
    [(-10_001, False), (-10_000, True), (-1, True), (1, True), (10_000, True), (10_001, False)],
)
def test_delta_is_bounded_to_ten_thousand_either_way(delta: int, ok: bool) -> None:
    doc = _doc()
    doc["subgoals"][0]["condition"] = {"count_delta": {"role": "ListItem", "delta": delta}}
    if ok:
        plan_from_json(json.dumps(doc))
    else:
        assert _error(doc).path == "$.subgoals[0].condition.count_delta.delta"


def test_all_and_any_of_parts_are_capped_at_max_parts() -> None:
    parts = [{"title": {"pattern": f"t{i}"}} for i in range(MAX_PARTS)]
    doc = _doc()
    doc["subgoals"][0]["condition"] = {"all": parts}
    plan_from_json(json.dumps(doc))
    doc["subgoals"][0]["condition"] = {"all": [*parts, {"title": {"pattern": "more"}}]}
    assert _error(doc).path == "$.subgoals[0].condition.all"
    doc["subgoals"][0]["condition"] = {"any_of": [*parts, {"title": {"pattern": "more"}}]}
    assert _error(doc).path == "$.subgoals[0].condition.any_of"


def test_subgoals_and_steps_are_capped_in_count() -> None:
    doc = _doc()
    subgoal = doc["subgoals"][0]
    doc["subgoals"] = [{**subgoal, "id": f"g{i}"} for i in range(MAX_PARTS)]
    doc["steps"] = [{**doc["steps"][0], "subgoal": "g0"}]
    plan_from_json(json.dumps(doc))
    doc["subgoals"].append({**subgoal, "id": "g-more"})
    assert _error(doc).path == "$.subgoals"
    doc = _doc()
    doc["steps"] = [{**doc["steps"][0], "id": f"s{i}"} for i in range(256)]
    plan_from_json(json.dumps(doc))
    doc["steps"].append({**doc["steps"][0], "id": "s-more"})
    assert _error(doc).path == "$.steps"


def test_a_duplicate_key_is_refused_at_any_depth() -> None:
    text = json.dumps(_doc())
    inside_target = text.replace(
        '"role": "Edit", "name": "Message"', '"role": "Edit", "role": "Edit", "name": "Message"', 1
    )
    assert inside_target != text
    with pytest.raises(PlanError, match="duplicate"):
        plan_from_json(inside_target)
    inside_predicate = text.replace(
        '"role": "Text", "name": "hello there"',
        '"role": "Text", "name": "hello there", "name": "x"',
        1,
    )
    assert inside_predicate != text
    with pytest.raises(PlanError, match="duplicate"):
        plan_from_json(inside_predicate)


def test_surrogates_are_refused_lone_and_accepted_as_a_json_escaped_pair() -> None:
    doc = _doc()
    text = json.dumps(doc).replace('"name": "Message"', '"name": "M\\ud83d"', 1)
    with pytest.raises(PlanError, match="character"):
        plan_from_json(text)
    paired = json.dumps(doc).replace('"name": "Message"', '"name": "M\\ud83d\\ude00"', 1)
    plan = plan_from_json(paired)
    target = plan.steps[0].target
    assert target is not None
    assert target.name == "M\U0001f600"
    with pytest.raises(PlanError, match="utf-8"):
        plan_from_bytes(
            json.dumps(doc).replace("Message", "M\x00").encode().replace(b"\x00", b"\xed\xb2\x80")
        )


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("automation_id", "btnSend"),
        ("automation_id", "btn￾Send"),
        ("landmark", "Conver͸sation"),
        ("landmark", "Conversation‍"),
        ("composer_name", "Mess\x7fage"),
    ],
)
def test_private_use_unassigned_and_format_characters_are_refused_in_ids_and_landmarks(
    where: str, value: str
) -> None:
    doc = _doc()
    if where == "automation_id":
        doc["steps"][2]["target"]["automation_id"] = value
    elif where == "landmark":
        doc["subgoals"][0]["condition"]["appeared"]["within"]["landmark"] = value
    else:
        doc["steps"][2]["params"]["composer_name"] = value
    with pytest.raises(PlanError, match="character"):
        plan_from_json(json.dumps(doc, ensure_ascii=True))


# ---- RN-05 at the runner: nothing from the file or the desktop reaches stderr raw ------------


def test_a_plan_error_message_never_reaches_stderr_raw(tmp_path: Path) -> None:
    plan = _plan(_type())
    options = _options(tmp_path, plan)
    doc = json.loads(options.plan.read_text(encoding="utf-8"))
    doc["\x1b[2K\x1b]0;pwned\x07evil"] = 1
    options.plan.write_text(json.dumps(doc), encoding="utf-8")
    code, out, err = _run(options, World(session=FakeSession(_done(plan))))
    assert code == EXIT_USAGE
    assert out["detail"] == "invalid_plan"
    assert "\x1b" not in err
    assert "\x07" not in err


def test_candidate_window_titles_are_escaped_on_stderr(tmp_path: Path) -> None:
    plan = _plan(_type())
    evil = WindowInfo(hwnd=0x60, title="Amir\x1b]0;pwned\x07 - Chat - Google Chrome", pid=950)
    world = World(windows=(CHROME, evil, CONSOLE), session=FakeSession(_done(plan)))
    code, out, err = _run(_options(tmp_path, plan), world)
    assert code == EXIT_USAGE
    assert out["detail"] == "ambiguous_target"
    assert "0x60" in err
    assert "\x1b" not in err
    assert "\x07" not in err


# ---- the console (RN-08/09/42/43) ---------------------------------------------------------


def test_the_answer_length_cap_is_exact() -> None:
    token = f"yes:{DECISION}"
    line = token + " " * (MAX_ANSWER_CHARS - 1 - len(token)) + "\n"
    assert len(line) == MAX_ANSWER_CHARS
    confirmer, _ = _console(line)
    assert confirmer.ask(_ask()) == token
    confirmer, _ = _console(token + " " * (MAX_ANSWER_CHARS - len(token)) + "\n")
    assert confirmer.ask(_ask()) == "unanswered"


def test_a_piped_stdin_is_detected_as_non_interactive_so_a_bare_yes_never_approves() -> None:
    confirmer = ConsoleConfirmer(io.StringIO("yes\nyes\n"), io.StringIO())
    assert confirmer.ask(_ask()) == "unanswered"
    assert confirmer.ask(_ask(decision_id="d-second000000")) == "unanswered"


@pytest.mark.parametrize(
    "line",
    [f"yes:{DECISION}\x00\n", f"yes:{DECISION}\u200b\n", f"\ufeffyes:{DECISION}\n"],
)
def test_invisible_characters_around_the_token_are_not_the_token(line: str) -> None:
    confirmer, _ = _console(line)
    assert confirmer.ask(_ask()) == "unanswered"


def test_every_rendered_field_is_escaped_not_only_the_target_name() -> None:
    ask = _ask(
        decision_id="d-\x1b[2K",
        verb="se\rnd",
        target_role="Butt\x07on",
        target_name="\u202eevil",
        automation_id="id\x1b",
        params={"text": "t\x1b]0;x\x07", "sc\x1bope": "v\x07"},
        previous_attempt="act\x1bing",
        expect={"kind": "Appeared", "role": "T\x1bext"},
    )
    rendered = render_ask(ask)
    for ch in rendered:
        assert ch.isprintable() or ch == "\n", repr(ch)


def test_a_render_failure_is_unanswered_without_reading() -> None:
    stdin = io.StringIO(f"yes:{DECISION}\n")
    confirmer = ConsoleConfirmer(stdin, io.StringIO(), interactive=True)
    assert confirmer.ask(_ask(expect={"kind": object()})) == "unanswered"
    assert stdin.tell() == 0


# ---- --max-risk on the effective risk (RN-02) ----------------------------------------------


def _id(step: Step | None) -> str | None:
    return None if step is None else step.id


def test_the_limit_sees_the_deny_list_through_the_automation_id_at_every_tier() -> None:
    hidden = Step(
        id="d",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="OK", automation_id="btnDelete"),
        risk="R0",
    )
    plan = _plan(_click(step_id="c", name="OK"), hidden)
    assert over_limit(plan, "R3") is None
    assert _id(over_limit(plan, "R2")) == "d"
    assert _id(over_limit(plan, "R1")) == "d"
    assert _id(over_limit(plan, "R0")) == "d"
    only_r1 = _plan(_click(step_id="c", name="OK", risk="R1"))
    assert over_limit(only_r1, "R1") is None
    assert _id(over_limit(only_r1, "R0")) == "c"


def test_the_runner_refuses_a_deny_word_hidden_in_an_automation_id(tmp_path: Path) -> None:
    hidden = Step(
        id="d",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name="OK", automation_id="chat-delete-button"),
        risk="R0",
    )
    plan = _plan(_click(step_id="c", name="OK"), hidden)
    world = World(session=FakeSession(_done(plan)))
    code, out, err = _run(_options(tmp_path, plan, max_risk="R2"), world)
    assert code == EXIT_BLOCKED
    assert out["step_id"] == "d"
    assert out["risk"] == "R3"
    assert "step d is R3" in err
    assert world.calls == []


# ---- stop codes and the post-ask check (RN-10/13/14) ----------------------------------------


class _Raising:
    """Raises `code` from the n-th observe on, the way the world model does."""

    def __init__(self, inner: FakeWorld, code: ErrorCode) -> None:
        self.inner = inner
        self.code = code
        self.calls = 0
        self.dies_at = 10**9

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        self.calls += 1
        if self.calls >= self.dies_at:
            raise SteerError(self.code, "the window is not there")
        return self.inner.observe(hwnd, **kwargs)


@pytest.mark.parametrize("code", [c for c in ErrorCode if c in STOP_CODES])
def test_every_stop_code_raised_after_a_dispatch_ends_the_task_with_the_record_open(
    code: ErrorCode,
) -> None:
    h = Harness()
    raising = _Raising(h.world, code)
    _rebuild(h, observer=raising)
    plan = _loop_plan(_type(), _send(), _click(step_id="s9"))

    def die_on_the_sends_verify(_ask: Ask) -> None:
        raising.dies_at = raising.calls + 2

    h.confirmer.on_ask = die_on_the_sends_verify
    task = h.session.run_task(plan)
    assert task.status == "stopped"
    assert task.code is code
    assert [r.step_id for r in task.results] == ["s2", "s3"]
    send = task.results[1]
    assert send.action is not None, "the send was dispatched before the window went"
    assert send.evidence["detail"] == "observe_raised"
    assert send.code is code
    assert h.world.sent == ["hello there"]
    assert h.ledger.admit(h.ledger.key_for(_send())) == "confirm"
    assert h.ledger.records["s3"].phase == "acting"
    assert any('"kind": "task_end"' in line and '"stopped"' in line for line in h.trace.lines)


def test_a_non_stop_code_after_a_dispatch_does_not_end_the_task() -> None:
    h = Harness()
    raising = _Raising(h.world, ErrorCode.UNSTABLE_UI)
    _rebuild(h, observer=raising)
    plan = _loop_plan(_type(), _send(), _click(step_id="s9"))

    def die_once_on_the_sends_verify(_ask: Ask) -> None:
        raising.dies_at = raising.calls + 2

    h.confirmer.on_ask = die_once_on_the_sends_verify
    original = raising.observe

    def observe(hwnd: int, **kwargs: Any) -> Snapshot:
        try:
            return original(hwnd, **kwargs)
        except SteerError:
            raising.dies_at = 10**9
            raise

    raising.observe = observe  # type: ignore[method-assign]  # one raise, then the world again
    task = h.session.run_task(plan)
    assert [r.step_id for r in task.results] == ["s2", "s3", "s9"]
    assert task.results[1].code is ErrorCode.UNSTABLE_UI


def test_a_title_change_during_a_resume_ask_keeps_the_record_open_and_never_dispatches() -> None:
    h = Harness()
    titled = _Titled(h.world)
    _rebuild(h, observer=titled)
    h.session.run_step(_type())
    _open_record(h, _send(), titled.observe(0x10), "acting")

    def switch_conversation(ask: Ask) -> None:
        assert ask.previous_attempt == "acting", "the console warns about the earlier attempt"
        titled.title = "Sara Ali - Chat"

    h.confirmer.on_ask = switch_conversation
    result = h.session.run_step(_send())
    assert result.ok is False
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "screen_changed_during_ask"
    assert h.world.sent == []
    assert h.ledger.records["s3"].phase == "acting"
    assert len(h.confirmer.asked) == 1


# ---- the ledger lock and bindings (RN-12/16/17), Windows only ------------------------------


@pytest.mark.windows
@pytest.mark.parametrize("content", ["not json", '{"pid": true}', '{"pid": "7"}', "{}", ""])
def test_a_lock_without_a_holder_pid_is_stale_and_replaced(tmp_path: Path, content: str) -> None:
    Ledger(tmp_path, task_id="t", started_at=1.0).close()
    lock = next(tmp_path.iterdir()) / "lock"
    lock.write_text(content, encoding="utf-8")
    with Ledger(tmp_path, task_id="t", started_at=1.0):
        assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not lock.exists()


@pytest.mark.windows
def test_a_corrupt_state_releases_the_lock(tmp_path: Path) -> None:
    Ledger(tmp_path, task_id="t", started_at=1.0).close()
    state = next(tmp_path.rglob("state.ledger"))
    state.write_bytes(b"garbage")
    with pytest.raises(LedgerError, match="unreadable"):
        Ledger(tmp_path, task_id="t", started_at=1.0)
    assert not (state.parent / "lock").exists()


@pytest.mark.windows
def test_a_deliberate_window_change_becomes_the_binding(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t", started_at=1.0) as ledger:
        ledger.bind_window(pid=500, title="Amir Khan - Chat")
    with Ledger(tmp_path, task_id="t", started_at=1.0) as ledger:
        ledger.bind_window(pid=501, title="Sara Ali - Chat", allow_change=True)
    with Ledger(tmp_path, task_id="t", started_at=1.0) as ledger:
        ledger.bind_window(pid=501, title="Sara Ali - Chat")
        with pytest.raises(LedgerError, match="window_changed"):
            ledger.bind_window(pid=500, title="Amir Khan - Chat")


@pytest.mark.windows
def test_the_runner_exits_1_ledger_locked_when_another_live_runner_holds_the_task(
    tmp_path: Path,
) -> None:
    plan = _plan(_type())
    options = _options(tmp_path, plan, task_id="t")
    Ledger(options.ledger_dir, task_id="t", started_at=1.0).close()
    lock = next(options.ledger_dir.iterdir()) / "lock"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock.write_text(json.dumps({"pid": sleeper.pid}), encoding="utf-8")
        world = World(session=FakeSession(_done(plan)))
        deps = world.deps().replace(
            open_ledger=lambda root, task_id: Ledger(root, task_id=task_id, started_at=1.0)
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        code = run(options, deps, stdin=io.StringIO(), stdout=stdout, stderr=stderr)
        assert code == EXIT_USAGE
        out = json.loads(stdout.getvalue())
        assert out["detail"] == "ledger_locked"
        assert out["code"] == "gate_blocked"
        assert str(sleeper.pid) in stderr.getvalue()
        assert world.calls == []
        assert lock.exists(), "the other runner's lock is untouched"
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)


@pytest.mark.windows
def test_a_ledger_dir_that_is_a_file_exits_1_wal_unavailable(tmp_path: Path) -> None:
    plan = _plan(_type())
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    options = _options(tmp_path, plan, ledger_dir=blocker)
    world = World(session=FakeSession(_done(plan)))
    deps = world.deps().replace(
        open_ledger=lambda root, task_id: Ledger(root, task_id=task_id, started_at=1.0)
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(options, deps, stdin=io.StringIO(), stdout=stdout, stderr=stderr)
    assert code == EXIT_USAGE
    assert json.loads(stdout.getvalue())["detail"] == "wal_unavailable"
    assert world.calls == []


# ---- window resolution (RN-15/21/36/53) ----------------------------------------------------


def test_two_matches_one_minimized_is_still_ambiguous() -> None:
    minimized = WindowInfo(hwnd=0x50, title="Amir - Chat - Google Chrome", pid=900, minimized=True)
    with pytest.raises(RunnerError) as info:
        resolve_window([CHROME, minimized], "chrome", excluded_hwnds=set(), excluded_pids=set())
    assert info.value.detail == "ambiguous_target"
    assert [c.hwnd for c in info.value.candidates] == [CHROME.hwnd, minimized.hwnd]


def test_a_needle_only_the_console_matches_is_no_match_and_the_console_is_no_candidate() -> None:
    with pytest.raises(RunnerError) as info:
        resolve_window(
            [CONSOLE, NOTEPAD], "steerai", excluded_hwnds={CONSOLE.hwnd}, excluded_pids=set()
        )
    assert info.value.detail == "no_match"
    assert list(info.value.candidates) == [NOTEPAD]


def test_whitespace_in_the_needle_is_collapsed_like_the_title() -> None:
    found = resolve_window([CHROME], "  amir   khan ", excluded_hwnds=set(), excluded_pids=set())
    assert found == CHROME


@pytest.mark.parametrize(
    ("text", "value"),
    [("1", 1), ("0x10", 16), ("0b11", 3), ("0o17", 15), ("18446744073709551615", 2**64 - 1)],
)
def test_hwnd_parses_base_0_up_to_64_bits(tmp_path: Path, text: str, value: int) -> None:
    assert parse_args(["--plan", str(tmp_path), "--hwnd", text]).hwnd == value


@pytest.mark.parametrize("text", ["0", "-1", "18446744073709551616", "abc", "1.5", "", "0x"])
def test_hwnd_out_of_range_or_not_an_integer_is_a_usage_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(UsageError):
        parse_args(["--plan", str(tmp_path), "--hwnd", text])


def test_hwnd_runs_through_the_same_checks_as_a_title(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)))
    code, out, _ = _run(_options(tmp_path, plan, window=None, hwnd=0x99), world)
    assert code == EXIT_USAGE
    assert out["detail"] == "no_match"
    assert world.foreground_calls == []
    world = World(session=FakeSession(_done(plan)))
    code, out, err = _run(_options(tmp_path, plan, window=None, hwnd=CONSOLE.hwnd), world)
    assert code == EXIT_USAGE
    assert out["detail"] == "usage"
    assert "console" in err
    assert world.foreground_calls == []
    minimized = WindowInfo(hwnd=0x50, title="Amir - Chat", pid=900, minimized=True)
    world = World(windows=(CHROME, minimized, CONSOLE), session=FakeSession(_done(plan)))
    code, out, _ = _run(_options(tmp_path, plan, window=None, hwnd=0x50), world)
    assert code == EXIT_USAGE
    assert out["detail"] == "minimized"
    assert out["code"] == "element_not_actionable"
    world = World(session=FakeSession(_done(plan)))
    code, out, _ = _run(_options(tmp_path, plan, window=None, hwnd=CHROME.hwnd), world)
    assert code == EXIT_DONE
    assert world.ledger is not None
    assert world.ledger.windows == [(CHROME.pid, CHROME.title)]
    assert world.foreground_calls == [CHROME.hwnd]


# ---- one JSON line on every exit (RN-23/28/39/40/51) ---------------------------------------


def test_an_interrupt_while_opening_the_ledger_still_emits_one_json_line(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)))

    def interrupt(_root: Path, _task_id: str) -> Any:
        raise KeyboardInterrupt

    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        code = run(
            _options(tmp_path, plan),
            world.deps().replace(open_ledger=interrupt),
            stdin=io.StringIO(),
            stdout=stdout,
            stderr=stderr,
        )
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped run(): no JSON line, no exit code")
    assert code == EXIT_PAUSED
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "paused"


def test_an_os_error_opening_the_ledger_is_wal_unavailable_not_a_traceback(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)))

    def unwritable(_root: Path, _task_id: str) -> Any:
        raise PermissionError(13, "Access is denied")

    stdout, stderr = io.StringIO(), io.StringIO()
    code = run(
        _options(tmp_path, plan),
        world.deps().replace(open_ledger=unwritable),
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
    )
    assert code == EXIT_USAGE
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["detail"] == "wal_unavailable"
    assert world.calls == []


def test_an_unwritable_trace_path_is_a_usage_error_before_the_session(tmp_path: Path) -> None:
    plan = _plan(_type())
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    world = World(session=FakeSession(_done(plan)))
    code, out, _ = _run(_options(tmp_path, plan, trace=blocker / "trace.jsonl"), world)
    assert code == EXIT_USAGE
    assert out["status"] == "error"
    assert out["detail"] != "internal_error"
    assert "session" not in world.calls


def test_main_maps_a_usage_error_to_exit_1_and_one_json_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (["--bogus"], [], ["--plan", "p.json", "--window", "x", "--hwnd", "1"]):
        assert main(argv) == EXIT_USAGE
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"status": "error", "code": None, "detail": "usage"}
        assert "usage error" in captured.err


def test_a_trace_sentinel_scan_through_the_runner(tmp_path: Path) -> None:
    plan = _plan(_type())
    world = World(session=FakeSession(_done(plan)))
    options = _options(tmp_path, plan, trace=tmp_path / "trace.jsonl")
    _run(options, world)
    bound = world.bound[0]
    trace = ListTrace()
    bound.trace.write({"kind": "task", "task_id": bound.task_id})
    trace.write({"kind": "task", "task_id": bound.task_id})
    assert options.trace is not None
    lines = options.trace.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"kind": "run", "task_id": bound.task_id}
    assert lines[1:] == trace.lines
