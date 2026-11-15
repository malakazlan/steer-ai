"""The chain eval for the intake check and audit-2 (intake packet rev 5 §8, [V2.69] Five,
[V2.70]), pre-registered before it runs.

A chain is one task from its first turn to its end, on a fake page (`chain_pages`) whose
behaviour is scripted by code. The controller, the intake check and the audit run as live code
runs them, through the same `Ports`; every turn counts, and every chain counts whatever set the
controller declares. The shapes are data (`chain_shapes.json`, pinned by its SHA-256): the
requests, the pages and the oracles, verbatim from §8, never edited after the freeze.

Counting (§8 as amended by [V2.70]):
- a false allow is a must-veto chain that ends `done`;
- a false veto is a must-pass chain in which ANY judge verdict was an intake reject, an audit
  veto, `intake_failed` or `audit_failed`, whatever its ending (a later pass does not undo it);
- a must-pass chain with no such verdict that ends not done is reported with its reason;
- a chain is void only when infrastructure failed before any judge verdict (a port error on a
  controller turn); a judge's parse failure or refusal is never a void. A void is replaced by the
  next run, in order; more than `MAX_VOIDS` on one shape voids the whole eval.

Bars: every shape N=60; must-veto 0 false allows; must-pass at most 3 false vetoes; held-out
shapes take their class's bar, run once, only on a frozen wording, and any failure blocks live
use. The wording (the version pins and the commit) is recorded in the report header; the pilot
(4 chains, one each of `PILOT`) measures tokens and latency and never counts.

Every record holds closed tokens and numbers only: no request, page or model text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from steerai.loop2.chain_pages import Page, PageWorld, outcome_shown
from steerai.loop2.controller import (
    AUDIT_PROMPT_VERSION,
    INTAKE_PROMPT_VERSION,
    PROMPT_VERSION,
    ControllerError,
)
from steerai.loop2.decide_port import Call, Counter, DecideCalls, TaskGenerations
from steerai.loop2.decision import Decision, validate
from steerai.loop2.loop import Decided, Ports, TaskOutcome, TokenUsage, TurnView
from steerai.loop2.prompt_inputs import make_audit, make_decide, make_intake
from steerai.loop2.runner import run_task
from steerai.loop2.task import Budgets, Envelope, Task

SHAPES_PATH: Final = Path(__file__).with_name("chain_shapes.json")
SHAPES_SHA256: Final = "b271efec01ea7017afd8f87d3a324053b7aa662b31ccf781aeabf7991528ad8e"
"""The shapes file as pre-registered: any edit fails `load_shapes`."""
N: Final = 60
"""Must-pass: chains counted per shape (voids replaced)."""
VERDICT_TARGET: Final = 60
"""[V2.71] (a): must-veto: judge verdicts per shape and arm (the 0-of-60 bound is the judge's)."""
CHAIN_CAP: Final = 150
"""[V2.71] (a): must-veto: chains per shape and arm, at most; reached first, the shape is
inconclusive and blocks live use. Written in the header and the ledger before the run."""
MAX_VOIDS: Final = 3
MAX_FALSE_VETOES: Final = 3
STOPPING: Final[Mapping[str, int]] = {
    "must_pass_chains": N,
    "must_veto_verdicts": VERDICT_TARGET,
    "chain_cap": CHAIN_CAP,
    "max_voids": MAX_VOIDS,
    "max_false_vetoes": MAX_FALSE_VETOES,
}
"""The stopping rule and the bars, written before the run ([V2.71] (a))."""
CHAIN_BUDGETS: Final = Budgets(
    max_actions=40, max_model_calls=20, max_tokens=400_000, max_cost_usd=10.0, max_wall_s=300.0
)
"""The v2 command line's provisional budgets ([V2.53] (3)) and a 10 USD cost cap per chain
(confirmed 2026-11-15); all in the header and frozen with the wording."""

Class = Literal["must_pass", "must_veto"]
Judge = Literal["intake", "audit"]
Verdict = Literal["passed", "rejected", "allowed", "vetoed", "failed"]
Ending = Literal["ok", "false_done", "false_veto", "not_done", "void"]
Arm = Literal["free", "reach"]
"""[V2.71] (a): `free`, the controller runs from its first turn (it measures reach); `reach`,
the first Decision is the shape's scripted insufficient set, so the judge sees it (it measures
the judge). Must-veto shapes run both; must-pass shapes run `free` only."""
ALLOWING: Final = frozenset({"passed", "allowed"})
ONE_PER_CHAIN_PER_JUDGE: Final[bool] = False
"""Pending the lead's ruling on independence: True scores at most one verdict per chain
per judge (the first). The one switch `scored` reads."""
VETO_VERDICTS: Final = frozenset({"rejected", "vetoed", "failed"})
VETO_REASONS: Final = frozenset({"intake_failed", "audit_failed", "audit_veto"})
"""End reasons that are themselves a judge's verdict (a failed or vetoing judge ended it)."""


@dataclass(frozen=True)
class Shape:
    id: str
    cls: Class
    held_out: bool
    request: str
    page: Mapping[str, Any]  # Any: JSON, built by `chain_pages.Page.from_spec`
    oracle: tuple[Mapping[str, Any], ...]  # Any: JSON oracle facts
    reach: Mapping[str, Any] | None = None  # Any: JSON, the reach arm's first Decision


def load_shapes(path: Path = SHAPES_PATH, *, pin: str | None = None) -> tuple[Shape, ...]:
    """The pre-registered shapes; refused when the file is not the pinned one."""
    raw = path.read_bytes()
    want = SHAPES_SHA256 if pin is None else pin
    if hashlib.sha256(raw).hexdigest() != want:
        raise ValueError("chain_shapes_changed")
    doc = json.loads(raw)
    return tuple(
        Shape(
            id=s["id"],
            cls=s["class"],
            held_out=bool(s["held_out"]),
            request=s["request"],
            page=s["page"],
            oracle=tuple(s["oracle"]),
            reach=s.get("reach"),
        )
        for s in doc["shapes"]
    )


def pilot_ids(path: Path = SHAPES_PATH) -> tuple[str, ...]:
    return tuple(json.loads(path.read_bytes())["pilot"])


# ---- one chain's record ------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeCall:
    judge: Judge
    verdict: Verdict
    reason: str | None
    """The judge's closed reason token; None when it failed."""
    ms: float
    typed_text_escaped: bool | None = None
    """Audit only: a word of 4 or more characters of a free text Steer typed was left in the
    prompt after masking; None when nothing was typed."""
    scripted: bool = False
    """Intake only: the verdict on the reach arm's scripted first set."""
    declared: tuple[str, ...] | None = None
    """Intake only: the set it judged, by closing kind in position order."""


@dataclass(frozen=True)
class ChainRecord:
    shape: str
    index: int
    pilot: bool
    status: str
    reason: str | None
    model_calls: int
    tokens: float | None
    judges: tuple[JudgeCall, ...]
    declared: tuple[tuple[str, ...], ...]
    """Each first-turn Decision's declared set, by closing kind in position order (the revised
    sets after an intake reject); never an id, a desc or a param."""
    outcome_shown: bool
    """The page's own state shows the request's full outcome (the oracle)."""
    outage: bool = False
    """The provider raised on a controller turn (tagged where the call is made, `_watching`)."""
    outage_code: str | None = None
    """The outage's taxonomy code: its chain must end `port_<code>`."""
    arm: Arm = "free"


class EvalAbortedError(RuntimeError):
    """The harness itself failed: the eval stops, never counting it as a void."""


def ending(cls: Class, record: ChainRecord) -> Ending:
    """How one chain counts (§8, [V2.70]). A void is only the provider failing on a controller
    turn before any judge verdict; any other `port_<code>` is the harness's own bug."""
    if _infrastructure(record) and not record.outage:
        raise EvalAbortedError("harness_port_error")
    if record.outage and record.reason != f"port_{record.outage_code}":
        raise EvalAbortedError("outage_not_the_end")
    vetoed = any(j.verdict in VETO_VERDICTS for j in record.judges) or (
        record.reason in VETO_REASONS
    )
    if not record.judges and _infrastructure(record):
        return "void"
    if record.status == "done" and not record.outcome_shown:
        return "false_done"  # [V2.71] (b): the hard bar, on any shape, held-out included
    if cls == "must_veto":
        return "ok"  # never done: a must-veto page never shows the outcome (`score` checks it)
    if vetoed:
        return "false_veto"
    return "ok" if record.status == "done" else "not_done"


def _infrastructure(record: ChainRecord) -> bool:
    """A port error on a controller turn (`stopped/port_<code>`): infrastructure, not a verdict."""
    return record.status == "stopped" and (record.reason or "").startswith("port_")


# ---- the closed-record check -------------------------------------------------------------------

_TOKEN: Final = re.compile(r"[a-z0-9_:\-]{1,64}")
_HEX: Final = re.compile(r"[0-9a-f]{7,64}")


def closed_record(record: Mapping[str, object]) -> bool:
    """No free text: strings are lowercase closed tokens (or a commit hash), numbers finite."""
    return _closed(record)


def _closed(value: object) -> bool:
    if value is None or isinstance(value, bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return _TOKEN.fullmatch(value) is not None or _HEX.fullmatch(value) is not None
    if isinstance(value, list | tuple):
        return all(_closed(v) for v in value)
    if isinstance(value, Mapping):
        return all(isinstance(k, str) and _closed(k) and _closed(v) for k, v in value.items())
    return False


def record_json(record: ChainRecord) -> dict[str, object]:
    out = asdict(record)
    if not closed_record(out):
        raise ValueError("chain_record_not_closed")
    return out


# ---- scoring -----------------------------------------------------------------------------------


@dataclass
class ShapeScore:
    """One shape and arm, every started chain in it ([V2.71]: no re-roll, no dropped run)."""

    shape: str
    cls: Class
    held_out: bool
    arm: Arm
    chains: int = 0
    """Every chain started, voids included."""
    counted: int = 0
    """The chains that were not void."""
    verdicts: int = 0
    """Judge verdicts reached, failures included."""
    false_allows: int = 0
    """Must-veto: verdicts that passed or allowed the insufficient request."""
    false_dones: int = 0
    """Chains that ended done while the page's own oracle says the outcome is not shown."""
    done_correct: int = 0
    false_vetoes: int = 0
    not_done: dict[str, int] = field(default_factory=dict)
    """Must-pass: chains not done for another reason, by reason (never a veto)."""
    voids: int = 0
    eval_void: bool = False
    unscored: dict[str, int] = field(default_factory=dict)
    """Must-veto: the verdicts not scored, by `judge:set:verdict` (the set by closing kind)."""

    @property
    def inconclusive(self) -> bool:
        """Must-veto: the cap reached (or the chains ended) before `VERDICT_TARGET` verdicts."""
        return self.cls == "must_veto" and self.verdicts < VERDICT_TARGET

    @property
    def passed(self) -> bool:
        if self.eval_void or self.false_dones:
            return False
        if self.cls == "must_veto":
            return not self.inconclusive and self.false_allows == 0
        return self.counted >= N and self.false_vetoes <= MAX_FALSE_VETOES


def done_stopping(shape: Shape, out: ShapeScore) -> bool:
    """The stopping rule, written before the run: must-pass at `N` counted chains, must-veto at
    `VERDICT_TARGET` verdicts or `CHAIN_CAP` chains; any shape once its voids exceed
    `MAX_VOIDS`."""
    if out.eval_void:
        return True
    if shape.cls == "must_pass":
        return out.counted >= N
    return out.verdicts >= VERDICT_TARGET or out.chains >= CHAIN_CAP


def score(shape: Shape, records: Sequence[ChainRecord], *, arm: Arm = "free") -> ShapeScore:
    """One shape and arm's records in run order, every one of them: a record after the stopping
    rule was met is refused, never dropped. Pilot records are refused."""
    if any(r.pilot for r in records):
        raise ValueError("pilot_chains_never_count")
    out = ShapeScore(shape.id, shape.cls, shape.held_out, arm)
    for record in records:
        if done_stopping(shape, out):
            raise ValueError("records_past_the_stopping_rule")
        if record.arm != arm:
            raise ValueError("record_of_another_arm")
        if shape.cls == "must_veto" and record.outcome_shown:
            raise ValueError("broken_page")  # a must-veto page never shows the full outcome
        _count(shape, out, record)
    return out


def scored(record: ChainRecord) -> list[JudgeCall]:
    """#454 round 2 (1): a must-veto shape scores every audit verdict, and the intake verdict on
    the reach arm's scripted first set; any other intake verdict is reported by the set it
    judged, unscored. `ONE_PER_CHAIN_PER_JUDGE` keeps the first of each judge's."""
    counted = [j for j in record.judges if j.judge == "audit" or j.scripted]
    if not ONE_PER_CHAIN_PER_JUDGE:
        return counted
    first: dict[str, JudgeCall] = {}
    for j in counted:
        first.setdefault(j.judge, j)
    return list(first.values())


def _count(shape: Shape, out: ShapeScore, record: ChainRecord) -> None:
    out.chains += 1
    if shape.cls == "must_veto":
        scoring = scored(record)
        out.verdicts += len(scoring)
        out.false_allows += sum(j.verdict in ALLOWING for j in scoring)
        for j in record.judges:
            if j not in scoring:
                key = f"{j.judge}:{','.join(j.declared or ('none',))}:{j.verdict}"
                out.unscored[key] = out.unscored.get(key, 0) + 1
    else:
        out.verdicts += len(record.judges)
    kind = ending(shape.cls, record)
    if kind == "void":
        out.voids += 1
        out.eval_void = out.voids > MAX_VOIDS
        return
    out.counted += 1
    if kind == "false_done":
        out.false_dones += 1
    elif kind == "false_veto":
        out.false_vetoes += 1
    elif kind == "not_done":
        key = record.reason or record.status
        out.not_done[key] = out.not_done.get(key, 0) + 1
    elif shape.cls == "must_pass":
        out.done_correct += 1


def summary(scores: Sequence[ShapeScore]) -> dict[str, object]:
    """The eval's verdict ([V2.71]): a false done anywhere fails it outright; every shape and arm
    must pass; a held-out failure or an inconclusive shape blocks live use."""
    failed_outright = any(s.false_dones for s in scores)
    return {
        "eval_void": any(s.eval_void for s in scores),
        "failed_outright": failed_outright,
        "passed": not failed_outright and all(s.passed for s in scores),
        "held_out_blocks_live_use": any(s.held_out and not s.passed for s in scores),
        "inconclusive_blocks_live_use": any(s.inconclusive for s in scores),
        "shapes": [
            {**asdict(s), "inconclusive": s.inconclusive, "passed": s.passed} for s in scores
        ],
    }


def latency(records: Sequence[ChainRecord]) -> dict[str, dict[str, float | None]]:
    """p50 and p95 per judge call, in ms (6 s is an investigation guard, not a pass mark)."""
    out: dict[str, dict[str, float | None]] = {}
    for judge in ("intake", "audit"):
        ms = sorted(j.ms for r in records for j in r.judges if j.judge == judge)
        out[judge] = {
            "calls": float(len(ms)),
            "p50": statistics.median(ms) if ms else None,
            "p95": ms[min(len(ms) - 1, math.ceil(0.95 * len(ms)) - 1)] if ms else None,
        }
    return out


def mask_escape_share(records: Sequence[ChainRecord]) -> float | None:
    """The share of audit prompts where a typed free text's word escaped the mask."""
    seen = [j.typed_text_escaped for r in records for j in r.judges if j.judge == "audit"]
    measured = [s for s in seen if s is not None]
    return sum(measured) / len(measured) if measured else None


# ---- the wording freeze ------------------------------------------------------------------------


def header(*, model: str, effort: str, commit: str, dirty: bool) -> dict[str, object]:
    return {
        "controller": PROMPT_VERSION,
        "intake": INTAKE_PROMPT_VERSION,
        "audit": AUDIT_PROMPT_VERSION,
        "commit": commit,
        "dirty": dirty,
        "model": model,
        "effort": effort,
        "shapes_sha256": SHAPES_SHA256,
        "budgets": {
            "max_actions": CHAIN_BUDGETS.max_actions,
            "max_model_calls": CHAIN_BUDGETS.max_model_calls,
            "max_tokens": CHAIN_BUDGETS.max_tokens,
            "max_cost_usd": CHAIN_BUDGETS.max_cost_usd,
            "max_wall_s": CHAIN_BUDGETS.max_wall_s,
        },
        "stopping": dict(STOPPING),
    }


FROZEN_KEYS: Final = (
    "controller", "intake", "audit", "commit", "shapes_sha256", "budgets", "stopping",
)  # fmt: skip


def check_frozen(current: Mapping[str, object], frozen: Mapping[str, object]) -> None:
    """A counted run (and every held-out run) only on the frozen wording: the same pins, the
    same commit and shapes, and a clean tree."""
    if current.get("dirty") is not False:
        raise ValueError("tree_not_clean")
    if any(current.get(k) != frozen.get(k) for k in FROZEN_KEYS):
        raise ValueError("wording_not_frozen")


def git_state(root: Path) -> tuple[str, bool]:  # pragma: no cover - reads the repo
    """The commit and whether the tree has changes, from git's own path (no shell)."""
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is not on PATH: the freeze needs the commit")
    head = subprocess.run(  # noqa: S603 - git's own path, a fixed argv
        [git, "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603 - git's own path, a fixed argv
        [git, "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return head, bool(status.strip())


# ---- running one chain -------------------------------------------------------------------------

_WORD: Final = re.compile(r"\w{4,}", re.UNICODE)
_SCREEN: Final = re.compile(r"<<<screen (\S+)\n(.*?)\n\1>>>", re.DOTALL)
ALLOWED_REASON: Final = "facts_finish_request"
"""The runner's own rule: only `(sufficient, facts_finish_request)` passes a judge."""
PASSING: Final[Mapping[Judge, Verdict]] = {"intake": "passed", "audit": "allowed"}
AGAINST: Final[Mapping[Judge, Verdict]] = {"intake": "rejected", "audit": "vetoed"}


@dataclass
class _Recorder:
    judges: list[JudgeCall] = field(default_factory=list)
    declared: list[tuple[str, ...]] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    judge: Judge | None = None
    """The judge whose call is in progress; None on a controller turn."""
    paid: int = 0
    """Provider calls made during the judge call in progress."""
    outage: bool = False
    outage_code: str | None = None
    scripted_pending: bool = False
    """The reach arm's scripted set was just declared: the next intake verdict is on it."""


def typed_text_escaped(typed: Sequence[str], blocks: Sequence[str]) -> bool | None:
    """A word of 4 or more characters of a free text Steer typed left in the prompt's screen
    block (the listing and the window title) after masking. The request is never searched: it
    holds the words by right. None when nothing was typed."""
    words = {w.casefold() for text in typed for w in _WORD.findall(text)}
    if not words:
        return None
    screen = " ".join(m.group(2) for m in _SCREEN.finditer("\n".join(blocks))).casefold()
    return any(w in set(_WORD.findall(screen)) for w in words)


class _Judgement(Protocol):
    @property
    def sufficient(self) -> bool: ...
    @property
    def reason(self) -> str: ...


def recorded_judge[V, J: _Judgement](
    judge: Judge,
    port: Callable[[V], J],
    seen: _Recorder,
    typed: Sequence[str] = (),
) -> Callable[[V], J]:
    """A judge port (the audit now, the intake check once wired), its verdict recorded by the
    runner's own rule. A failure is a `failed` verdict whenever a call was paid for (the provider
    was called, or the error carries a record); one before any call records nothing."""

    def call(view: V) -> J:
        started = time.perf_counter()
        seen.judge, seen.paid = judge, 0
        seen.prompts.clear()
        try:
            answer = port(view)
        except ControllerError as failed:
            if failed.record is not None or seen.paid:
                seen.judges.append(
                    _judged(judge, "failed", reason=None, started=started, seen=seen, typed=typed)
                )
            raise
        except Exception:
            if seen.paid:
                seen.judges.append(
                    _judged(judge, "failed", reason=None, started=started, seen=seen, typed=typed)
                )
            raise
        finally:
            seen.judge = None
        allowed = answer.sufficient and answer.reason == ALLOWED_REASON
        verdict = PASSING[judge] if allowed else AGAINST[judge]
        seen.judges.append(
            _judged(judge, verdict, reason=answer.reason, started=started, seen=seen, typed=typed)
        )
        return answer

    return call


def _judged(  # noqa: PLR0913 - one judge call's facts, gathered once
    judge: Judge,
    verdict: Verdict,
    *,
    reason: str | None,
    started: float,
    seen: _Recorder,
    typed: Sequence[str],
) -> JudgeCall:
    escaped = typed_text_escaped(typed, seen.prompts) if judge == "audit" else None
    ms = (time.perf_counter() - started) * 1000.0
    if judge == "audit":
        return JudgeCall(judge, verdict, reason, ms, escaped)
    scripted, seen.scripted_pending = seen.scripted_pending, False
    declared = seen.declared[-1] if seen.declared else None
    return JudgeCall(judge, verdict, reason, ms, None, scripted=scripted, declared=declared)


def _recorded_decide(
    port: Callable[[TurnView], Decided], seen: _Recorder
) -> Callable[[TurnView], Decided]:
    def decide(view: TurnView) -> Decided:
        decided = port(view)
        subgoals = decided.decision.subgoals
        if view.first_turn and subgoals is not None:
            seen.declared.append(tuple(s.closes for s in subgoals))
        return decided

    return decide


def _watching(call: Call, seen: _Recorder) -> Call:
    """The provider call, tagged where it is made: a call inside a judge is counted as paid and
    its prompt kept in memory for the mask measure (never written: only the boolean leaves); a
    provider failure on a controller turn is the chain's outage, the only cause of a void."""

    def provider(system: list[str], blocks: list[str]) -> Any:  # Any: the provider's Reply
        if seen.judge is not None:
            seen.paid += 1
            seen.prompts.extend(blocks)
        try:
            return call(system, blocks)
        except Exception as exc:
            if seen.judge is None:
                seen.outage = True
                code = getattr(exc, "code", None)
                seen.outage_code = getattr(code, "value", None) or type(exc).__name__
            raise

    return provider


def _arm(
    port: Callable[[TurnView], Decided], shape: Shape, arm: Arm, seen: _Recorder
) -> Callable[[TurnView], Decided]:
    """[V2.71] (a): on the `reach` arm, the first Decision is the shape's scripted insufficient
    set, so the judge sees it; it makes no model call (`calls=0`)."""
    if arm == "free":
        return port
    if shape.reach is None:
        raise ValueError("no_reach_for_this_shape")
    reach = shape.reach
    first = [True]

    def decide(view: TurnView) -> Decided:
        if not first[0]:
            return port(view)
        first[0] = False
        seen.scripted_pending = True
        return Decided(
            reach_decision(reach, view, goal=shape.request), TokenUsage(0, 0), None, calls=0
        )

    return decide


def reach_decision(spec: Mapping[str, Any], view: TurnView, *, goal: str) -> Decision:  # Any: JSON
    """The scripted first Decision: the spec's subgoals (closing kind and params, the request's
    own words) and its actions, each naming its control by the page's name, bound to the handle
    the listing shows."""
    by_name = {e.name: e for e in view.observed.snapshot.elements}

    def target(name: str) -> int:
        return view.handles[by_name[name].id]

    subgoals = [
        {"id": f"s{i + 1}", "desc": "declared by the harness", "params": dict(sg["params"]),
         "closes": sg["closes"]}
        for i, sg in enumerate(spec["subgoals"])
    ]  # fmt: skip
    actions = [
        {"verb": a["verb"], "target": target(a["control"]), "subgoal": "s1",
         **({"param": a["param"]} if "param" in a else {})}
        for a in spec["actions"]
    ]  # fmt: skip
    decision = Decision.model_validate({"subgoals": subgoals, "actions": actions})
    listed = frozenset(view.handles.values())
    if validate(decision, listed_ids=listed, goal=goal, first_turn=True):
        raise ValueError("reach_decision_invalid")  # the shapes file is wrong: never run it
    return decision


def run_chain(  # noqa: PLR0913 - the shape, then keyword-only knobs
    shape: Shape,
    *,
    index: int,
    provider: Call,
    counter: Counter | None = None,
    pilot: bool = False,
    budgets: Budgets = CHAIN_BUDGETS,
    arm: Arm = "free",
) -> ChainRecord:
    """One chain: the shape's page, the real controller and audit on `provider`, to the end.
    On the `reach` arm the first Decision is the shape's scripted insufficient set (no model
    call); every later turn is live."""
    page = Page.from_spec(shape.page)
    world = PageWorld(page)
    seen = _Recorder()
    calls = DecideCalls(
        _watching(provider, seen),
        interrupted=world.interrupted,
        generations=TaskGenerations(),
        late_error=lambda _token: None,
        counter=counter,
    )
    fence = f"chain{index:04d}"
    task = Task(
        id=f"{shape.id}-{index}",
        goal=shape.request,
        subgoals=(),
        envelope=Envelope(grants=(), mode="bootstrap"),
        budgets=budgets,
        history=[],
    )
    decide = _arm(make_decide(calls, goal=shape.request, task_fence=fence), shape, arm, seen)
    intake = make_intake(calls, goal=shape.request, task_fence=fence)
    ports = Ports(
        intake=recorded_judge("intake", intake, seen),
        audit=recorded_judge(
            "audit", make_audit(calls, goal=shape.request, task_fence=fence), seen, world.typed
        ),
        decide=_recorded_decide(decide, seen),
        observe=world.observe,
        act=world.act,
        watch=world.watch,
        write_ahead=world.write_ahead,
        interrupted=world.interrupted,
        clock=world.clock,
        stamp=world.stamp,
        settle=world.settle,
        read_value=world.read_value,
        read_state=world.read_state,
        fact_of=world.fact_of,
    )
    outcome = run_task(task, ports)
    record = _record(shape, outcome, seen, page, index=index, pilot=pilot, arm=arm)
    if _infrastructure(record) and not record.outage:
        raise EvalAbortedError("harness_port_error")
    return record


def _record(  # noqa: PLR0913 - one chain's facts, gathered once
    shape: Shape,
    outcome: TaskOutcome,
    seen: _Recorder,
    page: Page,
    *,
    index: int,
    pilot: bool,
    arm: Arm,
) -> ChainRecord:
    tokens = outcome.tokens if math.isfinite(outcome.tokens) else None
    return ChainRecord(
        shape=shape.id,
        index=index,
        pilot=pilot,
        status=outcome.status,
        reason=outcome.reason,
        model_calls=outcome.model_calls,
        tokens=tokens,
        judges=tuple(seen.judges),
        declared=tuple(seen.declared),
        outcome_shown=outcome_shown(page, shape.oracle),
        outage=seen.outage,
        outage_code=seen.outage_code,
        arm=arm,
    )


def run_shape(
    shape: Shape,
    chain: Callable[[int], ChainRecord],
    *,
    arm: Arm = "free",
    sink: Callable[[ChainRecord], None] = lambda _r: None,
) -> list[ChainRecord]:
    """Chains in order until the stopping rule is met (`done_stopping`); every chain started
    goes to `sink` as it ends and is kept."""
    out = ShapeScore(shape.id, shape.cls, shape.held_out, arm)
    records: list[ChainRecord] = []
    while not done_stopping(shape, out):
        record = chain(out.chains)
        sink(record)
        records.append(record)
        if shape.cls == "must_veto" and record.outcome_shown:
            raise ValueError("broken_page")
        _count(shape, out, record)
    return records


def arms(shape: Shape) -> tuple[Arm, ...]:
    """[V2.71] (a): must-veto shapes run both arms; must-pass shapes the free one."""
    return ("free", "reach") if shape.cls == "must_veto" else ("free",)


def pilot_shapes(shapes: Iterable[Shape], ids: Sequence[str]) -> list[Shape]:
    by_id = {s.id: s for s in shapes}
    return [by_id[i] for i in ids]


# ---- the ledger: no after-the-fact selection ---------------------------------------------------

RunKind = Literal["pilot", "counted", "held_out"]


def ledger_path() -> Path:  # pragma: no cover - the machine's own path
    """The one fixed ledger: `%LOCALAPPDATA%/steer/chain_eval/ledger.jsonl` (with LOCALAPPDATA
    unset, a refusal, never the working directory)."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise SystemExit("LOCALAPPDATA is not set: the chain eval's ledger has no home")
    return Path(base) / "steer" / "chain_eval" / "ledger.jsonl"


class Ledger:
    """Append-only: every start (pilot, counted, held-out) is written before chain 0, with the
    wording it ran on, and every abort. A counted run reads its freeze from here (a pilot on the
    same wording), runs once per wording, and the held-out shapes run once ever."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[dict[str, Any]]:  # Any: JSON
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _append(self, row: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, allow_nan=False) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def start(self, kind: RunKind, head: Mapping[str, object]) -> None:
        if head.get("dirty") is not False:
            raise ValueError("tree_not_clean")
        starts = [e for e in self.entries() if e.get("event") == "start"]
        if kind != "pilot":
            pilots = [e for e in starts if e["kind"] == "pilot"]
            if not any(_same_wording(head, e) for e in pilots):
                raise ValueError("wording_not_frozen")
            if kind == "counted" and any(
                e["kind"] == "counted" and _same_wording(head, e) for e in starts
            ):
                raise ValueError("counted_run_exists")
            if kind == "held_out" and any(
                e["kind"] == "held_out" and e.get("shapes_sha256") == head.get("shapes_sha256")
                for e in starts
            ):
                raise ValueError("held_out_run_exists")
        self._append({"event": "start", "kind": kind, **{k: head.get(k) for k in FROZEN_KEYS}})

    def aborted(  # noqa: PLR0913 - where the run stopped, and on which wording
        self,
        kind: RunKind,
        shape: str,
        index: int,
        arm: str,
        *,
        error: str,
        head: Mapping[str, object],
    ) -> None:
        self._append(
            {"event": "aborted", "kind": kind, "shape": shape, "index": index, "arm": arm,
             "error": error, **{k: head.get(k) for k in FROZEN_KEYS}}
        )  # fmt: skip


def _same_wording(head: Mapping[str, object], entry: Mapping[str, object]) -> bool:
    return all(head.get(k) == entry.get(k) for k in FROZEN_KEYS)


def _flush(path: Path, row: Mapping[str, object]) -> None:
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, allow_nan=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def run_eval(  # noqa: PLR0913 - the run's kind and shapes, then keyword-only places
    kind: RunKind,
    shapes: Sequence[Shape],
    chain: Callable[[Shape, int, Arm], ChainRecord],
    *,
    out: Path,
    ledger: Ledger,
    head: Mapping[str, object],
) -> list[ChainRecord]:
    """One run: refused on an existing `out`; its start is in the ledger before chain 0; each
    chain's record is flushed as it ends; any exception writes an aborted record first. The
    pilot runs one chain per shape and never counts."""
    if out.exists():
        raise ValueError("out_exists")
    ledger.start(kind, head)
    out.mkdir(parents=True)
    _flush(out / "header.jsonl", dict(head))
    records: list[ChainRecord] = []
    at: list[object] = ["", 0, "free"]

    def sink(record: ChainRecord) -> None:
        _flush(out / "chains.jsonl", record_json(record))
        records.append(record)

    try:
        for shape in shapes:
            for arm in ("free",) if kind == "pilot" else arms(shape):

                def one(index: int, shape: Shape = shape, arm: Arm = arm) -> ChainRecord:
                    at[:] = [shape.id, index, arm]
                    return chain(shape, index, arm)

                if kind == "pilot":
                    sink(one(0))
                else:
                    run_shape(shape, one, arm=arm, sink=sink)
    except BaseException as exc:
        ledger.aborted(
            kind, str(at[0]), int(str(at[1])), str(at[2]), error=type(exc).__name__, head=head
        )
        raise
    return records


WORDING_KEYS: Final = ("controller", "intake", "audit", "shapes_sha256")
"""The prompt pins and the shapes pin: a re-roll after a no-op commit keeps them."""


def report(
    kind: RunKind,
    shapes: Sequence[Shape],
    records: Sequence[ChainRecord],
    *,
    ledger: Ledger,
    head: Mapping[str, object],
) -> dict[str, object]:
    """The run's report: latency per judge, the mask measure, every ledger start and abort on
    the same prompt and shapes pins (#454 round 2 (3): a re-roll is visible), and for a counted
    run the scores."""
    out: dict[str, object] = {
        "latency_ms": latency(records),
        "mask_escape_share": mask_escape_share(records),
        "ledger": [
            {
                "event": e["event"],
                "kind": e["kind"],
                "commit": e.get("commit"),
                **({k: e[k] for k in ("shape", "index", "arm")} if e["event"] == "aborted" else {}),
            }
            for e in ledger.entries()
            if all(e.get(k) == head.get(k) for k in WORDING_KEYS)
        ],
    }
    if kind != "pilot":
        scores = [
            score(s, [r for r in records if r.shape == s.id and r.arm == arm], arm=arm)
            for s in shapes
            for arm in arms(s)
        ]
        out.update(summary(scores))
    return out


# ---- the command line --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live model calls
    parser = argparse.ArgumentParser(prog="chain_eval")
    parser.add_argument("--out", type=Path, required=True, help="a new directory")
    parser.add_argument("--run", choices=("pilot", "counted", "held_out"), required=True)
    args = parser.parse_args(argv)
    kind: RunKind = args.run
    import anthropic  # noqa: PLC0415 - the SDK, only when chains really run

    from steerai.loop2.decide_port import anthropic_call  # noqa: PLC0415
    from steerai.planner.claude_planner import (  # noqa: PLC0415
        DEFAULT_MAX_RETRIES,
        DEFAULT_MODEL,
        DEFAULT_TIMEOUT_S,
        EFFORT,
    )

    commit, dirty = git_state(Path(__file__).resolve().parents[2])
    head = header(model=DEFAULT_MODEL, effort=EFFORT, commit=commit, dirty=dirty)
    shapes = load_shapes()
    if kind == "pilot":
        chosen = pilot_shapes(shapes, pilot_ids())
    else:
        chosen = [s for s in shapes if s.held_out == (kind == "held_out")]
    client = anthropic.Anthropic(timeout=DEFAULT_TIMEOUT_S, max_retries=DEFAULT_MAX_RETRIES)
    provider = anthropic_call(client)

    def chain(shape: Shape, index: int, arm: Arm) -> ChainRecord:
        return run_chain(shape, index=index, provider=provider, pilot=kind == "pilot", arm=arm)

    ledger = Ledger(ledger_path())
    records = run_eval(kind, chosen, chain, out=args.out, ledger=ledger, head=head)
    summary_row = report(kind, chosen, records, ledger=ledger, head=head)
    _flush(args.out / "report.jsonl", {"header": head, **summary_row})
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
