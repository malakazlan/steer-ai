"""Fakes for the step 3c invariant tests: a small page, effect reports, and a scripted world that
plays the `Ports` of the v2 loop. No site or app names; nothing here touches the OS."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from steerai.loop2.decision import ActionDoc, Claim, Decision, SubgoalDoc
from steerai.loop2.loop import Dispatch, Ports, TurnView
from steerai.loop2.task import Budgets, Envelope, Task
from steerai.world.effects import (
    PRECEDENCE,
    EffectClass,
    EffectReport,
    Observed,
    Token,
    Unread,
)
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.watch import Watched


def pending(rule: str) -> pytest.MarkDecorator:
    """[V2.9] a 3c invariant written before its code: it must fail on the stub's
    `NotImplementedError` and nothing else, and the mark comes off when 3c implements it."""
    return pytest.mark.xfail(strict=True, raises=NotImplementedError, reason=f"3c: {rule}")


GOAL = "search the shop for red shoes"
URL = "https://shop.test/"


def element(
    eid: int,
    role: str,
    name: str,
    rid: tuple[int, ...],
    parent: int | None,
    *,
    secure: bool = False,
) -> Element:
    return Element(
        id=eid,
        depth=0 if parent is None else 1,
        role=role,
        name=name,
        box=(0, 10 * eid, 100, 10 * eid + 8),
        runtime_id=rid,
        parent_id=parent,
        secure=secure,
    )


DOC = element(1, "Document", "Shop", (9, 1), None)
FIELD = element(2, "Edit", "Search", (9, 2), 1)
GO = element(3, "Button", "Go", (9, 3), 1)
SEND = element(4, "Button", "Send", (9, 4), 1)
PASSWORD = element(5, "Edit", "Password", (9, 5), 1, secure=True)
MENU = element(6, "Button", "Options", (9, 6), 1)
ITEM = element(7, "MenuItem", "Dark", (9, 7), 6)
TAB = element(8, "TabItem", "Shop", (9, 8), None)
"""Browser frame: outside the page's Document."""
BAR = element(9, "Edit", "Address and search bar", (9, 9), None)
"""Browser frame: the address bar, outside the page's Document."""
RESULT = element(10, "ListItem", "Red shoes", (9, 10), 1)

PAGE = [DOC, FIELD, GO, SEND, PASSWORD, MENU, TAB, BAR]


def snapshot(elements: Sequence[Element], *, stable: bool = True) -> Snapshot:
    return Snapshot(
        id="s",
        hwnd=0x10,
        process="browser.exe",
        title="Shop",
        taken_at=0.0,
        elements=list(elements),
        node_count=len(elements),
        stable=stable,
    )


def observed(
    elements: Sequence[Element] = PAGE,
    *,
    focus: tuple[int, ...] | None = None,
    searchbox: bool = True,
) -> Observed:
    roles = {FIELD.runtime_id: "searchbox"} if searchbox else {}
    return Observed(
        snapshot=snapshot(elements),
        url=URL,
        windows=(16,),
        focus=focus,
        aria_roles=roles,
    )


def report(
    *classes: EffectClass,
    tokens: Sequence[Token] = (),
    unread: Sequence[Unread] = (),
    primary: EffectClass | None = None,
) -> EffectReport:
    chosen = primary if primary is not None else next((c for c in PRECEDENCE if c in classes), None)
    return EffectReport(
        classes=frozenset(classes),
        tokens=frozenset(tokens),
        unread=frozenset(unread),
        primary=chosen,
        revealed_root=None,
        carriers=frozenset(),
        stable_after=True,
    )


def task(budgets: Budgets | None = None) -> Task:
    """[V2.11] decision 1: built before the first call, with the goal and budgets and no
    subgoals; the first valid Decision fills them."""
    return Task(
        id="t",
        goal=GOAL,
        subgoals=(),
        envelope=Envelope(grants=(), mode="bootstrap"),
        budgets=budgets
        or Budgets(max_actions=20, max_model_calls=10, max_cost_usd=1.0, max_wall_s=60.0),
        history=[],
    )


SUBGOALS = [SubgoalDoc(id="s1", desc="search", params={"text": "red shoes"})]


def act(verb: str, **kw: object) -> ActionDoc:
    base: dict[str, object] = {"verb": verb, "subgoal": "s1"}
    base.update(kw)
    return ActionDoc.model_validate(base)


def decision(*actions: ActionDoc, first: bool = False, claims: Sequence[Claim] = ()) -> Decision:
    return Decision(
        subgoals=SUBGOALS if first else None, actions=list(actions), claims=list(claims)
    )


@dataclass
class World:
    """A scripted world: the controller's decisions in order (then `stop: cannot`), one effect
    report per dispatch in order (then `none`), and a record of everything the loop did."""

    decisions: list[Decision]
    effects: list[EffectReport] = field(default_factory=list)
    screen: Observed = field(default_factory=observed)
    read_backs: list[bool | None] = field(default_factory=list)
    interrupt_after: int | None = None
    """Report a takeover once this many actions were dispatched."""
    after: Observed | None = None
    """What each watch returns as its after snapshot; None: the current screen."""
    watch_s: float = 0.0
    """How far the clock moves during each watch."""
    now: float = 0.0
    events: list[str] = field(default_factory=list)
    """The order of the loop's calls to the world: `decide`, `observe`, `act`, `watch`."""
    views: list[TurnView] = field(default_factory=list)
    sent_at_call: list[int] = field(default_factory=list)
    """How many actions had been dispatched when each controller call was made."""
    dispatched: list[Dispatch] = field(default_factory=list)
    write_aheads: list[Dispatch] = field(default_factory=list)

    def _decide(self, view: TurnView) -> Decision:
        self.events.append("decide")
        self.views.append(view)
        self.sent_at_call.append(len(self.dispatched))
        if self.decisions:
            return self.decisions.pop(0)
        return Decision(stop="cannot")

    def _observe(self) -> Observed:
        self.events.append("observe")
        return self.screen

    def _act(self, sent: Dispatch) -> None:
        self.events.append("act")
        self.dispatched.append(sent)

    def _watch(self, before: Observed, sent: Dispatch) -> Watched:  # noqa: ARG002 - the Ports shape; scripted
        self.events.append("watch")
        self.now += self.watch_s
        effect = self.effects.pop(0) if self.effects else report("none")
        after = self.after if self.after is not None else self.screen
        return Watched(
            report=effect, after=after, first_change_ms=None, settled_ms=None, timed_out=False
        )

    def _read_back(self, sent: Dispatch) -> bool | None:  # noqa: ARG002 - the Ports shape; scripted
        return self.read_backs.pop(0) if self.read_backs else True

    def _write_ahead(self, sent: Dispatch) -> str:
        self.write_aheads.append(sent)
        return "w-000000000000"

    def _interrupted(self) -> str | None:
        if self.interrupt_after is not None and len(self.dispatched) >= self.interrupt_after:
            return "takeover"
        return None

    def ports(self) -> Ports:
        return Ports(
            decide=self._decide,
            observe=self._observe,
            act=self._act,
            watch=self._watch,
            read_back=self._read_back,
            write_ahead=self._write_ahead,
            interrupted=self._interrupted,
            clock=lambda: self.now,
        )
