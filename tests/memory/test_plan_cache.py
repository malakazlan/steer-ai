"""The verified plan cache store and its rules (5.12 [v2.33]; gate items 1-5, hunter PC-01..54)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.memory.plan_cache import (
    CacheKey,
    PlanCache,
    outcome_of,
    refusal,
)
from steerai.orchestrator.contracts import Plan, Step, StepResult, Subgoal, Target
from steerai.verify.predicates import TitleMatches

GOAL = "open notepad and type hello there"


def _fake_protect(data: bytes, *, entropy: bytes | None = None) -> bytes:
    return (entropy or b"") + b"|" + data[::-1]


def _fake_unprotect(blob: bytes, *, entropy: bytes | None = None) -> bytes:
    tag, _, body = blob.partition(b"|")
    if tag != (entropy or b""):
        raise OSError("CryptUnprotectData failed: wrong entropy")
    return body[::-1]


def _cache(root: Path) -> PlanCache:
    return PlanCache(root, protect=_fake_protect, unprotect=_fake_unprotect, clock=lambda: 7.0)


def _key(goal: str = GOAL, **kw: Any) -> CacheKey:
    base = {"app_kind": "Notepad.exe", "prompt_version": "planner-12", "matcher": "m|gm-1"}
    base.update(kw)
    return CacheKey.of(goal, **base)


def test_a_put_entry_reads_back_as_a_hit_on_probation(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    entry = cache.put(_key(), b'{"plan": 1}', {"s1": "serves"})
    found = cache.get(_key())
    assert found.event == "hit"
    assert found.entry == entry
    assert found.entry is not None
    assert (found.entry.state, found.entry.uses) == ("probation", 1)


@pytest.mark.parametrize(
    "other",
    [
        _key("open notepad and type Hello there"),  # case is kept (PC-01)
        _key("open notepad and type hello there!"),  # one character
        _key(prompt_version="planner-13"),
        _key(matcher="m|gm-2"),
        _key(app_kind="wordpad.exe"),
    ],
)
def test_any_difference_in_the_key_is_a_miss(tmp_path: Path, other: CacheKey) -> None:
    cache = _cache(tmp_path)
    cache.put(_key(), b"p", {})
    assert cache.get(other).event == "miss"


def test_whitespace_and_app_kind_case_fold_to_the_same_key(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put(_key(), b"p", {})
    assert cache.get(_key("open  notepad and type hello there", app_kind="NOTEPAD.EXE")).event == (
        "hit"
    )


def test_file_names_reveal_no_goal_and_the_secret_is_protected(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put(_key(), b"p", {})
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "secret.bin" in names
    entry = next(n for n in names if n.endswith(".entry"))
    assert "notepad" not in entry
    assert len(entry) == len("x" * 40 + ".entry")
    assert (tmp_path / "secret.bin").read_bytes().startswith(b"steer-plan-cache-secret-v1|")


def test_a_corrupt_or_foreign_entry_is_a_miss_and_is_deleted(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put(_key(), b"p", {})
    path = next(tmp_path.glob("*.entry"))
    path.write_bytes(b"garbage")
    assert cache.get(_key()).event == "corrupt"
    assert not path.exists()


def test_an_entry_copied_over_another_keys_file_is_refused(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.put(_key(), b"A", {})
    cache.put(_key("open notepad and type bye"), b"B", {})
    a = cache._path(_key())
    b = cache._path(_key("open notepad and type bye"))
    b.write_bytes(a.read_bytes())
    assert cache.get(_key("open notepad and type bye")).event == "corrupt"


def test_a_forged_verdict_or_state_is_refused(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    entry = cache.put(_key(), b"p", {"s1": "serves"})
    key = f'["{entry.key.goal}", "notepad.exe", "planner-12", "m|gm-1"]'
    forged = (
        f'{{"key": {key}, "plan": "cA==", "verdicts": {{"s1": "approve"}}, '
        '"state": "probation", "uses": 1, "fails": 0, "consecutive_fails": 0, "updated": 1}'
    )
    cache._path(_key()).write_bytes(
        _fake_protect(forged.encode(), entropy=b"steer-plan-cache-entry-v1")
    )
    assert cache.get(_key()).event == "corrupt"


def test_three_successes_trust_and_two_consecutive_fails_evict(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    entry = cache.put(_key(), b"p", {})
    assert cache.score(entry, "success") == "probation"
    current = cache.get(_key()).entry
    assert current is not None
    assert cache.score(current, "success") == "trusted"
    current = cache.get(_key()).entry
    assert current is not None
    assert cache.score(current, "neutral") == "trusted"
    assert cache.score(current, "fail") == "trusted"
    current = cache.get(_key()).entry
    assert current is not None
    assert current.consecutive_fails == 1
    assert cache.score(current, "fail") == "evicted"
    assert cache.get(_key()).event == "miss", "an evicted entry is deleted"


def _plan(*steps: Step, goal: str = GOAL) -> Plan:
    return Plan(
        goal=goal,
        subgoals=(Subgoal(id="g1", desc="d", condition=TitleMatches(pattern="Notepad")),),
        steps=steps,
    )


def _type(text: str = "hello there") -> Step:
    return Step(
        id="t1",
        subgoal="g1",
        verb="type",
        target=Target(role="Edit", name="Text"),
        text=text,
        risk="R1",
    )


def _click(role: str, name: str, step_id: str = "c1") -> Step:
    return Step(
        id=step_id,
        subgoal="g1",
        verb="click",
        target=Target(role=role, name=name),
        risk="R0",
        expect=TitleMatches(pattern="x"),
    )


def test_refusal_rules() -> None:
    assert refusal(_plan(_type()), GOAL, "notepad.exe") is None
    assert refusal(_plan(_type()), GOAL, None) == "app_kind"
    assert refusal(_plan(_type()), GOAL, "ApplicationFrameHost.exe") == "app_kind"
    assert refusal(_plan(_type()), GOAL.title(), "notepad.exe") == "goal"
    assert refusal(_plan(_type()), GOAL, "chrome.exe") == "browser_site"
    latest = _plan(_click("Hyperlink", "Drake - Rich Flex (Official Video)"), goal=GOAL)
    assert refusal(latest, GOAL, "notepad.exe") == "screen_content"
    named = _plan(_click("Hyperlink", "hello there"), goal=GOAL)
    assert refusal(named, GOAL, "notepad.exe") is None, "content the goal names is the goal's"


def _result(
    step_id: str,
    *,
    ok: bool = True,
    label: str | None = "t1",
    code: ErrorCode | None = None,
    **evidence: Any,
) -> StepResult:
    return StepResult(
        step_id=step_id,
        ok=ok,
        gate="approve",
        action=None,
        verify_code=None if ok else code,
        label_kind=label,  # type: ignore[arg-type]  # test literal
        latency_ms={},
        evidence=evidence,
        code=None if ok else code,
    )


def test_outcome_rules() -> None:
    plan = _plan(_type(), _click("Button", "Save", "c2"))
    assert outcome_of(plan, [_result("t1"), _result("c2")], replanned=False) == "success"
    assert outcome_of(plan, [_result("t1")], replanned=False) == "neutral", "a skipped step"
    assert (
        outcome_of(plan, [_result("t1"), _result("c2", label=None)], replanned=False) == "neutral"
    )
    asked = _result("t1", ask_reasons=["generated_text"])
    assert outcome_of(plan, [asked, _result("c2")], replanned=False) == "neutral"
    failed = _result("c2", ok=False, code=ErrorCode.VERIFY_FAILED)
    assert outcome_of(plan, [_result("t1"), failed], replanned=False) == "fail"
    declined = _result("c2", ok=False, code=ErrorCode.GATE_BLOCKED)
    assert outcome_of(plan, [_result("t1"), declined], replanned=False) == "neutral"
    assert outcome_of(plan, [_result("t1"), _result("c2")], replanned=True) == "neutral"


@pytest.mark.windows
def test_a_real_dpapi_round_trip_and_a_wrong_entropy_blob_is_corrupt(tmp_path: Path) -> None:
    from steerai.platform import dpapi  # noqa: PLC0415 - Windows only

    cache = PlanCache(tmp_path, protect=dpapi.protect, unprotect=dpapi.unprotect)
    cache.put(_key(), b"plan bytes", {"s1": "serves"})
    assert cache.get(_key()).event == "hit"
    path = cache._path(_key())
    path.write_bytes(dpapi.protect(b"{}", entropy=b"another purpose"))
    assert cache.get(_key()).event == "corrupt", "a blob of another purpose never decrypts"
