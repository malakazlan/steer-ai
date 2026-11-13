"""The suite's four-way outcome (V2 §11; packet v2-step1-observability §D).

Steer's verdict (did it say `done`) crossed with the independent read of the page or app: a
true done, a false done (the bar's "0 false done"), a false stop (the page shows the goal but
Steer stopped) or a true stop. Pure: the suite gives it the facts of a finished run.

An unread independent result is never `ok` (hunter OB-04); a run the suite blocked an `open`
in is never a true done (OB-05); a run found invalid after Steer ran keeps its verdict
(OB-06); the person taking over or killing the task is `interrupted` (OB-19); an error or an
unknown status is `error`, never a stop (OB-20).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

Independent = Literal["ok", "not", "unread"]
Outcome = Literal[
    "true_done",
    "false_done",
    "false_stop",
    "true_stop",
    "stop_unread",
    "interrupted",
    "error",
    "invalid",
]
OUTCOMES: Final[tuple[Outcome, ...]] = (
    "true_done",
    "false_done",
    "false_stop",
    "true_stop",
    "stop_unread",
    "interrupted",
    "error",
    "invalid",
)
"""Every outcome, in the order the verdict prints them."""

_STOPS: Final = frozenset({"stopped", "blocked", "paused"})
_INTERRUPTS: Final = frozenset({"takeover", "killed", "session_locked"})
"""Details under which the person, not Steer, ended the task (run.py's refusals)."""


@dataclass(frozen=True, slots=True)
class Run:
    """The facts of one finished suite run."""

    status: str | None
    """Steer's task status (`done`, `stopped`, `blocked`, `paused`, `error`), or None when its
    output was not a result."""
    independent: Independent
    detail: str | None = None
    opens: int = 0
    """`open` calls the suite recorded and blocked."""
    pre_invalid: bool = False
    """The run measured nothing: the person was active, or the window never came to front."""
    post_invalid: bool = False
    """The run was found invalid after Steer ran (a second tab, a bot check)."""


def outcome(run: Run) -> Outcome:
    """The run's outcome; see the module docstring for the rules."""
    if run.pre_invalid:
        return "invalid"
    if run.detail in _INTERRUPTS and run.status in _STOPS:
        return "interrupted"
    if run.status == "done":
        clean = run.independent == "ok" and run.opens == 0 and not run.post_invalid
        return "true_done" if clean else "false_done"
    if run.status in _STOPS:
        if run.independent == "ok":
            return "false_stop"
        return "true_stop" if run.independent == "not" else "stop_unread"
    return "error"


# ---- where a run's wall time went (packet §D; hunter OB-23/38/39) ----------------------------

STAGE_BUCKETS: Final[dict[str, str]] = {
    "observe": "observe",
    "gate": "gate",
    "decide": "decide",
    "act": "act",
    "settle": "settle",
    "verify": "verify_wait",
}
"""Every stage the loop measures (`_Stage.measure`) and its bucket; a stage missing here is
refused, never dropped. Model time is the `plan` and `goal_match` records, never a stage."""
BUCKETS: Final[tuple[str, ...]] = (
    "read",
    "model",
    "observe",
    "gate",
    "decide",
    "act",
    "settle",
    "verify_wait",
    "other",
)


def split_ms(records: Sequence[Mapping[str, Any]], *, seconds: float) -> dict[str, int]:
    """A run's wall time by bucket, from its trace records: the planner's screen read, model
    calls, each loop stage, and `other` (warm-up, waits outside the stages: the run's seconds
    minus the rest). More than the run itself is double counting and is refused."""
    totals = dict.fromkeys(BUCKETS, 0.0)
    for record in records:
        kind = record.get("kind")
        if kind == "plan_read":
            totals["read"] += float(record.get("read_ms") or 0)
        elif kind in ("plan", "goal_match"):
            totals["model"] += float(record.get("latency_ms") or 0)
        elif kind == "step":
            for stage, ms in (record.get("latency_ms") or {}).items():
                if stage not in STAGE_BUCKETS:
                    raise ValueError(f"unmapped stage {stage!r}")
                totals[STAGE_BUCKETS[stage]] += float(ms)
    split = {bucket: round(totals[bucket]) for bucket in BUCKETS if bucket != "other"}
    other = round(seconds * 1000) - sum(split.values())
    if other < 0:
        raise ValueError(f"double counting: the buckets exceed the run by {-other} ms")
    return {**split, "other": other}


def planner_output_tokens(records: Sequence[Mapping[str, Any]]) -> tuple[int, bool]:
    """The planner's output tokens over the run, and whether any plan record lacked them (a
    failed call's record carries none: counted 0, the total marked incomplete)."""
    total, incomplete = 0, False
    for record in records:
        if record.get("kind") != "plan":
            continue
        tokens = record.get("output_tokens")
        if isinstance(tokens, int):
            total += tokens
        else:
            incomplete = True
    return total, incomplete
