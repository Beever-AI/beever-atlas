"""Deterministic cross-batch entity deduplication.

Pure-Python module that replaces the LLM-based ``cross_batch_validator``
agent. Implements three tiers of dedup logic:

1. **Tier 1 — Exact match on normalized name.** Lowercase, strip
   punctuation, collapse whitespace; entities whose normalized names
   collide merge with no cosine computation. Free / deterministic.

2. **Tier 2 — Embedding cosine similarity ≥ 0.92.** Fresh per-batch
   embeddings via ``EntityRegistry.compute_name_embeddings_batch``
   (NOT graph-stored ``find_similar_by_embedding`` — new entities lack
   stored vectors pre-persistence). On embedding API failure, this
   tier is skipped entirely and the function degrades to exact-match-
   only mode (loud WARN, no crash).

3. **Tier 3 — Ambiguous band 0.85–0.92.** Bounded LLM fallback when
   ``llm_fallback_enabled=True`` (default), bounded to a small number
   of pairs per batch. When fallback is disabled, ambiguous pairs are
   logged but not merged. The per-batch fallback counter feeds a
   calibration metric that gates flipping the default to False after
   a soak window (see plan ``pipeline-cost-latency-reduction-v2.md``).

Orphan handling: entities with zero relationships pass through unchanged
— no ``status="pending"`` marker (no downstream consumer reads it; the
critic flagged it as an undefined state in P0-3 review).
"""

from __future__ import annotations

import logging
import math
import re
import string
from typing import TYPE_CHECKING, Any

from beever_atlas.agents.schemas.extraction import ExtractedEntity, ExtractedRelationship
from beever_atlas.agents.schemas.validation import MergeRecord, ValidationResult

if TYPE_CHECKING:
    from beever_atlas.stores.entity_registry import EntityRegistry

logger = logging.getLogger(__name__)

# Cosine similarity thresholds (architect-approved bands).
COSINE_MERGE_THRESHOLD = 0.92
COSINE_AMBIGUOUS_LOWER = 0.85

# Hard cap on LLM-resolved ambiguous pairs per batch invocation, to
# keep the safety net cheap. The plan permits up to 5 pairs per call.
MAX_LLM_FALLBACK_PAIRS = 5

# Translation table for the punctuation-strip step of name normalization.
_PUNCT_STRIP = str.maketrans({ch: " " for ch in string.punctuation})
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Return a canonical-comparison form of *name*.

    Steps: lowercase → strip punctuation → collapse whitespace. The
    output is suitable for exact-match equality keys in a dict.

    Empty / non-string inputs return an empty string so callers can
    use truthiness to filter unusable rows without raising.
    """
    if not isinstance(name, str):
        return ""
    lowered = name.lower().translate(_PUNCT_STRIP)
    return _WHITESPACE_RE.sub(" ", lowered).strip()


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors.

    Returns 0.0 when either vector is empty or zero-magnitude — those
    cases are treated as "no signal" rather than raising. Callers
    compare against threshold constants and ignore the 0.0 floor.
    """
    if not vec_a or not vec_b:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b, strict=False):
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _coerce_entity(raw: Any) -> ExtractedEntity | None:
    """Best-effort conversion of an LLM-emitted dict into ``ExtractedEntity``.

    Returns ``None`` on validation failure rather than raising — a
    single malformed row should not abort the dedup pass for the
    whole batch.
    """
    if isinstance(raw, ExtractedEntity):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return ExtractedEntity.model_validate(raw)
    except Exception:  # pragma: no cover — defensive
        logger.warning("entity_dedup: dropping malformed entity row: %r", raw)
        return None


def _coerce_relationship(raw: Any) -> ExtractedRelationship | None:
    """Best-effort conversion of an LLM-emitted dict into ``ExtractedRelationship``."""
    if isinstance(raw, ExtractedRelationship):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return ExtractedRelationship.model_validate(raw)
    except Exception:  # pragma: no cover — defensive
        logger.warning("entity_dedup: dropping malformed relationship row: %r", raw)
        return None


