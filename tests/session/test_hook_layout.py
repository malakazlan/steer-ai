"""[V2.60] hook helper (packet rev 2b §3.2, §3.4, K15): the shared words.

Every word is 8 bytes, aligned, with one writer. The status layout is a fixed list of counters,
QPC times, enums, a nonce and a pid. No field can carry a key code, a scan code, a position or
wheel data (K15), and the list is pinned exactly so a new field is a reviewed change."""

from __future__ import annotations

import pytest

from steerai.session import hook_layout
from steerai.session.hook_layout import (
    CONFIG_WORDS,
    KIND_OTHER_STEER,
    KIND_TAKEOVER,
    LATENCY_BUCKETS,
    RUN_TIME_BUCKETS,
    STATUS_WORDS,
    config_size,
    config_words,
    pack_last_user,
    status_size,
    status_words,
    unpack_last_user,
)

PINNED_STATUS = (
    "kill",
    "state",
    "error",
    "pid",
    "nonce_echo_lo",
    "nonce_echo_hi",
    "heartbeat_qpc",
    "heartbeat_count",
    "delivered_keyboard",
    "delivered_mouse",
    "last_own_qpc_keyboard",
    "last_own_qpc_mouse",
    "last_user_qpc_keyboard",
    "last_user_qpc_mouse",
    "last_user",
    "gap_events",
    "resume_events",
    "callback_errors",
    "loop_error",
    "count_user",
    "count_injected_other",
    "count_steer",
    "count_answer",
    "count_other_steer",
    "max_callback_run_qpc",
    "max_loop_gap_qpc",
    "max_event_lag_ms",
    "latency_events",
    "run_time_events",
    "ws_floor",
)
PINNED_CONFIG = (
    "magic",
    "layout_version",
    "run_tag",
    "v1_tag",
    "probe_vk",
    "kill_mods",
    "kill_vk",
    "dead_band_px",
    "heartbeat_ms",
    "hook_timeout_ms",
    "nonce_lo",
    "nonce_hi",
    "crc",
    "ask_depth",
    "kill_ack",
)
NO_INPUT_CONTENT = ("vk", "scan", "key_code", "char", "text", "point", "pos", "_x", "_y", "data")


def test_the_status_layout_is_exactly_the_pinned_list() -> None:
    """K15: a new status field is a reviewed change, never a silent one."""
    assert STATUS_WORDS == PINNED_STATUS


def test_the_config_layout_is_exactly_the_pinned_list() -> None:
    assert CONFIG_WORDS == PINNED_CONFIG


@pytest.mark.parametrize("name", PINNED_STATUS)
def test_no_status_word_can_carry_input_content(name: str) -> None:
    """K15: the helper records timestamps and counters only, never a key code."""
    assert not any(part in name for part in NO_INPUT_CONTENT)


def test_the_status_section_holds_the_words_then_the_two_histograms() -> None:
    assert status_size() == 8 * (len(STATUS_WORDS) + LATENCY_BUCKETS + RUN_TIME_BUCKETS)
    assert config_size() == 8 * len(CONFIG_WORDS)


def test_words_are_eight_byte_aligned_and_distinct() -> None:
    offsets = [hook_layout.status_offset(n) for n in STATUS_WORDS]
    assert offsets == [8 * i for i in range(len(STATUS_WORDS))]
    assert len(set(offsets)) == len(offsets)


def test_a_word_reads_back_what_was_written() -> None:
    buf = bytearray(status_size())
    words = status_words(memoryview(buf))
    words.set("delivered_mouse", 7)
    words.set("heartbeat_qpc", 2**63 - 1)
    assert words.get("delivered_mouse") == 7
    assert words.get("heartbeat_qpc") == 2**63 - 1
    assert words.get("kill") == 0


def test_an_unknown_word_is_refused() -> None:
    words = status_words(memoryview(bytearray(status_size())))
    with pytest.raises(KeyError):
        words.get("vk_code")


def test_a_view_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(ValueError, match="size"):
        status_words(memoryview(bytearray(8)))


def test_the_histograms_are_counters_by_bucket() -> None:
    buf = bytearray(status_size())
    words = status_words(memoryview(buf))
    words.bump_bucket("latency", 3)
    words.bump_bucket("latency", 3)
    words.bump_bucket("run_time", RUN_TIME_BUCKETS + 50)  # past the end: the last bucket
    assert words.bucket("latency", 3) == 2
    assert words.bucket("run_time", RUN_TIME_BUCKETS - 1) == 1


@pytest.mark.parametrize("kind", [KIND_TAKEOVER, KIND_OTHER_STEER])
def test_the_last_user_word_packs_the_time_and_the_kind_together(kind: int) -> None:
    """Rev 2 §3.2: one word, so the time and the kind can never tear apart."""
    word = pack_last_user(123_456_789, kind)
    assert unpack_last_user(word) == (123_456_789, kind)


def test_a_time_that_does_not_fit_in_63_bits_is_refused() -> None:
    with pytest.raises(ValueError, match="63"):
        pack_last_user(2**63, KIND_TAKEOVER)


def test_zero_means_no_person_input_yet() -> None:
    assert unpack_last_user(0) == (0, KIND_TAKEOVER)


def test_steers_own_mutable_config_words_read_back() -> None:
    """`ask_depth` and `kill_ack` are Steer's own words in the config section (rev 2b §6.1)."""
    words = config_words(memoryview(bytearray(config_size())))
    words.set("ask_depth", 1)
    words.set("kill_ack", 1)
    assert (words.get("ask_depth"), words.get("kill_ack")) == (1, 1)


def test_a_word_out_of_range_is_refused() -> None:
    words = config_words(memoryview(bytearray(config_size())))
    with pytest.raises(ValueError, match="word"):
        words.set("ask_depth", -1)


def test_a_zero_time_is_refused_because_zero_means_no_input_yet() -> None:
    """#373 review: 0 is the "no input from anyone yet" sentinel, so it is never packed."""
    with pytest.raises(ValueError, match="zero"):
        pack_last_user(0, KIND_TAKEOVER)
