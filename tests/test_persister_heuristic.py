"""Tests for the persister word-overlap heuristic (WS-M9).

The raised threshold must:
  - reject a single shared common word (prevents media mis-attribution)
  - require an author identity match
  - accept 3+ unique overlapping ≥4-char words with matching author
  - accept matching author + 1+ overlapping word with len ≥6
"""

from __future__ import annotations

from beever_atlas.agents.ingestion.persister import match_media_by_word_overlap


def _msg(author: str, text: str, **extra) -> dict:
    return {"author_id": author, "text": text, **extra}


def test_single_common_word_does_not_attribute_media():
    fact = {
        "author_id": "alice",
        "memory_text": "Working on the quarterly project plan",
    }
    # Another user mentions "project" — must not match.
    messages = [
        _msg("bob", "Demo project kicked off", source_media_urls=["http://x/img.png"]),
    ]
    assert match_media_by_word_overlap(fact, messages) is None


def test_three_plus_overlap_and_same_author_attributes():
    fact = {
        "author_id": "alice",
        "memory_text": "Shipping the quarterly plan review documents next week",
    }
    messages = [
        _msg("alice", "Shipping quarterly plan documents", source_media_urls=["http://x/a.png"]),
        _msg("bob", "quarterly plan review documents", source_media_urls=["http://x/b.png"]),
    ]
    match = match_media_by_word_overlap(fact, messages)
    assert match is not None
    # Must pick alice's message; bob is rejected by author filter.
    assert match["author_id"] == "alice"


def test_short_common_word_without_long_match_rejected_even_with_same_author():
    fact = {
        "author_id": "alice",
        "memory_text": "demo",  # single 4-char word
    }
    messages = [_msg("alice", "demo", source_media_urls=["http://x/c.png"])]
    # Only 1 overlap, no word ≥6 chars → rejected.
    assert match_media_by_word_overlap(fact, messages) is None


def test_long_word_plus_same_author_attributes():
    fact = {
        "author_id": "alice",
        "memory_text": "deployment",  # 10-char word
    }
    messages = [_msg("alice", "deployment", source_media_urls=["http://x/d.png"])]
    # 1 shared word, len ≥6, same author → qualifies.
    assert match_media_by_word_overlap(fact, messages) is not None


def test_missing_fact_author_refuses_to_guess():
    fact = {"memory_text": "deployment strategy meeting notes attached"}
    messages = [_msg("alice", "deployment strategy meeting notes")]
    assert match_media_by_word_overlap(fact, messages) is None


def test_author_mismatch_rejected_even_with_high_overlap():
    fact = {
        "author_id": "alice",
        "memory_text": "Shipping the quarterly plan review documents",
    }
    messages = [
        _msg("bob", "Shipping quarterly plan review documents"),
    ]
    assert match_media_by_word_overlap(fact, messages) is None
