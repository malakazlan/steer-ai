"""#492, [V2.81]: each probe carries a fresh random 32-bit tag, on both hooks; the classifier's
order (probe, run tag, retired probe, foreign); the layout pin; the ledger's lock."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator

import pytest

from steerai.session.hook_classifier import (
    LLKHF_INJECTED,
    LLKHF_UP,
    LLMHF_INJECTED,
    RECENT_DRAWN,
    RETIRED_KEPT,
    TAKEOVER,
    V1_TAG,
    Classifier,
    ProbeTags,
)
from steerai.session.hook_layout import CONFIG_WORDS, LAYOUT_VERSION, STATUS_WORDS
from steerai.session.probe_ledger import ProbeLedger

RUN = 0x1234_5678
PROBE = 0x0E
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
INJ_UP = LLKHF_INJECTED | LLKHF_UP

LAYOUT_DIGESTS = {4: "0d87873d6ce8558cd0f774153f738937012ad9a27fb5251092775488bed74ab2"}
"""LAYOUT_VERSION -> sha256 of the status and config word lists. A changed word list fails the
test until the version moves and a new digest is pinned."""


def _digest() -> str:
    text = "|".join(("status", *STATUS_WORDS, "config", *CONFIG_WORDS))
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_word_lists_are_pinned_to_the_layout_version() -> None:
    assert LAYOUT_VERSION == 4, "the words carry probe tags now ([V2.81]): version 4"
    assert LAYOUT_DIGESTS[LAYOUT_VERSION] == _digest()


# ---- the probe tags -------------------------------------------------------------------------


def _scripted(*values: int) -> ProbeTags:
    it: Iterator[int] = iter(values)
    return ProbeTags(RUN, rand32=lambda: next(it))


def test_a_probe_tag_is_redrawn_when_it_is_zero_the_run_tag_v1s_or_a_recent_probes() -> None:
    tags = _scripted(0, RUN, V1_TAG, 5, 5, 7)
    assert tags.draw() == 5  # 0, the run tag and v1's tag were refused
    assert tags.draw() == 7  # 5 is a recent probe tag


def test_every_drawn_tag_is_a_nonzero_32_bit_value_and_all_differ() -> None:
    tags = ProbeTags(RUN)
    drawn = [tags.draw() for _ in range(500)]
    assert all(0 < t < 1 << 32 for t in drawn)
    assert len(set(drawn)) == len(drawn)
    assert RUN not in drawn
    assert V1_TAG not in drawn


def test_a_run_tag_wider_than_32_bits_is_refused() -> None:
    with pytest.raises(ValueError, match="32-bit"):
        ProbeTags(1 << 32)
    with pytest.raises(ValueError, match="32-bit"):
        ProbeTags(0)


# ---- the classifier's order -----------------------------------------------------------------


def _c(*, v2: bool = True) -> Classifier:
    return Classifier(started_at=1.0, extra_info=RUN, probe_vk=PROBE, probe_tags=v2)


def _move(c: Classifier, extra: int, *, flags: int = LLMHF_INJECTED, now: float = 2.0) -> None:
    c.on_mouse(message=WM_MOUSEMOVE, flags=flags, extra=extra, pt=(1, 1), now=now)


def _key(c: Classifier, extra: int, *, flags: int = INJ_UP, vk: int = PROBE) -> None:
    c.on_keyboard(flags=flags, extra=extra, now=2.0, vk=vk)


def test_the_outstanding_probe_tag_proves_each_hook() -> None:
    c = _c()
    c.set_probe_tag(0xAAAA_0001)
    _move(c, 0xAAAA_0001)
    assert (c.proof("mouse"), c.proof("keyboard")) == (0xAAAA_0001, 0)
    _key(c, 0xAAAA_0001)
    assert c.proof("keyboard") == 0xAAAA_0001
    assert c.last_user() is None


def test_an_action_event_with_the_run_tag_proves_nothing_and_is_not_the_person() -> None:
    c = _c()
    c.set_probe_tag(0xAAAA_0001)
    _move(c, RUN)
    c.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=RUN, pt=(1, 1), now=2.1)
    c.on_keyboard(flags=0, extra=RUN, now=2.2, vk=0x41)
    _key(c, RUN)  # even the probe key's injected key-up, with the run tag
    assert (c.proof("mouse"), c.proof("keyboard")) == (0, 0)
    assert c.last_user() is None
    assert c.counts() == {"steer": 4}


def test_a_retired_probe_tag_is_own_and_stale_never_the_person() -> None:
    c = _c()
    c.set_probe_tag(0xAAAA_0001)
    c.set_probe_tag(0xAAAA_0002)
    _move(c, 0xAAAA_0001)
    _key(c, 0xAAAA_0001)
    c.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=0xAAAA_0001, pt=(1, 1), now=2.1)
    assert (c.proof("mouse"), c.proof("keyboard")) == (0, 0)
    assert c.last_user() is None
    assert c.counts() == {"steer": 3}


def test_the_retired_set_is_bounded_and_forgets_the_oldest() -> None:
    c = _c()
    for i in range(RETIRED_KEPT + 2):
        c.set_probe_tag(0xBBBB_0000 + i)
    _move(c, 0xBBBB_0000)  # the oldest, forgotten: foreign
    assert c.counts() == {"injected_other": 1}
    _move(c, 0xBBBB_0000 + RETIRED_KEPT)  # still kept: own and stale
    assert c.counts()["steer"] == 1


@pytest.mark.parametrize(
    "event",
    [
        ("mouse", WM_LBUTTONDOWN, LLMHF_INJECTED),  # the probe's tag on a click
        ("mouse", WM_MOUSEMOVE, 0),  # a move that is not injected
        ("key", 0x41, INJ_UP),  # another key's key-up
        ("key", PROBE, LLKHF_INJECTED),  # the probe key, a key-down
        ("key", PROBE, LLKHF_UP),  # the probe key's key-up, not injected
    ],
)
def test_the_probes_tag_on_a_non_probe_shaped_event_is_foreign_and_proves_nothing(
    event: tuple[str, int, int],
) -> None:
    kind, what, flags = event
    c = _c()
    c.set_probe_tag(0xAAAA_0001)
    if kind == "mouse":
        c.on_mouse(message=what, flags=flags, extra=0xAAAA_0001, pt=(1, 1), now=2.0)
    else:
        c.on_keyboard(flags=flags, extra=0xAAAA_0001, now=2.0, vk=what)
    assert (c.proof("mouse"), c.proof("keyboard")) == (0, 0)
    assert "steer" not in c.counts()


@pytest.mark.parametrize("extra", [0xDEAD_BEEF, RUN | 1 << 32, 0xAAAA_0001 | 1 << 32, 0])
def test_any_other_value_is_foreign_the_high_half_never_carries_meaning(extra: int) -> None:
    c = _c()
    c.set_probe_tag(0xAAAA_0001)
    c.on_keyboard(flags=0, extra=extra, now=2.0, vk=0x41)
    assert c.last_user() == 2.0
    assert c.last_kind() == TAKEOVER
    assert (c.proof("mouse"), c.proof("keyboard")) == (0, 0)


def test_v1_keeps_its_exact_compare_and_no_probe_proof() -> None:
    c = _c(v2=False)
    c.set_probe_tag(0xAAAA_0001)
    _move(c, 0xAAAA_0001)
    assert "steer" not in c.counts()
    _move(c, RUN)
    assert c.counts()["steer"] == 1
    assert c.proof("mouse") == 0


# ---- the ledger ----------------------------------------------------------------------------


def _ledger() -> tuple[ProbeLedger, ProbeTags]:
    return ProbeLedger(per_hook=True, deadline_s=1.05), ProbeTags(RUN)


def _begin(c: Classifier, tags: ProbeTags) -> int:
    tag = tags.draw()
    c.set_probe_tag(tag)
    return tag


def test_a_late_own_action_event_after_the_snapshot_does_not_prove_the_mouse_hook() -> None:
    """#492: an own click or move in flight when the probe is sent is not the probe."""
    c = _c()
    ledger, tags = _ledger()
    tag = ledger.probe_sent(now=20.0, begin=lambda: _begin(c, tags))
    c.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=RUN, pt=(5, 5), now=20.001)
    _move(c, RUN)
    _key(c, tag)
    assert not ledger.probe_seen(counts=c)
    assert ledger.lost_hooks(now=21.1, counts=c) == ("mouse",)


