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

from dataclasses import dataclass
from typing import Final, Literal

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
