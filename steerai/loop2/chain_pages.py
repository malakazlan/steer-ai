"""The chain eval's fake pages (intake packet rev 5 §8): one scripted page per shape, played as
the loop's world ports, so the real controller, intake check and audit run on it through the
same `Ports` a live task uses.

A page is data (`chain_shapes.json`), built here from a closed set of control kinds and
behaviours, never from a site or an app:
- `field`: an Edit; a `type` sets its text and reads it back;
- `select`: a ComboBox with its options; a `choose` of one of them sets it and reads it back;
- `switch`: a Button with Toggle (`toggle:off`); `check`/`uncheck`, or a click (Invoke toggles
  a switch), set it and read back;
- `checkbox`: a CheckBox, unchecked; as the switch;
- `button`: a Button; its click changes nothing, unless it carries `search`.
A control with `search: results` makes an Enter in the field, or a click on the button, open a
results page for the field's text (`?q=<text>`, the title `<text> - Articles`, result items);
`search: nothing` changes nothing, ever.

What changed is reported by the real effect classifier (`effects.observe`) on the page's own
before and after reads. Values are digests under a per-chain key, as the live world reads them.
The oracle (`outcome_shown`) reads the page's own state, never Steer's report.
"""

from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import quote_plus

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode
from steerai.loop2.loop import READ_BACK_VERBS, ActResult, Dispatch
from steerai.world.effects import Observed, ValueFact, observe, value_fact
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from steerai.world.watch import Watched

Kind = Literal["field", "select", "switch", "checkbox", "button"]
Search = Literal["nothing", "results"]
KINDS: Final = frozenset({"field", "select", "switch", "checkbox", "button"})
SEARCHES: Final = frozenset({"nothing", "results"})
ROLES: Final[Mapping[str, str]] = {
    "field": "Edit",
    "select": "ComboBox",
    "switch": "Button",
    "checkbox": "CheckBox",
    "button": "Button",
}
HWND: Final = 0x4C0
PROCESS: Final = "chrome.exe"
BASE_URL: Final = "https://pages.test/"
RESULTS: Final = 3
"""Result items a results page shows."""
TICK_S: Final = 0.01
"""How far the fake clock moves on each read of it: well inside every freshness bound."""


@dataclass
class Control:
    kind: Kind
    name: str
    options: tuple[str, ...] = ()
    search: Search | None = None
    text: str = ""
    """A field's text, or a select's chosen option."""
    on: bool = False


