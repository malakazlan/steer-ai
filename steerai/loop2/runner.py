"""`run_task`: the step-3 v2 loop (V2 §5, [V2.11], [V2.12], [V2.18], [V2.24] item 3, [V2.28]
item 3, [V2.29] Q2), behind `--loop v2`.

One controller call per turn; its actions run as a batch while the batch rule holds. Nothing at
R2 or above is dispatched in step 3: the task ends `blocked` / `commit_not_in_step` before any
write-ahead. Every outside effect goes through `Ports`.

The claim checks and the `navigated` auto proof are step 3c PR B (`verify.check_claim`,
`verify.auto_proof`); the auto proof is tried only when there is a change signal since a
subgoal's baseline (the URL changed, or the top Document was replaced), which §7.1 requires
before anything else.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from steerai.action.dispositions import Delivery
from steerai.loop2.actuate import may_retry, press_allowed
from steerai.loop2.batch import batch_continues
from steerai.loop2.decision import ActionDoc, Decision, subgoal_tokens, validate
from steerai.loop2.guards import ActionKey, StrikeReason, Strikes, TurnFacts, Usage, budget_hit
from steerai.loop2.loop import (
    COMMIT_RISK,
    READ_BACK_VERBS,
    ActResult,
    Dispatch,
    PendingText,
    Ports,
    Provenance,
    Reason,
    Status,
    TaskOutcome,
    TurnView,
    action_risk,
    resolve,
    type_refusal,
)
from steerai.loop2.query import SearchFacts, search_facts
from steerai.loop2.task import GoalSpan, Subgoal, Task, known_params
from steerai.loop2.verify import auto_proof, check_claim
from steerai.orchestrator.contracts import RISKS, Risk
from steerai.orchestrator.loop import REUSE_FRESHNESS_S
from steerai.world.effects import EffectReport, Observed, ValueFact, top_document
from steerai.world.elements import Element
from steerai.world.model import normalise_name
from steerai.world.observe2 import YOUNG_S
from steerai.world.reads import ReadCost

STRIKE_LIMIT: Final = 3
"""[V2.4]: the pooled set runs with a limit of 3, frozen before H7."""
RefusalKind = Literal["policy", "step", "model", "world"]
"""[V2.12]: a policy refusal ends `blocked` with its token; a step limit ends `blocked` with
`not_in_step`; a model fault keeps the stop's own value, recorded `after_refusal:<token>`. A
`world` refusal (nothing delivered, the focus not where the press goes) is a token and a strike
but no model fault: it leaves the status to the stop."""
STEP_TOKENS: Final = frozenset({"not_in_step"})
LOAD_GUARD_S: Final = YOUNG_S
"""[V2.31] the load guard: a task gives up (a `cannot` stop, the strike limit) on a top Document
younger than this only after one settle and one re-ask; provisional, from the 12.3 s reading.
The same number as a young page ([V2.35] (2)), so the two cannot drift."""
_R2: Final = RISKS.index("R2")


@dataclass(frozen=True)
class _Typed:
    """A field Steer typed into under a subgoal ([V2.22]): what the field read right after the
    verified type (None: unread), the query facts at type time, and the text's provenance."""

    subgoal: str
    field: tuple[int, ...]
    typed: ValueFact | None
    search: SearchFacts
    goal_span: bool


class _End(Exception):  # noqa: N818 - a control-flow signal, not an error
    """The task ends here with this status and cause."""

    def __init__(self, status: Status, cause: Reason | None) -> None:
        super().__init__(status)
        self.status = status
        self.cause = cause


@dataclass
class _Turn:
    """What one controller turn did, for the strike record."""

    action: ActionKey | None = None
    effect: EffectReport | None = None
    refused: bool = False
    ticked: bool = False
    neutral: bool = False
    """Not a strike and not a reset ([V2.31])."""


def run_task(task: Task, ports: Ports) -> TaskOutcome:
    """Run `task` to an outcome. Between batch actions the watch's after observation is reused
    only when it is a full-tier read, stable and within v1's R0/R1 freshness limit
    (`REUSE_FRESHNESS_S`, its age `clock() - snapshot.taken_at`); otherwise the loop re-observes
    ([V2.11] decision 6)."""
    return _Run(task, ports).go()