def _merge_two(primary: ExtractedEntity, secondary: ExtractedEntity) -> ExtractedEntity:
    """Merge *secondary* into *primary*: union aliases, keep more-complete props.

    ``primary`` wins on canonical ``name`` and ``type``. Aliases union
    (preserving order), and any unset properties on ``primary`` are
    filled from ``secondary``. Mutates a copy — the inputs are left
    intact so other merge paths can re-use them.
    """
    merged = primary.model_copy(deep=True)

    # Add secondary.name and its aliases to the alias list. We dedup
    # on the EXACT spelling (not normalized form) so casing variants
    # like ``"jeff tai"`` are retained as aliases of ``"Jeff Tai"`` —
    # downstream readers and the wiki layer can surface the variant
    # spellings that appeared in the source data.
    seen_exact: set[str] = {primary.name, *primary.aliases}
    for candidate in [secondary.name, *secondary.aliases]:
        if not candidate or candidate in seen_exact:
            continue
        merged.aliases.append(candidate)
        seen_exact.add(candidate)

    # Fill empty properties on primary from secondary.
    prim_props = merged.properties.model_dump()
    sec_props = secondary.properties.model_dump()
    for key, sec_val in sec_props.items():
        if sec_val and not prim_props.get(key):
            setattr(merged.properties, key, sec_val)

    # Backfill source_message_id when primary lacks one.
    if not merged.source_message_id and secondary.source_message_id:
        merged.source_message_id = secondary.source_message_id

    return merged


def _llm_resolve_pair(
    name_a: str,
    name_b: str,
) -> bool:  # pragma: no cover — hook patched in tests
    """Hook for the bounded LLM fallback on ambiguous (0.85–0.92) pairs.

    The default implementation conservatively returns ``False`` (no
    merge). Tests patch this symbol to simulate LLM-yes / LLM-no
    decisions. A future PR may wire a real Gemini call here, bounded
    to ``MAX_LLM_FALLBACK_PAIRS`` per batch — until then, the dedup
    pass relies on the deterministic tiers and the fallback counter
    feeds the calibration data needed to flip the default off.
    """
    logger.debug(
        "entity_dedup: ambiguous pair (no LLM hook): %r ~ %r — leaving unmerged",
        name_a,
        name_b,
    )
    return False


class _EmbeddingsFailed(Exception):
    """Wraps a ``compute_name_embeddings_batch`` failure.

    Re-raised inside the dedup body so the outer ``except`` clause
    can drop to exact-match-only mode without catching truly unrelated
    exceptions (e.g. ``TypeError`` from a logic bug).
    """


async def _safe_compute_embeddings(
    registry: "EntityRegistry",
    names: list[str],
) -> dict[str, list[float]]:
    """Call the registry's batch-embedding API, normalising failures.

    Any underlying exception is re-raised as ``_EmbeddingsFailed`` so
    the outer ``try/except`` in :func:`dedupe_entities` can detect
    the embedding-tier-only failure path without swallowing bugs.
    """
    try:
        return await registry.compute_name_embeddings_batch(names)
    except Exception as exc:
        raise _EmbeddingsFailed(str(exc)) from exc


