"""The v2 write-ahead file ([V2.88] L9, L11; [V2.15] Q9; V2 §8.4): a ledger file of its own under
the directory the caller passes, durable before any dispatch, one writer per task id, no resume,
and a torn or malformed file fails closed. The codec is a reversible stand-in for DPAPI so these
tests run anywhere; the DPAPI round trip is `windows` marked."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from steerai.loop2 import wal
from steerai.loop2.record import UNSETTLED, StartRefusal, V2Record, key_for
from steerai.loop2.wal import Codec, V2Ledger, WalError, ledger_path, start, unsettled
from steerai.orchestrator.contracts import GateDecision
from steerai.world.elements import Element
from tests.loop2.world_fakes import FIELD, PAGE, SEND, act, snapshot

REPO = Path(__file__).resolve().parents[2]
MARK = b"FAKE"
TYPED = "red shoes"


def _xor(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


def _unprotect(blob: bytes) -> bytes:
    if not blob.startswith(MARK):
        raise OSError("not a blob of this codec")
    return _xor(blob[len(MARK) :])


FAKE = Codec(protect=lambda data: MARK + _xor(data), unprotect=_unprotect)
"""Reversible, never clear text, and failing like DPAPI on a blob it did not make."""


def _open(root: Path, task_id: str = "t-1", **kw: object) -> V2Ledger:
    opened = start(root, task_id, codec=FAKE, **kw)  # type: ignore[arg-type]  # test kwargs
    assert isinstance(opened, V2Ledger), opened
    return opened


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[V2Ledger]:
    opened = _open(tmp_path)
    yield opened
    opened.close()


_DECISIONS = iter(range(1, 10_000))


def _yes(key: str) -> GateDecision:
    return GateDecision(decision_id=f"d-{next(_DECISIONS):012d}", action_key=key, outcome="confirm")


def _write(ledger: V2Ledger, element: Element = SEND, *, text: str | None = None) -> V2Record:
    action = act("type", target=element.id, text=text) if text else act("click", target=element.id)
    key = ledger.key_for(action, element, text=text)
    return ledger.write_ahead(
        action_key=key,
        subgoal="s1",
        verb=action.verb,
        role=element.role,
        before=snapshot(PAGE),
        decision=_yes(key),
    )


class CrashError(RuntimeError):
    """A crash injected into the gate's sequence."""


def _send_once(ledger: V2Ledger, dispatched: list[str], *, crash: str | None) -> None:
    """§3.4 steps 5, 10 and 11 as the runner will run them (4c.4): admission, write-ahead,
    `acting` before the dispatch, one dispatch, close. `crash` raises at the named point."""
    key = ledger.key_for(act("click", target=SEND.id), SEND, text=None)
    if ledger.admit(key, "R2") != "fresh":
        return
    record = ledger.write_ahead(
        action_key=key,
        subgoal="s1",
        verb="click",
        role="Button",
        before=snapshot(PAGE),
        decision=_yes(key),
    )
    if crash == "after_write":
        raise CrashError
    record = ledger.mark_acting(record)
    dispatched.append(key)
    if crash == "after_dispatch":
        raise CrashError
    ledger.close_record(record, "delivered_verified")


def _run(root: Path, dispatched: list[str], *, crash: str | None) -> StartRefusal | None:
    """One start of task `t-1`: a refusal, or the send run once; the runner's `finally`
    releases the lock whatever happened."""
    opened = start(root, "t-1", codec=FAKE)
    if isinstance(opened, StartRefusal):
        return opened
    try:
        _send_once(opened, dispatched, crash=crash)
    finally:
        opened.close()
    return None


# ---- [V2.88] L11: zero double dispatches with a crash injected after the ledger write ---------


def test_a_crash_after_the_write_ahead_refuses_the_next_start_and_dispatches_nothing(
    tmp_path: Path,
) -> None:
    dispatched: list[str] = []
    with pytest.raises(CrashError):
        _run(tmp_path, dispatched, crash="after_write")
    on_disk = ledger_path(tmp_path, "t-1").read_bytes()
    for _ in range(3):
        refusal = _run(tmp_path, dispatched, crash=None)
        assert refusal == StartRefusal(token=UNSETTLED, verb="click", role="Button")
    assert dispatched == []
    assert ledger_path(tmp_path, "t-1").read_bytes() == on_disk  # a start never writes


