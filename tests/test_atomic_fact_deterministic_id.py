"""Unit tests for ``AtomicFact.deterministic_id`` (PR-B, design D4).

The position-based key (``platform:channel_id:message_ts:fact_index``) was
replaced with a content-derived hash (``sha256(memory_text + sorted entities)``)
so retried extractions with reordered LLM output produce the same fact UUID
instead of phantom Weaviate duplicates.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/extraction-worker/``
"""

from __future__ import annotations

import uuid

from beever_atlas.models.domain import AtomicFact


def test_same_text_same_entities_yields_identical_uuid() -> None:
    """The minimum guarantee: same input → same UUID, run after run."""
    fid_a = AtomicFact.deterministic_id("Alice owns the auth service.", ["alice", "auth-service"])
    fid_b = AtomicFact.deterministic_id("Alice owns the auth service.", ["alice", "auth-service"])
    assert fid_a == fid_b


def test_entity_order_does_not_affect_uuid() -> None:
    """LLM output order on retry must not shift the UUID."""
    fid_a = AtomicFact.deterministic_id("Bob shipped feature X.", ["bob", "feature-x"])
    fid_b = AtomicFact.deterministic_id("Bob shipped feature X.", ["feature-x", "bob"])
    assert fid_a == fid_b


def test_different_memory_text_yields_different_uuid() -> None:
    """Subtle text differences must produce different UUIDs to avoid collisions."""
    fid_a = AtomicFact.deterministic_id("Alice owns auth.", ["alice"])
    fid_b = AtomicFact.deterministic_id("Alice owns billing.", ["alice"])
    assert fid_a != fid_b


def test_different_entities_yields_different_uuid() -> None:
    """Same fact text with different entity attribution must NOT collapse."""
    fid_a = AtomicFact.deterministic_id("Owns the auth service.", ["alice"])
    fid_b = AtomicFact.deterministic_id("Owns the auth service.", ["bob"])
    assert fid_a != fid_b


def test_empty_entity_list_is_supported() -> None:
    """``observation``-type facts may have zero extracted entities."""
    fid = AtomicFact.deterministic_id("Build broke at 3pm.", [])
    # Returns a parseable UUID without raising.
    parsed = uuid.UUID(fid)
    assert parsed.version == 5


def test_returns_valid_uuid5_string() -> None:
    """The output must be a parseable UUID5 string for downstream consumers."""
    fid = AtomicFact.deterministic_id("Some fact.", ["x", "y"])
    parsed = uuid.UUID(fid)
    assert parsed.version == 5
    # The DNS namespace UUID we use should produce a stable URN-form.
    assert isinstance(fid, str)
    assert len(fid) == 36  # 8-4-4-4-12 hex


def test_unicode_text_handled() -> None:
    """Non-ASCII memory_text and entity_names must hash deterministically."""
    fid_a = AtomicFact.deterministic_id("我喜歡吃壽司。", ["山田"])
    fid_b = AtomicFact.deterministic_id("我喜歡吃壽司。", ["山田"])
    assert fid_a == fid_b
    fid_c = AtomicFact.deterministic_id("私は寿司が好きです。", ["山田"])
    assert fid_a != fid_c


def test_pipe_in_text_does_not_collide_with_separator() -> None:
    """The internal ``|`` separator must not be exploitable for collisions."""
    # If the implementation naively joined without a separator, these would collide.
    fid_a = AtomicFact.deterministic_id("ab", ["c", "d"])
    fid_b = AtomicFact.deterministic_id("ab|", ["", "c", "d"])
    # The exact behavior depends on the canonicalization scheme, but they must not be equal.
    assert fid_a != fid_b


def test_string_coercion_for_non_string_entity_names() -> None:
    """Defensive: callers may pass int / object names; coerce to str without raising."""
    fid_a = AtomicFact.deterministic_id("fact", [1, 2])  # type: ignore[list-item]
    fid_b = AtomicFact.deterministic_id("fact", ["1", "2"])
    assert fid_a == fid_b
