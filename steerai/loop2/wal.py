"""The v2 write-ahead file ([V2.88] L9, L11; [V2.15] Q1, Q9; V2 §8.4; design 5.11).

A file of its own, beside v1's ledger and never read by it (L9): `<root>/<task digest>.v2wal`,
under the directory the caller passes, with a lock file `<task digest>.v2lock` beside it. v1's
ledger keeps a directory per task (`<root>/<task digest>/`), so the two never share a path.

**Format.** Append-only lines. Each line is one event, JSON, encrypted by the codec (DPAPI by
default: the before snapshot holds names, which are user data), base64, then `\\n`. Events:
`head` (the task's digest and the key secret, once, first), `write` (a record: key, subgoal id,
decision id, verb, role, before snapshot), `acting` (written just before the dispatch is
called), `void` (the dispatch was never called: closed `not_delivered`), `outcome`. A typed
text never reaches the file; it is only inside the keyed HMAC.

**Durability (L11).** An event is on disk before the call that writes it returns: the whole line
is written (short writes completed), then `os.fsync`, which on Windows is the CRT's `_commit`,
"forces the operating system to write the file associated with fd to disk"
(docs.python.org/3.12/library/os.html#os.fsync; learn.microsoft.com/cpp/c-runtime-library/
reference/commit). `_O_APPEND` only "moves the file pointer to the end of the file before every
write operation" (learn.microsoft.com/cpp/c-runtime-library/reference/open-wopen): an append is
not atomic, so nothing relies on it being atomic. A crash mid-line leaves a torn tail, and a torn
tail, like any line that does not decode, fails closed: the start and the listing refuse
(`ledger_unreadable`), never skip it. A failed write breaks the ledger: it never writes again.

**One writer per task id.** The lock file is locked exclusively, non-blocking (`LockFileEx`
through `msvcrt.locking` on Windows, `flock` elsewhere). A second handle is refused even in the
same process, and the OS releases the lock when the holder's handle closes or its process ends
(learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-lockfileex, Remarks; measured here:
a second handle in one process is refused with EACCES). The remarks add that the release after
a process ends is timed by the OS, so a start right after a crash may be refused
`ledger_locked`: that fails closed.

**No resume (Q9).** `start` reads the file under the lock; a record whose key is not settled
refuses the start and names its verb and role, and nothing is written. An open record is never
read as `not delivered` at a start ([V2.90] (3)): only the gate's `void`, written on the branch
where the dispatch was never called, closes a record that way.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Final, Literal, get_args

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.claims import Phase
from steerai.loop2.decision import ActionDoc, Verb
from steerai.loop2.record import (
    OUTCOMES,
    SETTLED,
    StartRefusal,
    V2Record,
    admission,
    key_for,
    on_kill,
    open_record,
    start_refusal,
    void_token,
)
from steerai.orchestrator.contracts import GateDecision, Risk
from steerai.orchestrator.ledger import DEFAULT_MAX_RECORD_BYTES, Admission
from steerai.platform import dpapi
from steerai.world.elements import Element
from steerai.world.model import Snapshot, snapshot_from_json, snapshot_to_json

if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    _BINARY: Final = os.O_BINARY
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    _BINARY: Final = 0

FORMAT: Final = 1
_ENTROPY: Final = b"steer-ledger-v2"
_SHA256: Final = re.compile("[0-9a-f]{64}")
_VERBS: Final = frozenset(get_args(Verb))
EXIT_UNREADABLE: Final = 3
"""`--list-unsettled`: the file could not be read (torn, malformed, another task's)."""

WalToken = Literal[
    "wal_unavailable",
    "ledger_locked",
    "ledger_unreadable",
    "record_too_large",
    "record_unknown",
    "record_refused",
    "decision_refused",
]

_FIELDS: Final[dict[str, frozenset[str]]] = {
    "head": frozenset({"v", "e", "task", "secret"}),
    "write": frozenset({"v", "e", "seq", "key", "subgoal", "decision", "verb", "role", "before"}),
    "acting": frozenset({"v", "e", "seq"}),
    "void": frozenset({"v", "e", "seq"}),
    "outcome": frozenset({"v", "e", "seq", "outcome"}),
}
"""Every event's exact fields: anything more or less is a malformed line."""


class WalError(SteerError):
    """The v2 ledger could not record or read: no R2/R3 action may run (5.11 [v0.8])."""

    def __init__(self, token: WalToken, detail: str) -> None:
        super().__init__(ErrorCode.GATE_BLOCKED, f"{token}: {detail}")
        self.token: WalToken = token


@dataclass(frozen=True)
class Codec:
    """How a line is sealed before it touches disk; DPAPI unless a test passes its own."""

    protect: Callable[[bytes], bytes]
    unprotect: Callable[[bytes], bytes]


DPAPI: Final = Codec(
    protect=partial(dpapi.protect, entropy=_ENTROPY),
    unprotect=partial(dpapi.unprotect, entropy=_ENTROPY),
)


@dataclass(frozen=True)
class UnsettledRow:
    """One unsettled record for the operator: closed facts and ids only, never a name or text."""

    seq: int
    subgoal: str
    verb: str
    role: str
    phase: Phase
    outcome: str | None


@dataclass
class _Entry:
    record: V2Record
    outcome: str | None = None
    issued: list[V2Record] = field(default_factory=list)
    """Every record object this ledger handed out for the entry: any of them names it."""


def _task_digest(task_id: str) -> str:
    return hashlib.sha256(task_id.encode()).hexdigest()


def ledger_path(root: Path, task_id: str) -> Path:
    """The task's v2 file under `root`; its name is a digest, never the task id."""
    return root / f"{_task_digest(task_id)[:16]}.v2wal"


def _lock_path(root: Path, task_id: str) -> Path:
    return root / f"{_task_digest(task_id)[:16]}.v2lock"


# ---- the rules every event obeys, live and on replay -------------------------------------------


def _acting(entry: _Entry) -> None:
    if entry.record.phase != "written" or entry.outcome is not None:
        raise ValueError("a record is marked acting only from written, with no outcome")
    entry.record = replace(entry.record, phase="acting")


def _void(entry: _Entry) -> None:
    if entry.record.phase != "written" or entry.outcome is not None:
        raise ValueError("only a written record with no outcome can be voided")
    entry.outcome = void_token(entry.record)


def _outcome(entry: _Entry, outcome: str) -> None:
    if outcome not in OUTCOMES:
        raise ValueError("the outcome is not in the closed set")
    if entry.record.phase == "written" and outcome != "unknown":
        # only the kill settles a written record, and as `unknown` ([V2.17] item 5)
        raise ValueError("an outcome needs the record acting: the dispatch was not called")
    if entry.outcome in SETTLED:
        raise ValueError("a settled outcome is final")
    if outcome == "not_delivered" and entry.outcome is not None:
        raise ValueError("not_delivered is only a record's first outcome")
    entry.outcome = outcome
    entry.record = replace(entry.record, phase="verified")


@dataclass
class _State:
    secret: bytes
    entries: list[_Entry] = field(default_factory=list)

    def outcomes(self) -> dict[str, str | None]:
        """Each key's outcome, from its latest record."""
        return {e.record.action_key: e.outcome for e in self.entries}

    def refusal(self) -> StartRefusal | None:
        return start_refusal([e.record for e in self.entries], self.outcomes())


def _seq(event: dict[str, Any], state: _State) -> _Entry:
    seq = event["seq"]
    if type(seq) is not int or not 0 <= seq < len(state.entries):
        raise ValueError("an event names no record")
    return state.entries[seq]


def _text(value: object, *, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError("a field is not a short text")
    return value


def _apply(event: dict[str, Any], state: _State) -> None:
    kind = event["e"]
    if kind == "write":
        if type(event["seq"]) is not int or event["seq"] != len(state.entries):
            raise ValueError("records are numbered in order")
        key, verb = _text(event["key"]), _text(event["verb"])
        if not _SHA256.fullmatch(key) or verb not in _VERBS:
            raise ValueError("a record's key or verb is malformed")
        record = V2Record(
            action_key=key,
            subgoal=_text(event["subgoal"]),
            decision_id=_text(event["decision"]),
            verb=verb,
            role=_text(event["role"]),
            phase="written",
            before=snapshot_from_json(json.dumps(event["before"])),
        )
        state.entries.append(_Entry(record=record))
    elif kind == "acting":
        _acting(_seq(event, state))
    elif kind == "void":
        _void(_seq(event, state))
    elif kind == "outcome":
        _outcome(_seq(event, state), _text(event["outcome"]))
    else:
        raise ValueError("an event of an unknown kind")


def _decode(line: bytes, codec: Codec) -> dict[str, Any]:
    payload = json.loads(codec.unprotect(base64.b64decode(line, validate=True)))
    if not isinstance(payload, dict) or payload.get("v") != FORMAT:
        raise ValueError("a line is not an event of this format")
    if set(payload) != _FIELDS.get(str(payload.get("e")), frozenset()):
        raise ValueError("an event's fields are not its kind's")
    return payload


def _replay(path: Path, task_id: str, codec: Codec) -> _State:
    """The file's state, every line checked; anything torn or malformed raises
    `ledger_unreadable`, never skipped."""
    try:
        data = path.read_bytes()
        if not data.endswith(b"\n"):
            raise ValueError("the file is empty or its last line is torn")
        lines = data[:-1].split(b"\n")
        head = _decode(lines[0], codec)
        if head["e"] != "head" or head["task"] != _task_digest(task_id):
            raise ValueError("the file is not this task's")
        secret = bytes.fromhex(_text(head["secret"]))
        if len(secret) != 32:
            raise ValueError("the key secret is malformed")
        state = _State(secret=secret)
        for line in lines[1:]:
            _apply(_decode(line, codec), state)
    except (OSError, ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise WalError("ledger_unreadable", f"the v2 ledger fails closed: {exc}") from exc
    return state


def _line(event: dict[str, Any], codec: Codec) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(codec.protect(payload)) + b"\n"


# ---- the ledger --------------------------------------------------------------------------------


class V2Ledger:
    """One task's v2 records, held under its lock; build it with `start`."""

    def __init__(  # noqa: PLR0913 - the open handles and the replayed state, from `start`
        self,
        path: Path,
        *,
        fd: int,
        lock_fd: int,
        state: _State,
        codec: Codec,
        max_record_bytes: int,
    ) -> None:
        self.path = path
        self._fd: int | None = fd
        self._lock_fd: int | None = lock_fd
        self._state = state
        self._codec = codec
        self._max_record_bytes = max_record_bytes
        self._broken = False

    # ---- keys and admission ----------------------------------------------------------------

    def key_for(self, action: ActionDoc, element: Element, *, text: str | None) -> str:
        """`record.key_for` under this task's secret; ValueError on an unkeyable action."""
        return key_for(self._state.secret, action, element, text=text)

    def admit(self, key: str, risk: Risk) -> Admission:
        """`record.admission` from the key's latest record in this task's file."""
        outcomes = self._state.outcomes()
        return admission(risk, outcomes.get(key), recorded=key in outcomes)

    def refusal(self) -> StartRefusal | None:
        """The unsettled record that would refuse the next start, if any."""
        return self._state.refusal()

    # ---- write-ahead -------------------------------------------------------------------------

    def write_ahead(  # noqa: PLR0913 - the record's facts, keyword-only
        self,
        *,
        action_key: str,
        subgoal: str,
        verb: str,
        role: str,
        before: Snapshot,
        decision: GateDecision,
    ) -> V2Record:
        """§3.4 step 10: consume `decision` and put the record on disk before returning. A
        failure raises `WalError` and the caller dispatches nothing (blocked
        `wal_unavailable`)."""
        self._writable()
        before_json = json.loads(snapshot_to_json(before))
        event: dict[str, Any] = {
            "v": FORMAT,
            "e": "write",
            "seq": len(self._state.entries),
            "key": action_key,
            "subgoal": subgoal,
            "decision": decision.decision_id,
            "verb": verb,
            "role": role,
            "before": before_json,
        }
        size = len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode())
        if size > self._max_record_bytes:
            raise WalError("record_too_large", "the before snapshot is over the record cap")
        try:
            record = open_record(
                action_key=action_key,
                subgoal=subgoal,
                verb=verb,
                role=role,
                before=before,
                decision=decision,
            )
        except ValueError as exc:
            raise WalError("decision_refused", str(exc)) from exc
        self._append(event)
        self._state.entries.append(_Entry(record=record, issued=[record]))
        return record

    def mark_acting(self, record: V2Record) -> V2Record:
        """Written durably just before the dispatch is called: from here on, nothing proves the
        dispatch did not run."""
        return self._step(record, {"e": "acting"}, _acting)

    def void(self, record: V2Record) -> V2Record:
        """[V2.90] (3): the dispatch was never called (input after the write-ahead, a
        takeover): close the record `not_delivered`. The decision stays consumed."""
        return self._step(record, {"e": "void"}, _void)

    def close_record(self, record: V2Record, outcome: str) -> V2Record:
        """§3.4 step 11: the delivery token, then the effect's or the proof's; a settled
        outcome is final."""
        return self._step(
            record, {"e": "outcome", "outcome": outcome}, lambda e: _outcome(e, outcome)
        )

    def settle_on_kill(self) -> int:
        """[V2.17] item 5: every record with no outcome closes `unknown`; how many did."""
        settled = 0
        for entry in self._state.entries:
            if entry.outcome is None:
                self.close_record(entry.record, on_kill(entry.record))
                settled += 1
        return settled

    def _step(
        self, record: V2Record, fields: dict[str, Any], rule: Callable[[_Entry], None]
    ) -> V2Record:
        self._writable()
        seq, entry = self._find(record)
        trial = _Entry(record=entry.record, outcome=entry.outcome)
        try:
            rule(trial)
        except ValueError as exc:
            raise WalError("record_refused", str(exc)) from exc
        self._append({"v": FORMAT, **fields, "seq": seq})
        entry.record, entry.outcome = trial.record, trial.outcome
        entry.issued.append(trial.record)
        return trial.record

    def _find(self, record: V2Record) -> tuple[int, _Entry]:
        for seq, entry in enumerate(self._state.entries):
            if entry.record is record or any(r is record for r in entry.issued):
                return seq, entry
        raise WalError("record_unknown", "the record was not written by this ledger")

    # ---- the file ----------------------------------------------------------------------------

    def _writable(self) -> None:
        if self._fd is None or self._broken:
            raise WalError("wal_unavailable", "the v2 ledger is closed or broken")

    def _append(self, event: dict[str, Any]) -> None:
        if self._fd is None:  # `_writable` ran first; kept for the type checker
            raise WalError("wal_unavailable", "the v2 ledger is closed")
        try:
            view = memoryview(_line(event, self._codec))
            while view:
                written = os.write(self._fd, view)
                if written <= 0:
                    raise OSError("a write made no progress")
                view = view[written:]
            os.fsync(self._fd)
        except OSError as exc:
            self._broken = True  # a tail may be torn: the next start reads it and refuses
            raise WalError("wal_unavailable", f"not durable: {type(exc).__name__}") from exc

    def close(self) -> None:
        """Release the file and the task lock. Open records stay open on disk, so the next
        start refuses. Never raises: closing a handle releases its lock anyway."""
        fd, self._fd = self._fd, None
        lock_fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            with contextlib.suppress(OSError):  # an already-closed handle: nothing to release
                os.close(fd)
        if lock_fd is not None:
            _release(lock_fd)

    def __enter__(self) -> V2Ledger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _release(lock_fd: int) -> None:
    with contextlib.suppress(OSError):  # the close below releases the lock too (LockFileEx)
        _unlock(lock_fd)
    with contextlib.suppress(OSError):  # an already-closed handle: nothing to release
        os.close(lock_fd)


def _take_lock(root: Path, task_id: str) -> int:
    try:
        lock_fd = os.open(_lock_path(root, task_id), os.O_RDWR | os.O_CREAT | _BINARY)
    except OSError as exc:
        raise WalError("wal_unavailable", f"no lock file: {type(exc).__name__}") from exc
    try:
        _lock(lock_fd)
    except OSError as exc:
        os.close(lock_fd)
        raise WalError("ledger_locked", "another runner holds this task") from exc
    return lock_fd


def _create(path: Path, task_id: str, codec: Codec) -> tuple[int, _State]:
    """The file with its head on disk; a crash before the head is a file `start` refuses."""
    state = _State(secret=os.urandom(32))
    try:
        head = _line(
            {"v": FORMAT, "e": "head", "task": _task_digest(task_id), "secret": state.secret.hex()},
            codec,
        )
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | _BINARY)
    except OSError as exc:
        raise WalError("wal_unavailable", f"no ledger file: {type(exc).__name__}") from exc
    try:
        view = memoryview(head)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("a write made no progress")
            view = view[written:]
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        raise WalError("wal_unavailable", f"not durable: {type(exc).__name__}") from exc
    return fd, state


def start(
    root: Path,
    task_id: str,
    *,
    codec: Codec = DPAPI,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
) -> V2Ledger | StartRefusal:
    """Take the task's lock and read its file under `root`: the ledger, or the refusal naming
    the first unsettled action (Q9: no resume; the lock is released and nothing is written).
    Raises `WalError`: `ledger_locked`, `ledger_unreadable` (torn or malformed: fails closed)
    or `wal_unavailable` (no directory, no codec, no durable write)."""
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WalError("wal_unavailable", f"no ledger directory: {type(exc).__name__}") from exc
    lock_fd = _take_lock(root, task_id)
    path = ledger_path(root, task_id)
    try:
        if path.exists():
            state = _replay(path, task_id, codec)
            refusal = state.refusal()
            if refusal is not None:
                _release(lock_fd)
                return refusal
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | _BINARY)
        else:
            fd, state = _create(path, task_id, codec)
    except OSError as exc:
        _release(lock_fd)
        raise WalError("wal_unavailable", f"no ledger file: {type(exc).__name__}") from exc
    except WalError:
        _release(lock_fd)
        raise
    return V2Ledger(
        path, fd=fd, lock_fd=lock_fd, state=state, codec=codec, max_record_bytes=max_record_bytes
    )


