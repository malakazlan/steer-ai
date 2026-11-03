"""Typing-pace probe: pure parts (sample text, summary, delay choice); the live run is a probe."""

from __future__ import annotations

from steerai.probes.typing_pace import Trial, make_sample, pick_delay, summarise


def test_sample_has_the_requested_code_units_and_no_newlines() -> None:
    sample = make_sample(200)
    assert len(sample.encode("utf-16-le")) // 2 == 200
    assert "\n" not in sample
    assert "\r" not in sample
    assert any(ord(ch) > 0x7F for ch in sample), "includes a non-ASCII BMP character"


def test_sample_is_deterministic() -> None:
    assert make_sample(50) == make_sample(50)


def test_summary_groups_by_target_and_delay() -> None:
    trials = [
        Trial(target="notepad", delay_ms=0, units=200, received=198, wall_ms=100.0),
        Trial(target="notepad", delay_ms=0, units=200, received=200, wall_ms=120.0),
        Trial(target="notepad", delay_ms=5, units=200, received=200, wall_ms=250.0),
    ]
    summary = summarise(trials)
    assert summary["notepad"][0]["drops_max"] == 2
    assert summary["notepad"][0]["runs"] == 2
    assert summary["notepad"][0]["wall_ms_p50"] == 100.0
    assert summary["notepad"][5]["drops_max"] == 0


def test_pick_delay_is_the_smallest_with_zero_drops_on_every_target() -> None:
    trials = [
        Trial(target="notepad", delay_ms=0, units=200, received=200, wall_ms=1.0),
        Trial(target="chrome", delay_ms=0, units=200, received=190, wall_ms=1.0),
        Trial(target="notepad", delay_ms=5, units=200, received=200, wall_ms=1.0),
        Trial(target="chrome", delay_ms=5, units=200, received=200, wall_ms=1.0),
        Trial(target="chrome", delay_ms=10, units=200, received=200, wall_ms=1.0),
    ]
    assert pick_delay(summarise(trials)) == 5


def test_pick_delay_is_none_when_every_delay_drops_somewhere() -> None:
    trials = [Trial(target="chrome", delay_ms=0, units=200, received=199, wall_ms=1.0)]
    assert pick_delay(summarise(trials)) is None
