"""Code-owned launch registries (design 5.5 P1 cheapest path; packet open-multiwindow-2026-11-06).

The model names an app key and, for a browser, a site key plus the content; code builds the
argument list and the full window title the `open` postcondition matches. No model-written
executable path or URL ever reaches `CreateProcess`, and no shell is involved.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

MAX_CONTENT_CHARS = 500
CONTENT: Final = "{content}"
_ALLOWED_VARS: Final = frozenset(
    {"ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "SystemRoot"}
)
_VAR = re.compile(r"%([^%]+)%")


class LaunchError(ValueError):
    """A launch the registries refuse; the message names the reason, never the content."""


@dataclass(frozen=True, slots=True)
class AppSpec:
    exe_candidates: tuple[str, ...]
    """Absolute paths; only the variables in `_ALLOWED_VARS` are expanded."""
    process_names: frozenset[str]
    """Lower-case image names whose new top-level window counts as the app's."""
    fresh_title: str
    """The full title of a new window opened with no site."""
    title_suffix: str = ""
    """What the app appends to a page title (a browser's " - Google Chrome")."""
    takes_url: bool = False
    url_flags: tuple[str, ...] = ()
    opens_bare: bool = True
    """Whether the app may open with no site: measured 2026-11-06, a bare Chrome window is
    titled "Google Chrome" (never "New Tab - ..."), too weak to identify, so Chrome needs a site."""


@dataclass(frozen=True, slots=True)
class SiteSpec:
    app: str
    url_template: str
    title_template: str
    names: frozenset[str] = frozenset()
    """The words a goal uses to name the site; content goes out at R1 only when the goal
    names it (hunter OW-02)."""


APPS: Final = MappingProxyType(
    {
        "chrome": AppSpec(
            exe_candidates=(
                "%ProgramFiles%/Google/Chrome/Application/chrome.exe",
                "%ProgramFiles(x86)%/Google/Chrome/Application/chrome.exe",
                "%LOCALAPPDATA%/Google/Chrome/Application/chrome.exe",
            ),
            process_names=frozenset({"chrome.exe"}),
            fresh_title="Google Chrome",  # never used: Chrome opens a site (opens_bare)
            title_suffix=" - Google Chrome",
            takes_url=True,
            url_flags=("--new-window",),
            opens_bare=False,
        ),
        "notepad": AppSpec(
            exe_candidates=("%SystemRoot%/System32/notepad.exe",),
            process_names=frozenset({"notepad.exe"}),
            fresh_title="Untitled - Notepad",  # measured 5/5 on Windows 10; Windows 11 unmeasured
        ),
    }
)

SITES: Final = MappingProxyType(
    {
        "google_search": SiteSpec(
            app="chrome",
            url_template="https://www.google.com/search?hl=en&q={content}",
            title_template="{content} - Google Search",
            names=frozenset({"google"}),
        ),
        "youtube_search": SiteSpec(
            app="chrome",
            url_template="https://www.youtube.com/results?search_query={content}",
            title_template="{content} - YouTube",
            names=frozenset({"youtube"}),
        ),
        "youtube": SiteSpec(
            app="chrome",
            url_template="https://www.youtube.com/",
            title_template="YouTube",
            names=frozenset({"youtube"}),
        ),
    }
)


def site_needs_content(site: str) -> bool:
    return CONTENT in _site(site).url_template


def launch_argv(app: str, site: str | None, text: str | None, *, exe: str) -> list[str]:
    """The argument list for `CreateProcess`: the resolved exe, then the app's URL flags and
    the site URL with the content URL-encoded, or nothing else for an app alone."""
    spec = _app(app)
    url = site_url(app, site, text)
    if url is None:
        return [exe]
    return [exe, *spec.url_flags, url]


def site_url(app: str, site: str | None, text: str | None) -> str | None:
    if site is None:
        if text is not None:
            raise LaunchError("an app opened without a site takes no content")
        if not _app(app).opens_bare:
            raise LaunchError(f"{app} needs a site")
        return None
    spec = _site(site)
    if spec.app != app:
        raise LaunchError(f"site {site} opens in {spec.app}, not {app}")
    content = _content(site, text)
    if content is None:
        return spec.url_template
    try:
        encoded = urllib.parse.quote_plus(content)
    except UnicodeEncodeError as exc:
        raise LaunchError("the content cannot be encoded in a URL") from exc
    return spec.url_template.replace(CONTENT, encoded)


def expected_title(app: str, site: str | None, text: str | None) -> str:
    """The full window title the `open` postcondition matches (full-string, case-insensitive)."""
    spec = _app(app)
    if site is None:
        if text is not None:
            raise LaunchError("an app opened without a site takes no content")
        if not spec.opens_bare:
            raise LaunchError(f"{app} needs a site")
        return spec.fresh_title
    site_spec = _site(site)
    if site_spec.app != app:
        raise LaunchError(f"site {site} opens in {site_spec.app}, not {app}")
    content = _content(site, text)
    page = (
        site_spec.title_template
        if content is None
        else site_spec.title_template.replace(CONTENT, content)
    )
    return page + spec.title_suffix


_COUNTER = re.compile(r"^\(\d{1,4}\)\s+")
"""A leading notification counter a page puts before its title ("(3) Inbox - ..."): code-owned
strip, the `title ~` rule of 5.10 [v1.2], applied to the `open` match only."""
_LAUNCH_ENV: Final = frozenset(
    {
        "SystemRoot",
        "windir",
        "SystemDrive",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "USERNAME",
        "USERDOMAIN",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "ProgramData",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramW6432",
        "CommonProgramFiles",
        "CommonProgramFiles(x86)",
        "COMPUTERNAME",
        "PUBLIC",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
    }
)


def title_matches(title: str, expected: str) -> bool:
    """Full-string, case-insensitive, whitespace-folded, after stripping a leading counter."""
    return _fold(_COUNTER.sub("", title.strip())) == _fold(expected)


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def launch_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The launched app's environment: an allowlist, never the runner's secrets (OW-19)."""
    return {key: value for key, value in environ.items() if key in _LAUNCH_ENV}


def resolve_exe(candidates: Sequence[str]) -> str:
    """The first candidate that exists, with only the known variables expanded."""
    for candidate in candidates:
        path = _expand(candidate)
        if path is not None and Path(path).is_file():
            return os.path.normpath(path)
    raise LaunchError("the app is not installed at a known path")


def _expand(candidate: str) -> str | None:
    missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group(1)
        value = os.environ.get(name) if name in _ALLOWED_VARS else None
        if value is None:
            missing = True
            return ""
        return value

    expanded = _VAR.sub(replace, candidate)
    return None if missing else expanded


def _content(site: str, text: str | None) -> str | None:
    needs = site_needs_content(site)
    if not needs:
        if text is not None:
            raise LaunchError(f"site {site} takes no content")
        return None
    content = " ".join((text or "").split())
    if not content:
        raise LaunchError(f"site {site} needs content")
    if len(content) > MAX_CONTENT_CHARS:
        raise LaunchError(f"content is too long (over {MAX_CONTENT_CHARS} characters)")
    return content


def _app(app: str) -> AppSpec:
    spec = APPS.get(app)
    if spec is None:
        raise LaunchError(f"unknown app {app!r}")
    return spec


def _site(site: str) -> SiteSpec:
    spec = SITES.get(site)
    if spec is None:
        raise LaunchError(f"unknown site {site!r}")
    return spec