def test_an_open_written_record_is_never_read_as_not_delivered(tmp_path: Path) -> None:
    """[V2.90] (3): a death before the dispatch or the void leaves the record open; no start
    and no listing infers `not_delivered` from it."""
    ledger = _open(tmp_path)
    record = _write(ledger)
    ledger.close()
    assert isinstance(start(tmp_path, "t-1", codec=FAKE), StartRefusal)
    rows = unsettled(tmp_path, "t-1", codec=FAKE)
    assert [(r.phase, r.outcome) for r in rows] == [("written", None)]
    assert record.phase == "written"


def test_a_crash_after_the_dispatch_never_dispatches_twice(tmp_path: Path) -> None:
    dispatched: list[str] = []
    with pytest.raises(CrashError):
        _run(tmp_path, dispatched, crash="after_dispatch")
    for _ in range(3):
        assert _run(tmp_path, dispatched, crash=None) is not None
    assert len(dispatched) == 1


def test_a_settled_send_is_done_already_on_a_later_start(tmp_path: Path) -> None:
    """Keys are stable across starts of one task id (the secret lives in the file), so a
    verified send is `done` and never sent again."""
    dispatched: list[str] = []
    assert _run(tmp_path, dispatched, crash=None) is None
    assert _run(tmp_path, dispatched, crash=None) is None
    assert len(dispatched) == 1


_CHILD = """
import os, sys
from pathlib import Path
from steerai.loop2.wal import Codec, start
from tests.loop2.world_fakes import PAGE, SEND, act, snapshot
from steerai.orchestrator.contracts import GateDecision
MARK = b"FAKE"
xor = lambda data: bytes(b ^ 0x5A for b in data)
codec = Codec(protect=lambda d: MARK + xor(d), unprotect=lambda b: xor(b[len(MARK):]))
ledger = start(Path(sys.argv[1]), "t-1", codec=codec)
key = ledger.key_for(act("click", target=SEND.id), SEND, text=None)
record = ledger.write_ahead(action_key=key, subgoal="s1", verb="click", role="Button",
    before=snapshot(PAGE), decision=GateDecision("d-1", key, "confirm"))
if sys.argv[2] == "acting":
    ledger.mark_acting(record)
os._exit(3)
"""


@pytest.mark.parametrize("phase", ["written", "acting"])
def test_a_process_killed_after_the_write_ahead_leaves_the_record_on_disk(
    tmp_path: Path, phase: str
) -> None:
    """A hard exit (no `finally`, no flush by Python): what reached the file before the exit is
    what the next start reads. The lock's release after a process dies is timed by the OS
    (LockFileEx remarks), so this reads through the lock-free operator listing."""
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed script, no shell
        [sys.executable, "-c", _CHILD, str(tmp_path), phase],
        cwd=REPO,
        check=False,
        capture_output=True,
        timeout=60,
    )
    assert done.returncode == 3, done.stderr.decode(errors="replace")
    rows = unsettled(tmp_path, "t-1", codec=FAKE)
    assert [(r.verb, r.role, r.phase, r.outcome) for r in rows] == [
        ("click", "Button", phase, None)
    ]


# ---- the void: input after the write-ahead, before the dispatch (principal review of #509) ----


