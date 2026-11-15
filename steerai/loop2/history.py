"""The history the controller sees, as typed records (§8.6; [V2.46], [V2.47], [V2.49]).

Every history entry is a record whose fields are closed tokens, the task's own subgoal ids and
params, and task handles this task issued. `render` is the one function that turns a record into
its line; no writer assembles a line by hand (a test checks that no other module formats one).
`record_ok` checks a record against the task's vocabulary (`Vocab`) and, as a backstop, its line
against the history grammar; the runner drops and counts a record that fails, and the controller
refuses one. No page or model text can be a field: names, values, titles and URLs have no field.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, get_args

from steerai.action.dispositions import Delivery
from steerai.loop2.claims import ClaimToken
from steerai.loop2.consent import CommitToken
from steerai.loop2.consent import RefusalToken as StrictRefusal
from steerai.loop2.decision import ValidateToken, Verb
from steerai.loop2.query import EnterToken, RowId
from steerai.loop2.verify import ClaimCheck, ProofToken
from steerai.world.effects import EffectClass, Token

UIA_CONTROL_TYPES: Final = frozenset(
    {
        "AppBar", "Button", "Calendar", "CheckBox", "ComboBox", "Custom", "DataGrid", "DataItem",
        "Document", "Edit", "Group", "Header", "HeaderItem", "Hyperlink", "Image", "List",
        "ListItem", "MenuBar", "Menu", "MenuItem", "Pane", "ProgressBar", "RadioButton",
        "ScrollBar", "SemanticZoom", "Separator", "Slider", "Spinner", "SplitButton", "StatusBar",
        "Tab", "TabItem", "Table", "Text", "Thumb", "TitleBar", "ToolBar", "ToolTip", "Tree",
        "TreeItem", "Window",
    }
)  # fmt: skip
"""[V2.46]: the UIA control types by their programmatic names (`Element.role` comes from the
`UIA_*ControlTypeId` constants via `control_type_names`): the 41 documented at
https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controltype-ids (50000-50040;
page dated 2025-07-14, read 2026-11-15)."""
LOG_ROLES: Final = frozenset({r.lower() for r in UIA_CONTROL_TYPES} | {"unknown"})
VERBS: Final = frozenset(get_args(Verb))
EFFECT_CLASSES: Final = frozenset(get_args(EffectClass))
EFFECT_TOKENS: Final = frozenset(get_args(Token))
"""The closed effect vocabularies: `unknown:<cause>` only with a cause the observer names."""
DELIVERIES: Final = frozenset(d.value for d in Delivery)
READ_BACK: Final = frozenset({"mismatch", "unread"})
ENTER_TOKENS: Final = frozenset(get_args(EnterToken))
ROWS: Final = frozenset(get_args(RowId))
MAX_LINE: Final = 256
RunnerRefusal = Literal[
    "field_not_empty",
    "focus_needs_field",
    "id_not_listed",
    "not_delivered",
    "not_own_text",
    "press_not_at_focus",
    "press_target_changed",
    "press_target_gone",
    "repeat_send",
    "type_not_at_focus",
]
"""What the runner itself refuses (it imports `history`, so its Literal lives here)."""
PolicyRefusal = Literal["secure_field", "secure_unread", "text_changed"]
"""The type and risk rules' refusals (`loop.type_refusal`, `RiskVerdict.refusal`)."""
RunnerClaim = Literal[
    "evidence_reused", "kind_mismatch", "param_missing", "standard_only", "words_missing",
    "accepted",
]  # fmt: skip
"""The runner's own claim results: `_refused`'s refusals, and a param proof held (`accepted`)."""
RefusalWord = ValidateToken | RunnerRefusal | PolicyRefusal | StrictRefusal | CommitToken
"""#349: a refusal token, by the producers' own Literals ([V2.49]: an enum)."""
ClaimWord = ClaimCheck | ClaimToken | RunnerClaim
"""#349: a claim result: the step 3 checks', the strict tier's (step 4) and the runner's."""
ProvenHow = Literal["claim"] | ProofToken
"""How a subgoal was proven: a claim, or the auto proof's token."""


def _words(annotation: object) -> frozenset[str]:
    """Every string a (union of) Literal type allows."""
    out: set[str] = set()
    for arg in get_args(annotation):
        out |= {arg} if isinstance(arg, str) else _words(arg)
    return frozenset(out)


REFUSAL_TOKENS: Final = _words(RefusalWord)
"""#347 re-review, #349: every refusal token a history line may carry (`Refused.token`)."""
CLAIM_RESULTS: Final = _words(ClaimWord)
"""Every claim result a history line may carry (`ClaimResult.result`)."""
PROVEN_HOW: Final = _words(ProvenHow)
"""Every way a subgoal may be proven (`Proven.how`)."""

