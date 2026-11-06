"""Task ledger (design 4 [v0.8], 5.11, orchestrator review OR-01/05/07/08/24): keys consumed at
write-ahead time and persisted, phased records, unreadable means unknown, keys keyed by a
per-task secret."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from steerai.orchestrator.contracts import GateDecision, Step, Target
from steerai.orchestrator.ledger import (
    Ledger,
    LedgerError,
    WriteAheadRecord,
    action_key,
)
from steerai.platform import dpapi
from steerai.probes.windows import process_alive
from steerai.world.elements import Element
from steerai.world.model import Snapshot

pytestmark = pytest.mark.windows  # DPAPI: the ledger never persists in clear anywhere else
NONCE = b"\x01" * 32


def _snapshot(snapshot_id: str = "before") -> Snapshot:
    return Snapshot(
        id=snapshot_id,
        hwnd=0x10,
        process="chrome.exe",
        title="Amir Khan - Chat",
        taken_at=10.0,
        elements=[
            Element(
                id=0, depth=0, role="Pane", name="Chat", box=(0, 0, 500, 400), runtime_id=(42, 1)
            ),
            Element(
                id=1,
                depth=1,
                role="Button",
                name="Send",
                box=(10, 10, 60, 30),
                runtime_id=(42, 42),
                parent_id=0,
                automation_id="send",
            ),
        ],
        node_count=2,
        stable=True,
        pid=777,
    )


def _send(step_id: str = "s3", text: str = "hello there") -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        text=None,
        risk="R2",
        params={"text": text},
    )


def _decision(key: str, outcome: str = "confirm", decision_id: str = "d-1") -> GateDecision:
    return GateDecision(decision_id=decision_id, action_key=key, outcome=outcome)  # type: ignore[arg-type]


# ---- action keys -------------------------------------------------------------------------------


def test_action_key_is_stable_over_runtime_ids_and_whitespace() -> None:
    a = action_key(NONCE, "send", Target(role="Button", name="Send"), {"text": "hello"})
    b = action_key(NONCE, "send", Target(role="Button", name=" send "), {"text": " hello "})
    assert a == b, "normalised name and parameters, no RuntimeId: survives a reload"
    assert len(a) == 64
    assert a != action_key(
        b"\x02" * 32, "send", Target(role="Button", name="Send"), {"text": "hello"}
    )
    assert a != action_key(NONCE, "send", Target(role="Button", name="Send"), {"text": "hello!"})
    assert a != action_key(NONCE, "click", Target(role="Button", name="Send"), {"text": "hello"})
    assert a != action_key(
        NONCE, "send", Target(role="Button", name="Send", automation_id="x"), {"text": "hello"}
    )


def test_action_key_is_keyed_not_a_plain_hash(tmp_path: Path) -> None:
    # OR-24: a plain sha256 of (task, verb, name, "hello") is dictionary-recoverable from a trace.
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    key = ledger.key_for(_send())
    assert key != action_key(
        b"", "send", Target(role="Button", name="Send"), {"text": "hello there"}
    )
    assert "hello" not in key
    reopened = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    assert reopened.key_for(_send()) == key, "the per-task secret persists with the task"
    other = Ledger(tmp_path, task_id="task-2", started_at=5.0)
    assert other.key_for(_send()) != key


# ---- consumed at write-ahead time, phases, admission ----------------------------------------


def test_write_ahead_consumes_the_key_and_a_crash_leaves_it_unknown(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    key = ledger.key_for(step)
    assert ledger.admit(key) == "fresh"
    record = ledger.write_ahead(step, _snapshot(), _decision(key, "approve"))
    assert record.phase == "written"
    # "crash" here: a new process reopens the ledger before any outcome was recorded
    reopened = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    assert reopened.admit(key) == "confirm", "written but never finished: unknown, never auto"
    assert reopened.read_ahead(step) is not None


def test_phases_advance_and_an_outcome_closes_the_key(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    key = ledger.key_for(step)
    ledger.write_ahead(step, _snapshot(), _decision(key, "approve"))
    ledger.mark(step, "acting")
    assert ledger.read_ahead(step).phase == "acting"  # type: ignore[union-attr]
    assert Ledger(tmp_path, task_id="task-1", started_at=5.0).admit(key) == "confirm"
    ledger.record_outcome(step, "delivered_verified")
    assert ledger.read_ahead(step).phase == "verified"  # type: ignore[union-attr]
    assert ledger.admit(key) == "done"
    assert Ledger(tmp_path, task_id="task-1", started_at=5.0).admit(key) == "done"


def test_nothing_sent_releases_the_key_but_anything_else_needs_a_fresh_decision(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    key = ledger.key_for(step)
    ledger.write_ahead(step, _snapshot(), _decision(key, "approve"))
    ledger.record_outcome(step, "not_delivered")
    assert ledger.admit(key) == "fresh", "nothing was sent: the local retry may run"
    ledger.write_ahead(step, _snapshot(), _decision(key, "approve", "d-2"))
    ledger.record_outcome(step, "delivery_uncertain")
    assert ledger.admit(key) == "confirm"
    assert ledger.admit(key, _decision(key, "approve", "d-3")) == "confirm", "approve is not enough"
    fresh = _decision(key, "confirm", "d-4")
    assert ledger.admit(key, fresh) == "fresh"
    assert ledger.admit(key, fresh) == "confirm", "a decision is single use"
    assert ledger.admit(key, _decision("other-key", "confirm", "d-5")) == "confirm"


def test_write_ahead_needs_a_decision_bound_to_the_key(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    with pytest.raises(LedgerError, match="decision"):
        ledger.write_ahead(step, _snapshot(), None)
    with pytest.raises(LedgerError, match="decision"):
        ledger.write_ahead(step, _snapshot(), _decision("other-key", "approve"))
    with pytest.raises(LedgerError, match="decision"):
        ledger.write_ahead(step, _snapshot(), _decision(ledger.key_for(step), "block"))
    assert list(tmp_path.rglob("*.wal")) == []


def test_the_retry_verifies_against_the_original_before(tmp_path: Path) -> None:
    # OR-05: after an uncertain outcome the persisted before stays the before of record.
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    key = ledger.key_for(step)
    ledger.write_ahead(step, _snapshot("first-before"), _decision(key, "approve"))
    ledger.record_outcome(step, "delivery_uncertain")
    assert ledger.admit(key, _decision(key, "confirm", "d-2")) == "fresh"
    record = ledger.before_of_record(step, _snapshot("fresh-before"))
    assert record.id == "first-before"
    # a fresh step with no record uses the fresh before
    assert ledger.before_of_record(_send("s9"), _snapshot("fresh-before")).id == "fresh-before"


# ---- write-ahead records on disk -----------------------------------------------------------------


def test_write_ahead_record_is_encrypted_on_disk_and_reads_back(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    key = ledger.key_for(step)
    record = ledger.write_ahead(step, _snapshot(), _decision(key, "approve"))
    assert isinstance(record, WriteAheadRecord)
    files = list(tmp_path.rglob("*.wal"))
    assert len(files) == 1
    raw = files[0].read_bytes()
    assert b"Amir Khan" not in raw
    assert b"Send" not in raw
    assert b"task-1" not in raw
    restored = ledger.read_ahead(step)
    assert restored is not None
    assert restored.step_id == "s3"
    assert restored.action_key == key
    assert restored.before.id == "before"
    assert restored.before.handles == []
    assert restored.before.elements[1].runtime_id == (42, 42)
    assert restored.written_at >= 5.0
    assert restored.decision_id == "d-1"


def test_a_record_for_another_task_or_step_is_ignored(tmp_path: Path) -> None:
    first = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    first.write_ahead(_send(), _snapshot(), _decision(first.key_for(_send()), "approve"))
    second = Ledger(tmp_path, task_id="task-2", started_at=6.0)
    assert second.read_ahead(_send()) is None
    assert first.read_ahead(_send("s9")) is None


def test_a_failed_write_means_no_action_and_no_consumed_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    key = ledger.key_for(step)

    def broken(_data: bytes, *, entropy: bytes | None = None) -> bytes:
        raise OSError("CryptProtectData failed: error 5")

    monkeypatch.setattr(dpapi, "protect", broken)
    with pytest.raises(LedgerError, match="write-ahead"):
        ledger.write_ahead(step, _snapshot(), _decision(key, "approve"))
    assert list(tmp_path.rglob("*.wal")) == []
    monkeypatch.undo()
    assert Ledger(tmp_path, task_id="task-1", started_at=5.0).admit(key) == "fresh"


def test_a_failed_state_save_after_the_record_write_unlinks_the_record_and_keeps_the_key_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 5.11 write order: the record first, then the state; the state owns the consumed key, so a
    # record without a state entry must not survive and the key must stay fresh everywhere.
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    key = ledger.key_for(step)
    real = dpapi.protect
    calls = {"n": 0}

    def fails_on_the_state_save(data: bytes, *, entropy: bytes | None = None) -> bytes:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("CryptProtectData failed: error 5")
        return real(data, entropy=entropy)

    monkeypatch.setattr(dpapi, "protect", fails_on_the_state_save)
    with pytest.raises(LedgerError, match="ledger state"):
        ledger.write_ahead(step, _snapshot(), _decision(key, "approve"))
    monkeypatch.undo()
    assert calls["n"] == 2, "the record write came first, then the state save"
    assert list(tmp_path.rglob("*.wal")) == [], "the record is unlinked"
    assert list(tmp_path.rglob("*.tmp")) == []
    assert ledger.read_ahead(step) is None
    reopened = Ledger(tmp_path, task_id="task-1", started_at=9.0)
    assert reopened.admit(key) == "fresh"
    assert reopened.started_at == 5.0, "the persisted start time wins over the caller's"
    assert reopened.key_for(step) == key
    assert ledger.admit(key) == "fresh", "the same instance holds no consumed key either"


def test_tampered_record_is_unknown_not_absent(tmp_path: Path) -> None:
    # OR-08: unreadable is never "no record".
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    ledger.write_ahead(step, _snapshot(), _decision(ledger.key_for(step), "approve"))
    path = next(tmp_path.rglob("*.wal"))
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(LedgerError, match="unreadable"):
        ledger.read_ahead(step)


def test_unreadable_state_refuses_the_task(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    ledger.write_ahead(_send(), _snapshot(), _decision(ledger.key_for(_send()), "approve"))
    path = next(tmp_path.rglob("state.ledger"))
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(LedgerError, match="unreadable"):
        Ledger(tmp_path, task_id="task-1", started_at=5.0)


def test_ledger_state_survives_a_restart_encrypted(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    step = _send()
    key = ledger.key_for(step)
    ledger.write_ahead(step, _snapshot(), _decision(key, "approve"))
    ledger.record_outcome(step, "delivered_verified")
    reopened = Ledger(tmp_path, task_id="task-1", started_at=5.0)
    assert reopened.admit(key) == "done"
    assert reopened.started_at == 5.0
    state = next(tmp_path.rglob("state.ledger")).read_bytes()
    assert key.encode() not in state
    assert b"delivered_verified" not in state
    assert b"task-1" not in state


def test_oversized_before_snapshot_is_refused_not_truncated(tmp_path: Path) -> None:
    # OR-38: a truncated snapshot would break the resume checks; over the cap means no action.
    ledger = Ledger(tmp_path, task_id="task-1", started_at=5.0, max_record_bytes=512)
    step = _send()
    with pytest.raises(LedgerError, match="too large"):
        ledger.write_ahead(step, _snapshot(), _decision(ledger.key_for(step), "approve"))
    assert list(tmp_path.rglob("*.wal")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="the refusal is for other platforms")
def test_ledger_refuses_to_exist_without_dpapi(tmp_path: Path) -> None:  # pragma: no cover
    with pytest.raises(OSError, match="Windows"):
        Ledger(tmp_path, task_id="task-1", started_at=5.0)


def test_record_payload_is_json_with_a_version_before_encryption() -> None:
    record = WriteAheadRecord(
        task_id="task-1",
        step_id="s3",
        action_key="k",
        decision_id="d-1",
        phase="written",
        before=_snapshot(),
        written_at=7.0,
    )
    payload = json.loads(record.to_json())
    assert payload["version"] == 1
    assert payload["before"]["id"] == "before"
    assert payload["phase"] == "written"
    assert WriteAheadRecord.from_json(record.to_json()) == record


# ---- the runner's needs: one runner per task, a plan and a window bound to the task ----------


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def test_the_lock_names_this_process_and_goes_with_close(
    tmp_path: Path,
) -> None:
    first = Ledger(tmp_path, task_id="t", started_at=1.0)
    lock = next(tmp_path.iterdir()) / "lock"
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    first.close()
    assert not lock.exists()
    with Ledger(tmp_path, task_id="t", started_at=1.0) as again:
        assert again.task_id == "t"
        assert lock.exists()
    assert not lock.exists()
    Ledger(tmp_path, task_id="other", started_at=1.0).close()


def test_a_lock_held_by_a_dead_process_is_replaced_and_a_live_one_honoured(
    tmp_path: Path,
) -> None:
    Ledger(tmp_path, task_id="t", started_at=1.0).close()
    lock = next(tmp_path.iterdir()) / "lock"
    lock.write_text(json.dumps({"pid": _dead_pid()}), encoding="utf-8")
    Ledger(tmp_path, task_id="t", started_at=1.0).close()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock.write_text(json.dumps({"pid": sleeper.pid}), encoding="utf-8")
        with pytest.raises(LedgerError, match="ledger_locked"):
            Ledger(tmp_path, task_id="t", started_at=1.0)
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    Ledger(tmp_path, task_id="t", started_at=1.0).close()  # this process takes its own lock over


def test_process_alive_tells_a_running_process_from_a_dead_one() -> None:
    assert process_alive(os.getpid()) is True
    assert process_alive(_dead_pid()) is False


def test_the_plan_is_bound_to_the_task_id(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64)
        ledger.bind_plan("a" * 64)
    with Ledger(tmp_path, task_id="t", started_at=1.0) as ledger:
        with pytest.raises(LedgerError, match="plan_changed"):
            ledger.bind_plan("b" * 64)
        ledger.bind_plan("b" * 64, allow_change=True)
    with Ledger(tmp_path, task_id="t", started_at=1.0) as ledger:
        ledger.bind_plan("b" * 64)


def test_the_window_is_bound_to_the_task_id_by_pid_and_title_digest(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t", started_at=1.0) as ledger:
        ledger.bind_window(pid=500, title="Amir Khan - Chat")
        ledger.bind_window(pid=500, title="Amir Khan - Chat")
    with Ledger(tmp_path, task_id="t", started_at=1.0) as ledger:
        with pytest.raises(LedgerError, match="window_changed"):
            ledger.bind_window(pid=501, title="Amir Khan - Chat")
        with pytest.raises(LedgerError, match="window_changed"):
            ledger.bind_window(pid=500, title="Sara Ali - Chat")
        ledger.bind_window(pid=501, title="Sara Ali - Chat", allow_change=True)
    state = next(tmp_path.rglob("state.ledger")).read_bytes()
    assert b"Sara" not in state
    assert "Sara Ali - Chat".encode("utf-16-le") not in state


def test_a_superseded_instances_close_leaves_the_successors_lock(tmp_path: Path) -> None:
    first = Ledger(tmp_path, task_id="t", started_at=1.0)
    lock = next(tmp_path.iterdir()) / "lock"
    second = Ledger(tmp_path, task_id="t", started_at=1.0)  # same process: takes the lock over
    token = json.loads(lock.read_text(encoding="utf-8"))["token"]
    first.close()
    assert lock.exists(), "the first instance's close never removes the second's lock"
    assert json.loads(lock.read_text(encoding="utf-8"))["token"] == token
    second.close()
    assert not lock.exists()


# ---- opened windows and window references (packet open-multiwindow-2026-11-06, P7, P8, OW-33) --


def _open_step(step_id: str = "o1") -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb="open",
        target=Target(role="App", name="chrome"),
        risk="R1",
        params={"site": "youtube"},
    )


WINDOW = {"hwnd": 0x30, "pid": 500, "start": 133_000_000, "image": "chrome.exe", "title": "YouTube"}


def test_a_verified_open_is_done_at_any_tier_and_its_window_survives_a_restart(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path, task_id="t-open", started_at=1.0)
    step = _open_step()
    key = ledger.key_for(step)
    assert ledger.admit(key) == "fresh"
    ledger.record_open(step, WINDOW)
    assert ledger.admit(key) == "done"
    ledger.close()
    again = Ledger(tmp_path, task_id="t-open", started_at=2.0)
    window = again.opened_window(step)
    assert window is not None
    assert (window["hwnd"], window["pid"], window["start"], window["image"]) == (
        0x30,
        500,
        133_000_000,
        "chrome.exe",
    )
    assert window["title"] != "YouTube", "the title is stored as a digest, never in clear"
    again.close()


def test_two_identical_opens_have_their_own_keys(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-two", started_at=1.0) as ledger:
        assert ledger.key_for(_open_step("o1")) != ledger.key_for(_open_step("o2"))


def test_the_window_reference_is_part_of_the_key_only_when_set(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-ref", started_at=1.0) as ledger:
        step = _send()
        start_key = ledger.key_for(step)
        ledger.set_window_ref("opened:o1")
        opened_key = ledger.key_for(step)
        ledger.set_window_ref(None)
        assert ledger.key_for(step) == start_key
        assert opened_key != start_key


# ---- the plan chain of a replanned task (packet replan-2026-11-06, addendum items 2, 3) -------


def test_a_replan_extends_the_chain_and_only_the_last_link_resumes(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-chain", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64, step_ids=["o1"])
        ledger.set_window_ref("opened:o1")
        ledger.extend_plan("b" * 64, ["p2.s1"])
    with Ledger(tmp_path, task_id="t-chain", started_at=1.0) as ledger:
        with pytest.raises(LedgerError, match="plan_superseded"):
            ledger.bind_plan("a" * 64)
        with pytest.raises(LedgerError, match="plan_changed"):
            ledger.bind_plan("c" * 64)
        refused_ref = ledger.window_ref
        assert refused_ref is None
        ledger.bind_plan("b" * 64)
        assert ledger.window_ref == "opened:o1", "the link's window: keys match the first run"
        assert ledger.plan_count() == 2


def test_the_first_link_resumes_in_the_start_window(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-first", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64, step_ids=["o1", "s2"])
        ledger.set_window_ref("opened:o1")  # plan 1 opened a window, then the process died
    with Ledger(tmp_path, task_id="t-first", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64)
        assert ledger.window_ref is None, "steps before the open keyed without a window"


def test_step_ids_are_unique_across_the_chain_after_a_restart(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-ids", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64, step_ids=["s1"])
        with pytest.raises(LedgerError, match="step_id_reused"):
            ledger.extend_plan("b" * 64, ["s1"])
        ledger.extend_plan("b" * 64, ["p2.s1"])
    with Ledger(tmp_path, task_id="t-ids", started_at=1.0) as ledger:
        ledger.bind_plan("b" * 64)
        with pytest.raises(LedgerError, match="step_id_reused"):
            ledger.extend_plan("c" * 64, ["p2.s1"])
        with pytest.raises(LedgerError, match="step_id_reused"):
            ledger.extend_plan("c" * 64, ["s1"])


def test_a_chain_needs_a_first_plan_and_never_repeats_a_link(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-empty", started_at=1.0) as ledger:
        with pytest.raises(LedgerError, match="plan_unbound"):
            ledger.extend_plan("b" * 64, ["p2.s1"])
        ledger.bind_plan("a" * 64, step_ids=["s1"])
        with pytest.raises(LedgerError, match="plan_repeated"):
            ledger.extend_plan("a" * 64, ["p2.s1"])


def test_a_state_from_before_the_chain_loads_as_one_link(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-old", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64)
    state_path = next(tmp_path.rglob("state.ledger"))
    payload = json.loads(dpapi.unprotect(state_path.read_bytes(), entropy=b"steer-ledger-v1"))
    for key in ("plan_chain",):
        payload.pop(key, None)
    state_path.write_bytes(dpapi.protect(json.dumps(payload).encode(), entropy=b"steer-ledger-v1"))
    with Ledger(tmp_path, task_id="t-old", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64)
        ledger.extend_plan("b" * 64, ["p2.s1"])
        with pytest.raises(LedgerError, match="plan_superseded"):
            ledger.bind_plan("a" * 64)


def test_an_opened_window_is_found_by_its_reference_after_a_restart(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-byref", started_at=1.0) as ledger:
        ledger.record_open(_open_step("o1"), WINDOW)
    with Ledger(tmp_path, task_id="t-byref", started_at=1.0) as ledger:
        window = ledger.opened_by_ref("opened:o1")
        assert window is not None
        assert (window["hwnd"], window["pid"], window["image"]) == (0x30, 500, "chrome.exe")
        assert ledger.opened_by_ref("opened:o2") is None
        assert ledger.opened_by_ref("o1") is None, "only an opened: reference names a window"


def test_a_chained_task_never_rewrites_its_chain_even_when_a_change_is_allowed(
    tmp_path: Path,
) -> None:
    """Hunter RP-07: a replaced link would lose its window ref and every key with it."""
    with Ledger(tmp_path, task_id="t-allow", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64, step_ids=["o1"])
        ledger.set_window_ref("opened:o1")
        ledger.extend_plan("b" * 64, ["p2.s1"])
        with pytest.raises(LedgerError, match="plan_changed"):
            ledger.bind_plan("c" * 64, allow_change=True)
        with pytest.raises(LedgerError, match="plan_superseded"):
            ledger.bind_plan("a" * 64, allow_change=True)


def test_closed_by_names_the_step_that_closed_a_key(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-closed", started_at=1.0) as ledger:
        step = _open_step("o1")
        key = ledger.key_for(step)
        assert ledger.closed_by(key) is None
        ledger.record_open(step, WINDOW)
        assert ledger.closed_by(key) == "o1"


def test_unsettled_is_any_attempt_not_known_done_or_not_sent(tmp_path: Path) -> None:
    """Hunter RP-06: an uncertain attempt of an earlier run stops a replan too."""
    with Ledger(tmp_path, task_id="t-unsettled", started_at=1.0) as ledger:
        assert ledger.unsettled() is False
        ledger.record_open(_open_step("o1"), WINDOW)
        assert ledger.unsettled() is False
        ledger.record_open_attempt(_open_step("o2"), "delivered_unverified")
        assert ledger.unsettled() is True


def test_an_open_write_ahead_record_is_unsettled_and_nothing_sent_is_settled(
    tmp_path: Path,
) -> None:
    """RP-06 boundaries: a crash between write-ahead and outcome may have acted; a launch that
    delivered nothing did not."""
    with Ledger(tmp_path, task_id="t-wal", started_at=1.0) as ledger:
        ledger.record_open_attempt(_open_step("o1"), "not_delivered")
        assert ledger.unsettled() is False
        step = _send()
        key = ledger.key_for(step)
        ledger.write_ahead(step, _snapshot(), _decision(key, "approve"))
        assert ledger.unsettled() is True
    with Ledger(tmp_path, task_id="t-wal", started_at=1.0) as ledger:
        assert ledger.unsettled() is True, "an open record survives a restart"


def test_a_state_from_before_the_chain_is_bound_to_its_one_plan(tmp_path: Path) -> None:
    """Addendum item 3: the legacy digest is the chain's one link, not an empty chain."""
    with Ledger(tmp_path, task_id="t-legacy", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64)
    state_path = next(tmp_path.rglob("state.ledger"))
    payload = json.loads(dpapi.unprotect(state_path.read_bytes(), entropy=b"steer-ledger-v1"))
    payload.pop("plan_chain")
    state_path.write_bytes(dpapi.protect(json.dumps(payload).encode(), entropy=b"steer-ledger-v1"))
    with Ledger(tmp_path, task_id="t-legacy", started_at=1.0) as ledger:
        assert ledger.plan_count() == 1
        with pytest.raises(LedgerError, match="plan_changed"):
            ledger.bind_plan("c" * 64)