def test_the_probes_own_tag_proves_both_hooks_and_a_stale_one_never_the_next() -> None:
    c = _c()
    ledger, tags = _ledger()
    first = ledger.probe_sent(now=20.0, begin=lambda: _begin(c, tags))
    _move(c, first)
    _key(c, first)
    assert ledger.probe_seen(counts=c)
    second = ledger.probe_sent(now=22.0, begin=lambda: _begin(c, tags))
    assert second != first
    assert not ledger.probe_seen(counts=c)
    _move(c, first)  # a late event of the first: own and stale
    _key(c, first)
    assert not ledger.probe_seen(counts=c)
    assert ledger.lost_hooks(now=23.2, counts=c) == ("keyboard", "mouse")
    _move(c, second)
    _key(c, second)
    assert ledger.probe_seen(counts=c)


def test_an_own_action_event_after_the_proof_keeps_the_hook_proven() -> None:
    c = _c()
    ledger, tags = _ledger()
    tag = ledger.probe_sent(now=20.0, begin=lambda: _begin(c, tags))
    _move(c, tag)
    _key(c, tag)
    c.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=RUN, pt=(5, 5), now=20.03)
    c.on_keyboard(flags=0, extra=RUN, now=20.04, vk=0x41)
    assert ledger.probe_seen(counts=c)
    assert ledger.lost_hooks(now=21.1, counts=c) == ()