@dataclass
class Page:
    """One shape's page and its state. Element ids and RuntimeIds are fixed by position; a
    results page is a new top Document (a new RuntimeId), as a navigation is."""

    title: str
    controls: list[Control]
    url: str = BASE_URL
    query: str | None = None
    """The text a results page was opened for; None while no results page was ever shown."""
    document: int = 1
    focus: int | None = None
    """The focused control's index; None: the Document."""
    taken: int = 0

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> Page:  # Any: JSON
        controls = []
        for raw in spec["controls"]:
            kind = raw["kind"]
            if kind not in KINDS:
                raise ValueError("unknown control kind")
            search = raw.get("search")
            if search is not None and search not in SEARCHES:
                raise ValueError("unknown search behaviour")
            controls.append(Control(kind, str(raw["name"]), tuple(raw.get("options", ())), search))
        return cls(title=str(spec["title"]), controls=controls)

    # ---- the reads -----------------------------------------------------------------------------

    def elements(self) -> list[Element]:
        doc_rid = (42, self.document)
        out = [
            Element(
                id=1,
                depth=0,
                role="Document",
                name=self.title if self.query is None else f"{self.query} - {self.title}",
                box=(0, 0, 1000, 640),
                runtime_id=doc_rid,
            )
        ]
        if self.query is not None:
            out += [
                Element(
                    id=100 + i,
                    depth=1,
                    role="ListItem",
                    name=f"{self.query} result {i + 1}",
                    box=(10, 200 + 30 * i, 600, 220 + 30 * i),
                    runtime_id=(42, self.document, 100 + i),
                    parent_id=1,
                )
                for i in range(RESULTS)
            ]
            return out
        for i, control in enumerate(self.controls):
            out.append(
                Element(
                    id=10 + i,
                    depth=1,
                    role=ROLES[control.kind],
                    name=control.name,
                    box=(10, 40 + 40 * i, 400, 70 + 40 * i),
                    runtime_id=self.rid(i),
                    parent_id=1,
                    caps=_caps(control),
                )
            )
        return out

    def rid(self, index: int) -> tuple[int, ...]:
        return (42, self.document, 10 + index)

    def roles(self) -> dict[tuple[int, ...], str]:
        """The AriaRole map as the real observer reports it: a text field that carries a `search`
        behaviour is a `searchbox`; no other control has an entry. A results page has no field."""
        if self.query is not None:
            return {}
        return {
            self.rid(i): "searchbox"
            for i, c in enumerate(self.controls)
            if c.kind == "field" and c.search is not None
        }

    def observed(self) -> Observed:
        self.taken += 1
        elements = self.elements()
        title = elements[0].name
        snap = Snapshot(
            id=f"chain-{self.taken}",
            hwnd=HWND,
            process=PROCESS,
            title=f"{title} - Google Chrome",
            taken_at=float(self.taken),
            elements=elements,
            node_count=len(elements),
            stable=True,
        )
        focus = (42, self.document) if self.focus is None else self.rid(self.focus)
        return Observed(
            snapshot=snap,
            url=self.url,
            windows=(HWND,),
            focus=focus,
            headings={},
            aria_roles=self.roles(),
            document_url=self.url,
            landmarks={e.runtime_id: None for e in elements},
        )

    def index_of(self, rid: tuple[int, ...]) -> int | None:
        for i in range(len(self.controls)):
            if self.query is None and self.rid(i) == rid:
                return i
        return None

    # ---- the scripted behaviour ----------------------------------------------------------------

    def act(self, sent: Dispatch) -> bool | None:  # noqa: PLR0911, PLR0912 - a rule per verb
        """Apply one dispatch; the read-back of a read-back verb (True, False), else None."""
        verb = sent.action.verb
        index = self.index_of(sent.runtime_id) if sent.runtime_id else self.focus
        control = None if index is None else self.controls[index]
        if control is None:
            return False if verb in READ_BACK_VERBS else None
        if verb in {"focus", "type", "click", "choose", "check", "uncheck"}:
            self.focus = index
        if verb == "focus":
            return True
        if verb == "type":
            if control.kind != "field":
                return False
            control.text = sent.text or ""
            return True
        if verb == "choose":
            option = sent.action.option
            if control.kind != "select" or option not in control.options:
                return False
            control.text = option
            return True
        if verb in {"check", "uncheck"}:
            if control.kind not in {"switch", "checkbox"}:
                return False
            control.on = verb == "check"
            return True
        if verb == "click":
            if control.kind in {"switch", "checkbox"}:
                control.on = not control.on
            elif control.kind == "button" and control.search == "results":
                self._search()
            return None
        if verb == "press" and sent.action.key == "Enter":
            if control.kind == "field" and control.search == "results":
                self._search()
            return None
        return None if verb not in READ_BACK_VERBS else False

    def _search(self) -> None:
        typed = next((c.text for c in self.controls if c.kind == "field"), "")
        if not typed:
            return
        self.query = typed
        self.document += 1
        self.url = f"{BASE_URL}?q={quote_plus(typed)}"
        self.focus = None

    # ---- the oracle ----------------------------------------------------------------------------

    def holds(self, fact: Mapping[str, Any]) -> bool:  # Any: JSON
        """One oracle fact on the page's own state: a field or select holds a text, a toggle is
        on, a results page was shown for a query, or `never` (a state this page cannot reach)."""
        if "value" in fact:
            name, wanted = fact["value"]
            return any(c.name == name and c.text == wanted for c in self.controls)
        if "on" in fact:
            return any(c.name == fact["on"] and c.on for c in self.controls)
        if "results" in fact:
            return self.query is not None and self.query == str(fact["results"])
        if "never" in fact:
            return False
        raise ValueError("unknown oracle fact")


