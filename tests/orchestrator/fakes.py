"""Doubles for the loop tests: a scripted world, scripted executors, an in-memory ledger with the
`TaskLedger` contract, and a recording confirmer. Pure, so the loop is tested on every CI runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.dispositions import Step as ActionStep
from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import GateDecision, Step
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.ledger import LedgerError, WriteAheadRecord, action_key, key_params
from steerai.world.elements import Element
from steerai.world.model import Snapshot

Row = tuple[int, str, str, tuple[int, ...], int | None]

WINDOW: Row = (0, "Pane", "Amir Khan - Chat", (42, 1), None)
CHATS: Row = (1, "Group", "Chats", (42, 10), 0)
CHAT_AMIR: Row = (2, "ListItem", "Amir · hi bro · 2h", (42, 12), 1)
HEADER: Row = (3, "Group", "Header", (42, 20), 0)
DELETE: Row = (4, "Button", "Delete chat", (42, 22), 3)
OPTIONS: Row = (10, "Button", "Chat options", (42, 23), 3)
CONVERSATION: Row = (5, "Group", "Conversation", (42, 30), 0)
BUBBLE_OK: Row = (6, "Text", "ok", (42, 31), 5)
COMPOSER_AREA: Row = (7, "Group", "Composer", (42, 40), 0)
COMPOSER: Row = (8, "Edit", "Message", (42, 41), 7)
SEND: Row = (9, "Button", "Send", (42, 42), 7)
BASE_ROWS: list[Row] = [
    WINDOW,
    CHATS,
    CHAT_AMIR,
    HEADER,
    DELETE,
    CONVERSATION,
    BUBBLE_OK,
    COMPOSER_AREA,
    COMPOSER,
    SEND,
    OPTIONS,
]


def build(
    rows: list[Row], *, snapshot_id: str, taken_at: float, stable: bool = True, pid: int = 777
) -> Snapshot:
    by_id = {r[0]: r for r in rows}

    def depth(row: Row) -> int:
        d, parent = 0, row[4]
        while parent is not None:
            d += 1
            parent = by_id[parent][4]
        return d

    elements = [
        Element(
            id=r[0],
            depth=depth(r),
            role=r[1],
            name=r[2],
            box=(0, 10 * r[0], 100, 10 * r[0] + 10),
            runtime_id=r[3],
            parent_id=r[4],
        )
        for r in rows
    ]
    return Snapshot(
        id=snapshot_id,
        hwnd=0x10,
        process="chrome.exe",
        title="Amir Khan - Chat",
        taken_at=taken_at,
        elements=elements,
        node_count=len(elements),
        stable=stable,
        pid=pid,
        handles=[e.runtime_id for e in elements],
    )


@dataclass
class FakeWorld:
    """Scripted snapshots: `observe` returns the current page, which executors mutate."""

    rows: list[Row] = field(default_factory=lambda: list(BASE_ROWS))
    clock: float = 100.0
    stable: bool = True
    pid: int = 777
    observed: list[dict[str, Any]] = field(default_factory=list)
    counter: int = 0
    composer_value: str = ""
    sent: list[str] = field(default_factory=list)
    bubbles: int = 0

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        self.observed.append({"hwnd": hwnd, **kwargs})
        self.counter += 1
        self.clock += 0.03
        return build(
            self.rows,
            snapshot_id=f"snap-{self.counter}",
            taken_at=self.clock,
            stable=self.stable,
            pid=self.pid,
        )

    def add_bubble(self, text: str) -> None:
        """Append a bubble to the conversation; positional ids are renumbered as a real snapshot
        would, RuntimeIds stay."""
        self.bubbles += 1
        rows: list[Row] = list(self.rows)
        insert_at = next(i for i, r in enumerate(rows) if r[2] == "Composer")
        conversation_old_id = next(r[0] for r in rows if r[2] == "Conversation")
        rows.insert(insert_at, (-1, "Text", text, (42, 100 + self.bubbles), conversation_old_id))
        old_to_new = {r[0]: i for i, r in enumerate(rows) if r[0] != -1}
        self.rows = [
            (i, r[1], r[2], r[3], None if r[4] is None else old_to_new[r[4]])
            for i, r in enumerate(rows)
        ]


def _ok(verb: str, mechanism: str = "semantic") -> ActionResult:
    return ActionResult(
        verb=verb,
        delivery=Delivery.DELIVERED_VERIFIED,
        steps=[ActionStep(label=f"{mechanism}.{verb}", mechanism=mechanism, outcome="succeeded")],  # type: ignore[arg-type]
    )


@dataclass
class FakeExecutor:
    """Executes against the FakeWorld; scripted outcomes per element id override the default."""

    world: FakeWorld
    mechanism: str = "semantic"
    outcomes: dict[int, ActionResult] = field(default_factory=dict)
    calls: list[tuple[str, int, str | None]] = field(default_factory=list)
    on_execute: Callable[[FakeExecutor, Any], None] | None = None

    def execute(self, action: Any, snapshot: Snapshot) -> ActionResult:
        self.calls.append((action.verb, action.element_id, getattr(action, "text", None)))
        if self.on_execute is not None:
            self.on_execute(self, action)
        if action.element_id in self.outcomes:
            return self.outcomes[action.element_id]
        element = snapshot.element(action.element_id)
        if action.verb == "type":
            self.world.composer_value = action.text
            return _ok("type", "physical")
        if action.verb == "click" and element.name == "Send":
            text = self.world.composer_value
            if text:
                self.world.sent.append(text)
                self.world.add_bubble(text)
                self.world.composer_value = ""
            return _ok("click", self.mechanism)
        return _ok(action.verb, self.mechanism)


@dataclass
class FakeLive:
    world: FakeWorld

    def value(self, _handle: Any) -> str | None:
        return self.world.composer_value

    def toggle_state(self, _handle: Any) -> int | None:
        return None

    def is_selected(self, _handle: Any) -> bool | None:
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        return ()


@dataclass
class FakeConfirmer:
    answers: list[str] = field(default_factory=list)
    default: str = "yes"
    asked: list[Ask] = field(default_factory=list)
    on_ask: Callable[[Ask], None] | None = None

    def ask(self, ask: Ask) -> str:
        self.asked.append(ask)
        if self.on_ask is not None:
            self.on_ask(ask)
        return self.answers.pop(0) if self.answers else self.default


@dataclass
class MemoryLedger:
    """The `TaskLedger` contract in memory: same admission and phase rules as the DPAPI ledger."""

    task_id: str
    started_at: float
    secret: bytes = b"\x07" * 32
    executions: dict[str, dict[str, Any]] = field(default_factory=dict)
    records: dict[str, WriteAheadRecord] = field(default_factory=dict)
    fail_writes: bool = False
    unreadable: set[str] = field(default_factory=set)
    window_ref: str | None = None

    def set_window_ref(self, ref: str | None) -> None:
        self.window_ref = ref

    def key_for(self, step: Step) -> str:
        assert step.target is not None
        return action_key(self.secret, step.verb, step.target, key_params(step, self.window_ref))

    def record_open(self, step: Step, window: Mapping[str, Any]) -> None:
        key = self.key_for(step)
        entry = dict(self.executions.get(key) or {"step_id": step.id})
        entry.update(phase="verified", outcome="delivered_verified", window=dict(window))
        self.executions[key] = entry

    def record_open_attempt(self, step: Step, outcome: str) -> None:
        key = self.key_for(step)
        entry = dict(self.executions.get(key) or {"step_id": step.id})
        entry.update(phase="verified", outcome=outcome)
        self.executions[key] = entry

    def opened_window(self, step: Step) -> dict[str, Any] | None:
        entry = self.executions.get(self.key_for(step))
        window = None if entry is None else entry.get("window")
        return None if window is None else dict(window)

    def opened_by_ref(self, ref: str) -> dict[str, Any] | None:
        step_id = ref.removeprefix("opened:") if ref.startswith("opened:") else None
        for entry in self.executions.values():
            if step_id is not None and entry.get("step_id") == step_id and "window" in entry:
                return dict(entry["window"])
        return None

    def admit(self, key: str, decision: GateDecision | None = None) -> str:
        record = self.executions.get(key)
        if record is None or record.get("outcome") == "not_delivered":
            return "fresh"
        if record.get("outcome") in ("delivered_verified", "done_already"):
            return "done"
        if decision is not None and decision.outcome == "confirm" and decision.consume(key):
            return "fresh"
        return "confirm"

    def outcome_of(self, key: str) -> str | None:
        record = self.executions.get(key)
        return None if record is None else record.get("outcome")

    def closed_by(self, key: str) -> str | None:
        record = self.executions.get(key)
        return None if record is None else record.get("step_id")

    def write_ahead(
        self,
        step: Step,
        before: Snapshot,
        decision: GateDecision | None,
        chosen: dict[str, Any] | None = None,
    ) -> WriteAheadRecord:
        key = self.key_for(step)
        if decision is None or decision.action_key != key or decision.outcome == "block":
            raise LedgerError("a write-ahead record needs the gate decision bound to this key")
        if not decision.consumed and not decision.consume(key):
            raise LedgerError("the gate decision could not be consumed for this key")
        if self.fail_writes:
            raise LedgerError("write-ahead record could not be written: CryptProtectData failed")
        record = WriteAheadRecord(
            task_id=self.task_id,
            step_id=step.id,
            action_key=key,
            decision_id=decision.decision_id,
            phase="written",
            chosen=chosen,
            before=replace(before, handles=[]),
            written_at=self.started_at,
        )
        self.records[step.id] = record
        self.executions[key] = {"step_id": step.id, "phase": "written", "outcome": None}
        return record

    def mark(self, step: Step, phase: str) -> None:
        record = self.records[step.id]
        self.records[step.id] = replace(record, phase=phase)  # type: ignore[arg-type]
        self.executions[record.action_key]["phase"] = phase

    def record_outcome(self, step: Step, outcome: str) -> None:
        record = self.records[step.id]
        self.records[step.id] = replace(record, phase="verified")
        self.executions[record.action_key]["phase"] = "verified"
        self.executions[record.action_key]["outcome"] = outcome

    def read_ahead(self, step: Step) -> WriteAheadRecord | None:
        if step.id in self.unreadable:
            raise LedgerError("write-ahead record unreadable: tampered")
        return self.records.get(step.id)

    def before_of_record(self, step: Step, fresh: Snapshot) -> Snapshot:
        record = self.read_ahead(step)
        return fresh if record is None else record.before


def not_delivered(code: ErrorCode, detail: str) -> ActionResult:
    return ActionResult(
        verb="click",
        delivery=Delivery.NOT_DELIVERED,
        steps=[],
        code=code,
        evidence={"reason": code.value, "detail": detail},
    )


def uncertain(verb: str = "click") -> ActionResult:
    return ActionResult(
        verb=verb,
        delivery=Delivery.DELIVERY_UNCERTAIN,
        steps=[ActionStep(label="semantic.click", mechanism="semantic", outcome="failed")],
        code=ErrorCode.VERIFY_FAILED,
        evidence={"reason": "element_not_found", "phase": "call"},
    )