def test_the_ledger_without_a_probe_proves_nothing() -> None:
    ledger, _ = _ledger()
    assert not ledger.probe_seen(counts=_c())
    assert ledger.lost_hooks(now=99.0, counts=_c()) == ()


def test_two_threads_never_share_a_probe_tag() -> None:
    """A scripted interleave: the first caller is held inside `begin` while the second comes
    in; without the lock both would draw before either was recorded."""
    ledger, tags = _ledger()
    c = _c()
    inside, release, first = threading.Event(), threading.Event(), [True]

    def begin() -> int:
        if first[0]:
            first[0] = False
            inside.set()
            release.wait(0.5)
        return _begin(c, tags)

    got: list[int] = []
    a = threading.Thread(target=lambda: got.append(ledger.probe_sent(now=1.0, begin=begin)))
    b = threading.Thread(target=lambda: got.append(ledger.probe_sent(now=1.0, begin=begin)))
    a.start()
    assert inside.wait(2)
    b.start()
    b.join(0.1)
    assert got == []  # b waits on the lock while a is inside
    release.set()
    a.join(2)
    b.join(2)
    assert len(got) == 2
    assert got[0] != got[1]


# ---- [V2.81] review: the injected flag for every own class, the pen/touch range -----------------


def test_the_run_tag_counts_only_on_an_injected_event_in_v2() -> None:
    c = _c()
    c.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=RUN, pt=(1, 1), now=2.0)
    assert "steer" not in c.counts()
    assert c.last_user() == 2.0  # a person's, whatever it carries
    c.on_keyboard(flags=0, extra=RUN, now=2.1, vk=0x41)
    assert "steer" not in c.counts()
    _move(c, RUN)
    assert c.counts()["steer"] == 1


def test_a_retired_probe_tag_counts_only_on_an_injected_probe_shaped_event() -> None:
    c = _c()
    c.set_probe_tag(0xAAAA_0001)
    c.set_probe_tag(0xAAAA_0002)
    c.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=0xAAAA_0001, pt=(1, 1), now=2.0)
    assert c.last_user() == 2.0  # a click with a stale tag, not injected: a person
    c2 = _c()
    c2.set_probe_tag(0xAAAA_0001)
    c2.set_probe_tag(0xAAAA_0002)
    c2.on_mouse(
        message=WM_LBUTTONDOWN, flags=LLMHF_INJECTED, extra=0xAAAA_0001, pt=(1, 1), now=2.0
    )
    assert c2.last_user() == 2.0  # injected but not probe-shaped: still a person
    assert "steer" not in c2.counts()


