"""Task ledger (design 4 [v0.8], 5.11, orchestrator review OR-01/05/07/08/24/38): what this task
already did, so nothing irreversible runs twice.

Action keys are HMAC-SHA256 under a per-task secret that lives only in the encrypted ledger
state, over the verb, the target's identity (role, normalised name, automation id; never a
RuntimeId, which does not survive a reload) and the normalised parameters: stable across a
re-observe, not recoverable from a trace. A key is consumed when its write-ahead record is
written, before dispatch; the record then moves through the phases written → acting → verified.
A verified delivery closes the key; `not_delivered` (nothing sent) releases it for the loop's one
local retry; anything else, including a record with no outcome after a crash, is unknown and only
a fresh single-use `confirm` decision admits it. A retry after an uncertain outcome verifies
against the original persisted before. Unreadable state or records are unknown, never "never
ran". Everything on disk is DPAPI-encrypted: no DPAPI, no ledger, no action.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import GateDecision, Step, Target
from steerai.platform import dpapi
from steerai.world.model import Snapshot, normalise_name, snapshot_from_json, snapshot_to_json

Admission = Literal["fresh", "done", "confirm"]
Phase = Literal["written", "acting", "verified"]
RECORD_FORMAT = 1
DEFAULT_MAX_RECORD_BYTES = 2_000_000
"""Cap on a serialised before snapshot (OR-38). Unmeasured: a 460-node Chrome tree is to be
measured by a probe; a truncated snapshot is never written because it would break resume."""
_ENTROPY = b"steer-ledger-v1"
_DONE_OUTCOMES = frozenset({"delivered_verified", "done_already"})
_RETRY_OUTCOMES = frozenset({"not_delivered"})


class LedgerError(SteerError):
    """The ledger could not record or read: the action must not run (5.11 [v0.8])."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.GATE_BLOCKED, message)