def test_a_resumed_link_records_its_step_ids_for_the_next_replan(tmp_path: Path) -> None:
    """A legacy link loads with no step ids; the resume's bind fills them, so a later replan
    cannot reuse one (addendum item 2)."""
    with Ledger(tmp_path, task_id="t-fill", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64)
    state_path = next(tmp_path.rglob("state.ledger"))
    payload = json.loads(dpapi.unprotect(state_path.read_bytes(), entropy=b"steer-ledger-v1"))
    payload.pop("plan_chain")
    state_path.write_bytes(dpapi.protect(json.dumps(payload).encode(), entropy=b"steer-ledger-v1"))
    with Ledger(tmp_path, task_id="t-fill", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64, step_ids=["s1"])
    with Ledger(tmp_path, task_id="t-fill", started_at=1.0) as ledger:
        ledger.bind_plan("a" * 64)
        with pytest.raises(LedgerError, match="step_id_reused"):
            ledger.extend_plan("b" * 64, ["s1"])


# ---- the start window's identity (packet step-window-2026-11-07, gate item 3, Q3) -----------

START_IDENTITY = {"hwnd": 0x10, "pid": 500, "start": 133_000_001, "image": "chrome.exe"}


def test_the_start_windows_identity_is_recorded_once_and_survives_a_restart(
    tmp_path: Path,
) -> None:
    with Ledger(tmp_path, task_id="t-start", started_at=1.0) as ledger:
        ledger.bind_window(pid=500, title="Amir Khan - Chat", identity=START_IDENTITY)
    with Ledger(tmp_path, task_id="t-start", started_at=1.0) as ledger:
        # a resume binds the same window again: the first identity stays (gate Q3)
        ledger.bind_window(
            pid=500, title="Amir Khan - Chat", identity={**START_IDENTITY, "start": 9}
        )
        assert ledger.start_window() == START_IDENTITY
    state = next(tmp_path.rglob("state.ledger")).read_bytes()
    assert b"Amir" not in state


def test_a_state_without_the_start_identity_reads_none(tmp_path: Path) -> None:
    with Ledger(tmp_path, task_id="t-old-start", started_at=1.0) as ledger:
        ledger.bind_window(pid=500, title="Amir Khan - Chat")
    with Ledger(tmp_path, task_id="t-old-start", started_at=1.0) as ledger:
        ledger.bind_window(pid=500, title="Amir Khan - Chat", identity=START_IDENTITY)
        assert ledger.start_window() is None, "never read on a resume (a reused pid would pass)"


def test_an_allowed_window_change_forgets_the_start_identity(tmp_path: Path) -> None:
    """SW-22, gate Q3: the start keys (ref None) belong to the old window, and the new identity
    would be a resume-time read; `window: start` then stops `window_gone`."""
    other = {**START_IDENTITY, "hwnd": 0x20, "pid": 501}
    with Ledger(tmp_path, task_id="t-change", started_at=1.0) as ledger:
        ledger.bind_window(pid=500, title="Amir Khan - Chat", identity=START_IDENTITY)
        ledger.bind_window(pid=501, title="Sara Ali - Chat", identity=other, allow_change=True)
        assert ledger.start_window() is None
