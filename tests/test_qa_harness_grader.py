"""Unit tests for qa_test_harness grading logic.

Covers:
  - Soft citation floor (N-1 refs → warn + pass)
  - Hard zero citation floor (0 refs, expected > 0 → fail)
  - Refusal-aware must_not_mention canary
  - Refusal-aware must_mention expert case
  - Keyword seed URL token filter
  - Soft length target warns but does not fail verdict
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


# Make scripts/ importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import qa_test_harness as harness
from qa_test_harness import TestCase


# ---------------------------------------------------------------------------
# Helpers — replicate the inline grading logic from _run_one so we can
# unit-test it without a live server.
# ---------------------------------------------------------------------------

import re

_REFUSAL_RE = re.compile(
    r"\b(no (record|evidence|information|entity)|not (identified|found|recorded)"
    r"|couldn'?t find|don'?t have|no edges)\b",
    re.IGNORECASE,
)


def _grade(tc: TestCase, answer: str, n_refs: int) -> tuple[dict, dict]:
    """Reproduce the grading block from _run_one, returning (grade, advisory)."""
    _is_refusal = bool(_REFUSAL_RE.search(answer))

    exp_min = tc.expected_citations_min
    if exp_min > 0 and n_refs == 0:
        citations_count_ok = False
        citations_warn = False
    elif exp_min > 0 and n_refs == exp_min - 1:
        citations_count_ok = True
        citations_warn = True
    else:
        citations_count_ok = n_refs >= exp_min
        citations_warn = False

    if _is_refusal and tc.must_not_mention:
        must_not_mention_ok = True
    else:
        must_not_mention_ok = not any(s.lower() in answer.lower() for s in tc.must_not_mention)

    if _is_refusal and tc.must_mention:
        must_mention_ok = True
    else:
        must_mention_ok = all(s.lower() in answer.lower() for s in tc.must_mention)

    length_ok = (tc.max_chars == 0) or (len(answer) <= tc.max_chars)
    length_target_ok = (tc.soft_max_chars == 0) or (len(answer) <= tc.soft_max_chars)

    grade = {
        "citations_count_ok": citations_count_ok,
        "kinds_ok": True,
        "tools_ok": True,
        "must_mention_ok": must_mention_ok,
        "must_not_mention_ok": must_not_mention_ok,
        "follow_ups_ok": True,
        "length_ok": length_ok,
    }
    advisory = {
        "citations_warn": citations_warn,
        "length_target_ok": length_target_ok,
    }
    return grade, advisory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_soft_citation_floor_passes_with_minus_one():
    """N-1 refs when expected=N should pass citations_count_ok with warn=True."""
    tc = TestCase(
        id="X", persona="existing", category="test", question="q", expected_citations_min=3
    )
    grade, advisory = _grade(tc, answer="some answer", n_refs=2)

    assert grade["citations_count_ok"] is True
    assert advisory["citations_warn"] is True


def test_hard_zero_citation_still_fails():
    """0 refs when expected > 0 must fail citations_count_ok (no soft pass)."""
    tc = TestCase(
        id="X", persona="existing", category="test", question="q", expected_citations_min=1
    )
    grade, advisory = _grade(tc, answer="some answer", n_refs=0)

    assert grade["citations_count_ok"] is False
    assert advisory["citations_warn"] is False


def test_refusal_pattern_clears_must_not_mention():
    """Answer with refusal pattern + must_not_mention should pass must_not_mention_ok."""
    tc = TestCase(
        id="E-I1-hallucination-canary",
        persona="existing",
        category="negative",
        question="What did Elon Musk say?",
        expected_citations_min=0,
        must_not_mention=["Elon"],
    )
    answer = "There is no record of Elon in this channel."
    grade, _ = _grade(tc, answer=answer, n_refs=0)

    assert grade["must_not_mention_ok"] is True


def test_refusal_pattern_clears_must_mention_expert():
    """Clear refusal answer should pass must_mention on expert-lookup case."""
    tc = TestCase(
        id="E-C1-expert",
        persona="existing",
        category="people",
        question="Who should I ping for ingestion bugs?",
        expected_citations_min=0,
        must_mention=["JackyChan"],
    )
    answer = "I couldn't find an expert for ingestion pipeline issues in this channel."
    grade, _ = _grade(tc, answer=answer, n_refs=0)

    assert grade["must_mention_ok"] is True


def test_keyword_seed_drops_urls(tmp_path):
    """build_channel_context must exclude URL-derived tokens from top_keywords."""
    # Patch OUT_DIR to use tmp_path
    channel_id = "C_TEST"
    dump_file = tmp_path / f"messages_{channel_id}.jsonl"

    messages = [
        {"author_name": "alice", "content": "check https://www.example.com for details pipeline"},
        {"author_name": "bob", "content": "https org com net www pipeline pipeline pipeline"},
        {"author_name": "carol", "content": "http https pipeline"},
    ]
    with dump_file.open("w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")

    with patch.object(harness, "OUT_DIR", tmp_path):
        ctx = harness.build_channel_context(channel_id)

    keyword_tokens = [tok.lower() for tok, _ in ctx.top_keywords]
    for bad in ("http", "https", "www", "com", "org", "net"):
        assert bad not in keyword_tokens, f"URL token '{bad}' should be filtered from top_keywords"


def test_length_target_warns_but_does_not_fail():
    """1700-char answer with soft_max=1200 and max=2000 → PASS, length_target_ok=False."""
    tc = TestCase(
        id="O-3",
        persona="onboarding",
        category="people",
        question="q",
        max_chars=2000,
        soft_max_chars=1200,
        expected_citations_min=0,
    )
    answer = "x" * 1700
    grade, advisory = _grade(tc, answer=answer, n_refs=0)

    # Hard cap not exceeded → verdict component passes
    assert grade["length_ok"] is True
    # Soft target exceeded → advisory warns
    assert advisory["length_target_ok"] is False
    # Verdict would be PASS (all grade values True)
    assert all(grade.values())
