"""#347 review, decision (B): until the refusal, claim and proven tokens are closed as typed
Literals ([V2.49]: record fields are enums; the follow-up issue, which merges before slice 7),
no module that produces them may build a string from runtime data.

Every refusal token, claim result and proven "how" is then a code literal or a value from a
closed module-level mapping: never an f-string, a concatenation, a `%` or a `str.format` /
`str.join` over runtime values. Two uses are allowed, and neither is a history token: an outcome
`Reason` is rendered (`render_reason`) and a port failure is named (`port_<code>`, in `go`).
"""

from __future__ import annotations

import ast
from pathlib import Path

LOOP2 = Path(__file__).resolve().parents[2] / "steerai" / "loop2"
PRODUCERS = (
    "actuate.py",
    "batch.py",
    "claims.py",
    "consent.py",
    "decision.py",
    "form.py",
    "guards.py",
    "loop.py",
    "owned.py",
    "query.py",
    "record.py",
    "runner.py",
    "verify.py",
)
"""The modules whose values can become a refusal token, a claim result or a proven `how`."""
ALLOWED = {("loop.py", "render_reason"), ("runner.py", "go")}
"""Building a string here is not a history token: a Reason's rendering, a port failure's name."""


def _builds_a_string(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        sides = (node.left, node.right)
        return any(isinstance(s, ast.Constant) and isinstance(s.value, str) for s in sides)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        target = node.func.value
        literal = isinstance(target, ast.Constant) and isinstance(target.value, str)
        return literal and node.func.attr in {"format", "join"}
    return False


def _sites(path: Path) -> list[tuple[str, str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(fn):
                owner.setdefault(id(node), fn.name)
    return [
        (path.name, owner.get(id(node), "<module>"), node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.expr) and _builds_a_string(node)
    ]


def test_every_producer_module_exists() -> None:
    """The list is kept honest: a module renamed away would silently leave the check."""
    assert all((LOOP2 / name).is_file() for name in PRODUCERS)


def test_no_token_producer_builds_a_string_from_runtime_data() -> None:
    sites = [site for name in PRODUCERS for site in _sites(LOOP2 / name)]
    offenders = [site for site in sites if (site[0], site[1]) not in ALLOWED]
    assert offenders == []


def test_the_allowed_sites_are_still_there() -> None:
    """An allowance that no longer matches anything is removed, not kept as a hole."""
    sites = {(name, fn) for name in PRODUCERS for name, fn, _ in _sites(LOOP2 / name)}
    assert sites >= ALLOWED
