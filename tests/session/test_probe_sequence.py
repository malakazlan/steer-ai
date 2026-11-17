"""#492, [V2.80], #493 reviews: the probe's sequence number, the own-event definition, the
layout pin, the ledger's lock and the monitor's tag."""

from __future__ import annotations

import hashlib
import threading

import pytest

from steerai.session.hook_classifier import (
    LLKHF_INJECTED,
    LLKHF_UP,
    LLMHF_INJECTED,
    PROBE_SEQ_MAX,
    TAKEOVER,
    Classifier,
    next_probe_seq,
    probe_tag,
)
from steerai.session.hook_layout import CONFIG_WORDS, LAYOUT_VERSION, STATUS_WORDS
from steerai.session.probe_ledger import ProbeLedger

TAG = 0x1234_5678
PROBE = 0x0E
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201

LAYOUT_DIGESTS = {3: "6531582621cbb9ac574d22279ec77adaa46872b0fb313b29b3f7b55490ba6194"}
"""LAYOUT_VERSION -> sha256 of the status and config word lists. A changed word list fails the
test until the version moves and a new digest is pinned (PS-01)."""


def _digest() -> str:
    text = "|".join(("status", *STATUS_WORDS, "config", *CONFIG_WORDS))
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_word_lists_are_pinned_to_the_layout_version() -> None:
    assert LAYOUT_VERSION == 3, "the status words changed meaning in [V2.80]: version 3"
    assert LAYOUT_DIGESTS[LAYOUT_VERSION] == _digest()


def _c(*, numbered: bool = True) -> Classifier:
    return Classifier(started_at=1.0, extra_info=TAG, probe_vk=PROBE, numbered_probes=numbered)


def _foreign_leaves_proof_at_zero(c: Classifier) -> None:
    """Not Steer's own (no `steer` count, no proof); a person's, or an injected bare move,
    which is counted apart and is never a takeover."""
    assert c.probe_seq("keyboard") == 0
    assert c.probe_seq("mouse") == 0
    assert "steer" not in c.counts()
    if c.last_user() is not None:
        assert c.last_kind() == TAKEOVER
    else:
        assert c.counts() == {"injected_other": 1}


@pytest.mark.parametrize(
    "event",
    [
        ("mouse", WM_LBUTTONDOWN, LLMHF_INJECTED, probe_tag(TAG, 5)),  # numbered click
        ("mouse", WM_MOUSEMOVE, 0, probe_tag(TAG, 5)),  # numbered move, not injected
        ("mouse", WM_MOUSEMOVE, LLMHF_INJECTED, TAG | (PROBE_SEQ_MAX + 1) << 32),  # past the max
        ("mouse", WM_MOUSEMOVE, LLMHF_INJECTED, TAG | 0x8000_0000 << 32),  # high bit set
        ("mouse", WM_MOUSEMOVE, LLMHF_INJECTED, (TAG ^ 1) | 5 << 32),  # another run's low half
        ("key", 0x41, LLKHF_INJECTED | LLKHF_UP, probe_tag(TAG, 5)),  # another key's key-up
        ("key", PROBE, LLKHF_INJECTED, probe_tag(TAG, 5)),  # the probe key, a key-down
        ("key", PROBE, LLKHF_UP, probe_tag(TAG, 5)),  # the probe key's key-up, not injected
    ],
)
def test_a_numbered_event_that_is_not_probe_shaped_is_foreign(
    event: tuple[str, int, int, int],
) -> None:
    """The principal's blocking 1 and PS-02/18/22: own is the exact tag, or a probe-shaped,
    injected event with the tag below and a number in range above; any other is a person's."""
    kind, what, flags, extra = event
    c = _c()
    if kind == "mouse":
        c.on_mouse(message=what, flags=flags, extra=extra, pt=(1, 1), now=2.0)
    else:
        c.on_keyboard(flags=flags, extra=extra, now=2.0, vk=what if what != PROBE else PROBE)
    _foreign_leaves_proof_at_zero(c)


def test_a_numbered_probe_that_is_injected_and_in_shape_is_own() -> None:
    c = _c()
    c.on_mouse(
        message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=probe_tag(TAG, 7), pt=(1, 1), now=2.0
    )
    c.on_keyboard(flags=LLKHF_INJECTED | LLKHF_UP, extra=probe_tag(TAG, 7), now=2.1, vk=PROBE)
    assert (c.probe_seq("mouse"), c.probe_seq("keyboard")) == (7, 7)
    assert c.last_user() is None


def test_v1_keeps_its_exact_compare() -> None:
    """per_hook False: only the exact tag is own, a numbered one is foreign (v1 is unchanged)."""
    c = _c(numbered=False)
    c.on_mouse(
        message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=probe_tag(TAG, 7), pt=(1, 1), now=2.0
    )
    assert "steer" not in c.counts()
    c2 = _c(numbered=False)
    c2.on_mouse(message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=TAG, pt=(1, 1), now=2.0)
    assert c2.last_user() is None


