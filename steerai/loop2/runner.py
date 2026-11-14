"""`run_task`: the step-3 v2 loop (V2 §5, [V2.11], [V2.12], [V2.18], [V2.24] item 3, [V2.28]
item 3, [V2.29] Q2, [V2.37], [V2.38]), behind `--loop v2`.

One controller call per turn; its actions run as a batch while the batch rule holds. Nothing at
R2 or above is dispatched in step 3: the task ends `blocked` / `commit_not_in_step` before any
write-ahead. Every outside effect goes through `Ports`; a port error ends the task `stopped` with
a taxonomy token and the evidence kept (RT-16).

The claim checks and the `navigated` auto proof are step 3c PR B (`verify.check_claim`,
`verify.auto_proof`); the auto proof is tried only when there is a change signal since a
subgoal's baseline (the URL changed, or the top Document was replaced), which §7.1 requires
before anything else. The Enter table on the watch's final report and the staleness rule are
PR C (`enter_outcome`, `stale_observation`, stubs here so `loop2.ready()` stays False).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from steerai.action.dispositions import Delivery
from steerai.errors import ErrorCode, SteerError
from steerai.loop2._stub import stub
from steerai.loop2.actuate import may_retry, press_allowed
from steerai.loop2.batch import batch_continues
from steerai.loop2.controller import ControllerError
from steerai.loop2.decision import (
    CLAIM_CLOSES,
    ActionDoc,
    Claim,
    Decision,
    subgoal_tokens,
    validate,
)
from steerai.loop2.guards import (
    ActionKey,
    StrikeReason,
    Strikes,
    TurnFacts,
    Usage,
    budget_hit,
    made_progress,
    same_action,
)
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
    TokenUsage,
    TurnView,
    action_risk,
    type_refusal,
)
from steerai.loop2.query import SearchFacts, search_facts
from steerai.loop2.task import GoalSpan, Subgoal, Task, known_params
from steerai.loop2.verify import Readers, auto_proof, check_claim
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
"""[V2.12], [V2.37] P5: every refusal replaces the latest one, whatever its kind. A policy
refusal ends `blocked` with its token; a step limit ends `blocked` with `not_in_step`; a model
fault and a `world` refusal (nothing delivered, the focus not where the press goes) keep the
stop's own value, recorded `after_refusal:<token>`."""
STEP_TOKENS: Final = frozenset({"not_in_step"})
CLOCK_DOMAIN_S: Final = 3600.0
"""RT-10: a snapshot stamped further than this from `Ports.clock` was stamped on another clock;
the loop refuses to compare them (the reuse age between batch actions would be meaningless)."""
LOAD_GUARD_S: Final = YOUNG_S
"""[V2.31], [V2.38] RT-24: one total per top Document, from when it was first seen: the waits
that are no strike on a growing or young page and the re-ask before giving up all end when the
Document is this old, or when the wall budget is spent. The flags are still reported after it.
The same number as a young page ([V2.35] (2)), so the two cannot drift; provisional."""
_R2: Final = RISKS.index("R2")
Identity = tuple[str, str, str]
"""[V2.11] decision 3, [V2.38] RT-03: the ledger identity (role, normalised name, automation id)."""


@dataclass(frozen=True)
class _Typed:
    """[V2.37] P2, [V2.38] RT-03: a field Steer typed into in this task, whatever subgoal typed
    it. Keyed by RuntimeId, found again by its ledger identity under the same top Document; the
    value it read right after a verified type (None: unread, or the type was not verified), the
    query facts at type time, and the text's provenance travel with the field."""

    field: tuple[int, ...]
    identity: Identity
    scope: tuple[int, ...] | None
    """The top Document's RuntimeId at type time: a replaced Document ends the pending text."""
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
    claim_rejected: bool = False
    neutral: bool = False
    """Not a strike and not a reset ([V2.31], [V2.35] (2))."""
    sent: int = 0
    """The actions this turn dispatched."""