async def dedupe_entities(
    entities: list[dict[str, Any]] | list[ExtractedEntity],
    relationships: list[dict[str, Any]] | list[ExtractedRelationship],
    prior_entities: list[dict[str, Any]] | None = None,
    llm_fallback_enabled: bool = True,
    entity_registry: "EntityRegistry | None" = None,
) -> tuple[ValidationResult, int]:
    """Deterministic cross-batch dedup over *entities* and *relationships*.

    Args:
        entities: Current-batch extracted entities (dicts or models).
        relationships: Current-batch extracted relationships.
        prior_entities: Optional list of ``{name, type, aliases}`` dicts
            from the graph (``EntityRegistry.get_all_canonical()``).
            New-batch entities whose normalized name (or alias)
            matches a prior entity will adopt the prior canonical name.
        llm_fallback_enabled: When True, ambiguous (0.85–0.92) pairs
            are routed through ``_llm_resolve_pair`` (bounded by
            ``MAX_LLM_FALLBACK_PAIRS``). When False, ambiguous pairs
            are logged but not merged.
        entity_registry: Used for the Tier-2 embedding pass and for
            ``is_merge_rejected`` lookups. When ``None``, Tier 2 is
            skipped (exact-match-only mode).

    Returns:
        ``(ValidationResult, llm_fallback_count)``. The second element
        is the number of pairs that triggered the LLM fallback in this
        invocation — surfaced by the caller as the
        ``cross_batch_validator_llm_fallback_count`` metric.
    """
    # ── Coerce inputs to typed models ────────────────────────────────
    typed_entities: list[ExtractedEntity] = []
    for raw in entities or []:
        coerced = _coerce_entity(raw)
        if coerced is not None:
            typed_entities.append(coerced)

    typed_relationships: list[ExtractedRelationship] = []
    for raw in relationships or []:
        coerced_rel = _coerce_relationship(raw)
        if coerced_rel is not None:
            typed_relationships.append(coerced_rel)

    if not typed_entities:
        return (
            ValidationResult(
                entities=[],
                relationships=typed_relationships,
                merges=[],
            ),
            0,
        )

    # ── Build prior-entity alias map (norm-name → canonical) ─────────
    prior_canonical_by_norm: dict[str, str] = {}
    for prior in prior_entities or []:
        if not isinstance(prior, dict):
            continue
        canonical = (prior.get("name") or "").strip()
        if not canonical:
            continue
        norm_canon = normalize_name(canonical)
        if norm_canon:
            prior_canonical_by_norm.setdefault(norm_canon, canonical)
        for alias in prior.get("aliases") or []:
            norm_alias = normalize_name(alias if isinstance(alias, str) else "")
            if norm_alias:
                prior_canonical_by_norm.setdefault(norm_alias, canonical)

    # ── Tier 1: exact-match merges on normalized name ────────────────
    buckets: dict[str, ExtractedEntity] = {}
    rename_map: dict[str, str] = {}  # original name → canonical name

    for ent in typed_entities:
        norm = normalize_name(ent.name)
        if not norm:
            # Defensive: keep nameless rows out of the buckets so
            # later merges don't collapse unrelated entities.
            continue
        # Prefer prior canonical name when the new entity matches an
        # existing graph entity by normalized name OR alias.
        prior_canonical = prior_canonical_by_norm.get(norm)
        if prior_canonical:
            canonical_name = prior_canonical
            canonical_norm = normalize_name(prior_canonical)
        else:
            canonical_name = ent.name
            canonical_norm = norm

        if canonical_norm in buckets:
            # The first-seen entity already owns the canonical slot —
            # merge the new entity into it. The bucket head's name
            # (not the new entity's spelling) is the canonical the
            # relationship rewriter must point at.
            existing_head = buckets[canonical_norm]
            buckets[canonical_norm] = _merge_two(existing_head, ent)
            rename_map[ent.name] = existing_head.name
        else:
            head = ent.model_copy(deep=True)
            if head.name != canonical_name:
                # Demote the original name to an alias if it differs
                # from the prior-canonical preference.
                if normalize_name(head.name) != normalize_name(canonical_name):
                    if head.name and head.name not in head.aliases:
                        head.aliases.append(head.name)
                head.name = canonical_name
            buckets[canonical_norm] = head
            rename_map[ent.name] = canonical_name

    # Also fold any alias variants of canonical names into the rename map.
    for head in buckets.values():
        for alias in head.aliases:
            rename_map.setdefault(alias, head.name)

    # ── Tier 2: embedding cosine similarity merges (try/except guarded) ──
    embedding_failed = False
    cosine_pairs: list[tuple[str, str, float]] = []  # (norm_a, norm_b, score)

    if entity_registry is not None and len(buckets) > 1:
        canonical_norms = list(buckets.keys())
        names_for_embedding = [buckets[n].name for n in canonical_norms]
        try:
            vectors_by_name = await _safe_compute_embeddings(entity_registry, names_for_embedding)
        except _EmbeddingsFailed:
            embedding_failed = True
            logger.warning("entity_dedup: embedding call failed, falling back to exact-match-only")
            vectors_by_name = {}

        if not embedding_failed:
            # Pairwise cosine. O(N^2) is fine here — batch size is
            # bounded by the LLM-output cap (~50 entities/batch).
            for i in range(len(canonical_norms)):
                for j in range(i + 1, len(canonical_norms)):
                    norm_a = canonical_norms[i]
                    norm_b = canonical_norms[j]
                    if norm_a not in buckets or norm_b not in buckets:
                        continue
                    name_a = buckets[norm_a].name
                    name_b = buckets[norm_b].name
                    # Skip previously-rejected merges.
                    if entity_registry.is_merge_rejected(name_a, name_b):
                        continue
                    vec_a = vectors_by_name.get(name_a) or []
                    vec_b = vectors_by_name.get(name_b) or []
                    sim = _cosine_similarity(vec_a, vec_b)
                    if sim >= COSINE_MERGE_THRESHOLD:
                        # Merge b → a; preserve the older entry as canonical.
                        absorbed_aliases = list(buckets[norm_b].aliases)
                        absorbed_name = buckets[norm_b].name
                        merged = _merge_two(buckets[norm_a], buckets[norm_b])
                        buckets[norm_a] = merged
                        # Record the rename for relationship rewrite.
                        rename_map[absorbed_name] = merged.name
                        for alias in absorbed_aliases:
                            rename_map.setdefault(alias, merged.name)
                        del buckets[norm_b]
                    elif sim >= COSINE_AMBIGUOUS_LOWER:
                        cosine_pairs.append((norm_a, norm_b, sim))

    # ── Tier 3: bounded LLM fallback on ambiguous pairs ───────────────
    llm_fallback_count = 0
    for norm_a, norm_b, score in cosine_pairs[:MAX_LLM_FALLBACK_PAIRS]:
        if norm_a not in buckets or norm_b not in buckets:
            continue  # already merged via a chain earlier
        name_a = buckets[norm_a].name
        name_b = buckets[norm_b].name
        if not llm_fallback_enabled:
            logger.info(
                "entity_dedup: ambiguous pair %r ~ %r score=%.3f (fallback disabled, not merged)",
                name_a,
                name_b,
                score,
            )
            continue
        llm_fallback_count += 1
        if _llm_resolve_pair(name_a, name_b):
            absorbed_aliases = list(buckets[norm_b].aliases)
            absorbed_name = buckets[norm_b].name
            merged = _merge_two(buckets[norm_a], buckets[norm_b])
            buckets[norm_a] = merged
            rename_map[absorbed_name] = merged.name
            for alias in absorbed_aliases:
                rename_map.setdefault(alias, merged.name)
            del buckets[norm_b]

    # Emit MergeRecord rows for every canonical that absorbed at least
    # one source-name variant. The canonical's own spelling is excluded
    # from ``merged_from`` (we report only the discarded variants), but
    # case / punctuation variants that share its normalized form ARE
    # included — they were discarded from the bucket and are useful to
    # surface for audit.
    merges: list[MergeRecord] = []
    for _canonical_norm, head in buckets.items():
        sources = [src for src, dst in rename_map.items() if dst == head.name and src != head.name]
        # Dedup while preserving order.
        seen_srcs: set[str] = set()
        merged_from: list[str] = []
        for src in sources:
            if src and src not in seen_srcs:
                merged_from.append(src)
                seen_srcs.add(src)
        if merged_from:
            merges.append(MergeRecord(canonical=head.name, merged_from=merged_from))

    # ── Relationship rewrite: point source/target at canonical names ──
    rewritten: list[ExtractedRelationship] = []
    for rel in typed_relationships:
        new_rel = rel.model_copy(deep=True)
        new_rel.source = rename_map.get(rel.source, rel.source)
        new_rel.target = rename_map.get(rel.target, rel.target)
        rewritten.append(new_rel)

    # Final entity list — ordered by first occurrence (insertion order
    # of dict is preserved in CPython 3.7+).
    final_entities = list(buckets.values())

    return (
        ValidationResult(
            entities=final_entities,
            relationships=rewritten,
            merges=merges,
        ),
        llm_fallback_count,
    )
