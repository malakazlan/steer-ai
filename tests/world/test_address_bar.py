"""Chrome's address bar through UIA (#148 design D item 3, probe #157): FindAll by ClassName
`OmniboxViewViews` (the name is localized), keep only an element with no Document above it (a
page's own node may carry the class), exactly one, out of focus, with a value."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from steerai.world.address_bar import (
    OMNIBOX_CLASS,
    DocumentUrl,
    read_address_bar,
    read_document_url,
)

DOCUMENT, PANE, EDIT = 50030, 50033, 50004


@dataclass(eq=False)
class Node:
    control_type: int
    parent: Node | None = None
    value: str = ""
    focused: bool = False
    patterns: list[int] = field(default_factory=list)
    runtime_id: tuple[int, ...] = (42, 7)

    def GetRuntimeId(self) -> tuple[int, ...]:  # noqa: N802 - the COM method name
        return self.runtime_id

    @property
    def CurrentControlType(self) -> int:  # noqa: N802 - the COM property name
        return self.control_type

    @property
    def CurrentHasKeyboardFocus(self) -> bool:  # noqa: N802
        return self.focused

    def GetCurrentPattern(self, pattern_id: int) -> Node:  # noqa: N802
        self.patterns.append(pattern_id)
        return self

    def QueryInterface(self, _interface: object) -> Node:  # noqa: N802
        return self

    @property
    def CurrentValue(self) -> str:  # noqa: N802
        return self.value


class Found:
    def __init__(self, nodes: list[Node]) -> None:
        self.nodes = nodes

    @property
    def Length(self) -> int:  # noqa: N802
        return len(self.nodes)

    def GetElement(self, index: int) -> Node:  # noqa: N802
        return self.nodes[index]


class Walker:
    def GetParentElement(self, node: Node) -> Node | None:  # noqa: N802
        return node.parent


class Root(Node):
    def __init__(self, found: list[Node]) -> None:
        super().__init__(PANE)
        self.found = found
        self.conditions: list[Any] = []

    def FindAll(self, _scope: int, condition: Any) -> Found:  # noqa: N802
        self.conditions.append(condition)
        return Found(self.found)


class Uia:
    ControlViewWalker = Walker()

    def __init__(self, root: Root) -> None:
        self.root = root

    def ElementFromHandle(self, _hwnd: int) -> Root:  # noqa: N802
        return self.root

    def CreatePropertyCondition(self, prop: int, value: object) -> tuple[int, object]:  # noqa: N802
        return (prop, value)


class Mod:
    TreeScope_Descendants = 4
    UIA_ClassNamePropertyId = 30012
    UIA_DocumentControlTypeId = DOCUMENT
    UIA_ValuePatternId = 10002
    IUIAutomationValuePattern = object()


def _read(*found: Node) -> str | None:
    root = Root(list(found))
    for node in found:
        if node.parent is None:
            node.parent = root
    return read_address_bar(Uia(root), Mod, 0x10)


def test_the_omnibox_value_is_read_by_class_name() -> None:
    root = Root([])
    omnibox = Node(EDIT, parent=Node(PANE, parent=root), value="www.bing.com/search?q=x")
    root.found = [omnibox]
    assert read_address_bar(Uia(root), Mod, 0x10) == "www.bing.com/search?q=x"
    assert root.conditions == [(Mod.UIA_ClassNamePropertyId, OMNIBOX_CLASS)]


def test_om04_a_page_node_carrying_the_class_is_ignored() -> None:
    page = Node(EDIT, parent=Node(DOCUMENT), value="evil.example/search?q=x")
    assert _read(Node(EDIT, value="www.bing.com/"), page) == "www.bing.com/"
    assert _read(page) is None


def test_two_omniboxes_are_ambiguous() -> None:
    assert _read(Node(EDIT, value="a.org/"), Node(EDIT, value="b.org/")) is None


def test_om05_a_focused_omnibox_is_no_url() -> None:
    """While focused it shows typed text and inline autocomplete, not the page's URL."""
    assert _read(Node(EDIT, value="hello there radio", focused=True)) is None


def test_an_empty_or_missing_omnibox_is_no_url() -> None:
    assert _read(Node(EDIT, value="")) is None
    assert _read() is None


def test_an_unending_ancestor_chain_is_not_trusted() -> None:
    node = Node(EDIT, value="a.org/")
    node.parent = node  # a provider that never reaches the root
    root = Root([node])
    assert read_address_bar(Uia(root), Mod, 0x10) is None


class DocMod(Mod):
    UIA_ControlTypePropertyId = 30003


def _doc_url(*found: Node) -> DocumentUrl | None:
    root = Root(list(found))
    for node in found:
        if node.parent is None:
            node.parent = root
    return read_document_url(Uia(root), DocMod, 0x10)


def test_the_top_document_url_is_read() -> None:
    """Probe document_url (Chrome 156): Chromium fills the top Document's value with its URL;
    error and certificate pages read chrome-error://chromewebdata/."""
    top = Node(DOCUMENT, value="https://developer.mozilla.org/en-US/search?q=flexbox")
    frame = Node(DOCUMENT, parent=top, value="https://ads.example/frame")
    assert _doc_url(top, frame) == DocumentUrl(
        runtime_id=(42, 7), url="https://developer.mozilla.org/en-US/search?q=flexbox"
    )


def test_the_document_url_carries_the_runtime_id_it_was_read_from() -> None:
    """Principal item 1 (H4 packet): the URL is bound to the Document whose RuntimeId the
    snapshot judges new, read in one go (H4-17)."""
    top = Node(DOCUMENT, value="https://rubygems.org/search?query=rails", runtime_id=(42, 91))
    got = _doc_url(top)
    assert got is not None
    assert got.runtime_id == (42, 91)
    assert got.url == "https://rubygems.org/search?query=rails"
    assert _doc_url(Node(DOCUMENT, value="https://a.org/", runtime_id=())) is None, "no rid"


def test_no_or_two_top_documents_or_an_empty_value_is_none() -> None:
    assert _doc_url() is None
    assert _doc_url(Node(DOCUMENT, value="a"), Node(DOCUMENT, value="b")) is None
    assert _doc_url(Node(DOCUMENT, value="")) is None
