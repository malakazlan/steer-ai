"""Chrome's address bar through UIA (#148 design D item 3, probe #157 on Chrome 156).

Found by ClassName `OmniboxViewViews`, never by name (localized) or AutomationId (a per-build
view id); FindAll, never FindFirst, then only an element with no Document above it is kept: a
page's own node may carry the class (Chromium maps an HTML class to the UIA ClassName). The value
is the URL with the scheme and "www." hidden; while focused it is typed text and autocomplete,
so a focused omnibox is no URL.
"""

from __future__ import annotations

from typing import Any

OMNIBOX_CLASS = "OmniboxViewViews"
MAX_DEPTH = 60
"""Ancestors walked above a candidate; a chain longer than this is not trusted (Chrome 156:
the omnibox sits about ten levels below the window, unmeasured past that build)."""


def read_address_bar(uia: Any, mod: Any, hwnd: int) -> str | None:  # Any: comtypes, no stubs
    """The one omnibox of the window, out of focus, with a value; else None."""
    root = uia.ElementFromHandle(hwnd)
    found = root.FindAll(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_ClassNamePropertyId, OMNIBOX_CLASS),
    )
    kept = [
        element
        for element in (found.GetElement(i) for i in range(found.Length))
        if _browser_owned(uia, mod, element)
    ]
    if len(kept) != 1 or kept[0].CurrentHasKeyboardFocus:
        return None
    pattern = (
        kept[0]
        .GetCurrentPattern(mod.UIA_ValuePatternId)
        .QueryInterface(mod.IUIAutomationValuePattern)
    )
    value = pattern.CurrentValue
    return str(value) if value else None


def read_document_url(uia: Any, mod: Any, hwnd: int) -> str | None:  # Any: comtypes
    """The URL of the window's one top-level Document (no Document above it), which Chromium
    exposes as the Document's value (probe `document_url`: results pages read their full URL,
    error and certificate pages `chrome-error://chromewebdata/`, a pending navigation the old
    URL); None when there is no such Document, more than one, or no value."""
    root = uia.ElementFromHandle(hwnd)
    found = root.FindAll(
        mod.TreeScope_Descendants,
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, mod.UIA_DocumentControlTypeId),
    )
    tops = [
        element
        for element in (found.GetElement(i) for i in range(found.Length))
        if _browser_owned(uia, mod, element)
    ]
    if len(tops) != 1:
        return None
    pattern = tops[0].GetCurrentPattern(mod.UIA_ValuePatternId)
    if not pattern:
        return None
    value = pattern.QueryInterface(mod.IUIAutomationValuePattern).CurrentValue
    return str(value) if value else None


def _browser_owned(uia: Any, mod: Any, element: Any) -> bool:  # Any: comtypes, no stubs
    """No Document between the element and the root, within MAX_DEPTH ancestors."""
    node = element
    for _ in range(MAX_DEPTH):
        node = uia.ControlViewWalker.GetParentElement(node)
        if not node:
            return True
        if node.CurrentControlType == mod.UIA_DocumentControlTypeId:
            return False
    return False