WorldMove = Literal["page", "target", "focus"]


def log_role(role: str) -> str:
    """[V2.46]: the role as the history shows it: the control-type enum name, lowercased;
    `unknown` for anything else (an unknown id, a localized type, an ARIA role)."""
    return role.lower() if role in UIA_CONTROL_TYPES else "unknown"


@dataclass(frozen=True)
class LogEntry:
    """[V2.46], [V2.47]: one past action, as closed tokens and the target's task handle. No
    name and no text: a name in the history is page text that would follow the model to later
    pages. No claim result either: a claim result is always its own record."""

    verb: str
    handle: int | None
    """The target's task handle; None for an action with no target."""
    role: str
    """`log_role` of the target's control type."""
    subgoal: str | None
    param: str | None
    """For a type, the name of the param typed (never its text)."""
    delivery: str
    classes: tuple[str, ...]
    tokens: tuple[str, ...]
    read_back: str | None
    """`mismatch` or `unread` for a read-back verb whose read-back did not verify."""

    def token(self) -> str:
        """This record's history line (`render`)."""
        return render(self)


@dataclass(frozen=True)
class Refused:
    """A refusal: its token, and the refused element's task handle when there is one ([V2.47]
    (a)). `id_not_listed` never has one: the model's number may never have been issued."""

    token: RefusalWord
    handle: int | None = None


@dataclass(frozen=True)
class WorldMoved:
    """[V2.43] (2): the world moved after the observation the model saw: the page (a stale
    observation), the named target, or the focus, with that element's handle ([V2.47] (a))."""

    what: WorldMove
    handle: int | None = None


@dataclass(frozen=True)
class FocusMovedAfterPress:
    """[V2.43] (1), #298: after a Space or an Enter on a state-set element, the watch's after-read
    shows the focus off the target (an arrow key is exempt: it moves focus by design). The model
    sees it, with the target's handle; the outcome counts it."""

    handle: int | None = None


@dataclass(frozen=True)
class ClaimResult:
    """[V2.47] (b): a claim's result is its own record: a refusal token, `accepted` (one param's
    proof held, the subgoal not yet closed) or `standard_only`."""

    subgoal: str
    result: ClaimWord


@dataclass(frozen=True)
class Proven:
    """A subgoal proven, and how (`claim`, or the auto proof that closed it)."""

    subgoal: str
    how: ProvenHow


@dataclass(frozen=True)
class Entered:
    """[V2.21] #24: the Enter table's outcome token and the row that matched."""

    token: str
    row: str


@dataclass(frozen=True)
class LoadGuard:
    """[V2.31]: the load guard held the page before the decision."""


HistoryRecord = (
    LogEntry
    | Refused
    | WorldMoved
    | FocusMovedAfterPress
    | ClaimResult
    | Proven
    | Entered
    | LoadGuard
)
RECORD_TYPES: Final = (
    LogEntry,
    Refused,
    WorldMoved,
    FocusMovedAfterPress,
    ClaimResult,
    Proven,
    Entered,
    LoadGuard,
)
_MOVED: Final[Mapping[str, str]] = {
    "page": "stale_observation",
    "target": "target_changed",
    "focus": "focus_lost",
}


def render(record: HistoryRecord) -> str:  # noqa: PLR0911 - one form per record kind
    """The one function that turns a history record into its line (§8.6)."""
    if isinstance(record, LogEntry):
        parts = [
            "act",
            record.verb,
            "-" if record.handle is None else str(record.handle),
            record.role,
            record.subgoal or "-",
            record.param or "-",
            record.delivery,
            ".".join(sorted(record.classes)) or "empty",
            ".".join(sorted(record.tokens)) or "none",
        ]
        if record.read_back is not None:
            parts.append(f"read_back_{record.read_back}")
        return ":".join(parts)
    if isinstance(record, Refused):
        return f"refused:{record.token}" + _slot(record.handle)
    if isinstance(record, FocusMovedAfterPress):
        return "focus_moved_after_press" + _slot(record.handle)
    if isinstance(record, WorldMoved):
        tail = "" if record.what == "page" else _slot(record.handle)
        return _MOVED[record.what] + tail
    if isinstance(record, ClaimResult):
        return f"claim:{record.subgoal}:{record.result}"
    if isinstance(record, Proven):
        return f"proven:{record.subgoal}:{record.how}"
    if isinstance(record, Entered):
        return f"enter:{record.token}:{record.row}"
    return "load_guard"


def _slot(handle: int | None) -> str:
    return "" if handle is None else f":{handle}"


@dataclass(frozen=True)
class Vocab:
    """[V2.47], [V2.49]: what a history record may name: the task's frozen subgoals (id to its
    params) and the task handles this task issued."""

    known: Mapping[str, frozenset[str]]
    issued: frozenset[int]