def unsettled(root: Path, task_id: str, *, codec: Codec = DPAPI) -> list[UnsettledRow]:
    """For the operator, read-only: every record of the task whose outcome is not settled. No
    lock, no write, nothing created; a file that does not decode raises `ledger_unreadable`."""
    path = ledger_path(root, task_id)
    if not path.exists():
        return []
    state = _replay(path, task_id, codec)
    return [
        UnsettledRow(
            seq=seq,
            subgoal=e.record.subgoal,
            verb=e.record.verb,
            role=e.record.role,
            phase=e.record.phase,
            outcome=e.outcome,
        )
        for seq, e in enumerate(state.entries)
        if e.outcome not in SETTLED
    ]


def main(argv: Sequence[str], *, codec: Codec = DPAPI) -> int:
    """`python -m steerai.loop2.wal --list-unsettled --ledger-dir DIR --task-id ID`: one JSON
    line per unsettled record on stdout; exit 0, or `EXIT_UNREADABLE` with the token on
    stderr."""
    parser = argparse.ArgumentParser(prog="python -m steerai.loop2.wal")
    parser.add_argument("--list-unsettled", action="store_true", required=True)
    parser.add_argument("--ledger-dir", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args(list(argv))
    try:
        rows = unsettled(args.ledger_dir, args.task_id, codec=codec)
    except WalError as exc:
        sys.stderr.write(json.dumps({"error": exc.token}) + "\n")
        return EXIT_UNREADABLE
    for row in rows:
        sys.stdout.write(json.dumps(row.__dict__) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
