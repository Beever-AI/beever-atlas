"""Tests for WikiCompiler empty-title defense.

Consolidation is expected to assign a title to every MemoryCluster, but a
real-world compile surfaced a cluster with ``title == ""`` which the LLM
faithfully rendered as "**** (N members) — ...". These tests exercise the
module-level `_apply_title_fallbacks` helper directly so we don't need to
stand up a full `compile()` pipeline.
"""
from __future__ import annotations

from beever_atlas.models.domain import TopicCluster
from beever_atlas.wiki.compiler import _apply_title_fallbacks


def _make_cluster(**overrides) -> TopicCluster:
    defaults = {"channel_id": "C_TEST"}
    defaults.update(overrides)
    return TopicCluster(**defaults)


def test_empty_title_replaced_with_topic_tags() -> None:
    c = _make_cluster(title="", topic_tags=["ai", "memory"])
    _apply_title_fallbacks([c])
    assert c.title == "ai, memory"


def test_empty_title_no_tags_uses_id_prefix() -> None:
    c = _make_cluster(title="", topic_tags=[])
    _apply_title_fallbacks([c])
    assert c.title == f"Topic {c.id[:6]}"


def test_non_empty_title_preserved() -> None:
    c = _make_cluster(title="Real Title", topic_tags=["x"])
    _apply_title_fallbacks([c])
    assert c.title == "Real Title"


def test_whitespace_only_title_treated_as_empty() -> None:
    c = _make_cluster(title="   ", topic_tags=["alpha", "beta", "gamma", "delta"])
    _apply_title_fallbacks([c])
    # Only first three tags used.
    assert c.title == "alpha, beta, gamma"


def test_empty_string_tags_skipped() -> None:
    c = _make_cluster(title="", topic_tags=["", "", ""])
    _apply_title_fallbacks([c])
    assert c.title == f"Topic {c.id[:6]}"
