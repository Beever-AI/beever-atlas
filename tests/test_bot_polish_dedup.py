"""Unit tests for the P0 (dedup + channel-id scrub) and P1 (cross-channel
detector) bot-reply polish helpers in api/ask.py."""

from __future__ import annotations

import pytest

from beever_atlas.api.ask import (
    _collapse_repeated_paragraphs,
    _detect_cross_channel,
    _finalize_answer,
    _scrub_channel_id,
)

CID = "C0B5YCR1NL8"


# --------------------------------------------------------------------------
# P0: channel-id scrub
# --------------------------------------------------------------------------


def test_scrub_removes_parenthesized_and_bare_id():
    assert _scrub_channel_id("scoped to this channel (C0B5YCR1NL8).", CID) == (
        "scoped to this channel."
    )
    assert CID not in _scrub_channel_id(f"id is {CID} here", CID)


def test_scrub_noop_when_absent():
    assert _scrub_channel_id("no id here", CID) == "no id here"
    assert _scrub_channel_id("text", None) == "text"


def test_scrub_leaves_clean_spacing():
    # bare id mid-sentence → no double space; id before period → no dangling space
    assert _scrub_channel_id("the channel C0B5YCR1NL8 is active.", CID) == (
        "the channel is active."
    )
    assert _scrub_channel_id("scoped to C0B5YCR1NL8.", CID) == "scoped to."


# --------------------------------------------------------------------------
# P0: dedup — the id-variant double collapses once the id is scrubbed
# --------------------------------------------------------------------------


def test_finalize_collapses_id_variant_double():
    a = (
        "I am Beever Atlas, scoped to this channel (C0B5YCR1NL8). I cannot list others."
        "I am Beever Atlas, scoped to this channel. I cannot list others."
    )
    out = _finalize_answer(a, "s", CID)
    assert CID not in out
    assert out.count("I am Beever Atlas") == 1


def test_finalize_collapses_exact_double():
    half = "The Celtics won the 2024 title, beating Dallas in the Finals clean. "
    out = _finalize_answer(half + half, "s", CID)
    assert out.strip() == half.strip()


def test_collapse_repeated_paragraphs():
    p = "Para one is a long sentence about basketball players in this channel."
    out = _collapse_repeated_paragraphs(f"{p}\n\n{p}", "s")
    assert out.count("Para one") == 1


def test_collapse_drops_only_long_repeats():
    # short repeats survive (under the 40-char floor)
    out = _collapse_repeated_paragraphs("Yes.\n\nYes.", "s")
    assert out.count("Yes.") == 2


def test_finalize_leaves_legit_answer_untouched():
    c = "The Celtics won. Jaylen Brown was MVP. They beat Dallas in five games clean."
    assert _finalize_answer(c, "s", CID) == c


def test_finalize_never_guts_to_empty():
    # pathological input that normalizes to a single repeated block but is long
    big = "x" * 200
    out = _finalize_answer(big + "\n\n" + big, "s", CID)
    assert len(out.strip()) >= 30


# --------------------------------------------------------------------------
# P1: cross-channel detector
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "what is being discussed in #research?",
        "summarize #product-decisions for me",
        "who is active in #general",
    ],
)
def test_detect_flags_other_channel(q):
    assert _detect_cross_channel(q, "basketball") is not None


@pytest.mark.parametrize(
    "q",
    [
        "what is this #basketball about?",  # same channel
        "who is SGA?",  # no channel token
        "should we create a #research channel?",  # creation/meta verb
        "let's make a #standup channel",
    ],
)
def test_detect_allows_same_or_meta(q):
    assert _detect_cross_channel(q, "basketball") is None


def test_detect_meta_verb_match_is_word_bounded():
    # "address" contains "add" but must NOT be treated as a channel-meta verb,
    # so a genuine cross-channel question still refuses.
    assert (
        _detect_cross_channel("what is the address discussed in #research", "basketball")
        == "#research"
    )
    # "removed" contains "remove"; still a read question → refuse.
    assert _detect_cross_channel("what was removed from #research", "basketball") == "#research"


def test_detect_no_guess_when_name_unresolved():
    # current name still a raw platform id → cannot tell same vs different
    assert _detect_cross_channel("what is in #research", "C0B5YCR1NL8") is None


def test_detect_handles_leading_hash_in_current_name():
    assert _detect_cross_channel("what is in #research", "#basketball") == "#research"