class _Run:
    def __init__(self, task: Task, ports: Ports) -> None:
        self.task = task
        self.ports = ports
        self.subgoals: tuple[Subgoal, ...] = task.subgoals
        self.frozen = bool(task.subgoals)
        self.proven: set[str] = set()
        self.history: list[str] = []
        self.dispatched: list[Dispatch] = []
        self.model_calls = 0
        self.strikes = Strikes(STRIKE_LIMIT)
        self.strike_log: list[tuple[StrikeReason, ...]] = []
        self.read_costs: list[ReadCost] = []
        self.refusal: tuple[RefusalKind, str] | None = None
        self.typed: list[_Typed] = []
        self.baselines: dict[str, Observed] = {}
        self.last_acted: tuple[int, ...] | None = None
        self.started = ports.clock()
        self.turn = _Turn()
        self.reasked = False

    # ---- the outcome ----------------------------------------------------------------------

    def go(self) -> TaskOutcome:
        try:
            self._loop()
        except _End as end:
            return self._outcome(end.status, end.cause)
        raise AssertionError("the loop ends only through _End")  # pragma: no cover

    def _outcome(self, status: Status, cause: Reason | None) -> TaskOutcome:
        return TaskOutcome(
            status=status,
            cause=cause,
            dispatched=tuple(self.dispatched),
            model_calls=self.model_calls,
            strikes=tuple(self.strike_log),
            read_costs=tuple(self.read_costs),
        )

    def _stopped(self, stop: Literal["cannot", "blocked"]) -> _End:
        """[V2.12]: the status after a stop, from the latest refusal since the last progress."""
        if self.refusal is None:
            return _End(stop, Reason("controller_stop"))
        kind, token = self.refusal
        if kind == "policy":
            return _End("blocked", Reason(token))
        if kind == "step":
            return _End("blocked", Reason("not_in_step"))
        return _End(stop, Reason(token, after_refusal=True))

    # ---- the loop -------------------------------------------------------------------------

    def _loop(self) -> None:
        observed = self.ports.observe()
        while True:
            self._check_interrupt()
            self._check_budgets()
            decision = self._decide(observed)
            self.turn = _Turn()
            # [V2.31], [V2.35] (2): a wait chosen while the page grows or is young is not a strike
            # (more may arrive); neither is the turn whose stop the load guard held
            loading = observed.growing or observed.young
            self.turn.neutral = loading and any(a.verb == "wait" for a in decision.actions)
            observed = self._turn(decision, observed)
            observed = self._record_strike(observed)

    def _decide(self, observed: Observed) -> Decision:
        view = TurnView(observed=observed, history=tuple(self.history), first_turn=not self.frozen)
        decision = self.ports.decide(view)
        self.model_calls += 1
        return decision

    def _turn(self, decision: Decision, observed: Observed) -> Observed:
        listed = frozenset(e.id for e in observed.snapshot.elements)
        first = not self.frozen
        goal = self.task.goal
        if first and decision.subgoals and not subgoal_tokens(decision, goal=goal):
            self._intake(decision)  # valid subgoals are taken even when an action is refused
        tokens = validate(
            decision,
            listed_ids=listed,
            goal=goal,
            first_turn=first,
            known_subgoals=known_params(self.subgoals) if self.frozen else None,
        )
        if tokens:
            kind: RefusalKind = "step" if tokens[0] in STEP_TOKENS else "model"
            self._refuse(kind, tokens[0])
            return observed
        for claim in decision.claims:
            subgoal = self._subgoal(claim.subgoal)
            rejected = check_claim(claim, subgoal, self._baseline(subgoal.id, observed), observed)
            self.history.extend(f"claim:{claim.subgoal}:{t}" for t in rejected)
            if not rejected:
                self._tick(subgoal.id)
        self._check_done()
        if decision.ask is not None:
            raise _End("asked", Reason("controller_ask"))
        if decision.stop is not None:
            held = self._hold(observed) if decision.stop == "cannot" else None
            if held is None:
                raise self._stopped(decision.stop)
            self.turn.neutral = True
            return held
        return self._batch(decision.actions, observed)

    def _intake(self, decision: Decision) -> None:
        """[V2.11] decision 1: the first valid Decision fills the subgoals once; code freezes
        them."""
        docs = decision.subgoals or []
        goal = self.task.goal
        self.subgoals = tuple(
            Subgoal(
                id=doc.id,
                desc=doc.desc,
                params={name: _span(goal, text) for name, text in doc.params.items()},
                commit=doc.commit,
            )
            for doc in docs
        )
        self.frozen = True

    def _subgoal(self, sid: str) -> Subgoal:
        return next(s for s in self.subgoals if s.id == sid)

    def _baseline(self, sid: str, observed: Observed) -> Observed:
        return self.baselines.get(sid, observed)

    def _tick(self, sid: str) -> None:
        self.proven.add(sid)
        self.turn.ticked = True
        self.refusal = None
        self.history.append(f"proven:{sid}")

    def _check_done(self) -> None:
        """A task with zero subgoals never ends `done` ([V2.11] decision 1)."""
        if self.subgoals and all(s.id in self.proven for s in self.subgoals):
            raise _End("done", None)

    # ---- the batch ------------------------------------------------------------------------

    def _batch(self, actions: Sequence[ActionDoc], observed: Observed) -> Observed:
        listed = observed
        for index, action in enumerate(actions):
            self._check_interrupt()
            result = self._one(action, listed, observed)
            if result is None:
                return observed
            after, sent = result
            observed = after
            self._auto_proofs(observed)
            nxt = actions[index + 1] if index + 1 < len(actions) else None
            if nxt is None or not self._continues(sent, nxt, listed, observed):
                return observed
        return observed

    def _continues(
        self, sent: tuple[ActResult, EffectReport], nxt: ActionDoc, listed: Observed, now: Observed
    ) -> bool:
        act, report = sent
        if act.delivery is Delivery.DELIVERY_UNCERTAIN:
            return False
        if act.delivery is Delivery.NOT_DELIVERED and not act.action.skipped:
            return (
                False  # a state already as wanted sent nothing, and continues like a verified one
            )
        if act.action.verb in READ_BACK_VERBS and act.read_back is not True:
            return False
        same = nxt.target is None or resolve(nxt.target, listed.snapshot, now.snapshot) is not None
        return batch_continues(report, verified_type=act.read_back is True, same_target=same)

    def _one(
        self, action: ActionDoc, listed: Observed, observed: Observed
    ) -> tuple[Observed, tuple[ActResult, EffectReport]] | None:
        """Gate, dispatch and watch one action; None when it was refused (the batch ends)."""
        element = self._target(action, listed, observed)
        if element is None and action.verb != "wait":
            return None
        if element is not None and action.verb == "type":
            refusal = type_refusal(element, observed.snapshot)
            if refusal is not None:
                self._refuse("policy", refusal)
                return None
        risk = self._risk(action, element, observed)
        if risk is None:
            return None
        if RISKS.index(risk) >= _R2:
            raise _End("blocked", Reason("commit_not_in_step"))
        key = None if element is None else _key(action, element)
        present = frozenset(e.runtime_id for e in observed.snapshot.elements)
        if key is not None and not self.strikes.may_send(key, present=present):
            self._refuse("model", "repeat_send")
            return None
        self._check_budgets()
        sent = Dispatch(
            action=action,
            runtime_id=() if element is None else element.runtime_id,
            risk=risk,
            provenance=_provenance(action),
        )
        return self._dispatch(sent, element, observed, key)

    def _target(self, action: ActionDoc, listed: Observed, observed: Observed) -> Element | None:
        """§8.1: the element the action names, re-resolved; a press goes to the last acted
        element only when the keyboard focus is read there ([V2.6])."""
        if action.verb == "wait":
            return None
        if action.verb == "press":
            if not press_allowed(focus=observed.focus, last_acted=self.last_acted):
                self._refuse("world", "press_not_at_focus")
                return None
            return next(e for e in observed.snapshot.elements if e.runtime_id == self.last_acted)
        if action.target is None:  # validate refuses this shape first
            raise AssertionError("target_missing")
        element = resolve(action.target, listed.snapshot, observed.snapshot)
        if element is None:
            self._refuse("model", "id_not_listed")
        return element

    def _risk(self, action: ActionDoc, element: Element | None, observed: Observed) -> Risk | None:
        if element is None:  # a wait sends nothing: only a declared commit raises it
            return COMMIT_RISK[action.commit]
        verdict = action_risk(
            action,
            element,
            goal=self.task.goal,
            observed=observed,
            pending=self._pending(action.subgoal, observed),
        )
        if verdict.refusal is not None:
            self._refuse("policy", verdict.refusal)
            return None
        return verdict.risk

    def _pending(self, subgoal: str, observed: Observed) -> list[PendingText]:
        """The fields typed into under `subgoal`, each with its value read now from the field
        itself ([V2.29] Q2); a gone or secure field is not read."""
        by_rid = {e.runtime_id: e for e in observed.snapshot.elements}
        out: list[PendingText] = []
        for typed in self.typed:
            if typed.subgoal != subgoal:
                continue
            field = by_rid.get(typed.field)
            now = None if field is None else self._read_value(field, observed)
            out.append(PendingText(typed.field, typed.typed, now, typed.search, typed.goal_span))
        return out

    def _read_value(self, element: Element, observed: Observed) -> ValueFact | None:
        """[V2.29] Q2: never read a field whose secure flag is set or unread."""
        if element.secure or element.id in observed.snapshot.unread_secure:
            return None
        return self.ports.read_value(element)

    def _dispatch(
        self,
        sent: Dispatch,
        element: Element | None,
        observed: Observed,
        key: ActionKey | None,
    ) -> tuple[Observed, tuple[ActResult, EffectReport]] | None:
        sid = sent.action.subgoal
        self.baselines.setdefault(sid, observed)
        before = observed
        if element is not None:  # [V2.28] item 3: the target's own value, just before dispatch
            before = dataclasses.replace(observed, acted_value=self._read_value(element, observed))
        act = self._act(sent)
        if act.delivery is Delivery.NOT_DELIVERED and not act.action.skipped:
            self._refuse("world", "not_delivered")
            return None
        watched = self.ports.watch(before, sent)
        self.read_costs.append(watched.read_cost)
        report = watched.report
        self.turn.action, self.turn.effect = key, report
        if element is not None:
            self.last_acted = element.runtime_id
        self.history.append(_act_token(sent, act, report))
        if not report.classes <= {"none", "unknown"}:
            self.refusal = None  # [V2.12]: progress resets the refusal that decides the status
        if element is not None and sent.action.verb == "type" and act.read_back is True:
            self._remember_type(sent, element, observed)
        return self._after(watched.after), (act, report)

    def _act(self, sent: Dispatch) -> ActResult:
        """[V2.25] (c): one local retry when nothing was sent, under the same decision."""
        act = self.ports.act(sent)
        self.dispatched.append(sent)
        if may_retry(risk=sent.risk, verified=False, report=None, act=act.action):
            act = self.ports.act(sent)
        return act

    def _remember_type(self, sent: Dispatch, element: Element, observed: Observed) -> None:
        typed = _Typed(
            subgoal=sent.action.subgoal,
            field=element.runtime_id,
            typed=self._read_value(element, observed),
            search=search_facts(observed, element.runtime_id),
            goal_span=sent.provenance == "goal_span",
        )
        self.typed = [t for t in self.typed if t.field != typed.field or t.subgoal != typed.subgoal]
        self.typed.append(typed)

    def _after(self, after: Observed) -> Observed:
        """[V2.11] decision 6: reuse the watch's after only when it is full, stable and fresh."""
        age = self.ports.clock() - after.snapshot.taken_at
        if after.tier == "full" and after.snapshot.stable and age <= REUSE_FRESHNESS_S:
            return after
        return self.ports.observe()

    def _auto_proofs(self, observed: Observed) -> None:
        """§7.1: the code-only proof is tried after every effect, for each open subgoal with a
        change signal since its baseline."""
        for subgoal in self.subgoals:
            baseline = self.baselines.get(subgoal.id)
            if subgoal.id in self.proven or baseline is None:
                continue
            if _changed_since(baseline, observed) and auto_proof(subgoal, baseline, observed):
                self._tick(subgoal.id)
        self._check_done()

    # ---- refusals, guards -----------------------------------------------------------------

    def _refuse(self, kind: RefusalKind, token: str) -> None:
        """[V2.11] decision 4: nothing dispatched is a refusal with a token and a strike."""
        if kind != "world":
            self.refusal = (kind, token)
        self.history.append(f"refused:{token}")
        self.turn.refused = True

    def _hold(self, observed: Observed) -> Observed | None:
        """[V2.31] the load guard: giving up rests on a settled observation. Before the task
        ends on a `cannot` stop or on the strike limit, when the top Document appeared or
        changed less than `LOAD_GUARD_S` ago, wait (the `settle` port) until its node count holds
        for a quiet window or the guard expires, then re-observe and re-ask, once per task.
        None when the guard does not apply."""
        since = observed.document_since
        if self.reasked or since is None:
            return None
        age = self.ports.clock() - since
        if age >= LOAD_GUARD_S:
            return None
        self.reasked = True
        self.history.append("load_guard")
        return self.ports.settle(observed, LOAD_GUARD_S - age)

    def _record_strike(self, observed: Observed) -> Observed:
        if self.turn.neutral:
            return observed
        facts = TurnFacts(
            ticked=self.turn.ticked,
            action=self.turn.action,
            effect=self.turn.effect,
            state_digest=_digest(observed),
            claim_rejected=False,
            present=frozenset(e.runtime_id for e in observed.snapshot.elements),
            refused=self.turn.refused,
        )
        reasons = self.strikes.record(facts)
        if reasons:
            self.strike_log.append(reasons)
        if self.strikes.paused:
            held = self._hold(observed)
            if held is None:
                raise _End("paused", Reason("no_progress"))
            return held
        return observed

    def _check_budgets(self) -> None:
        used = Usage(
            actions=len(self.dispatched),
            model_calls=self.model_calls,
            cost_usd=0.0,
            wall_s=self.ports.clock() - self.started,
        )
        hit = budget_hit(self.task.budgets, used)
        if hit is not None:
            raise _End("paused", Reason(hit))

    def _check_interrupt(self) -> None:
        token = self.ports.interrupted()
        if token is not None:
            raise _End("stopped", Reason(token))