def test_a_voided_record_lets_the_next_start_go_and_the_key_is_fresh(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    record = _write(ledger)
    ledger.void(record)
    ledger.close()
    again = _open(tmp_path)
    assert again.admit(record.action_key, "R2") == "fresh"
    again.close()


def test_the_void_keeps_the_yes_consumed(ledger: V2Ledger) -> None:
    """[V2.90] (3): a new attempt needs a new ask."""
    key = ledger.key_for(act("click", target=SEND.id), SEND, text=None)
    yes = _yes(key)
    record = ledger.write_ahead(
        action_key=key,
        subgoal="s1",
        verb="click",
        role="Button",
        before=snapshot(PAGE),
        decision=yes,
    )
    ledger.void(record)
    assert yes.consumed
    with pytest.raises(WalError, match="decision_refused"):
        ledger.write_ahead(
            action_key=key,
            subgoal="s1",
            verb="click",
            role="Button",
            before=snapshot(PAGE),
            decision=yes,
        )


def test_an_acting_record_is_never_voided(ledger: V2Ledger) -> None:
    record = ledger.mark_acting(_write(ledger))
    with pytest.raises(WalError, match="void"):
        ledger.void(record)


# ---- torn and malformed files fail closed ------------------------------------------------------


def _torn(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data[:-7])


def _garbage_line(path: Path) -> None:
    with path.open("ab") as handle:
        handle.write(base64.b64encode(MARK + _xor(b"{not json")) + b"\n")


def _unknown_codec(path: Path) -> None:
    with path.open("ab") as handle:
        handle.write(base64.b64encode(b"other blob") + b"\n")


def _half_line(path: Path) -> None:
    with path.open("ab") as handle:
        handle.write(b"QUJD")


def _no_final_newline(path: Path) -> None:
    path.write_bytes(path.read_bytes()[:-1])


def _empty(path: Path) -> None:
    path.write_bytes(b"")


def _crlf(path: Path) -> None:
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))


BREAKS: list[Callable[[Path], None]] = [
    _torn,
    _half_line,
    _no_final_newline,
    _garbage_line,
    _unknown_codec,
    _empty,
    _crlf,
]


@pytest.mark.parametrize("damage", BREAKS, ids=lambda f: f.__name__.strip("_"))
def test_a_damaged_file_refuses_the_start_and_the_listing(
    tmp_path: Path, damage: Callable[[Path], None]
) -> None:
    ledger = _open(tmp_path)
    ledger.close_record(ledger.mark_acting(_write(ledger)), "delivered_verified")
    ledger.close()
    damage(ledger_path(tmp_path, "t-1"))
    with pytest.raises(WalError, match="ledger_unreadable"):
        start(tmp_path, "t-1", codec=FAKE)
    with pytest.raises(WalError, match="ledger_unreadable"):
        unsettled(tmp_path, "t-1", codec=FAKE)


def _event(**fields: object) -> bytes:
    payload = json.dumps({"v": 1, **fields}).encode()
    return base64.b64encode(MARK + _xor(payload)) + b"\n"


ILLEGAL = {
    "outcome_before_acting": _event(e="outcome", seq=0, outcome="delivered_verified"),
    "unknown_record": _event(e="acting", seq=7),
    "outcome_off_the_closed_set": _event(e="outcome", seq=0, outcome="fine"),
    "unknown_event": _event(e="resume", seq=0),
    "second_head": _event(e="head", task="0" * 64, secret="00" * 32),
}


@pytest.mark.parametrize("line", list(ILLEGAL.values()), ids=list(ILLEGAL))
def test_an_event_the_rules_do_not_allow_is_unreadable(tmp_path: Path, line: bytes) -> None:
    ledger = _open(tmp_path)
    _write(ledger)
    ledger.close()
    with ledger_path(tmp_path, "t-1").open("ab") as handle:
        handle.write(line)
    with pytest.raises(WalError, match="ledger_unreadable"):
        start(tmp_path, "t-1", codec=FAKE)


def test_another_tasks_file_is_unreadable(tmp_path: Path) -> None:
    _open(tmp_path, "t-1").close()
    _open(tmp_path, "t-2").close()
    ledger_path(tmp_path, "t-1").write_bytes(ledger_path(tmp_path, "t-2").read_bytes())
    with pytest.raises(WalError, match="ledger_unreadable"):
        start(tmp_path, "t-1", codec=FAKE)