def _caps(control: Control) -> tuple[str, ...]:
    if control.kind == "field":
        return ("editable",)
    if control.kind == "select":
        return ("collapsed", "readonly")
    if control.kind in {"switch", "checkbox"}:
        return ("toggle:on",) if control.on else ("toggle:off",)
    return ()


def outcome_shown(page: Page, oracle: Sequence[Mapping[str, Any]]) -> bool:  # Any: JSON
    """The request's full outcome is on the page: every oracle fact holds."""
    return all(page.holds(fact) for fact in oracle)


@dataclass
class PageWorld:
    """The page as the loop's world ports. The clock moves one tick per call that reads it."""

    page: Page
    key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    now: float = 0.0
    typed: list[str] = field(default_factory=list)
    """The free texts Steer typed (a `type` with no param), for the audit mask measure."""

    def clock(self) -> float:
        self.now += TICK_S
        return self.now

    def observe(self) -> Observed:
        return self._stamped(self.page.observed())

    def _stamped(self, seen: Observed) -> Observed:
        snap = dataclasses.replace(seen.snapshot, taken_at=self.now)
        return dataclasses.replace(seen, snapshot=snap, document_since=0.0)

    def stamp(self, seen: Observed) -> Observed:
        return seen

    def settle(self, first: Observed, guard_s: float) -> Observed:  # noqa: ARG002 - the Ports shape
        self.now += TICK_S
        return self.observe()

    def act(self, sent: Dispatch) -> ActResult:
        if sent.action.verb == "type" and sent.action.param is None and sent.text:
            self.typed.append(sent.text)
        read_back = self.page.act(sent)
        verb = sent.action.verb
        if verb not in READ_BACK_VERBS:
            return ActResult(ActionResult(verb=verb, delivery=Delivery.DELIVERED_VERIFIED), None)
        if read_back:
            return ActResult(
                ActionResult(verb=verb, delivery=Delivery.DELIVERED_VERIFIED), read_back=True
            )
        refused = ActionResult(
            verb=verb,
            delivery=Delivery.NOT_DELIVERED,
            code=ErrorCode.ELEMENT_NOT_ACTIONABLE,
            evidence={"detail": "no_pattern"},
        )
        return ActResult(refused, None)

    def watch(self, before: Observed, sent: Dispatch) -> Watched:
        after = self.observe()
        report = observe(before, after, acted=sent.runtime_id or None, text=sent.text)
        return Watched(
            report=report, after=after, first_change_ms=None, settled_ms=None, timed_out=False
        )

    def read_value(self, element: Element) -> ValueFact | None:
        index = self.page.index_of(element.runtime_id)
        if index is None:
            return None
        return value_fact(self.page.controls[index].text, key=self.key)

    def read_state(self, element: Element, wanted: str) -> bool | None:
        index = self.page.index_of(element.runtime_id)
        if index is None:
            return None
        control = self.page.controls[index]
        if wanted in {"on", "off"} and control.kind in {"switch", "checkbox"}:
            return control.on == (wanted == "on")
        if wanted == "collapsed" and control.kind == "select":
            return True
        if wanted == "expanded" and control.kind == "select":
            return False
        return None

    def fact_of(self, text: str) -> ValueFact:
        return value_fact(text, key=self.key)

    @staticmethod
    def write_ahead(_sent: Dispatch) -> str:
        return "w-chain"

    @staticmethod
    def interrupted() -> str | None:
        return None
