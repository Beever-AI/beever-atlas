"""Tests for the post-extraction narration filter.

These tests cover the three layers of defence:
  1. ``detect_activity_narration`` — regex detection with word boundaries.
  2. ``attempt_rewrite`` — confident mechanical rewrites of common patterns.
  3. ``filter_fact`` / ``filter_facts`` — orchestration: rewrite when
     confident, demote ``importance`` when not, log telemetry, idempotent.
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.ingestion.narration_filter import (
    attempt_rewrite,
    detect_activity_narration,
    filter_fact,
    filter_facts,
)


# ---------------------------------------------------------------------------
# detect_activity_narration
# ---------------------------------------------------------------------------


def test_detect_activity_narration_finds_shared_a_link() -> None:
    text = "Thomas Chong shared a link to the GitHub repository for Ory Hydra"
    matched = detect_activity_narration(text)
    assert matched is not None
    assert "shared a link" in matched.lower()


def test_detect_activity_narration_finds_noted_that() -> None:
    text = "Jacky noted that the API is rate-limited at 100 RPS"
    matched = detect_activity_narration(text)
    assert matched is not None
    assert "noted that" in matched.lower()


def test_detect_activity_narration_finds_mentioned_that() -> None:
    text = "Alice mentioned that we should consider Redis for caching"
    matched = detect_activity_narration(text)
    assert matched is not None


def test_detect_activity_narration_finds_shared_blog_post() -> None:
    text = "Thomas Chong shared a Neo4j blog post titled Build AI Agents"
    matched = detect_activity_narration(text)
    assert matched is not None
    assert "blog post" in matched.lower()


def test_detect_activity_narration_clean_text_returns_none() -> None:
    text = (
        "Authlib provides OAuth 2.0 integration patterns and is the team's "
        "chosen authentication library."
    )
    assert detect_activity_narration(text) is None


def test_detect_activity_narration_word_boundary() -> None:
    """``denoted`` and ``reposted`` must NOT trigger a false positive."""
    assert detect_activity_narration("This is denoted that x in the spec") is None
    assert detect_activity_narration("The change was reposted about midnight") is None


def test_detect_activity_narration_empty_returns_none() -> None:
    assert detect_activity_narration("") is None
    assert detect_activity_narration(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# attempt_rewrite
# ---------------------------------------------------------------------------


def test_attempt_rewrite_strip_share_link_pattern() -> None:
    text = "Thomas Chong shared a link to fastapi-sso for OAuth integration"
    rewritten, ok = attempt_rewrite(text)
    assert ok is True
    # Author opener stripped, "shared a link to" removed, residual capitalised
    assert "Thomas Chong" not in rewritten
    assert rewritten.lower().startswith("fastapi-sso")


def test_attempt_rewrite_strip_share_repository_pattern() -> None:
    text = "Thomas Chong shared a repository for Ory Hydra OAuth provider"
    rewritten, ok = attempt_rewrite(text)
    assert ok is True
    assert "shared" not in rewritten.lower()
    assert rewritten.startswith("Ory Hydra")


def test_attempt_rewrite_strip_noted_that_pattern() -> None:
    text = "Thomas Chong noted that the API is rate-limited"
    rewritten, ok = attempt_rewrite(text)
    assert ok is True
    assert "noted" not in rewritten.lower()
    # First letter capitalised after rewrite
    assert rewritten == "The API is rate-limited"


def test_attempt_rewrite_strip_mentioned_that_pattern() -> None:
    text = "Alice mentioned that fastapi-sso provides OAuth integration"
    rewritten, ok = attempt_rewrite(text)
    assert ok is True
    assert "mentioned" not in rewritten.lower()
    assert rewritten.startswith("fastapi-sso")


def test_attempt_rewrite_unconfident_returns_original() -> None:
    """``asked the team about Y`` is too semantic for mechanical rewrite."""
    text = "Thomas Chong asked the team about the SSO migration timeline"
    rewritten, ok = attempt_rewrite(text)
    assert ok is False
    assert rewritten == text


def test_attempt_rewrite_clean_text_unchanged() -> None:
    text = "Ory Hydra is an OAuth 2.0 provider."
    rewritten, ok = attempt_rewrite(text)
    assert ok is False
    assert rewritten == text


def test_attempt_rewrite_empty_unchanged() -> None:
    rewritten, ok = attempt_rewrite("")
    assert ok is False
    assert rewritten == ""


# ---------------------------------------------------------------------------
# filter_fact
# ---------------------------------------------------------------------------


def test_filter_fact_rewrites_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """The autouse ``_auth_bypass`` fixture indirectly imports
    ``beever_atlas.server.app`` which sets ``propagate=False`` on the
    ``beever_atlas`` logger — so root-level ``caplog`` cannot see
    records from ``beever_atlas.agents.ingestion.narration_filter``.
    Capture the INFO call via direct monkeypatch on the module logger.
    """
    from beever_atlas.agents.ingestion import narration_filter as nf

    captured: list[str] = []
    monkeypatch.setattr(
        nf.logger,
        "info",
        lambda msg, *a, **kw: captured.append(msg % a if a else msg),
    )

    fact = {
        "fact_id": "f-1",
        "memory_text": "Thomas Chong shared a link to fastapi-sso for OAuth",
        "author_name": "Thomas Chong",
        "importance": "medium",
    }
    out = filter_fact(fact)

    assert out["memory_text"] != fact["memory_text"]
    assert "Thomas Chong" not in out["memory_text"]
    # Importance is preserved when rewrite is confident
    assert out["importance"] == "medium"
    # Telemetry log emitted
    assert any("fact_narration_rewritten" in m for m in captured), (
        f"expected fact_narration_rewritten log; got: {captured}"
    )


def test_filter_fact_demotes_unconfident(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct monkeypatch on the module logger — see
    ``test_filter_fact_rewrites_in_place`` for why caplog won't work."""
    from beever_atlas.agents.ingestion import narration_filter as nf

    captured: list[str] = []
    monkeypatch.setattr(
        nf.logger,
        "warning",
        lambda msg, *a, **kw: captured.append(msg % a if a else msg),
    )

    fact = {
        "fact_id": "f-2",
        "memory_text": "Thomas Chong asked the team about the SSO migration",
        "author_name": "Thomas Chong",
        "importance": "high",
    }
    out = filter_fact(fact)

    # Memory text unchanged (no confident rewrite)
    assert out["memory_text"] == fact["memory_text"]
    # But importance demoted to "low"
    assert out["importance"] == "low"
    assert any("fact_narration_demoted" in m for m in captured), (
        f"expected fact_narration_demoted log; got: {captured}"
    )