def test_a_17th_back_probe_tag_is_foreign_and_never_proof() -> None:
    c = _c()
    for i in range(RETIRED_KEPT + 2):
        c.set_probe_tag(0xCCCC_0000 + i)
    c.on_keyboard(flags=INJ_UP, extra=0xCCCC_0000, now=2.0, vk=PROBE)
    assert c.proof("keyboard") == 0
    assert c.counts() == {"injected_other": 1}


def test_the_recent_drawn_window_is_wider_than_the_retired_one() -> None:
    assert RECENT_DRAWN > RETIRED_KEPT


@pytest.mark.parametrize("pen", [0xFF51_5700, 0xFF51_5780, 0xFF51_57FF])
def test_the_pen_and_touch_signature_range_is_never_drawn_or_a_run_tag(pen: int) -> None:
    with pytest.raises(ValueError, match="pen"):
        ProbeTags(pen)
    tags = _scripted(pen, 9)
    assert tags.draw() == 9


def test_a_draw_that_cannot_find_a_tag_gives_up() -> None:
    tags = ProbeTags(RUN, rand32=lambda: 0)
    with pytest.raises(RuntimeError, match="probe tag"):
        tags.draw()


def test_the_tags_before_the_latest_are_the_retired_ring_newest_first() -> None:
    tags = _scripted(11, 12, 13)
    assert tags.retired() == ()
    tags.draw()
    assert tags.retired() == ()
    tags.draw()
    tags.draw()
    assert tags.retired() == (12, 11)


# ---- PT-02: an echo after the deadline is a lost hook ------------------------------------------


def _timed(c: Classifier, tag: int, *, at: float) -> None:
    c.on_mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=tag, pt=(1, 1), now=at)
    c.on_keyboard(flags=INJ_UP, extra=tag, now=at, vk=PROBE)


def test_an_echo_at_the_deadline_counts_and_one_a_millisecond_later_is_lost() -> None:
    for at, proven in ((21.05, True), (21.051, False)):
        c = _c()
        ledger, tags = _ledger()
        tag = ledger.probe_sent(now=20.0, begin=lambda c=c, tags=tags: _begin(c, tags))
        _timed(c, tag, at=at)
        assert ledger.probe_seen(counts=c) is proven
        assert ledger.lost_hooks(now=21.055, counts=c) == (
            () if proven else ("keyboard", "mouse")
        )
        assert ledger.probe_ok(now=21.055, counts=c) is proven


# ---- v1 never draws; a superseded probe's echo is not the watch probe's -------------------------


def test_v1_never_draws_a_probe_tag() -> None:
    from steerai.session.monitor import MonitorCore  # noqa: PLC0415 - this test only

    core = MonitorCore(started_at=1.0)
    assert core.probe_sent(now=2.0) == 0
    assert core._tags.retired() == ()
    assert not core._tags._recent


def test_a_superseded_probes_late_echo_is_not_the_next_probes_proof() -> None:
    """The loss watch sends one probe after the loss; the first, unanswered probe's echo that
    arrives inside the window is stale: the hook is still unproven, never 'slow'."""
    from steerai.session.monitor import MonitorCore  # noqa: PLC0415 - this test only

    core = MonitorCore(started_at=1.0, per_hook=True, extra_info=RUN, deadline_s=1.05)
    first = core.probe_sent(now=10.0)
    second = core.probe_sent(now=12.0)  # the watch probe
    assert second != first
    core.on_mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=first, pt=(1, 1), now=12.1)
    core.on_keyboard(flags=INJ_UP, extra=first, now=12.1, vk=PROBE)
    assert core.probe_seen() is False
    assert core.lost_hooks(now=13.2) == ("keyboard", "mouse")