def action_key(secret: bytes, verb: str, target: Target, params: Mapping[str, str]) -> str:
    """HMAC-SHA256 under the task secret over the verb, target identity and sorted, normalised
    parameters; the same action after a re-observe keys the same."""
    normalised = sorted((k, normalise_name(v)) for k, v in params.items())
    payload = json.dumps([verb, target.identity, normalised], ensure_ascii=False)
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class WriteAheadRecord:
    task_id: str
    step_id: str
    action_key: str
    decision_id: str
    phase: Phase
    before: Snapshot
    written_at: float

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": RECORD_FORMAT,
                "task_id": self.task_id,
                "step_id": self.step_id,
                "action_key": self.action_key,
                "decision_id": self.decision_id,
                "phase": self.phase,
                "written_at": self.written_at,
                "before": json.loads(snapshot_to_json(self.before)),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> WriteAheadRecord:
        try:
            payload = json.loads(text)
            if payload["version"] != RECORD_FORMAT:
                raise ValueError("unknown record version")
            phase = str(payload["phase"])
            if phase not in ("written", "acting", "verified"):
                raise ValueError("unknown phase")
            return cls(
                task_id=str(payload["task_id"]),
                step_id=str(payload["step_id"]),
                action_key=str(payload["action_key"]),
                decision_id=str(payload["decision_id"]),
                phase=phase,  # type: ignore[arg-type]  # validated above
                before=snapshot_from_json(json.dumps(payload["before"])),
                written_at=float(payload["written_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"write-ahead record is malformed: {type(exc).__name__}") from exc


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Ledger:
    def __init__(
        self,
        root: Path,
        *,
        task_id: str,
        started_at: float,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
    ) -> None:
        if sys.platform != "win32":
            raise OSError("the ledger needs DPAPI, which requires Windows")
        self.task_id = task_id
        self.started_at = started_at
        self._max_record_bytes = max_record_bytes
        self._dir = root / _digest(task_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._dir / "state.ledger"
        self._executions: dict[str, dict[str, Any]] = {}
        self._secret = b""
        if self._state_path.exists():
            self._load_state()
        else:
            self._secret = os.urandom(32)
            self._save_state()

    # ---- keys and admission ------------------------------------------------------------------

    def key_for(self, step: Step) -> str:
        if step.target is None:  # enforced by Step.__post_init__; kept for the type checker
            raise LedgerError("a step without a target has no action key")
        params = dict(step.params)
        if step.text is not None:
            params["text"] = step.text
        return action_key(self._secret, step.verb, step.target, params)

    def admit(self, key: str, decision: GateDecision | None = None) -> Admission:
        """Whether `key` may execute now: fresh (never ran, or nothing was delivered), done
        (verified in this task), or confirm (unknown or uncertain: only a fresh single-use
        `confirm` decision admits it)."""
        record = self._executions.get(key)
        if record is None:
            return "fresh"
        outcome = record.get("outcome")
        if outcome in _RETRY_OUTCOMES:
            return "fresh"
        if outcome in _DONE_OUTCOMES:
            return "done"
        if decision is not None and decision.outcome == "confirm" and decision.consume(key):
            return "fresh"
        return "confirm"

    # ---- write-ahead records -----------------------------------------------------------------

    def _record_path(self, step_id: str) -> Path:
        return self._dir / f"{_digest(step_id)}.wal"

    def write_ahead(
        self, step: Step, before: Snapshot, decision: GateDecision | None
    ) -> WriteAheadRecord:
        """Consume the key and persist the before snapshot, before any R2/R3 action; a failed
        write means no action and no consumed key."""
        key = self.key_for(step)
        if decision is None or decision.action_key != key or decision.outcome == "block":
            raise LedgerError("a write-ahead record needs the gate decision bound to this key")
        if not decision.consumed and not decision.consume(key):
            raise LedgerError("the gate decision could not be consumed for this key")
        record = WriteAheadRecord(
            task_id=self.task_id,
            step_id=step.id,
            action_key=key,
            decision_id=decision.decision_id,
            phase="written",
            before=before,
            written_at=time.time(),
        )
        text = record.to_json()
        if len(text.encode()) > self._max_record_bytes:
            raise LedgerError("write-ahead record too large: the before snapshot is over the cap")
        path = self._record_path(step.id)
        try:
            self._write_encrypted(path, text)
        except OSError as exc:
            raise LedgerError(f"write-ahead record could not be written: {exc}") from exc
        self._executions[key] = {
            "step_id": step.id,
            "decision_id": decision.decision_id,
            "phase": "written",
            "outcome": None,
        }
        try:
            self._save_state()
        except LedgerError:
            path.unlink(missing_ok=True)
            raise
        return record

    def mark(self, step: Step, phase: Phase) -> None:
        """Advance the record's phase (acting once dispatched, verified once T1 ran)."""
        record = self.read_ahead(step)
        if record is None:
            raise LedgerError(f"no write-ahead record for step {step.id}")
        self._rewrite(replace(record, phase=phase))

    def record_outcome(self, step: Step, outcome: str) -> None:
        """Close the attempt with the executor's delivery or the final code (phase verified)."""
        record = self.read_ahead(step)
        if record is None:
            raise LedgerError(f"no write-ahead record for step {step.id}")
        self._rewrite(replace(record, phase="verified"), outcome=outcome)

    def read_ahead(self, step: Step) -> WriteAheadRecord | None:
        """This task's record for `step`, or None; a record of another task is ignored (SM-51);
        an unreadable one is unknown (OR-08), raised, never treated as absent."""
        path = self._record_path(step.id)
        if not path.exists():
            return None
        try:
            record = WriteAheadRecord.from_json(self._read_encrypted(path))
        except (OSError, ValueError) as exc:
            raise LedgerError(f"write-ahead record unreadable: {exc}") from exc
        if record.task_id != self.task_id or record.step_id != step.id:
            return None
        return record

    def before_of_record(self, step: Step, fresh: Snapshot) -> Snapshot:
        """The before a (re)try is verified against: the persisted one when a record exists
        (OR-05: the first attempt may have landed late), else the fresh snapshot."""
        record = self.read_ahead(step)
        return fresh if record is None else record.before

    def _rewrite(self, record: WriteAheadRecord, *, outcome: str | None = None) -> None:
        try:
            self._write_encrypted(self._record_path(record.step_id), record.to_json())
        except OSError as exc:
            raise LedgerError(f"write-ahead record could not be updated: {exc}") from exc
        entry = self._executions.setdefault(
            record.action_key,
            {"step_id": record.step_id, "decision_id": record.decision_id, "outcome": None},
        )
        entry["phase"] = record.phase
        if outcome is not None:
            entry["outcome"] = outcome
        self._save_state()

    # ---- encrypted files -----------------------------------------------------------------------

    def _write_encrypted(self, path: Path, text: str) -> None:
        blob = dpapi.protect(text.encode(), entropy=_ENTROPY)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(blob)
        temporary.replace(path)

    def _read_encrypted(self, path: Path) -> str:
        return dpapi.unprotect(path.read_bytes(), entropy=_ENTROPY).decode()

    def _load_state(self) -> None:
        try:
            payload: dict[str, Any] = json.loads(self._read_encrypted(self._state_path))
            if payload.get("task_id") != self.task_id:
                raise ValueError("ledger state belongs to another task")
            self.started_at = float(payload["started_at"])
            self._secret = bytes.fromhex(str(payload["secret"]))
            self._executions = {str(k): dict(v) for k, v in payload["executions"].items()}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LedgerError(f"ledger state unreadable: {exc}") from exc

    def _save_state(self) -> None:
        payload = {
            "version": RECORD_FORMAT,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "secret": self._secret.hex(),
            "executions": self._executions,
        }
        try:
            self._write_encrypted(self._state_path, json.dumps(payload, separators=(",", ":")))
        except OSError as exc:
            raise LedgerError(f"ledger state could not be written: {exc}") from exc
