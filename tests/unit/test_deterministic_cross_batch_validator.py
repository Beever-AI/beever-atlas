"""Unit tests for the deterministic cross-batch validator dedup logic.

Covers the three tiers and the embedding-failure degradation path
described in plan section P0-3 (``pipeline-cost-latency-reduction-v2.md``):

  * Tier 1 — exact-match merges on normalized name (no embedding call)
  * Tier 2 — cosine ≥ 0.92 merges via mocked
    ``compute_name_embeddings_batch``
  * Tier 3 — ambiguous band 0.85–0.92 with ``llm_fallback`` ON / OFF
  * Embedding API failure — degrades to exact-match-only, no crash,
    no orphan ``status="pending"`` markers
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from beever_atlas.services import entity_dedup
from beever_atlas.services.entity_dedup import dedupe_entities, normalize_name


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_registry(
    embeddings: dict[str, list[float]] | None = None,
    embedding_exc: Exception | None = None,
) -> MagicMock:
    """Build a mock ``EntityRegistry`` whose ``compute_name_embeddings_batch``
    either returns the supplied dict or raises ``embedding_exc``."""
    registry = MagicMock()
    if embedding_exc is not None:
        registry.compute_name_embeddings_batch = AsyncMock(side_effect=embedding_exc)
    else:
        registry.compute_name_embeddings_batch = AsyncMock(return_value=embeddings or {})
    registry.is_merge_rejected = MagicMock(return_value=False)
    return registry


def _entity(name: str, type_: str = "Person", aliases: list[str] | None = None) -> dict:
    return {
        "name": name,
        "type": type_,
        "aliases": aliases or [],
    }


# ── Tier 1: exact-match merges ──────────────────────────────────────────


async def test_normalize_name_lowercase_strips_punct_and_collapses_ws() -> None:
    assert normalize_name("Jeff Tai") == normalize_name("jeff tai") == "jeff tai"
    assert normalize_name("Atlas-API!") == "atlas api"
    assert normalize_name("  Multi   Space  ") == "multi space"
    assert normalize_name(None) == ""  # type: ignore[arg-type]


async def test_tier1_exact_match_merges_case_and_punct_variants() -> None:
    """Two entities with identical normalized names collapse into one
    canonical row, with the secondary name preserved as an alias.

    No embedding call needed — ``entity_registry=None`` proves Tier 1
    is independent of the embedding tier."""
    entities = [
        _entity("Jeff Tai", "Person"),
        _entity("jeff tai", "Person", aliases=["JT"]),
    ]
    result, fallback = await dedupe_entities(
        entities=entities,
        relationships=[],
        prior_entities=None,
        llm_fallback_enabled=True,
        entity_registry=None,
    )

    assert len(result.entities) == 1
    canonical = result.entities[0]
    assert canonical.name == "Jeff Tai"
    # Secondary variant + alias are preserved.
    assert "jeff tai" in canonical.aliases
    assert "JT" in canonical.aliases
    # Merge record emitted.
    assert any(m.canonical == "Jeff Tai" and "jeff tai" in m.merged_from for m in result.merges)
    assert fallback == 0


async def test_tier1_relationship_rewrite_uses_canonical_names() -> None:
    """Relationships referencing the absorbed name should be rewritten
    to point at the canonical name."""
    entities = [
        _entity("Jeff Tai"),
        _entity("jeff tai"),
        _entity("Atlas API", "Technology"),
    ]
    relationships = [
        {
            "type": "WORKS_ON",
            "source": "jeff tai",
            "target": "Atlas API",
            "confidence": 0.9,
        }
    ]
    result, _ = await dedupe_entities(
        entities=entities,
        relationships=relationships,
        prior_entities=None,
        llm_fallback_enabled=True,
        entity_registry=None,
    )

    assert len(result.relationships) == 1
    assert result.relationships[0].source == "Jeff Tai"
    assert result.relationships[0].target == "Atlas API"


async def test_orphan_entity_not_marked_pending() -> None:
    """An entity with zero relationships must pass through unchanged —
    the legacy ``status="pending"`` orphan marker has been removed
    per critic feedback (no downstream consumer)."""
    entities = [_entity("Lonely Atom", "Decision")]
    result, _ = await dedupe_entities(
        entities=entities,
        relationships=[],
        prior_entities=None,
        llm_fallback_enabled=True,
        entity_registry=None,
    )
    assert len(result.entities) == 1
    # Default status is "active" and must not be flipped to "pending".
    assert result.entities[0].status == "active"


# ── Tier 2: embedding cosine merges ─────────────────────────────────────


async def test_tier2_high_cosine_merges_with_mocked_embeddings() -> None:
    """Two entities whose cosine similarity is ≥ 0.92 should merge,
    even when their normalized names differ (e.g. ``JS`` vs ``JavaScript``).

    Uses unit vectors with controlled dot products so we exercise the
    real cosine math path, not just the threshold compare."""
    # Two vectors with cosine ≈ 0.95 (within the merge band).
    vec_js = [1.0, 0.0, 0.0]
    vec_javascript = [0.95, 0.3122, 0.0]  # sqrt(0.0975) ≈ 0.3122
    registry = _make_registry(embeddings={"JS": vec_js, "JavaScript": vec_javascript})

    entities = [
        _entity("JS", "Technology"),
        _entity("JavaScript", "Technology"),
    ]
    result, fallback = await dedupe_entities(
        entities=entities,
        relationships=[],
        prior_entities=None,
        llm_fallback_enabled=True,
        entity_registry=registry,
    )

    assert len(result.entities) == 1
    # The first-seen entity wins as canonical.
    assert result.entities[0].name == "JS"
    assert "JavaScript" in result.entities[0].aliases
    assert fallback == 0  # Tier 2 doesn't increment the LLM-fallback counter
    # Embedding API was called exactly once.
    registry.compute_name_embeddings_batch.assert_awaited_once()


async def test_tier2_below_threshold_does_not_merge() -> None:
    """Two distinct entities with cosine well below 0.85 must NOT merge."""
    registry = _make_registry(
        embeddings={
            "Postgres": [1.0, 0.0, 0.0],
            "Redis": [0.0, 1.0, 0.0],  # cosine = 0.0
        }
    )

    entities = [
        _entity("Postgres", "Technology"),
        _entity("Redis", "Technology"),
    ]
    result, _ = await dedupe_entities(
        entities=entities,
        relationships=[],
        prior_entities=None,
        llm_fallback_enabled=True,
        entity_registry=registry,
    )
    assert len(result.entities) == 2


# ── Tier 3: ambiguous band 0.85–0.92 with LLM fallback toggle ──────────


def _ambiguous_vectors() -> dict[str, list[float]]:
    """Two unit vectors with cosine ≈ 0.88 — squarely in the ambiguous band."""
    # cos(theta) for vectors [1, 0] and [0.88, sqrt(1-0.88^2)] is 0.88.
    import math

    a = [1.0, 0.0]
    b = [0.88, math.sqrt(1.0 - 0.88**2)]
    return {"OpenClaw": a, "OpenClaw API": b}


async def test_tier3_fallback_enabled_invokes_llm_hook(monkeypatch) -> None:
    """With ``llm_fallback_enabled=True`` and an ambiguous pair, the LLM
    hook is consulted exactly once and the fallback counter increments."""
    calls: list[tuple[str, str]] = []

    def fake_llm(name_a: str, name_b: str) -> bool:
        calls.append((name_a, name_b))
        return True  # LLM says: yes, merge.

    monkeypatch.setattr(entity_dedup, "_llm_resolve_pair", fake_llm)

    registry = _make_registry(embeddings=_ambiguous_vectors())
    entities = [
        _entity("OpenClaw", "Technology"),
        _entity("OpenClaw API", "Technology"),
    ]
    result, fallback = await dedupe_entities(
        entities=entities,
        relationships=[],
        prior_entities=None,
        llm_fallback_enabled=True,
        entity_registry=registry,
    )

    assert fallback == 1
    assert len(calls) == 1
    # LLM said merge → output has one entity.
    assert len(result.entities) == 1


async def test_tier3_fallback_disabled_skips_llm_and_does_not_merge(monkeypatch) -> None:
    """With ``llm_fallback_enabled=False`` the LLM hook is NEVER called
    and the ambiguous pair stays as two distinct entities."""
    calls: list[tuple[str, str]] = []

    def fake_llm(name_a: str, name_b: str) -> bool:
        calls.append((name_a, name_b))
        return True

    monkeypatch.setattr(entity_dedup, "_llm_resolve_pair", fake_llm)

    registry = _make_registry(embeddings=_ambiguous_vectors())
    entities = [
        _entity("OpenClaw", "Technology"),
        _entity("OpenClaw API", "Technology"),
    ]
    result, fallback = await dedupe_entities(
        entities=entities,
        relationships=[],
        prior_entities=None,
        llm_fallback_enabled=False,
        entity_registry=registry,
    )

    assert calls == []
    assert fallback == 0
    assert len(result.entities) == 2  # ambiguous pair left unmerged


# ── Embedding API failure → graceful degradation ────────────────────────


async def test_embedding_api_failure_degrades_to_exact_match_only(monkeypatch) -> None:
    """When ``compute_name_embeddings_batch`` raises, the function must
    NOT crash. Tier 2 is skipped entirely; Tier 1 results still apply.
    A warning log line surfaces the degradation for ops visibility."""
    registry = _make_registry(embedding_exc=RuntimeError("simulated API outage"))

    # Spy on the module logger to verify the degradation warning fires.
    # The project's structured JSON logger bypasses ``caplog``'s root
    # propagation, so we hook ``logger.warning`` directly.
    warnings_seen: list[str] = []
    original_warning = entity_dedup.logger.warning

    def _spy_warning(msg, *args, **kwargs):  # type: ignore[no-untyped-def]
        warnings_seen.append(msg % args if args else str(msg))
        return original_warning(msg, *args, **kwargs)

    monkeypatch.setattr(entity_dedup.logger, "warning", _spy_warning)

    # Mix of exact-match dups (Tier 1 catches) + semantically-similar
    # names that would only merge in Tier 2 (skipped on failure).
    entities = [
        _entity("Jeff Tai"),
        _entity("jeff tai"),  # Tier 1 merges into Jeff Tai
        _entity("JS", "Technology"),
        _entity("JavaScript", "Technology"),  # Tier 2 would merge — but fails
    ]

    result, fallback = await dedupe_entities(
        entities=entities,
        relationships=[],
        prior_entities=None,
        llm_fallback_enabled=True,
        entity_registry=registry,
    )

    # Tier 1 still ran: Jeff Tai duplicate collapsed.
    canonical_names = {e.name for e in result.entities}
    assert "Jeff Tai" in canonical_names
    # Tier 2 skipped: JS and JavaScript remain distinct.
    assert "JS" in canonical_names
    assert "JavaScript" in canonical_names
    assert fallback == 0
    # Warning log fired.
    assert any("exact-match-only" in w for w in warnings_seen), (
        f"expected degradation warning when embedding API fails; got {warnings_seen!r}"
    )


# ── Prior-entity canonical preference ───────────────────────────────────


async def test_prior_canonical_name_wins_over_extracted_variant() -> None:
    """When ``prior_entities`` contains a graph-canonical name matching
    a new variant, the canonical name is preserved (not the new spelling)."""
    prior = [{"name": "PostgreSQL", "type": "Technology", "aliases": ["postgres", "pg"]}]
    entities = [_entity("postgres", "Technology")]

    result, _ = await dedupe_entities(
        entities=entities,
        relationships=[],
        prior_entities=prior,
        llm_fallback_enabled=False,
        entity_registry=None,
    )
    assert len(result.entities) == 1
    assert result.entities[0].name == "PostgreSQL"
    # The extracted variant becomes an alias.
    assert "postgres" in result.entities[0].aliases
