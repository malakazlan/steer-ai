"""Task ledger (design 4 [v0.8], 5.11, orchestrator review OR-01/05/07/08/24): keys consumed at
write-ahead time and persisted, phased records, unreadable means unknown, keys keyed by a
per-task secret."""

from __future__ import annotations

import json
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
