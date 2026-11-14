"""[V2.11] decision 8, the conditions for merging the 3c invariant tests:
(a) no v1 path imports `steerai.loop2`;
(b) `--loop v2` refuses to start while any 3c interface is still a stub, and `--loop` defaults
    to v1 with v1 unchanged;
(c) every pending test names the invariant it guards."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from steerai.loop2 import ready
from steerai.loop2._stub import PENDING
from steerai.run import EXIT_USAGE, main, parse_args

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "steerai"
LOOP2 = PACKAGE / "loop2"
TESTS = Path(__file__).resolve().parent
WHERE = ["--goal", "do a thing", "--window", "w"]


def _loop2_imports(tree: ast.Module) -> list[tuple[int, bool]]:
    """Each import of `steerai.loop2` in `tree`: (line, whether it sits inside a function)."""
    found: list[tuple[int, bool]] = []

    def visit(node: ast.AST, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            nested = in_function or isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            names: list[str] = []
            if isinstance(child, ast.Import):
                names = [alias.name for alias in child.names]
            elif isinstance(child, ast.ImportFrom) and child.module is not None:
                names = [child.module] + [f"{child.module}.{a.name}" for a in child.names]
            hit = any(n == "steerai.loop2" or n.startswith("steerai.loop2.") for n in names)
            if hit and isinstance(child, ast.Import | ast.ImportFrom):
                found.append((child.lineno, in_function))
            visit(child, nested)

    visit(tree, in_function=False)
    return found


def test_no_module_outside_loop2_imports_it_except_runs_v2_branch() -> None:
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        if LOOP2 in path.parents:
            continue
        for line, in_function in _loop2_imports(ast.parse(path.read_text(encoding="utf-8"))):
            allowed = path == PACKAGE / "run.py" and in_function
            if not allowed:
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert offenders == []


def test_the_v1_path_never_loads_loop2() -> None:
    probe = (
        "import sys; from steerai.run import parse_args; "
        f"parse_args({WHERE!r}); print('steerai.loop2' in sys.modules)"
    )
    done = subprocess.run(  # noqa: S603 - a fixed argument list, no shell
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=True
    )
    assert done.stdout.strip() == "False"


def test_loop_defaults_to_v1() -> None:
    assert parse_args(WHERE).loop == "v1"


def test_loop_v2_is_parsed() -> None:
    assert parse_args([*WHERE, "--loop", "v2"]).loop == "v2"


def test_an_unknown_loop_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([*WHERE, "--loop", "v3"]) == EXIT_USAGE
    assert "usage error" in capsys.readouterr().err


def test_loop2_is_not_ready_while_any_stub_is_left() -> None:
    assert ready() is False
    assert "steerai.loop2.loop.run_task" in PENDING


def test_the_step_4_interfaces_keep_loop2_not_ready() -> None:
    ready()
    step4 = {
        "steerai.loop2.claims.check_content",
        "steerai.loop2.consent.gate_pass",
        "steerai.loop2.consent.Consents.grant",
        "steerai.loop2.record.key_for",
    }
    assert step4 <= PENDING


def test_loop_v2_refuses_to_start_while_it_is_not_built(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([*WHERE, "--loop", "v2"]) == 1
    captured = capsys.readouterr()
    assert "loop v2 is not built yet: step 3c" in captured.err
    assert '"status": "error"' in captured.out


PENDING_MARK = re.compile(r'@pending\(\s*"([^"]*)"\s*,?\s*\)')
NAMES_INVARIANT = re.compile(r"§\d|\[V2\.\d+\]")
INVARIANT_FILES = sorted(TESTS.glob("test_invariants_*.py"))


@pytest.mark.parametrize("path", INVARIANT_FILES, ids=lambda p: p.name)
def test_every_remaining_pending_mark_names_its_invariant(path: Path) -> None:
    """[V2.11].8, [V2.19]: marks come off as each part lands; every mark still on names the
    invariant it guards (a mark split over lines is read too)."""
    source = path.read_text(encoding="utf-8")
    reasons = PENDING_MARK.findall(source)
    assert len(reasons) == source.count("@pending("), "every pending mark is read"
    assert [r for r in reasons if not NAMES_INVARIANT.search(r)] == []


@pytest.mark.parametrize("path", INVARIANT_FILES, ids=lambda p: p.name)
def test_no_invariant_test_is_xfailed_other_than_by_pending(path: Path) -> None:
    """Only `pending` (strict, raises=NotImplementedError) may mark an invariant test, so none
    is silently weakened to a plain or non-strict xfail."""
    assert "xfail" not in path.read_text(encoding="utf-8")