class _Handles:
    """[V2.38] RT-17: task-scoped listing handles. Each is bound to one RuntimeId for the whole
    task and never reassigned; a new RuntimeId (a re-rendered element) gets a new one. A handle
    takes the element's snapshot ordinal when that number was never used, else the next free
    number, so the numbers stay small. An element with no RuntimeId gets none."""

    def __init__(self) -> None:
        self.by_rid: dict[tuple[int, ...], int] = {}
        self.by_handle: dict[int, tuple[int, ...]] = {}

    def bind(self, observed: Observed) -> dict[int, int]:
        """Each element's handle, by its snapshot id, binding the RuntimeIds seen first now. A
        RuntimeId held by more than one element of the snapshot gets no handle there: the number
        could land on either (#330 review)."""
        out: dict[int, int] = {}
        shared = _shared_rids(observed)
        for element in observed.snapshot.elements:
            rid = element.runtime_id
            if not rid or rid in shared:
                continue
            if rid not in self.by_rid:
                handle = element.id
                if handle in self.by_handle:
                    handle = max(self.by_handle) + 1
                self.by_rid[rid] = handle
                self.by_handle[handle] = rid
            out[element.id] = self.by_rid[rid]
        return out

    def element(self, handle: int, observed: Observed) -> Element | None:
        """The one element the handle names in `observed`; None when it is gone or its RuntimeId
        is held by more than one element there."""
        rid = self.by_handle.get(handle)
        if rid is None:
            return None
        matches = [e for e in observed.snapshot.elements if e.runtime_id == rid]
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class _Ever:
    """[V2.38] RT-39: one type Steer sent in this task, never pruned (unlike pending text): the
    field, the subgoal it was typed under, and the words when typed by param (None: generated
    text, whose words are not kept)."""

    field: tuple[int, ...]
    subgoal: str
    words: str | None


@dataclass(frozen=True)
class _Proof:
    """[V2.39] (2): one param's accepted proof: the element by its task handle, read again when
    the subgoal closes."""

    handle: int
    kind: str
    wanted: str | None


@dataclass(frozen=True)
class _Acted:
    """The last element acted on, for a press (§8.1: re-checked by role and name, P4)."""

    runtime_id: tuple[int, ...]
    role: str
    name: str


def run_task(task: Task, ports: Ports) -> TaskOutcome:
    """Run `task` to an outcome. Between batch actions the watch's after observation is reused
    only when it is a full-tier read, stable and within v1's R0/R1 freshness limit
    (`REUSE_FRESHNESS_S`, its age `clock() - snapshot.taken_at`); otherwise the loop re-observes
    ([V2.11] decision 6)."""
    return _Run(task, ports).go()


@stub
def stale_observation(decided_on: Observed, now: Observed) -> bool:
    """PR C, [V2.28] item 1: the top Document was replaced or the URL changed since the
    observation a Decision was made on (the Decision is discarded, re-observed, re-asked)."""
    raise NotImplementedError("3c PR C: the staleness rule")


