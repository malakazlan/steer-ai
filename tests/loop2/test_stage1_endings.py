"""[V2.85] (v), lead's confirmation on #503 (3): no default. Every ending the loop can produce is
classified by `stage1.ENDINGS` as a recognised safe stop, a safe-only stop or not safe (done is
its own status); a reason the table does not name fails this file, as `REFUSAL_KIND` (#446)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from steerai.errors import ErrorCode
from steerai.loop2 import runner, stage1
from steerai.loop2.guards import BudgetToken
from steerai.loop2.runner import REFUSAL_KIND
from steerai.loop2.stage1 import (
    ENDINGS,
    INTERRUPT_TOKENS,
    NOT_SAFE,
    PORT_TOKENS,
    RECOGNISED,
    SAFE_ONLY,
    SAFE_STOP,
    TAKEOVER_ENDINGS,
    ending_of,
)
from steerai.session.monitor import TAKEOVER_TOKENS

PACKAGE = Path(stage1.__file__).resolve().parents[1]
LIVE_WORLD = PACKAGE / "loop2" / "live_world.py"
HELPER = PACKAGE / "session" / "helper_monitor.py"


def _reason_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Reason"
    ]


def _literal(call: ast.Call) -> str | None:
    arg = call.args[0]
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


DYNAMIC: dict[str, frozenset[str]] = {
    "f'port_{code}'[:64]": PORT_TOKENS,
    "hit": frozenset(get_args(BudgetToken)),
    "detail": frozenset(TAKEOVER_TOKENS),
    "token": INTERRUPT_TOKENS | {t for t, k in REFUSAL_KIND.items() if k == "policy"},
    "token if kind == 'policy' else 'not_in_step'": (
        frozenset({"not_in_step"}) | {t for t, k in REFUSAL_KIND.items() if k == "policy"}
    ),
}
"""Each `Reason(<expression>)` in the runner whose token is not a literal, and the closed family
it draws from (`token` serves the switch's interrupt and a standing policy refusal)."""
AFTER_REFUSAL = frozenset(
    f"after_refusal:{t}" for t, k in REFUSAL_KIND.items() if k in ("world", "model")
)
"""`Reason(token, after_refusal=True)`: a world or model refusal ([V2.12])."""


def test_every_literal_ending_of_the_runner_is_classified() -> None:
    literals = {t for c in _reason_calls(Path(runner.__file__)) if (t := _literal(c)) is not None}
    assert literals, "the scan found no ending"
    assert sorted(literals - set(ENDINGS)) == []


def test_every_computed_ending_of_the_runner_is_a_declared_family_and_classified() -> None:
    computed = [c for c in _reason_calls(Path(runner.__file__)) if _literal(c) is None]
    after = [c for c in computed if any(k.arg == "after_refusal" for k in c.keywords)]
    texts = {ast.unparse(c.args[0]) for c in computed if c not in after}
    assert sorted(texts - set(DYNAMIC)) == []
    for family in DYNAMIC.values():
        assert sorted(family - set(ENDINGS)) == []
    assert [ast.unparse(c.args[0]) for c in after] == ["token"]
    assert sorted(AFTER_REFUSAL - set(ENDINGS)) == []


def test_no_module_but_the_runner_makes_an_ending() -> None:
    makers = sorted(str(p.relative_to(PACKAGE)) for p in PACKAGE.rglob("*.py") if _reason_calls(p))
    assert makers == [str(Path("loop2") / "runner.py")]


def _returned_literals(path: Path, names: set[str]) -> set[str]:
    """Every string a named function returns: a literal `return`, or a branch of a conditional
    `return` (a comparison's operand is not returned)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name in names):
            continue
        for ret in ast.walk(fn):
            if not (isinstance(ret, ast.Return) and ret.value is not None):
                continue
            values = [ret.value]
            while values:
                value = values.pop()
                if isinstance(value, ast.IfExp):
                    values += [value.body, value.orelse]
                elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
    return found


def test_every_switch_token_is_an_interrupt_ending() -> None:
    """The switch (`LiveWorld.interrupted` and what it reads) only ever returns these tokens."""
    world = _returned_literals(LIVE_WORLD, {"interrupted", "_kill_token", "_session_signal"})
    helper = _returned_literals(HELPER, {"session_change"})
    assert world, "the scan found no switch token"
    assert sorted((world | helper) - INTERRUPT_TOKENS) == []
    assert "interrupted" in INTERRUPT_TOKENS  # the runner's own fallback for a malformed token


def test_the_tiers_are_disjoint_and_cover_the_table() -> None:
    assert not RECOGNISED & SAFE_ONLY
    assert not SAFE_STOP & NOT_SAFE
    assert set(ENDINGS) == SAFE_STOP | NOT_SAFE
    assert {ENDINGS[t] for t in RECOGNISED} == {"recognised"}
    assert {ENDINGS[t] for t in SAFE_ONLY} == {"safe_only"}
    assert {ENDINGS[t] for t in NOT_SAFE} == {"not_safe"}


def test_the_recognised_tier_is_the_leads_list() -> None:
    assert frozenset({
        "commit_not_in_step", "not_in_step", "controller_stop", "controller_ask",
        "secure_field", "secure_unread", "text_changed", "after_refusal:intake_insufficient",
    }) == RECOGNISED  # fmt: skip
    assert {t for t, k in REFUSAL_KIND.items() if k == "policy"} == {
        "secure_field", "secure_unread", "text_changed",
    }  # fmt: skip


def test_the_safe_only_tier_is_no_progress_audit_veto_and_the_other_refusals() -> None:
    others = AFTER_REFUSAL - {"after_refusal:intake_insufficient"}
    assert frozenset({"no_progress", "audit_veto"}) | others == SAFE_ONLY


def test_the_judge_failures_and_the_machines_endings_are_not_safe() -> None:
    for token in ("intake_failed", "audit_failed", "unexpected_effect", "window_gone",
                  "page_not_ready", "max_wall", "takeover", "killed", "monitor_lost"):  # fmt: skip
        assert ENDINGS[token] == "not_safe"
    assert {f"port_{c.value}"[:64] for c in ErrorCode} == PORT_TOKENS
    assert frozenset(TAKEOVER_TOKENS) == TAKEOVER_ENDINGS


@pytest.mark.parametrize(
    ("final", "ending"),
    [
        ({"status": "done", "reason": None}, "done"),
        ({"status": "blocked", "reason": "commit_not_in_step"}, "recognised"),
        ({"status": "cannot", "reason": "after_refusal:intake_insufficient"}, "recognised"),
        ({"status": "paused", "reason": "no_progress:intake_insufficient"}, "safe_only"),
        ({"status": "blocked", "reason": "audit_veto"}, "safe_only"),
        ({"status": "cannot", "reason": "after_refusal:press_not_at_focus"}, "safe_only"),
        ({"status": "blocked", "reason": "intake_failed"}, "not_safe"),
        ({"status": "error", "reason": None}, "not_safe"),
        ({"status": "stopped", "reason": "a_reason_nobody_declared"}, "unclassified"),
        ({"status": "cannot", "reason": None}, "unclassified"),
        ({"status": "done", "reason": "controller_stop"}, "unclassified"),
    ],
)
def test_an_ending_is_classified_from_its_status_and_reason(
    final: dict[str, object], ending: str
) -> None:
    assert ending_of({**final, "used": {"max_actions": 1}}) == ending


def test_a_run_that_never_began_has_no_ending() -> None:
    assert ending_of(None) is None
    refused = {"status": "blocked", "code": "gate_blocked", "detail": "price_missing"}
    assert ending_of(refused) is None
