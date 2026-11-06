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
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import GateDecision, Step, Target
from steerai.platform import dpapi
from steerai.probes.windows import process_alive
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


def key_params(step: Step, window_ref: str | None) -> dict[str, str]:
    """The parameters an action key covers: the step's own, its text, an `open`'s step id
    (two identical opens are two actions, OW-33) and the window the step acts in once the task
    left its start window (`opened:<step id>`, schema gate P8)."""
    params = dict(step.params)
    if step.text is not None:
        params["text"] = step.text
    if step.verb == "open":
        params["_step"] = step.id
    if window_ref is not None:
        params["_window"] = window_ref
    return params


OPENED_WINDOW_KEYS = ("hwnd", "pid", "start", "image", "title")
"""What a verified `open` records: the identity a resume re-binds by (P7, decision 3)."""


@dataclass(frozen=True, slots=True)
class WriteAheadRecord:
    task_id: str
    step_id: str
    action_key: str
    decision_id: str
    phase: Phase
    before: Snapshot
    written_at: float
    chosen: dict[str, Any] | None = field(default=None, repr=False)
    """The identity a person target was bound to (role, RuntimeId, name part, full name),
    so a resume never dispatches to another row (PT-05)."""

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
                "chosen": self.chosen,
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
                chosen=payload.get("chosen") if isinstance(payload.get("chosen"), dict) else None,
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
        self._lock_path = self._dir / "lock"
        self._executions: dict[str, dict[str, Any]] = {}
        self._secret = b""
        self._chain: list[dict[str, Any]] = []
        """The task's plans in order (replan [v2.1]): digest, step ids, the window ref in effect."""
        self._window: dict[str, Any] | None = None
        self._start_window: dict[str, Any] | None = None
        self.window_ref: str | None = None
        """The window steps act in once the task left its start window; a resumed link restores
        the ref it started with, so its keys match the first run's (replan item 3)."""
        self._lock_token = uuid.uuid4().hex
        self._locked = False
        self._acquire_lock()
        try:
            if self._state_path.exists():
                self._load_state()
            else:
                self._secret = os.urandom(32)
                self._save_state()
        except LedgerError:
            self.close()
            raise

    # ---- one runner per task (RN-12) -----------------------------------------------------

    def _acquire_lock(self) -> None:
        for _ in range(2):
            try:
                fd = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                holder = self._lock_holder()
                if holder is not None and holder != os.getpid() and process_alive(holder):
                    raise LedgerError(
                        f"ledger_locked: another runner (pid {holder}) holds this task; "
                        f"the lock is {self._lock_path}"
                    ) from None
                self._lock_path.unlink(missing_ok=True)  # stale: the holder is gone
                continue
            except OSError as exc:
                raise LedgerError(f"ledger lock could not be taken: {exc}") from exc
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "token": self._lock_token}, handle)
            self._locked = True
            return
        raise LedgerError("ledger_locked: the lock could not be taken")

    def _lock_token_on_disk(self) -> str | None:
        try:
            payload = json.loads(self._lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        token = payload.get("token") if isinstance(payload, dict) else None
        return token if isinstance(token, str) else None

    def _lock_holder(self) -> int | None:
        try:
            payload = json.loads(self._lock_path.read_text(encoding="utf-8"))
            pid = payload["pid"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return pid if isinstance(pid, int) and not isinstance(pid, bool) else None

    def close(self) -> None:
        """Release the task lock; the state stays on disk for a resume. The lock guards against
        another runner process; a second ledger in this process takes it over."""
        if self._locked:
            if self._lock_token_on_disk() == self._lock_token:
                self._lock_path.unlink(missing_ok=True)
            self._locked = False

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- what the task is bound to (RN-16/17) ----------------------------------------

    def bind_plan(
        self, digest: str, *, allow_change: bool = False, step_ids: Iterable[str] = ()
    ) -> None:
        """A task id runs one chain of plans and resumes only its last link; a different plan
        is refused unless the caller says the change is deliberate (it then replaces the last
        link). Binding a link restores the window ref it started in."""
        ids = sorted(set(step_ids))
        last = self._chain[-1] if self._chain else None
        if last is not None and last["digest"] == digest:
            self.window_ref = last["window_ref"]
            if not set(ids) <= set(last["step_ids"]):
                last["step_ids"] = sorted(set(last["step_ids"]) | set(ids))
                self._save_state()
            return
        if last is not None and (not allow_change or len(self._chain) > 1):
            # a replanned task's links carry its window refs: never rewritten (RP-07)
            if any(link["digest"] == digest for link in self._chain):
                raise LedgerError("plan_superseded: a later plan of this task replaced it")
            raise LedgerError("plan_changed: this task id already ran a different plan")
        link: dict[str, Any] = {"digest": digest, "step_ids": ids, "window_ref": None}
        if last is None:
            self._chain = [link]
        else:
            link["window_ref"] = last["window_ref"]
            self._chain[-1] = link
        self.window_ref = link["window_ref"]
        self._save_state()

    def plan_count(self) -> int:
        """How many plans the task's chain holds; the bound one is the last."""
        return len(self._chain)

    def extend_plan(self, digest: str, step_ids: Iterable[str]) -> None:
        """A replan: the successor of the last link, in the window the task is in now; a step
        id the task already used is refused (its write-ahead path is the id's digest)."""
        if not self._chain:
            raise LedgerError("plan_unbound: a replan needs the task's first plan")
        if any(link["digest"] == digest for link in self._chain):
            raise LedgerError("plan_repeated: this plan is already a link of the task")
        ids = set(step_ids)
        used = {i for link in self._chain for i in link["step_ids"]}
        if ids & used:
            raise LedgerError("step_id_reused: a step id of an earlier plan of this task")
        self._chain.append(
            {"digest": digest, "step_ids": sorted(ids), "window_ref": self.window_ref}
        )
        self._save_state()

    def bind_window(
        self,
        *,
        pid: int,
        title: str,
        allow_change: bool = False,
        identity: Mapping[str, Any] | None = None,
    ) -> None:
        """A task's done keys mean nothing in another window: the pid and a title digest
        are bound on the first run and must match on a resume. The first bind also records
        the window's identity (handle, pid, process start, image) for `window: start`; a
        resume never records it (a reused pid would pass, step-window gate Q3)."""
        window = {"pid": pid, "title": _digest(title)}
        if self._window not in (None, window) and not allow_change:
            raise LedgerError("window_changed: this task id was bound to another window")
        if self._window != window:
            first = self._window is None
            self._window = window
            # only the first bind records it; a changed window forgets it (SW-22, gate Q3)
            self._start_window = (
                {k: identity[k] for k in OPENED_WINDOW_KEYS[:4]}
                if first and identity is not None
                else None
            )
            self._save_state()

    def start_window(self) -> dict[str, Any] | None:
        """The start window's recorded identity, None for a state written before it."""
        return None if self._start_window is None else dict(self._start_window)

    # ---- keys and admission ------------------------------------------------------------------

    def set_window_ref(self, ref: str | None) -> None:
        self.window_ref = ref

    def key_for(self, step: Step) -> str:
        if step.target is None:  # enforced by Step.__post_init__; kept for the type checker
            raise LedgerError("a step without a target has no action key")
        return action_key(self._secret, step.verb, step.target, key_params(step, self.window_ref))

    def record_open(self, step: Step, window: Mapping[str, Any]) -> None:
        """A verified `open`, at any tier: done, with the window it opened (title digested)."""
        key = self.key_for(step)
        entry = dict(self._executions.get(key) or {"step_id": step.id})
        entry.update(
            phase="verified",
            outcome="delivered_verified",
            window={
                **{k: window[k] for k in OPENED_WINDOW_KEYS[:4]},
                "title": _digest(str(window["title"])),
            },
        )
        self._executions[key] = entry
        self._save_state()

    def record_open_attempt(self, step: Step, outcome: str) -> None:
        """A launched `open` that was not verified, at any tier: not done, and a resume must
        confirm before launching again (a window may already be open)."""
        key = self.key_for(step)
        entry = dict(self._executions.get(key) or {"step_id": step.id})
        entry.update(phase="verified", outcome=outcome)
        self._executions[key] = entry
        self._save_state()

    def opened_window(self, step: Step) -> dict[str, Any] | None:
        entry = self._executions.get(self.key_for(step))
        window = None if entry is None else entry.get("window")
        return None if window is None else dict(window)

    def opened_records(self, ref: str) -> list[dict[str, Any]]:
        """Every window recorded under an `opened:<step id>` ref: more than one means a
        replaced plan reused the id (SW-04), which selection refuses."""
        return _opened_records(self._executions, ref)

    def opened_by_ref(self, ref: str) -> dict[str, Any] | None:
        """The window an `opened:<step id>` ref names (step ids are unique in a task)."""
        return _opened_by_ref(self._executions, ref)

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

    def outcome_of(self, key: str) -> str | None:
        """The recorded outcome for `key`, None while an attempt is open or never ran."""
        record = self._executions.get(key)
        return None if record is None else record.get("outcome")

    def closed_by(self, key: str) -> str | None:
        """The step id whose attempt `key` records, None if it never ran."""
        record = self._executions.get(key)
        return None if record is None else record.get("step_id")

    def unsettled(self) -> bool:
        """Any attempt of this task that may have had an effect nobody verified: an open
        record or an outcome other than done or nothing sent (replan RP-06)."""
        settled = _DONE_OUTCOMES | _RETRY_OUTCOMES
        return any(entry.get("outcome") not in settled for entry in self._executions.values())

    # ---- write-ahead records -----------------------------------------------------------------

    def _record_path(self, step_id: str) -> Path:
        return self._dir / f"{_digest(step_id)}.wal"

    def write_ahead(
        self,
        step: Step,
        before: Snapshot,
        decision: GateDecision | None,
        chosen: dict[str, Any] | None = None,
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
            chosen=chosen,
        )
        text = record.to_json()
        if len(text.encode()) > self._max_record_bytes:
            raise LedgerError("write-ahead record too large: the before snapshot is over the cap")
        path = self._record_path(step.id)
        try:
            self._write_encrypted(path, text)
        except OSError as exc:
            raise LedgerError(f"write-ahead record could not be written: {exc}") from exc
        previous = self._executions.get(key)
        self._executions[key] = {
            "step_id": step.id,
            "decision_id": decision.decision_id,
            "phase": "written",
            "outcome": None,
        }
        try:
            self._save_state()
        except LedgerError:
            # no action and no consumed key: this instance must agree with the disk
            if previous is None:
                del self._executions[key]
            else:
                self._executions[key] = previous
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
            self._chain = _loaded_chain(payload)
            window = payload.get("window")
            self._window = None if window is None else dict(window)
            start = payload.get("start_window")
            self._start_window = None if start is None else dict(start)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LedgerError(f"ledger state unreadable: {exc}") from exc

    def _save_state(self) -> None:
        payload = {
            "version": RECORD_FORMAT,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "secret": self._secret.hex(),
            "executions": self._executions,
            "plan_digest": self._chain[-1]["digest"] if self._chain else None,
            "plan_chain": self._chain,
            "window": self._window,
            "start_window": self._start_window,
        }
        try:
            self._write_encrypted(self._state_path, json.dumps(payload, separators=(",", ":")))
        except OSError as exc:
            raise LedgerError(f"ledger state could not be written: {exc}") from exc


def _opened_records(executions: Mapping[str, Mapping[str, Any]], ref: str) -> list[dict[str, Any]]:
    if not ref.startswith("opened:"):
        return []
    step_id = ref.removeprefix("opened:")
    return [
        dict(entry["window"])
        for entry in executions.values()
        if entry.get("step_id") == step_id and isinstance(entry.get("window"), dict)
    ]


def _opened_by_ref(executions: Mapping[str, Mapping[str, Any]], ref: str) -> dict[str, Any] | None:
    if not ref.startswith("opened:"):
        return None
    step_id = ref.removeprefix("opened:")
    for entry in executions.values():
        window = entry.get("window")
        if entry.get("step_id") == step_id and isinstance(window, dict):
            return dict(window)
    return None


def _loaded_chain(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The persisted chain; a state written before the chain is a one-link chain."""
    chain = payload.get("plan_chain")
    if chain is None:
        digest = payload.get("plan_digest")
        return (
            [] if digest is None else [{"digest": str(digest), "step_ids": [], "window_ref": None}]
        )
    if not isinstance(chain, list):
        raise ValueError("plan chain is not a list")
    links = []
    for link in chain:
        ref = link["window_ref"]
        links.append(
            {
                "digest": str(link["digest"]),
                "step_ids": [str(i) for i in link["step_ids"]],
                "window_ref": None if ref is None else str(ref),
            }
        )
    return links