def test_filter_fact_clean_unchanged() -> None:
    fact = {
        "fact_id": "f-3",
        "memory_text": "Authlib provides OAuth 2.0 integration patterns.",
        "author_name": "Alice",
        "importance": "high",
    }
    out = filter_fact(fact)
    # Same data, no mutation of input
    assert out == fact
    assert out is not fact or out == fact  # idempotent equality


def test_filter_fact_idempotent_on_clean() -> None:
    fact = {
        "memory_text": "Ory Hydra is an OAuth 2.0 + OIDC provider.",
        "author_name": "Bob",
        "importance": "medium",
    }
    once = filter_fact(fact)
    twice = filter_fact(once)
    assert twice == once


def test_filter_fact_handles_missing_memory_text() -> None:
    fact = {"author_name": "Bob"}  # no memory_text
    out = filter_fact(fact)
    assert out == fact


def test_filter_fact_handles_non_string_memory_text() -> None:
    fact = {"memory_text": 42, "author_name": "Bob"}
    out = filter_fact(fact)
    assert out == fact


# ---------------------------------------------------------------------------
# filter_facts
# ---------------------------------------------------------------------------


def test_filter_facts_processes_list() -> None:
    facts = [
        {
            "memory_text": "Thomas shared a link to fastapi-sso for auth",
            "author_name": "Thomas",
            "importance": "medium",
        },
        {
            "memory_text": "Authlib supports OIDC discovery.",
            "author_name": "Alice",
            "importance": "high",
        },
        {
            "memory_text": "Bob asked the team about the rollout date",
            "author_name": "Bob",
            "importance": "high",
        },
    ]
    out = filter_facts(facts)
    assert len(out) == 3
    # Fact 0: rewritten
    assert "Thomas" not in out[0]["memory_text"]
    # Fact 1: untouched
    assert out[1] == facts[1]
    # Fact 2: demoted to low
    assert out[2]["importance"] == "low"


def test_filter_facts_skips_non_dict_entries() -> None:
    facts = [
        {"memory_text": "Clean fact.", "importance": "medium"},
        "not a dict",  # type: ignore[list-item]
        None,  # type: ignore[list-item]
    ]
    out = filter_facts(facts)  # type: ignore[arg-type]
    assert len(out) == 1


def test_filter_facts_empty_list() -> None:
    assert filter_facts([]) == []
