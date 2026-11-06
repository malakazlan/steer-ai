"""Code-owned launch registries (packet open-multiwindow-2026-11-06, scope item 1): the model
picks an app key and a site key and gives the content; code builds the argv and the window title
the postcondition matches. No model-written path or URL ever reaches `CreateProcess`."""

from __future__ import annotations

from pathlib import Path

import pytest

from steerai.action.launch import (
    APPS,
    SITES,
    LaunchError,
    expected_title,
    launch_argv,
    resolve_exe,
    site_needs_content,
)


def test_the_registries_hold_the_slice_keys_and_every_site_names_a_known_app() -> None:
    # only measured templates ship (schema gate P3): Edge, Explorer, Wikipedia return when measured
    assert set(APPS) == {"chrome", "notepad"}
    assert set(SITES) == {"google_search", "youtube_search", "youtube"}
    for key, site in SITES.items():
        assert site.app in APPS, key
        assert APPS[site.app].takes_url, key
        assert site.url_template.startswith("https://"), key


def test_a_site_url_is_built_by_code_with_the_content_url_encoded() -> None:
    argv = launch_argv("chrome", "youtube_search", "drake latest video & more #1", exe="C:/c.exe")
    assert argv[0] == "C:/c.exe"
    assert (
        argv[-1] == "https://www.youtube.com/results?search_query=drake+latest+video+%26+more+%231"
    )
    assert "--new-window" in argv


def test_an_app_alone_launches_with_no_url() -> None:
    assert launch_argv("notepad", None, None, exe="C:/n.exe") == ["C:/n.exe"]


def test_the_expected_title_is_the_full_window_title_code_owns() -> None:
    assert (
        expected_title("chrome", "youtube_search", "drake latest video")
        == "drake latest video - YouTube - Google Chrome"
    )
    assert expected_title("chrome", "youtube", None) == "YouTube - Google Chrome"
    assert expected_title("notepad", None, None) == "Untitled - Notepad"


@pytest.mark.parametrize(
    ("app", "site", "text", "match"),
    [
        ("calculator", None, None, "unknown app"),
        ("chrome", "made_up", "x", "unknown site"),
        ("notepad", "youtube_search", "x", "opens in"),
        ("chrome", "youtube_search", None, "needs content"),
        ("chrome", "youtube_search", "   ", "needs content"),
        ("chrome", "youtube", "extra", "takes no content"),
        ("chrome", "youtube_search", "x" * 501, "too long"),
        ("chrome", "youtube_search", "bad " + chr(0xD800) + " surrogate", "cannot be encoded"),
    ],
)
def test_refusals_name_the_reason(app: str, site: str | None, text: str | None, match: str) -> None:
    with pytest.raises(LaunchError, match=match):
        launch_argv(app, site, text, exe="C:/x.exe")


def test_a_leading_counter_is_stripped_and_matching_is_full_string() -> None:
    from steerai.action.launch import title_matches  # noqa: PLC0415

    expected = "drake latest video - YouTube - Google Chrome"
    assert title_matches("(3) drake latest video - YouTube - Google Chrome", expected)
    assert title_matches("DRAKE latest  video - YouTube - Google Chrome", expected)
    assert not title_matches("drake latest video - YouTube - Google Chrome (2)", expected)
    assert not title_matches("not drake latest video - YouTube - Google Chrome", expected)
    assert not title_matches("YouTube - Google Chrome", expected)


def test_the_launched_environment_is_an_allowlist() -> None:
    from steerai.action.launch import launch_env  # noqa: PLC0415

    env = launch_env(
        {
            "SystemRoot": "C:/Windows",
            "ANTHROPIC_API_KEY": "k",
            "TYPESAFE_API_KEY": "t",
            "PATH": "C:/bin",
            "USERPROFILE": "C:/Users/p",
            "STEER_X": "1",
        }
    )
    assert env == {"SystemRoot": "C:/Windows", "PATH": "C:/bin", "USERPROFILE": "C:/Users/p"}


def test_content_needs_are_known_per_site() -> None:
    assert site_needs_content("youtube_search") is True
    assert site_needs_content("youtube") is False


def test_the_first_existing_candidate_is_the_exe(tmp_path: Path) -> None:
    second = tmp_path / "b" / "app.exe"
    second.parent.mkdir()
    second.write_bytes(b"")
    missing = tmp_path / "a" / "app.exe"
    assert resolve_exe((str(missing), str(second))) == str(second)
    with pytest.raises(LaunchError, match="not installed"):
        resolve_exe((str(missing),))


def test_candidates_expand_only_the_known_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "Google" / "app.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("STEER_EVIL", str(tmp_path))
    assert resolve_exe(("%ProgramFiles%/Google/app.exe",)) == str(exe)
    with pytest.raises(LaunchError):
        resolve_exe(("%STEER_EVIL%/Google/app.exe",))