@stub
def enter_outcome(report: EffectReport) -> str:
    """PR C, [V2.28] item 1: the Enter table evaluated once on the watch's final report."""
    raise NotImplementedError("3c PR C: the Enter table on the final report")


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
        self.tokens = 0.0
        self.strikes = Strikes(STRIKE_LIMIT)
        self.strike_log: list[tuple[StrikeReason, ...]] = []
        self.refusal: tuple[RefusalKind, str] | None = None
        self.typed: list[_Typed] = []
        self.baselines: dict[str, Observed] = {}
        self.first: Observed | None = None
        self.last_acted: _Acted | None = None
        self.batch_last: tuple[ActionKey, EffectReport] | None = None
        self.fresh: dict[tuple[int, ...], ValueFact | None] = {}
        self.started = ports.clock()
        self.turn = _Turn()
        self.read_costs: list[ReadCost] = []
        self.reasked: set[tuple[int, ...]] = set()
        """[V2.38] RT-12: the top Documents already re-asked on (once per Document)."""
        self.handles = _Handles()
        self.ever: list[_Ever] = []
        self.proofs: dict[str, dict[str, _Proof]] = {}
        """[V2.39] (2): per value or state subgoal, the accepted proof of each param."""

    # ---- the outcome ----------------------------------------------------------------------

    def go(self) -> TaskOutcome:
        try:
            self._loop()
        except _End as end:
            return self._outcome(end.status, end.cause)
        except (SteerError, ControllerError) as exc:  # RT-16: a port failed; keep the evidence
            code = exc.code.value if isinstance(exc.code, ErrorCode) else exc.code
            return self._outcome("stopped", Reason(f"port_{code}"[:64]))
        raise AssertionError("the loop ends only through _End")  # pragma: no cover

    def _outcome(self, status: Status, cause: Reason | None) -> TaskOutcome:
        return TaskOutcome(
            status=status,
            cause=cause,
            dispatched=tuple(d.redacted() for d in self.dispatched),
            model_calls=self.model_calls,
            strikes=tuple(self.strike_log),
            tokens=self.tokens,
            cost_usd=self._cost(),
            read_costs=tuple(self.read_costs),
        )

    def _cost(self) -> float | None:
        """[V2.37] P6: tokens times the configured price; None when no price is configured."""
        price = self.task.budgets.price
        if price is None:
            return None
        return self.tokens * max(price.usd_per_input_token, price.usd_per_output_token)

    def _stopped(self, stop: Literal["cannot", "blocked"]) -> _End:
        """[V2.12], [V2.37] P5: the status after a stop, from the latest refusal since the last
        progress."""
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
        observed = self._observe()
        self.first = observed
        while True:
            self._check_interrupt()
            self._check_budgets()
            decision = self._decide(observed)
            self.turn = _Turn()
            loading = (observed.growing or observed.young) and self._guard_left(observed) > 0
            observed = self._turn(decision, observed)
            waits = bool(decision.actions) and all(a.verb == "wait" for a in decision.actions)
            if loading and waits and self.turn.sent == len(decision.actions):
                # [V2.31], [V2.35] (2), P3: a wait chosen on a growing or young page, dispatched
                # in full with nothing refused, is no strike (more may arrive)
                self.turn.neutral = self.turn.neutral or not self.turn.refused
            observed = self._record_strike(observed)

    def _observe(self) -> Observed:
        return self._in_domain(self.ports.observe())

    def _in_domain(self, observed: Observed) -> Observed:
        """RT-10, RT-11: every observation is stamped by the one `stamp` port, on `Ports.clock`.
        Its `Snapshot.taken_at` must be in that clock's domain, or the reuse age means nothing.
        A `document_since` far in the past is legitimate (a long-lived stamp); one in the future
        beyond `CLOCK_DOMAIN_S`, or not finite, is another clock (small skew is clamped by the
        load guard)."""
        observed = self.ports.stamp(observed)
        now = self.ports.clock()
        if not abs(now - observed.snapshot.taken_at) <= CLOCK_DOMAIN_S:
            raise _End("stopped", Reason("clock_domain"))
        since = observed.document_since
        if since is not None and not since - now <= CLOCK_DOMAIN_S:  # NaN and inf fail it too
            raise _End("stopped", Reason("clock_domain"))
        return observed

    def _decide(self, observed: Observed) -> Decision:
        view = TurnView(
            observed=observed,
            history=tuple(self.history),
            first_turn=not self.frozen,
            handles=self.handles.bind(observed),
        )
        decided = self.ports.decide(view)
        self.model_calls += 1
        self.tokens += _tokens(decided.usage)
        return decided.decision

    def _turn(self, decision: Decision, observed: Observed) -> Observed:
        listed = frozenset(self.handles.bind(observed).values())
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
            self._claim(claim, observed)
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

    def _cited(self, claim: Claim, observed: Observed) -> Claim:
        """The claim with its handles read as this snapshot's elements; a handle with no element
        here reads as an id no snapshot holds (`id_not_listed`)."""
        ids = []
        for handle in claim.evidence:
            element = self.handles.element(handle, observed)
            ids.append(-1 if element is None else element.id)
        return claim.model_copy(update={"evidence": ids})

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
                closes=doc.closes,
                commit=doc.commit,
            )
            for doc in docs
        )
        self.frozen = True

    def _subgoal(self, sid: str) -> Subgoal:
        return next(s for s in self.subgoals if s.id == sid)

    def _baseline(self, sid: str) -> Observed:
        """[V2.38] RT-39: the observation before the first action dispatched under `sid`
        (mid-batch, the previous action's after); with none yet, the task's first observation."""
        if sid in self.baselines:
            return self.baselines[sid]
        if self.first is None:  # set before the first decide
            raise AssertionError("no first observation")
        return self.first

    def _claim(self, claim: Claim, observed: Observed) -> None:
        """One claim: a commit subgoal is never closed in step 3 (RT-06); a claim of another kind
        than the subgoal's closing kind is `kind_mismatch` ([V2.39]); else checked by code. A
        page claim closes when it proves every param; a value or state claim records the proof
        of one param, and the subgoal closes when every param's proof reads true again now."""
        subgoal = self._subgoal(claim.subgoal)
        if subgoal.commit != "none":
            self._reject(subgoal, ("standard_only",))
            return
        if CLAIM_CLOSES.get(claim.kind) != subgoal.closes:
            self._reject(subgoal, ("kind_mismatch",))
            return
        param = _proved_param(claim, subgoal)
        if subgoal.closes != "page" and param is None:
            self._reject(subgoal, ("param_missing",))
            return
        if subgoal.closes != "page" and self._reused(subgoal, param or "", claim.evidence[0]):
            self._reject(subgoal, ("evidence_reused",))
            return
        rejected = check_claim(
            self._cited(claim, observed),
            subgoal,
            self._baseline(subgoal.id),
            observed,
            readers=self._readers(observed),
            typed=self._typed_fields(),
            acted=self._acted(subgoal),
            echo=self._echo(subgoal),
            allow_file=self._allow_file(),
        )
        if rejected:
            self._reject(subgoal, rejected)
            return
        if subgoal.closes == "page":
            self._tick(subgoal, how="claim")
            return
        proofs = self.proofs.setdefault(subgoal.id, {})
        proofs[param or ""] = _Proof(claim.evidence[0], claim.kind, claim.wanted)
        wanted = set(subgoal.params) or {""}  # a subgoal with no param needs one proof
        if set(proofs) >= wanted and self._still(subgoal, observed):
            self._tick(subgoal, how="claim")

    def _reused(self, subgoal: Subgoal, param: str, handle: int) -> bool:
        """#334 review: one element proves at most one param of a subgoal; the model's param is
        only a hint, so the same handle cited for a second param proves nothing more."""
        proofs = self.proofs.get(subgoal.id, {})
        return any(p.handle == handle for name, p in proofs.items() if name != param)

    def _still(self, subgoal: Subgoal, observed: Observed) -> bool:
        """[V2.39] (2): every param's proof read again now, on this observation: a subgoal
        never closes on a subset, nor on a read from an earlier turn."""
        readers = self._readers(observed)
        for param, proof in self.proofs.get(subgoal.id, {}).items():
            element = self.handles.element(proof.handle, observed)
            if element is None:
                return False
            if proof.kind == "state":
                if proof.wanted is None or readers.read_state(element, proof.wanted) is not True:
                    return False
                continue
            want = readers.fact_of(subgoal.params[param].text)
            if readers.read_value(element) != want:
                return False
        return True

    def _reject(self, subgoal: Subgoal, tokens: Sequence[str]) -> None:
        self.history.extend(f"claim:{subgoal.id}:{t}" for t in tokens)
        self.turn.claim_rejected = True

    def _allow_file(self) -> bool:
        """[V2.39]: a `file:` page counts only when the task started on one."""
        first = self.first
        address = None if first is None else (first.document_url or first.url)
        return address is not None and address.lower().startswith("file:")

    def _tick(self, subgoal: Subgoal, *, how: str) -> None:
        """A proven subgoal. RT-06, §8.8: a commit subgoal is never closed in step 3 (a standard
        proof never closes it; the strict tier is step 4)."""
        if subgoal.commit != "none":
            self.history.append(f"claim:{subgoal.id}:standard_only")
            self.turn.claim_rejected = True
            return
        self.proven.add(subgoal.id)
        self.turn.ticked = True
        self.refusal = None
        self.history.append(f"proven:{subgoal.id}:{how}")

    def _check_done(self) -> None:
        """A task with zero subgoals never ends `done` ([V2.11] decision 1)."""
        if self.subgoals and all(s.id in self.proven for s in self.subgoals):
            raise _End("done", None)

    # ---- the batch ------------------------------------------------------------------------

    def _batch(self, actions: Sequence[ActionDoc], observed: Observed) -> Observed:
        listed = observed
        self.batch_last = None
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
            return False  # a state already as wanted sent nothing, and continues
        if act.action.verb in READ_BACK_VERBS and act.read_back is not True:
            return False
        same = nxt.target is None or self._listed(nxt.target, listed, now) is not None
        return batch_continues(report, verified_type=act.read_back is True, same_target=same)

    def _one(
        self, action: ActionDoc, listed: Observed, observed: Observed
    ) -> tuple[Observed, tuple[ActResult, EffectReport]] | None:
        """Gate, dispatch and watch one action; None when it was refused (the batch ends)."""
        self.fresh = {}  # one read per element per action ([V2.29] Q2)
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
        if key is not None and not self._may_send(key, observed):
            self._refuse("model", "repeat_send")
            return None
        self._check_budgets()
        sent = Dispatch(
            action=action,
            runtime_id=() if element is None else element.runtime_id,
            role="" if element is None else element.role,
            name="" if element is None else element.name,
            risk=risk,
            provenance=_provenance(action),
        )
        return self._dispatch(sent, element, observed, key)

    def _may_send(self, key: ActionKey, observed: Observed) -> bool:
        """[V2.18]: the same send again only after progress, against the previous turn's last
        send and, within this batch, the last one sent (RT-07)."""
        present = frozenset(e.runtime_id for e in observed.snapshot.elements)
        if not self.strikes.may_send(key, present=present):
            return False
        if self.batch_last is None:
            return True
        earlier, effect = self.batch_last
        return not same_action(earlier, key, present) or made_progress(key.verb, effect)

    def _target(self, action: ActionDoc, listed: Observed, observed: Observed) -> Element | None:
        """§8.1: the element the action names, re-resolved; a press goes to the last acted
        element only when the keyboard focus is read there ([V2.6]) and it still holds the same
        role and name (P4)."""
        if action.verb == "wait":
            return None
        if action.verb == "press":
            return self._press_target(observed)
        if action.target is None:  # validate refuses this shape first
            raise AssertionError("target_missing")
        element = self._listed(action.target, listed, observed)
        if element is None:
            self._refuse("model", "id_not_listed")
        return element

    def _listed(self, handle: int, listed: Observed, now: Observed) -> Element | None:
        """§8.1, [V2.38] RT-17: the element the handle names, only when its RuntimeId is in the
        listing the decision saw and still in `now` with the same role and name."""
        if listed.snapshot.hwnd != now.snapshot.hwnd:
            return None  # another window: nothing listed there (RT-13)
        then = self.handles.element(handle, listed)
        element = self.handles.element(handle, now)
        if then is None or element is None:
            return None
        same = (element.role, normalise_name(element.name)) == (
            then.role,
            normalise_name(then.name),
        )
        return element if same else None

    def _press_target(self, observed: Observed) -> Element | None:
        acted = self.last_acted
        last = None if acted is None else acted.runtime_id
        if not press_allowed(focus=observed.focus, last_acted=last):
            self._refuse("world", "press_not_at_focus")
            return None
        element = next((e for e in observed.snapshot.elements if e.runtime_id == last), None)
        if element is None or acted is None:
            self._refuse("world", "press_target_gone")
            return None
        if (element.role, element.name) != (acted.role, acted.name):
            self._refuse("world", "press_target_changed")
            return None
        return element

    def _risk(self, action: ActionDoc, element: Element | None, observed: Observed) -> Risk | None:
        if element is None:  # a wait sends nothing: only a declared commit raises it
            return COMMIT_RISK[action.commit]
        verdict = action_risk(
            action, element, goal=self.task.goal, observed=observed, pending=self._pending(observed)
        )
        if verdict.refusal is not None:
            self._refuse("policy", verdict.refusal)
            return None
        return verdict.risk

    def _pending(self, observed: Observed) -> list[PendingText]:
        """[V2.37] P2, [V2.38] RT-03: every field typed into in this task that may still hold the
        text, found by RuntimeId or else by its ledger identity under the same top Document.
        Several identity matches keep it pending, read as unread. A replaced top Document, a
        field proven gone (no match of either kind on a stable observation) or emptied ends it."""
        top = top_document(observed.snapshot)
        scope = None if top is None else top.runtime_id
        kept: list[_Typed] = []
        out: list[PendingText] = []
        for typed in self.typed:
            if typed.scope is not None and scope is not None and typed.scope != scope:
                continue  # a replaced top Document ends it; an unreadable one proves nothing
            matches = _matches(typed, observed)
            if not matches:
                if not _complete(observed):
                    kept.append(typed)  # gone is not proven on a read that may be partial
                continue
            field = matches[0]
            now = None if len(matches) > 1 else self._read_value(field, observed)
            if now is not None and not now.filled:
                continue  # emptied: the text is no longer pending
            kept.append(typed)
            out.append(
                PendingText(field.runtime_id, typed.typed, now, typed.search, typed.goal_span)
            )
        self.typed = kept
        return out

    def _readers(self, observed: Observed) -> Readers:
        """[V2.29] Q2: the claim checks' live reads, bound to the ports, taken at the claim and
        never served from an earlier read (#331 review); a secure or secure-unread field is
        never read."""

        def read_value(element: Element) -> ValueFact | None:
            if element.secure or element.id in observed.snapshot.unread_secure:
                return None
            return self.ports.read_value(element)

        return Readers(
            read_state=self.ports.read_state, read_value=read_value, fact_of=self.ports.fact_of
        )

    def _typed_fields(self) -> tuple[tuple[int, ...], ...]:
        """[V2.38] RT-39: the echo guards are task-wide: every field Steer typed into in the
        task, whether its text is still pending or not."""
        return tuple(dict.fromkeys(e.field for e in self.ever))

    def _acted(self, subgoal: Subgoal) -> tuple[int, ...] | None:
        """The field whose Document scopes a subgoal's carriers: the last one typed under it,
        else the last one typed in the task, else none (#331 review B8b)."""
        mine = [e.field for e in self.ever if e.subgoal == subgoal.id]
        if mine:
            return mine[-1]
        return self.ever[-1].field if self.ever else None

    def _echo(self, subgoal: Subgoal) -> bool:
        """[V2.38] RT-39: the subgoal's words are text Steer typed anywhere in the task; any
        generated text counts, since its words are not kept."""
        words = {_folded(span.text) for span in subgoal.params.values()}
        return any(e.words is None or _folded(e.words) in words for e in self.ever)

    def _read_value(self, element: Element, observed: Observed) -> ValueFact | None:
        """[V2.29] Q2: never read a field whose secure flag is set or unread; one read per
        element per action."""
        if element.secure or element.id in observed.snapshot.unread_secure:
            return None
        if element.runtime_id not in self.fresh:
            self.fresh[element.runtime_id] = self.ports.read_value(element)
        return self.fresh[element.runtime_id]

    def _dispatch(
        self,
        sent: Dispatch,
        element: Element | None,
        observed: Observed,
        key: ActionKey | None,
    ) -> tuple[Observed, tuple[ActResult, EffectReport]] | None:
        self.baselines.setdefault(sent.action.subgoal, observed)
        before = observed
        if element is not None:  # [V2.28] item 3: the target's own value, just before dispatch
            before = _with_value(observed, self._read_value(element, observed))
        act = self._act(sent)
        if act.delivery is Delivery.NOT_DELIVERED and not act.action.skipped:
            self._refuse("world", "not_delivered")
            return None
        watched = self.ports.watch(before, sent)
        self.read_costs.append(watched.read_cost)
        self.turn.sent += 1
        report = watched.report
        self.turn.action, self.turn.effect = key, report
        if key is not None:
            self.batch_last = (key, report)
        if element is not None:
            self.last_acted = _Acted(element.runtime_id, element.role, element.name)
        self.history.append(_act_token(sent, act, report))
        if not report.classes <= {"none", "unknown"}:
            self.refusal = None  # [V2.12]: progress resets the refusal that decides the status
        if element is not None and sent.action.verb == "type":
            self._remember_type(sent, element, observed, verified=act.read_back is True)
        return self._after(watched.after), (act, report)

    def _act(self, sent: Dispatch) -> ActResult:
        """[V2.25] (c): one local retry when nothing was sent, under the same decision, unless
        the person stopped the task in between (RT-15)."""
        self.dispatched.append(sent)  # before the call: an act that raises may still have landed
        act = self.ports.act(sent)
        if may_retry(risk=sent.risk, verified=False, report=None, act=act.action):
            self._check_interrupt()
            act = self.ports.act(sent)
        return act

    def _remember_type(
        self, sent: Dispatch, element: Element, observed: Observed, *, verified: bool
    ) -> None:
        """P1/RT-02: every type that may have sent text is pending; its typed value is read only
        after a verified type, else unread (never a query)."""
        self.fresh.pop(element.runtime_id, None)  # the value after the type, not before it
        top = top_document(observed.snapshot)
        typed = _Typed(
            field=element.runtime_id,
            identity=_identity(element),
            scope=None if top is None else top.runtime_id,
            typed=self._read_value(element, observed) if verified else None,
            search=search_facts(observed, element.runtime_id),
            goal_span=sent.provenance == "goal_span",
        )
        self.typed = [t for t in self.typed if t.field != typed.field]
        self.typed.append(typed)
        param = sent.action.param
        subgoal = self._subgoal(sent.action.subgoal)
        words = (
            subgoal.params[param].text if param is not None and param in subgoal.params else None
        )
        self.ever.append(_Ever(element.runtime_id, subgoal.id, words))

    def _after(self, after: Observed) -> Observed:
        """[V2.11] decision 6: reuse the watch's after only when it is full, stable and fresh."""
        after = self._in_domain(after)  # stamped like every observation (RT-11)
        age = self.ports.clock() - after.snapshot.taken_at
        if after.tier == "full" and after.snapshot.stable and age <= REUSE_FRESHNESS_S:
            return after
        return self._observe()

    def _auto_proofs(self, observed: Observed) -> None:
        """§7.1: the code-only proof is tried after every effect, for each open subgoal with a
        change signal since its baseline."""
        for subgoal in self.subgoals:
            if subgoal.id in self.proven or subgoal.id not in self.baselines:
                continue
            if subgoal.closes != "page":
                continue  # [V2.39]: the navigated auto proof closes only a page subgoal
            baseline = self.baselines[subgoal.id]
            if not _changed_since(baseline, observed):
                continue
            proof = auto_proof(
                subgoal,
                baseline,
                observed,
                typed=self._typed_fields(),
                acted=self._acted(subgoal),
                echo=self._echo(subgoal),
                allow_file=self._allow_file(),
            )
            if proof is not None:
                self._tick(subgoal, how=proof)
        self._check_done()

    # ---- refusals, guards -----------------------------------------------------------------

    def _refuse(self, kind: RefusalKind, token: str) -> None:
        """[V2.11] decision 4, [V2.37] P5: nothing dispatched is a refusal with a token and a
        strike; it replaces the latest refusal, whatever its kind."""
        self.refusal = (kind, token)
        self.history.append(f"refused:{token}")
        self.turn.refused = True

    def _guard_left(self, observed: Observed) -> float:
        """[V2.38] RT-24: what is left of the one load guard of this observation's top Document,
        capped by the wall budget left; 0 when it has no Document time. A future or broken stamp
        reads as a Document seen just now (T6, RT-20): the guard never exceeds `LOAD_GUARD_S`."""
        since = observed.document_since
        if since is None:
            return 0.0
        now = self.ports.clock()
        age = now - since
        age = age if math.isfinite(age) and age > 0 else 0.0
        wall_left = self.task.budgets.max_wall_s - (now - self.started)
        return max(0.0, min(LOAD_GUARD_S - age, wall_left))

    def _hold(self, observed: Observed) -> Observed | None:
        """[V2.31], [V2.38] RT-12: the load guard. Before the task ends on a `cannot` stop or on
        the strike limit while the top Document's guard is not spent, wait (the `settle` port)
        until its node count holds or the rest of the guard expires, then re-observe and re-ask:
        once per top Document. None when the guard does not apply. The person's kill is honoured
        right after the wait (RT-23)."""
        top = top_document(observed.snapshot)
        left = self._guard_left(observed)
        if top is None or top.runtime_id in self.reasked or left <= 0:
            return None
        self.reasked.add(top.runtime_id)
        self.history.append("load_guard")
        held = self.ports.settle(observed, left)
        self._check_interrupt()
        return self._in_domain(held)

    def _record_strike(self, observed: Observed) -> Observed:
        if self.turn.neutral:
            return observed
        facts = TurnFacts(
            ticked=self.turn.ticked,
            action=self.turn.action,
            effect=self.turn.effect,
            state_digest=_digest(observed),
            claim_rejected=self.turn.claim_rejected,
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
        """Invariant 9 ([V2.37] P6): actions, calls, tokens, wall time, and cost when priced."""
        used = Usage(
            actions=len(self.dispatched),
            model_calls=self.model_calls,
            cost_usd=self._cost(),
            wall_s=self.ports.clock() - self.started,
            tokens=self.tokens,
        )
        hit = budget_hit(self.task.budgets, used)
        if hit is not None:
            raise _End("paused", Reason(hit))

    def _check_interrupt(self) -> None:
        token = self.ports.interrupted()
        if token is None:
            return
        try:
            cause = Reason(token)
        except ValueError:  # RT-37: a malformed token still stops the task
            cause = Reason("interrupted")
        raise _End("stopped", cause)


# ---- pure helpers -----------------------------------------------------------------------------


def _tokens(usage: TokenUsage | None) -> float:
    """[V2.37] P6: a call's tokens; NaN when the usage is missing or invalid, so the token
    budget trips."""
    if usage is None:
        return math.nan
    counts = (usage.input_tokens, usage.output_tokens)
    if any(isinstance(c, bool) or not isinstance(c, int) or c < 0 for c in counts):
        return math.nan
    return float(sum(counts))


def _shared_rids(observed: Observed) -> frozenset[tuple[int, ...]]:
    """The RuntimeIds more than one element of the snapshot holds."""
    seen: set[tuple[int, ...]] = set()
    shared: set[tuple[int, ...]] = set()
    for element in observed.snapshot.elements:
        rid = element.runtime_id
        if rid:
            (shared if rid in seen else seen).add(rid)
    return frozenset(shared)


def _folded(text: str) -> str:
    """Words as the echo guard compares them: NFKC, collapsed whitespace, case-folded."""
    return normalise_name(text).casefold()


def _proved_param(claim: Claim, subgoal: Subgoal) -> str | None:
    """[V2.39] (2): which param a value or state claim proves. The claim's param is only a hint
    for that; with none, a subgoal with one param (or none) takes it."""
    if claim.param is not None:
        return claim.param if claim.param in subgoal.params else None
    names = list(subgoal.params)
    if len(names) <= 1:
        return names[0] if names else ""
    return None


def _complete(observed: Observed) -> bool:
    """[V2.38] RT-03: a read that can prove a field gone: stable, full tier, and the page neither
    growing nor young (more may still arrive)."""
    settled = observed.snapshot.stable and observed.tier == "full"
    return settled and not observed.growing and not observed.young


def _identity(element: Element) -> Identity:
    return (element.role, normalise_name(element.name), element.automation_id)


def _matches(typed: _Typed, observed: Observed) -> list[Element]:
    """The field by its RuntimeId, else every element with its ledger identity ([V2.38] RT-03)."""
    elements = observed.snapshot.elements
    by_rid = [e for e in elements if e.runtime_id == typed.field]
    if by_rid:
        return by_rid
    return [e for e in elements if e.runtime_id and _identity(e) == typed.identity]


def _with_value(observed: Observed, value: ValueFact | None) -> Observed:
    return replace(observed, acted_value=value)


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
        verb=action.verb, params=params, runtime_id=element.runtime_id, identity=_identity(element)
    )