def test_a_probe_tag_rejects_a_run_tag_wider_than_32_bits() -> None:
    with pytest.raises(ValueError, match="32-bit"):
        probe_tag(1 << 32, 1)


# ---- PS-04: a number is never reused for a tag in a process ---------------------------------


def test_each_ledger_starts_at_its_own_random_number() -> None:
    firsts = {ProbeLedger(per_hook=True, deadline_s=1.0).probe_sent(now=1.0) for _ in range(8)}
    assert len(firsts) > 1
    assert all(1 <= n <= PROBE_SEQ_MAX for n in firsts)


def test_a_ledger_counts_up_from_its_start() -> None:
    ledger = ProbeLedger(per_hook=True, deadline_s=1.0)
    a = ledger.probe_sent(now=1.0)
    b = ledger.probe_sent(now=2.0)
    assert b == next_probe_seq(a)


# ---- PS-05: the ledger is thread-safe -------------------------------------------------------


def test_two_threads_never_get_the_same_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scripted interleave: the first caller is held inside the increment while the second
    comes in; without the lock both read the same number."""
    real = next_probe_seq
    inside = threading.Event()
    release = threading.Event()
    first = [True]

    def slow(seq: int) -> int:
        if first[0]:
            first[0] = False
            inside.set()
            release.wait(0.5)
        return real(seq)

    monkeypatch.setattr("steerai.session.probe_ledger.next_probe_seq", slow)
    ledger = ProbeLedger(per_hook=True, deadline_s=1.0)
    got: list[int] = []
    a = threading.Thread(target=lambda: got.append(ledger.probe_sent(now=1.0)))
    b = threading.Thread(target=lambda: got.append(ledger.probe_sent(now=1.0)))
    a.start()
    assert inside.wait(2)
    b.start()
    b.join(0.1)  # with the lock, b waits here
    release.set()
    a.join(2)
    b.join(2)
    assert len(got) == 2
    assert got[0] != got[1]


# ---- the ledger proves by exactly its number --------------------------------------------------

_INJ_UP = LLKHF_INJECTED | LLKHF_UP


def _numbered(c: Classifier, seq: int, *, now: float) -> None:
    c.on_mouse(
        message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=probe_tag(TAG, seq), pt=(5, 5), now=now
    )
    c.on_keyboard(flags=_INJ_UP, extra=probe_tag(TAG, seq), now=now + 0.01, vk=PROBE)


def test_a_higher_number_than_the_probe_sent_does_not_prove_a_hook() -> None:
    """The ledger's equality must never become `>=`."""
    c = _c()
    ledger = ProbeLedger(per_hook=True, deadline_s=1.05)
    seq = ledger.probe_sent(now=20.0)
    _numbered(c, seq + 1, now=20.01)
    assert not ledger.probe_seen(counts=c)
    assert ledger.lost_hooks(now=21.1, counts=c) == ("keyboard", "mouse")


def test_an_own_action_event_after_the_proof_keeps_the_hook_proven() -> None:
    c = _c()
    ledger = ProbeLedger(per_hook=True, deadline_s=1.05)
    seq = ledger.probe_sent(now=20.0)
    _numbered(c, seq, now=20.01)
    c.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=TAG, pt=(5, 5), now=20.03)
    c.on_keyboard(flags=0, extra=TAG, now=20.04, vk=0x41)
    assert (c.probe_seq("mouse"), c.probe_seq("keyboard")) == (seq, seq)
    assert ledger.probe_seen(counts=c)
    assert ledger.lost_hooks(now=21.1, counts=c) == ()


def test_the_probe_number_boundaries() -> None:
    assert next_probe_seq(1) == 2
    assert next_probe_seq(PROBE_SEQ_MAX - 1) == PROBE_SEQ_MAX
    assert next_probe_seq(PROBE_SEQ_MAX) == 1
    for bad in (-1, 0, PROBE_SEQ_MAX + 1):
        with pytest.raises(ValueError, match="PROBE_SEQ_MAX"):
            probe_tag(TAG, bad)
    c = _c()
    c.on_keyboard(flags=_INJ_UP, extra=probe_tag(TAG, PROBE_SEQ_MAX), now=11.0, vk=PROBE)
    c.on_mouse(
        message=WM_MOUSEMOVE, flags=LLMHF_INJECTED, extra=probe_tag(TAG, 1), pt=(5, 5), now=11.1
    )
    assert (c.probe_seq("keyboard"), c.probe_seq("mouse")) == (PROBE_SEQ_MAX, 1)
    assert c.last_user() is None