def _alt(words: frozenset[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_H = r"[0-9]{1,9}"
_ID = r"[a-z0-9_]{1,24}"
_CLS = _alt(EFFECT_CLASSES)
_TOK = _alt(EFFECT_TOKENS)
def history_grammar(subgoal: str, param: str) -> re.Pattern[str]:
    """[V2.47], #347 review: one form per kind of line, every token from its closed set (no
    shape check is left). A handle slot is digits only; `id_not_listed` has none. The subgoal
    and param slots are given: the model's own ids for the history (`GRAMMAR`), positions for
    the redacted trace (`POSITIONAL_GRAMMAR`, [V2.67] D)."""
    return re.compile(
        "|".join(
            (
                rf"act:(?:{_alt(VERBS)}):(?:{_H}|-):(?:{_alt(LOG_ROLES)}):(?:{subgoal})"
                rf":(?:{param}|-):(?:{_alt(DELIVERIES)}):(?:empty|(?:{_CLS})(?:\.(?:{_CLS}))*)"
                rf":(?:none|(?:{_TOK})(?:\.(?:{_TOK}))*)(?::read_back_(?:mismatch|unread))?",
                r"refused:id_not_listed",
                rf"refused:(?:{_alt(REFUSAL_TOKENS - {'id_not_listed'})})(?::{_H})?",
                rf"(?:target_changed|focus_lost|focus_moved_after_press)(?::{_H})?",
                r"stale_observation",
                r"load_guard",
                rf"enter:(?:{_alt(ENTER_TOKENS)}):(?:{_alt(ROWS)})",
                rf"claim:(?:{subgoal}):(?:{_alt(CLAIM_RESULTS)})",
                rf"proven:(?:{subgoal}):(?:{_alt(PROVEN_HOW)})",
            )
        )
    )


GRAMMAR: Final = history_grammar(_ID, _ID)
"""The history's backstop on `render`: subgoal ids and param names as the model wrote them."""
POSITIONAL_GRAMMAR: Final = history_grammar(r"s[0-9]{1,3}", r"param:[0-9]{1,3}")
"""[V2.67] D ruling (a): the redacted trace's lines, every model-chosen name replaced by its
position (`turn_trace.positional`): a raw subgoal id or param name fails it."""


def record_ok(record: HistoryRecord, vocab: Vocab) -> bool:
    """[V2.47], [V2.49]: the record may be shown. Every field is in its closed set, every
    subgoal id and param is the task's own, every handle is one this task issued, and the line
    `render` gives matches the grammar. A record that fails is dropped and counted by the
    runner and refused by the controller, never shown."""
    if not isinstance(record, RECORD_TYPES) or not _fields_ok(record, vocab):
        return False
    line = render(record)
    return len(line) <= MAX_LINE and GRAMMAR.fullmatch(line) is not None


def _handle_ok(handle: int | None, vocab: Vocab) -> bool:
    if handle is None:
        return True
    return not isinstance(handle, bool) and isinstance(handle, int) and handle in vocab.issued


def _fields_ok(record: HistoryRecord, vocab: Vocab) -> bool:  # noqa: PLR0911 - one rule per kind
    if isinstance(record, LogEntry):
        return _entry_ok(record, vocab)
    if isinstance(record, Refused):
        no_slot = record.token != "id_not_listed" or record.handle is None  # noqa: S105 - a refusal token
        return record.token in REFUSAL_TOKENS and no_slot and _handle_ok(record.handle, vocab)
    if isinstance(record, FocusMovedAfterPress):
        return _handle_ok(record.handle, vocab)
    if isinstance(record, WorldMoved):
        no_slot = record.what != "page" or record.handle is None
        return record.what in _MOVED and no_slot and _handle_ok(record.handle, vocab)
    if isinstance(record, ClaimResult):
        return record.subgoal in vocab.known and record.result in CLAIM_RESULTS
    if isinstance(record, Proven):
        return record.subgoal in vocab.known and record.how in PROVEN_HOW
    if isinstance(record, Entered):
        return record.token in ENTER_TOKENS and record.row in ROWS
    return True


def _entry_ok(entry: LogEntry, vocab: Vocab) -> bool:
    sid, param = entry.subgoal, entry.param
    if sid is None or sid not in vocab.known:
        return False
    return (
        (param is None or param in vocab.known[sid])
        and _handle_ok(entry.handle, vocab)
        and entry.verb in VERBS
        and entry.role in LOG_ROLES
        and entry.delivery in DELIVERIES
        and set(entry.classes) <= EFFECT_CLASSES
        and set(entry.tokens) <= EFFECT_TOKENS
        and (entry.read_back is None or entry.read_back in READ_BACK)
    )