def test_a_write_that_fails_half_way_breaks_the_ledger_and_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _open(tmp_path)
    real = os.write

    def half(fd: int, data: bytes | memoryview) -> int:
        real(fd, bytes(data)[: len(data) // 2])
        raise OSError(28, "no space left")

    monkeypatch.setattr("steerai.loop2.wal.os.write", half)
    with pytest.raises(WalError, match="wal_unavailable"):
        _write(ledger)
    monkeypatch.setattr("steerai.loop2.wal.os.write", real)
    with pytest.raises(WalError, match="wal_unavailable"):
        _write(ledger)  # a broken ledger never writes again
    ledger.close()
    with pytest.raises(WalError, match="ledger_unreadable"):
        start(tmp_path, "t-1", codec=FAKE)


def test_a_failed_fsync_is_wal_unavailable(
    ledger: V2Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(fd: int) -> None:
        raise OSError(5, "i/o error")

    monkeypatch.setattr("steerai.loop2.wal.os.fsync", fail)
    with pytest.raises(WalError, match="wal_unavailable"):
        _write(ledger)


def test_a_short_write_is_completed(ledger: V2Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.write

    def short(fd: int, data: bytes | memoryview) -> int:
        return real(fd, bytes(data)[:5])

    monkeypatch.setattr("steerai.loop2.wal.os.write", short)
    record = _write(ledger)
    monkeypatch.setattr("steerai.loop2.wal.os.write", real)
    ledger.close()
    assert [r.seq for r in unsettled(ledger.path.parent, "t-1", codec=FAKE)] == [0]
    assert record.phase == "written"


def test_the_write_ahead_is_on_disk_when_it_returns(
    ledger: V2Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    real = os.fsync

    def count(fd: int) -> None:
        synced.append(fd)
        real(fd)

    monkeypatch.setattr("steerai.loop2.wal.os.fsync", count)
    _write(ledger)
    assert len(synced) == 1
    assert [r.verb for r in unsettled(ledger.path.parent, "t-1", codec=FAKE)] == ["click"]


def test_a_record_over_the_cap_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    ledger = _open(tmp_path, max_record_bytes=200)
    size = ledger.path.stat().st_size
    with pytest.raises(WalError, match="record_too_large"):
        _write(ledger)
    assert ledger.path.stat().st_size == size
    ledger.close()
    assert isinstance(start(tmp_path, "t-1", codec=FAKE), V2Ledger)


def test_no_codec_no_ledger(tmp_path: Path) -> None:
    def refuse(_: bytes) -> bytes:
        raise OSError("DPAPI requires Windows")

    with pytest.raises(WalError, match="wal_unavailable"):
        start(tmp_path, "t-1", codec=Codec(protect=refuse, unprotect=refuse))


# ---- one writer per task id --------------------------------------------------------------------


def test_a_second_writer_in_this_process_is_refused(ledger: V2Ledger) -> None:
    with pytest.raises(WalError, match="ledger_locked"):
        start(ledger.path.parent, "t-1", codec=FAKE)


def test_the_lock_is_free_again_after_close(tmp_path: Path) -> None:
    _open(tmp_path).close()
    _open(tmp_path).close()


def test_another_task_id_is_another_writer(ledger: V2Ledger) -> None:
    _open(ledger.path.parent, "t-2").close()


_HOLDER = """
import sys
from pathlib import Path
from steerai.loop2.wal import Codec, start
xor = lambda data: bytes(b ^ 0x5A for b in data)
codec = Codec(protect=lambda d: b"FAKE" + xor(d), unprotect=lambda b: xor(b[4:]))
ledger = start(Path(sys.argv[1]), "t-1", codec=codec)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.readline()
ledger.close()
"""


def test_a_second_writer_in_another_process_is_refused(tmp_path: Path) -> None:
    holder = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed script, no shell
        [sys.executable, "-c", _HOLDER, str(tmp_path)],
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdin is not None
        assert holder.stdout.readline().strip() == b"held"  # text mode: CRLF on Windows
        with pytest.raises(WalError, match="ledger_locked"):
            start(tmp_path, "t-1", codec=FAKE)
        holder.stdin.write(b"\n")
        holder.stdin.flush()
        assert holder.wait(timeout=60) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=60)
    assert unsettled(tmp_path, "t-1", codec=FAKE) == []


# ---- keys, records and what reaches the disk ---------------------------------------------------


def test_the_ledger_key_is_v1s_action_key_on_fixed_inputs() -> None:
    """The fixed values pinned for v1 in `test_ledger_keys.py`, reached through the v2 key."""
    secret = bytes(range(32))
    send = act("click", target=SEND.id)
    assert key_for(secret, send, SEND, text=None) == (
        "1c953a3f129179724abe4937713c49fc675d7cb9e6e0cbfa435d2c908f9ae8f9"
    )
    message = Element(
        id=30, depth=1, role="Edit", name="Message", box=(0, 0, 9, 9), automation_id="msg"
    )
    typed = act("type", target=30, text="ok thanks")
    assert key_for(secret, typed, message, text="ok thanks") == (
        "9823c6b4f8e305ac6848f776e992326b3b8ae6846179504fb3cd66ec1858e260"
    )


def test_keys_differ_between_task_ids(tmp_path: Path) -> None:
    one, two = _open(tmp_path, "t-1"), _open(tmp_path, "t-2")
    click = act("click", target=SEND.id)
    assert one.key_for(click, SEND, text=None) != two.key_for(click, SEND, text=None)
    one.close()
    two.close()


def test_the_file_lives_under_the_given_root_and_names_no_task_id(tmp_path: Path) -> None:
    root = tmp_path / "run"
    ledger = _open(root, "task-secretive")
    _write(ledger)
    ledger.close()
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert ledger.path.parent == root
    assert len(written) == 3  # the root, the ledger file and its lock file
    assert all("secretive" not in name for name in written)


def _decoded(path: Path) -> list[dict[str, object]]:
    lines = path.read_bytes().split(b"\n")[:-1]
    return [json.loads(_unprotect(base64.b64decode(line))) for line in lines]


def test_nothing_on_disk_is_clear_text(ledger: V2Ledger) -> None:
    _write(ledger, FIELD, text=TYPED)
    raw = ledger.path.read_bytes()
    for clear in (TYPED, "Search", "Button", "s1", "type"):
        assert clear.encode() not in raw


def test_a_record_keeps_the_key_not_the_typed_text(ledger: V2Ledger) -> None:
    """The typed text reaches the file only through the keyed HMAC; the before snapshot holds
    names (user data, encrypted as in v1), never a field's value ([V2.5])."""
    record = _write(ledger, FIELD, text=TYPED)
    events = _decoded(ledger.path)
    assert TYPED not in json.dumps(events)
    head, written = events
    assert set(head) == {"v", "e", "task", "secret"}
    assert TYPED not in str(head["task"])
    assert set(written) == {"v", "e", "seq", "key", "subgoal", "decision", "verb", "role", "before"}
    assert written["key"] == record.action_key


def test_the_operator_listing_names_closed_facts_only(ledger: V2Ledger) -> None:
    record = ledger.mark_acting(_write(ledger, FIELD, text=TYPED))
    settled = ledger.mark_acting(_write(ledger))
    ledger.close_record(settled, "delivered_verified")
    rows = unsettled(ledger.path.parent, "t-1", codec=FAKE)
    assert [(r.seq, r.subgoal, r.verb, r.role, r.phase, r.outcome) for r in rows] == [
        (0, "s1", "type", "Edit", "acting", None)
    ]
    assert record.phase == "acting"


def test_the_listing_of_a_task_that_never_ran_is_empty(tmp_path: Path) -> None:
    assert unsettled(tmp_path, "t-never", codec=FAKE) == []
    assert list(tmp_path.iterdir()) == []


def test_the_listing_reads_while_a_runner_holds_the_task(ledger: V2Ledger) -> None:
    _write(ledger)
    assert len(unsettled(ledger.path.parent, "t-1", codec=FAKE)) == 1


def test_the_list_unsettled_command_prints_json_lines(
    ledger: V2Ledger, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(ledger)
    argv = ["--list-unsettled", "--ledger-dir", str(ledger.path.parent), "--task-id", "t-1"]
    assert wal.main(argv, codec=FAKE) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows == [
        {
            "seq": 0,
            "subgoal": "s1",
            "verb": "click",
            "role": "Button",
            "phase": "written",
            "outcome": None,
        }
    ]


def test_the_list_unsettled_command_reports_an_unreadable_file(
    ledger: V2Ledger, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(ledger)
    _torn(ledger.path)
    argv = ["--list-unsettled", "--ledger-dir", str(ledger.path.parent), "--task-id", "t-1"]
    assert wal.main(argv, codec=FAKE) == wal.EXIT_UNREADABLE
    assert "ledger_unreadable" in capsys.readouterr().err


# ---- the record's phases and outcomes ----------------------------------------------------------


def test_a_close_needs_the_record_acting(ledger: V2Ledger) -> None:
    with pytest.raises(WalError, match="acting"):
        ledger.close_record(_write(ledger), "delivered_verified")


def test_a_settled_outcome_is_final(ledger: V2Ledger) -> None:
    record = ledger.mark_acting(_write(ledger))
    ledger.close_record(record, "delivered_verified")
    with pytest.raises(WalError, match="settled"):
        ledger.close_record(record, "unknown")


def test_an_unsettled_outcome_is_replaced_by_a_later_one(ledger: V2Ledger) -> None:
    """A delivery token first, then the effect's (`close_token`) or the proof's."""
    record = ledger.mark_acting(_write(ledger))
    ledger.close_record(record, "delivered_unverified")
    ledger.close_record(record, "no_effect")
    assert ledger.admit(record.action_key, "R2") == "fresh"


def test_nothing_delivered_never_follows_another_outcome(ledger: V2Ledger) -> None:
    record = ledger.mark_acting(_write(ledger))
    ledger.close_record(record, "delivery_uncertain")
    with pytest.raises(WalError, match="not_delivered"):
        ledger.close_record(record, "not_delivered")


def test_an_outcome_off_the_closed_set_is_refused(ledger: V2Ledger) -> None:
    with pytest.raises(WalError, match="outcome"):
        ledger.close_record(ledger.mark_acting(_write(ledger)), "fine")


def test_a_record_this_ledger_did_not_return_is_refused(ledger: V2Ledger) -> None:
    record = _write(ledger)
    stranger = V2Record(**{**record.__dict__, "phase": "acting"})
    with pytest.raises(WalError, match="record_unknown"):
        ledger.close_record(stranger, "delivered_verified")


def test_an_open_record_asks_again_and_a_done_key_is_done(ledger: V2Ledger) -> None:
    record = ledger.mark_acting(_write(ledger))
    assert ledger.admit(record.action_key, "R2") == "confirm"
    assert ledger.admit(record.action_key, "R1") == "fresh"
    ledger.close_record(record, "delivered_verified")
    assert ledger.admit(record.action_key, "R3") == "done"


def test_a_second_record_under_one_key_after_an_unknown(ledger: V2Ledger) -> None:
    """[V2.17] item 4: an unknown keeps the key consumed; a fresh yes opens a second record,
    and the key's admission follows the latest record."""
    first = ledger.mark_acting(_write(ledger))
    ledger.close_record(first, "unknown")
    second = ledger.mark_acting(_write(ledger))
    assert second.action_key == first.action_key
    ledger.close_record(second, "delivered_verified")
    assert ledger.admit(first.action_key, "R2") == "done"
    assert ledger.refusal() is None


def test_the_kill_settles_every_open_record_unknown(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    ledger.mark_acting(_write(ledger))
    _write(ledger, FIELD, text=TYPED)
    assert ledger.settle_on_kill() == 2
    ledger.close()
    rows = unsettled(tmp_path, "t-1", codec=FAKE)
    assert [(r.phase, r.outcome) for r in rows] == [("verified", "unknown")] * 2
    assert isinstance(start(tmp_path, "t-1", codec=FAKE), StartRefusal)


def test_a_closed_ledger_never_writes(tmp_path: Path) -> None:
    ledger = _open(tmp_path)
    ledger.close()
    with pytest.raises(WalError, match="wal_unavailable"):
        _write(ledger)


@pytest.mark.windows
def test_the_default_codec_is_dpapi(tmp_path: Path) -> None:
    opened = start(tmp_path, "t-1")
    assert isinstance(opened, V2Ledger)
    _write(opened, FIELD, text=TYPED)
    opened.close()
    assert b"Search" not in ledger_path(tmp_path, "t-1").read_bytes()
    assert [r.verb for r in unsettled(tmp_path, "t-1")] == ["type"]
    with pytest.raises(WalError, match="ledger_unreadable"):
        unsettled(tmp_path, "t-1", codec=FAKE)