# ---- pure helpers -----------------------------------------------------------------------------


def _span(goal: str, text: str) -> GoalSpan:
    """A param's text as a span of the goal; `validate` already checked it is one."""
    start = goal.index(text)
    return GoalSpan.of(goal, start, start + len(text))


def _provenance(action: ActionDoc) -> Provenance | None:
    """[V2.11] decision 5: set by code; free text is always generated."""
    if action.verb != "type":
        return None
    return "goal_span" if action.param is not None else "generated"


def _key(action: ActionDoc, element: Element) -> ActionKey:
    params = tuple(p for p in (action.key, action.param, action.option) if p is not None)
    return ActionKey(
        verb=action.verb,
        params=params,
        runtime_id=element.runtime_id,
        identity=(element.role, normalise_name(element.name), element.automation_id),
    )


def _act_token(sent: Dispatch, act: ActResult, report: EffectReport) -> str:
    """§8.6: closed tokens only: the verb, the listed id, the delivery and the effect classes."""
    target = "-" if sent.action.target is None else str(sent.action.target)
    classes = ".".join(sorted(report.classes)) or "empty"
    read = {True: "", False: ":read_back_mismatch", None: ""}[act.read_back]
    if sent.action.verb in READ_BACK_VERBS and act.read_back is None:
        read = ":read_back_unread"
    delivery = act.delivery.value
    return f"act:{sent.action.verb}:{target}:{delivery}:{classes}{read}"


def _changed_since(baseline: Observed, now: Observed) -> bool:
    """§7.1's change signal: the URL changed (both read), or the top Document was replaced."""
    if baseline.url is not None and now.url is not None and baseline.url != now.url:
        return True
    old, new = top_document(baseline.snapshot), top_document(now.snapshot)
    return old is not None and new is not None and old.runtime_id != new.runtime_id


def _digest(observed: Observed) -> str:
    """The element-set digest for `state_seen` (in memory only, never logged)."""
    rows = sorted((e.runtime_id, e.role, e.name) for e in observed.snapshot.elements)
    return hashlib.sha256(repr(rows).encode()).hexdigest()