def _act_token(sent: Dispatch, act: ActResult, report: EffectReport) -> str:
    """§8.6, [V2.38] RT-17 (the minimum): closed tokens only, and no ids: the verb, the delivery
    and the effect classes."""
    classes = ".".join(sorted(report.classes)) or "empty"
    read = {True: "", False: ":read_back_mismatch", None: ""}[act.read_back]
    if sent.action.verb in READ_BACK_VERBS and act.read_back is None:
        read = ":read_back_unread"
    return f"act:{sent.action.verb}:{act.delivery.value}:{classes}{read}"


def _changed_since(baseline: Observed, now: Observed) -> bool:
    """§7.1's change signal: the URL changed (both read), or the top Document was replaced."""
    if baseline.url is not None and now.url is not None and baseline.url != now.url:
        return True
    old, new = top_document(baseline.snapshot), top_document(now.snapshot)
    return old is not None and new is not None and old.runtime_id != new.runtime_id


def _digest(observed: Observed) -> str:
    """The element-set digest for `state_seen` (in memory only, never logged): identity and the
    pattern states, so ticking another box is new state (RT-09)."""
    rows = sorted((e.runtime_id, e.role, e.name, e.caps) for e in observed.snapshot.elements)
    return hashlib.sha256(repr(rows).encode()).hexdigest()
