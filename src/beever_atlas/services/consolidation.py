"""Consolidation service — builds Tier 1 topic clusters and Tier 0 channel summaries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from beever_atlas.infra.config import Settings
from beever_atlas.models.domain import AtomicFact, ChannelSummary, TopicCluster
from beever_atlas.models.sync_policy import ConsolidationConfig
from beever_atlas.stores.graph_protocol import GraphStore
from beever_atlas.stores.weaviate_store import WeaviateStore

logger = logging.getLogger(__name__)


@dataclass
class ConsolidationResult:
    """Stats returned after a consolidation run."""

    channel_id: str
    clusters_created: int = 0
    clusters_updated: int = 0
    clusters_merged: int = 0
    clusters_split: int = 0
    clusters_deleted: int = 0
    facts_clustered: int = 0
    summaries_generated: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ClusterContext:
    """Aggregated context for a single topic cluster's LLM prompt."""

    facts: list[AtomicFact]  # filtered + sorted, top 20
    aggregated_entity_tags: list[str]
    aggregated_action_tags: list[str]
    all_topic_tags: list[str]  # union of all member topic_tags for LLM selection
    authors: list[str]
    date_range_start: str
    date_range_end: str
    media_refs: list[str]
    media_names: list[str]
    link_refs: list[str]
    high_importance_count: int
    fact_type_counts: dict[str, int]
    graph_entities: list[dict[str, str]]  # [{"id", "name", "type"}]
    graph_relationships: list[dict[str, str]]  # [{"source", "type", "target", "confidence"}]


@dataclass
class ChannelContext:
    """Aggregated context for a channel-level LLM prompt."""

    clusters: list[TopicCluster]
    graph_decisions: list[dict[str, str]]
    graph_entities: list[dict[str, str]]
    graph_relationships: list[dict[str, str]]
    date_range_start: str
    date_range_end: str
    total_media: int
    total_authors: int


class ConsolidationService:
    """Builds Tier 1 topic clusters and Tier 0 channel summaries from atomic facts.

    Clustering uses pre-computed Jina v4 embedding cosine similarity (no LLM cost).
    Summaries are generated via ADK summarizer agent.
    """

    def __init__(
        self,
        weaviate: WeaviateStore,
        settings: Settings,
        graph: GraphStore | None = None,
        consolidation_config: ConsolidationConfig | None = None,
    ) -> None:
        self._weaviate = weaviate
        self._settings = settings
        # Per-channel overrides from policy (if provided)
        if consolidation_config and consolidation_config.similarity_threshold is not None:
            self._similarity_threshold = consolidation_config.similarity_threshold
        else:
            self._similarity_threshold = settings.cluster_similarity_threshold
        if consolidation_config and consolidation_config.merge_threshold is not None:
            self._merge_threshold = consolidation_config.merge_threshold
        else:
            self._merge_threshold = settings.cluster_merge_threshold
        self._max_cluster_size = settings.cluster_max_size
        self._min_facts_for_clustering = (
            consolidation_config.min_facts_for_clustering
            if consolidation_config and consolidation_config.min_facts_for_clustering is not None
            else 0
        )
        self._graph = graph

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def on_sync_complete(
        self,
        channel_id: str,
        channel_name: str = "",
    ) -> ConsolidationResult:
        """Run incremental consolidation after a channel sync."""
        result = ConsolidationResult(channel_id=channel_id)

        try:
            created, updated = await self._incremental_cluster(channel_id, result)
            touched = created + updated
            if touched:
                await self._generate_summaries(channel_id, touched, result)
                await self._generate_channel_summary(
                    channel_id,
                    result,
                    channel_name=channel_name,
                )
                await self._apply_cross_cluster_links(channel_id)
            await self._health_check(channel_id, result)
        except Exception as exc:
            logger.error("Consolidation error for %s: %s", channel_id, exc, exc_info=True)
            result.errors.append(str(exc))

        # Mark wiki as stale after consolidation
        try:
            from beever_atlas.infra.config import get_settings
            from beever_atlas.wiki.cache import WikiCache

            settings = get_settings()
            cache = WikiCache(settings.mongodb_uri)
            await cache.mark_all_stale(channel_id)
        except Exception:
            logger.warning("Failed to mark wiki stale for channel %s", channel_id)

        return result

    async def full_reconsolidate(
        self,
        channel_id: str,
        channel_name: str = "",
    ) -> ConsolidationResult:
        """Full rebuild: reset all clusters and re-cluster from scratch."""
        result = ConsolidationResult(channel_id=channel_id)

        try:
            # Delete all existing clusters for this channel
            existing = await self._weaviate.list_clusters(channel_id)
            for cluster in existing:
                await self._weaviate.delete_cluster(cluster.id)
                result.clusters_deleted += 1

            # Reset cluster_id on every fact. Stream via cursor pagination over
            # id-only records (no vectors, no text) so the reset scales to
            # channels with tens of thousands of facts without hitting the
            # Weaviate 10MB gRPC response cap.
            batch: list[tuple[str, str]] = []
            BATCH = 500
            async for fact_id, cluster_id in self._weaviate.iter_all_fact_ids(channel_id):
                if cluster_id != "__none__":
                    batch.append((fact_id, "__none__"))
                if len(batch) >= BATCH:
                    await self._weaviate.batch_update_fact_clusters(batch)
                    batch = []
            if batch:
                await self._weaviate.batch_update_fact_clusters(batch)

            # Now run incremental (which will process all facts since none are clustered)
            created, updated = await self._incremental_cluster(channel_id, result)
            touched = created + updated
            if touched:
                await self._generate_summaries(channel_id, touched, result)
                await self._generate_channel_summary(
                    channel_id,
                    result,
                    channel_name=channel_name,
                )
                await self._apply_cross_cluster_links(channel_id)
        except Exception as exc:
            logger.error("Full reconsolidation error for %s: %s", channel_id, exc, exc_info=True)
            result.errors.append(str(exc))

        return result

    # ------------------------------------------------------------------
    # Clustering (UNCHANGED)
    # ------------------------------------------------------------------

    async def _incremental_cluster(
        self,
        channel_id: str,
        result: ConsolidationResult,
    ) -> tuple[list[str], list[str]]:
        """Assign unclustered facts to existing or new clusters."""
        created_ids: list[str] = []
        updated_ids: list[str] = []

        unclustered = await self._weaviate.get_unclustered_facts(channel_id)
        if not unclustered:
            return created_ids, updated_ids

        existing_clusters = await self._weaviate.list_clusters(channel_id)

        # Build assignments: fact_id -> cluster
        assignments: dict[str, str] = {}  # fact_id -> cluster_id
        touched_cluster_ids: set[str] = set()

        for fact in unclustered:
            if not fact.text_vector:
                continue

            best_cluster = None
            best_sim = 0.0

            for cluster in existing_clusters:
                if not cluster.centroid_vector:
                    continue
                sim = self._cosine_similarity(fact.text_vector, cluster.centroid_vector)
                if sim > best_sim:
                    best_sim = sim
                    best_cluster = cluster

            if best_cluster and best_sim > self._similarity_threshold:
                assignments[fact.id] = best_cluster.id
                best_cluster.member_ids.append(fact.id)
                best_cluster.member_count += 1
                touched_cluster_ids.add(best_cluster.id)
            else:
                # Create new cluster seeded with this fact
                new_cluster = TopicCluster(
                    channel_id=channel_id,
                    summary="",
                    topic_tags=list(fact.topic_tags),
                    member_ids=[fact.id],
                    member_count=1,
                    centroid_vector=list(fact.text_vector),
                )
                existing_clusters.append(new_cluster)
                assignments[fact.id] = new_cluster.id
                created_ids.append(new_cluster.id)
                touched_cluster_ids.add(new_cluster.id)

        # Batch update fact cluster_ids
        if assignments:
            await self._weaviate.batch_update_fact_clusters(
                [(fid, cid) for fid, cid in assignments.items()]
            )
            result.facts_clustered += len(assignments)

        # Recompute centroids for touched clusters and upsert
        for cluster in existing_clusters:
            if cluster.id not in touched_cluster_ids:
                continue

            # Use the facts we already have in memory for centroid recomputation
            member_vectors = []
            for uf in unclustered:
                if uf.id in cluster.member_ids and uf.text_vector:
                    member_vectors.append(uf.text_vector)

            if member_vectors:
                cluster.centroid_vector = self._compute_centroid(member_vectors)

            await self._weaviate.upsert_cluster(cluster)
            if cluster.id not in created_ids:
                updated_ids.append(cluster.id)

        result.clusters_created += len(created_ids)
        result.clusters_updated += len(updated_ids)

        return created_ids, updated_ids

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Pure Python cosine similarity for 2048-dim vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _compute_centroid(vectors: list[list[float]]) -> list[float]:
        """Compute the mean vector (centroid) of a list of vectors."""
        if not vectors:
            return []
        dim = len(vectors[0])
        centroid = [0.0] * dim
        for vec in vectors:
            for i, v in enumerate(vec):
                centroid[i] += v
        n = len(vectors)
        return [c / n for c in centroid]

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    async def _build_cluster_context(
        self,
        members: list[AtomicFact],
        channel_id: str,
    ) -> ClusterContext:
        """Build aggregated context from cluster member facts."""
        # Filter out superseded facts, keep potential_contradiction
        active = [f for f in members if f.superseded_by is None]
        # Sort by quality_score descending, take top 20
        active.sort(key=lambda f: f.quality_score, reverse=True)
        top_facts = active[:20]

        # Aggregate tags
        all_entity_tags: set[str] = set()
        all_action_tags: set[str] = set()
        all_topic_tags: set[str] = set()
        authors: set[str] = set()
        media_refs: list[str] = []
        media_names: list[str] = []
        link_refs: list[str] = []
        timestamps: list[str] = []
        high_count = 0
        fact_type_counts: dict[str, int] = {}

        for f in active:
            all_entity_tags.update(f.entity_tags)
            all_action_tags.update(f.action_tags)
            all_topic_tags.update(f.topic_tags)
            if f.author_name:
                authors.add(f.author_name)
            media_refs.extend(f.source_media_urls)
            media_names.extend(f.source_media_names)
            link_refs.extend(f.source_link_urls)
            if f.message_ts:
                timestamps.append(f.message_ts)
            if f.importance in ("high", "critical"):
                high_count += 1
            if f.fact_type:
                ft = f.fact_type.lower().strip()
                fact_type_counts[ft] = fact_type_counts.get(ft, 0) + 1

        timestamps.sort()
        date_start = timestamps[0] if timestamps else ""
        date_end = timestamps[-1] if timestamps else ""

        # Graph enrichment
        graph_entities: list[dict[str, str]] = []
        graph_relationships: list[dict[str, str]] = []

        if self._graph is not None:
            try:
                entities_list = await self._graph.list_entities(
                    channel_id=channel_id,
                    limit=200,
                )
                entity_lookup = {e.name.lower(): e for e in entities_list}

                # Resolve entity_tags against graph entities
                seen_ids: set[str] = set()
                for tag in all_entity_tags:
                    entity = entity_lookup.get(tag.lower())
                    if entity and entity.id not in seen_ids:
                        seen_ids.add(entity.id)
                        graph_entities.append(
                            {
                                "id": entity.id,
                                "name": entity.name,
                                "type": entity.type,
                            }
                        )
            except Exception:
                logger.debug("Graph entity lookup failed for channel %s", channel_id)

            try:
                rels = await self._graph.list_relationships(
                    channel_id=channel_id,
                    limit=100,
                )
                graph_relationships = [
                    {
                        "source": r.source,
                        "type": r.type,
                        "target": r.target,
                        "confidence": str(r.confidence),
                    }
                    for r in rels
                    if r.confidence >= 0.3
                ]
            except Exception:
                logger.debug("Graph relationship lookup failed for channel %s", channel_id)

        return ClusterContext(
            facts=top_facts,
            aggregated_entity_tags=sorted(all_entity_tags),
            aggregated_action_tags=sorted(all_action_tags),
            all_topic_tags=sorted(all_topic_tags),
            authors=sorted(authors),
            date_range_start=date_start,
            date_range_end=date_end,
            media_refs=media_refs,
            media_names=media_names,
            link_refs=link_refs,
            high_importance_count=high_count,
            fact_type_counts=fact_type_counts,
            graph_entities=graph_entities,
            graph_relationships=graph_relationships,
        )

    @staticmethod
    def _format_topic_prompt(ctx: ClusterContext) -> str:
        """Format an LLM prompt for topic cluster summarization.

        Requests structured JSON output matching TopicSummaryResult:
        title, summary_text, current_state, open_questions, impact_note,
        topic_tags (3 most representative), faq_candidates.
        """
        parts: list[str] = [
            "You are summarizing a topic cluster from a team channel. "
            "Return structured JSON with the following fields:\n"
            "\n"
            "- **title**: A short descriptive name for this topic (5-10 words, e.g. "
            "'JWT Migration to RS256', 'CI/CD Pipeline Redesign'). "
            "Do NOT use the first sentence of the summary as the title.\n"
            "- **summary_text**: A 2-3 sentence narrative of what happened chronologically. "
            "Include key decisions, actions, and who was involved.\n"
            "- **current_state**: 1-2 sentences on where things stand NOW — "
            "what's done, what's in progress, what's pending. "
            "Reflect the most recent facts, not a blend of old and new.\n"
            "- **open_questions**: 1-2 sentences on unresolved tensions, pending debates, "
            "or blocked items. Leave as empty string if everything is resolved.\n"
            "- **impact_note**: 1 sentence on scope and significance — "
            "what depends on this, how many people or services are affected.\n"
            "- **topic_tags**: Select exactly 3 most representative thematic tags "
            "from the available tags listed below. If fewer than 3 distinct themes exist, "
            "use fewer.\n"
            "- **faq_candidates**: 0-3 Q&A pairs based on question-type facts and "
            "common themes. Each has 'question' and 'answer' fields.\n"
            "\n"
            "IMPORTANT: Each text field must contain DISTINCT, non-overlapping information. "
            "Do not repeat content across fields.",
        ]

        if ctx.date_range_start or ctx.date_range_end:
            parts.append(f"\nTime range: {ctx.date_range_start} to {ctx.date_range_end}")

        if ctx.authors:
            parts.append(f"Contributors: {', '.join(ctx.authors)}")

        if ctx.graph_entities:
            entity_strs = [f"{e['name']} ({e['type']})" for e in ctx.graph_entities[:10]]
            parts.append(f"Key entities: {', '.join(entity_strs)}")

        if ctx.graph_relationships:
            rel_strs = [
                f"{r['source']} -> {r['type']} -> {r['target']}"
                for r in ctx.graph_relationships[:8]
            ]
            parts.append(f"Key relationships: {', '.join(rel_strs)}")

        if ctx.fact_type_counts:
            type_strs = [f"{v} {k}s" for k, v in sorted(ctx.fact_type_counts.items())]
            parts.append(f"Fact types: {', '.join(type_strs)}")

        # Available topic tags for selection
        if ctx.all_topic_tags:
            parts.append(
                f"\nAvailable topic tags (select 3 most representative): "
                f"{', '.join(ctx.all_topic_tags)}"
            )

        # Facts section
        fact_lines: list[str] = []
        for f in ctx.facts:
            label = f.importance.upper() if f.importance else "MEDIUM"
            ft = f.fact_type or "observation"
            text = f.memory_text[:200]
            author_part = f" (by {f.author_name})" if f.author_name else ""
            fact_lines.append(f"- [{label}/{ft}] {text}{author_part}")

        if fact_lines:
            parts.append("\nFacts (ranked by quality):")
            parts.extend(fact_lines)

        # Question-type facts for FAQ generation
        question_facts = [f for f in ctx.facts if f.fact_type == "question"]
        if question_facts:
            parts.append("\nQuestion-type facts (use for faq_candidates):")
            for qf in question_facts:
                parts.append(f"- Q: {qf.memory_text[:200]}")

        media_count = len(ctx.media_refs)
        link_count = len(ctx.link_refs)
        if media_count or link_count:
            parts.append(f"\nMedia/links: {media_count} files, {link_count} links")

        return "\n".join(parts)

    async def _build_channel_context(
        self,
        clusters: list[TopicCluster],
        channel_id: str,
    ) -> ChannelContext:
        """Build aggregated context for a channel-level summary."""
        graph_decisions: list[dict[str, str]] = []
        graph_entities: list[dict[str, str]] = []
        graph_relationships: list[dict[str, str]] = []

        if self._graph is not None:
            try:
                decisions_raw, entities_raw, rels_raw = await asyncio.gather(
                    self._graph.get_decisions(channel_id=channel_id, limit=10),
                    self._graph.list_entities(channel_id=channel_id, limit=20),
                    self._graph.list_relationships(channel_id=channel_id, limit=20),
                )
                graph_decisions = [{"name": e.name, "type": e.type} for e in decisions_raw]
                graph_entities = [
                    {"id": e.id, "name": e.name, "type": e.type} for e in entities_raw
                ]
                graph_relationships = [
                    {
                        "source": r.source,
                        "type": r.type,
                        "target": r.target,
                        "confidence": str(r.confidence),
                    }
                    for r in rels_raw
                ]
            except Exception:
                logger.debug("Graph channel context failed for %s", channel_id)

        # Compute temporal range across all clusters
        all_starts = [c.date_range_start for c in clusters if c.date_range_start]
        all_ends = [c.date_range_end for c in clusters if c.date_range_end]
        date_start = min(all_starts) if all_starts else ""
        date_end = max(all_ends) if all_ends else ""

        total_media = sum(len(c.media_refs) for c in clusters)
        all_authors: set[str] = set()
        for c in clusters:
            all_authors.update(c.authors)

        return ChannelContext(
            clusters=clusters,
            graph_decisions=graph_decisions,
            graph_entities=graph_entities,
            graph_relationships=graph_relationships,
            date_range_start=date_start,
            date_range_end=date_end,
            total_media=total_media,
            total_authors=len(all_authors),
        )

    @staticmethod
    def _format_channel_prompt(ctx: ChannelContext) -> str:
        """Format an LLM prompt for channel-level summarization.

        Requests structured JSON output matching ChannelSummaryResult:
        summary_text, description, themes, momentum, team_dynamics, glossary_terms.
        """
        parts: list[str] = [
            "You are generating a channel-level knowledge summary. "
            "Return structured JSON with the following fields:\n"
            "\n"
            "- **summary_text**: 3-5 sentence overview narrative of what this channel "
            "is about and what the team has been working on.\n"
            "- **description**: One-line channel purpose statement, max 200 chars "
            "(e.g. 'Backend architecture decisions, deployment workflows, and "
            "infrastructure discussions').\n"
            "- **themes**: 2-3 sentences on the main knowledge areas and how they "
            "interrelate. Don't just list topics — explain how they connect "
            "(e.g. 'Authentication and API design are closely linked, with most auth "
            "decisions driving API changes.').\n"
            "- **momentum**: 1-2 sentences on what's active vs. completed vs. stale. "
            "Include concrete numbers from the data below (active topics, recent facts).\n"
            "- **team_dynamics**: 1-2 sentences on who drives decisions, collaboration "
            "patterns, and expertise distribution.\n"
            "- **glossary_terms**: 0-10 channel-specific jargon, acronyms, or technical "
            "terms with definitions. Each entry has 'term', 'definition' (1-2 sentences), "
            "'first_mentioned_by' (author name or empty string), and 'related_topics' "
            "(list of topic names). Exclude common terms (API, database, server) — only "
            "include terms specific to this channel's domain.\n"
            "\n"
            "IMPORTANT: Each text field must contain DISTINCT, non-overlapping information. "
            "Do not repeat content across fields.",
        ]

        if ctx.date_range_start or ctx.date_range_end:
            parts.append(f"\nTime range: {ctx.date_range_start} to {ctx.date_range_end}")
            parts.append(f"Total media: {ctx.total_media}, Total contributors: {ctx.total_authors}")

        if ctx.graph_decisions:
            dec_strs = [d["name"] for d in ctx.graph_decisions[:5]]
            parts.append(f"Key decisions: {', '.join(dec_strs)}")

        if ctx.graph_entities:
            ent_strs = [f"{e['name']} ({e['type']})" for e in ctx.graph_entities[:10]]
            parts.append(f"Key entities: {', '.join(ent_strs)}")

        if ctx.graph_relationships:
            rel_strs = [
                f"{r['source']} -> {r['type']} -> {r['target']}"
                for r in ctx.graph_relationships[:8]
            ]
            parts.append(f"Key relationships: {', '.join(rel_strs)}")

        # Topics sorted by member_count descending — include multi-angle data
        sorted_clusters = sorted(ctx.clusters, key=lambda c: c.member_count, reverse=True)
        topic_lines: list[str] = []
        for c in sorted_clusters:
            title = c.title or ", ".join(c.topic_tags) or "General"
            line = f"- **{title}** ({c.member_count} facts, status: {c.status})"
            if c.summary:
                line += f": {c.summary}"
            if c.current_state:
                line += f" | Current: {c.current_state}"
            if c.authors:
                line += f" | Contributors: {', '.join(c.authors[:5])}"
            topic_lines.append(line)

        if topic_lines:
            parts.append("\nTopics:")
            parts.extend(topic_lines)

        # People data for team_dynamics
        all_people: list[str] = []
        for c in sorted_clusters:
            for p in c.people:
                role_str = f"{p.get('name', '')} ({p.get('role', 'mentioned')})"
                if role_str not in all_people:
                    all_people.append(role_str)
        if all_people:
            parts.append(f"\nPeople & roles: {', '.join(all_people[:15])}")

        # Activity counts for momentum
        active_count = sum(1 for c in ctx.clusters if c.status == "active")
        completed_count = sum(1 for c in ctx.clusters if c.status == "completed")
        stale_count = sum(1 for c in ctx.clusters if c.status == "stale")
        parts.append(
            f"\nTopic status: {active_count} active, {completed_count} completed, "
            f"{stale_count} stale"
        )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Staleness + status
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_staleness(
        date_range_start: str,
        date_range_end: str,
        member_count: int,
        now: datetime | None = None,
    ) -> float:
        """Compute a staleness score in [0.0, 1.0] for a cluster."""
        if not date_range_end:
            return 0.0

        if now is None:
            now = datetime.now(tz=UTC)

        try:
            end = datetime.fromisoformat(date_range_end)
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            # Try parsing as epoch timestamp
            try:
                end = datetime.fromtimestamp(float(date_range_end), tz=UTC)
            except (ValueError, TypeError):
                return 0.0

        try:
            start = datetime.fromisoformat(date_range_start)
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            try:
                start = datetime.fromtimestamp(float(date_range_start), tz=UTC)
            except (ValueError, TypeError):
                start = end

        cadence = (end - start).days / member_count if member_count > 0 else 0
        days_since = (now - end).days

        staleness = days_since / max(cadence * 3, 30)
        return max(0.0, min(1.0, staleness))

    @staticmethod
    def _derive_status(
        staleness_score: float,
        fact_type_counts: dict[str, int],
        action_tags: list[str],
    ) -> str:
        """Derive topic status from staleness, fact types, and action tags."""
        if staleness_score > 0.8:
            return "stale"

        completion_prefixes = (
            "ship",
            "complet",
            "done",
            "close",
            "resolv",
            "finish",
            "deliver",
            "launch",
        )
        if fact_type_counts.get("decision", 0) > 0:
            for tag in action_tags:
                tag_lower = tag.lower()
                if any(tag_lower.startswith(p) for p in completion_prefixes):
                    return "completed"

        return "active"

    # ------------------------------------------------------------------
    # Wiki-ready enrichment (graph-derived structured data)
    # ------------------------------------------------------------------

    async def _enrich_decisions(
        self,
        entity_tags: list[str],
        channel_id: str,
    ) -> list[dict[str, Any]]:
        """Query Neo4j for Decision-type entities and resolve SUPERSEDES chains."""
        if not self._graph:
            return []

        decisions: list[dict[str, Any]] = []
        try:
            decision_entities = await self._graph.get_decisions(
                channel_id=channel_id,
                limit=50,
            )
            if not decision_entities:
                return []

            # Filter to decisions matching cluster entity_tags
            tag_lower = {t.lower() for t in entity_tags}
            relevant = [
                e
                for e in decision_entities
                if e.name.lower() in tag_lower or any(a.lower() in tag_lower for a in e.aliases)
            ]
            # If no tag match, include all channel decisions
            if not relevant:
                relevant = decision_entities

            # Build supersede map via relationships
            rels = await self._graph.list_relationships(
                channel_id=channel_id,
                limit=200,
            )
            # SUPERSEDES: source supersedes target
            supersedes_map: dict[str, str] = {}  # target_name -> source_name
            for r in rels:
                if r.type == "SUPERSEDES":
                    supersedes_map[r.target.lower()] = r.source

            for e in relevant:
                decided_by = ""
                if e.properties:
                    decided_by = (
                        e.properties.get("decided_by", "") or e.properties.get("owner", "") or ""
                    )

                superseded_by = supersedes_map.get(e.name.lower())
                status = "superseded" if superseded_by else "active"

                decisions.append(
                    {
                        "name": e.name,
                        "decided_by": decided_by,
                        "status": status,
                        "superseded_by": superseded_by or "",
                        "date": e.message_ts or e.created_at.isoformat() if e.created_at else "",
                        "context": "",
                    }
                )
        except Exception:
            logger.debug("Decision enrichment failed for channel %s", channel_id)

        return decisions

    async def _enrich_people(
        self,
        entity_tags: list[str],
        authors: list[str],
        channel_id: str,
    ) -> list[dict[str, str]]:
        """Derive people with roles from Neo4j graph edges.

        Roles: decision_maker > expert > contributor > mentioned.
        Falls back to authors list with role 'mentioned' when graph unavailable.
        """
        if not self._graph:
            return [{"name": a, "role": "mentioned", "entity_id": ""} for a in authors]

        people: dict[str, dict[str, str]] = {}  # name -> {"role", "entity_id"}
        role_priority = {"decision_maker": 4, "expert": 3, "contributor": 2, "mentioned": 1}

        try:
            entities = await self._graph.list_entities(
                channel_id=channel_id,
                entity_type="Person",
                limit=100,
            )
            rels = await self._graph.list_relationships(
                channel_id=channel_id,
                limit=300,
            )

            tag_lower = {t.lower() for t in entity_tags}

            # Count connections per person to cluster entities
            person_connections: dict[str, int] = {}
            person_roles: dict[str, str] = {}

            for r in rels:
                src_lower = r.source.lower()
                tgt_lower = r.target.lower()

                # Check if this person is connected to cluster entities
                for person_name in [r.source, r.target]:
                    pn_lower = person_name.lower()
                    other = tgt_lower if pn_lower == src_lower else src_lower

                    # Is the other side a cluster entity?
                    if other not in tag_lower:
                        continue

                    # Is this a Person?
                    is_person = any(
                        e.name.lower() == pn_lower and e.type == "Person" for e in entities
                    )
                    if not is_person:
                        continue

                    person_connections[person_name] = person_connections.get(person_name, 0) + 1

                    if r.type == "DECIDED":
                        current = person_roles.get(person_name, "mentioned")
                        if role_priority.get("decision_maker", 0) > role_priority.get(current, 0):
                            person_roles[person_name] = "decision_maker"
                    elif r.type in ("WORKS_ON", "USES"):
                        current = person_roles.get(person_name, "mentioned")
                        if role_priority.get("contributor", 0) > role_priority.get(current, 0):
                            person_roles[person_name] = "contributor"

            # Assign expert role for 3+ connections
            for name, count in person_connections.items():
                if count >= 3:
                    current = person_roles.get(name, "mentioned")
                    if role_priority.get("expert", 0) > role_priority.get(current, 0):
                        person_roles[name] = "expert"

            # Build result from graph-derived people
            for name, role in person_roles.items():
                entity = next(
                    (e for e in entities if e.name.lower() == name.lower()),
                    None,
                )
                people[name] = {
                    "name": name,
                    "role": role,
                    "entity_id": entity.id if entity else "",
                }

            # Add authors not found in graph as "mentioned"
            for author in authors:
                if author not in people:
                    people[author] = {
                        "name": author,
                        "role": "mentioned",
                        "entity_id": "",
                    }

        except Exception:
            logger.debug("People enrichment failed for channel %s", channel_id)
            return [{"name": a, "role": "mentioned", "entity_id": ""} for a in authors]

        return list(people.values())

    async def _enrich_technologies(
        self,
        entity_tags: list[str],
        channel_id: str,
    ) -> list[dict[str, str]]:
        """Query Neo4j for Technology-type entities with category and champion."""
        if not self._graph:
            return []

        technologies: list[dict[str, str]] = []
        try:
            entities = await self._graph.list_entities(
                channel_id=channel_id,
                entity_type="Technology",
                limit=100,
            )
            tag_lower = {t.lower() for t in entity_tags}
            relevant = [
                e
                for e in entities
                if e.name.lower() in tag_lower or any(a.lower() in tag_lower for a in e.aliases)
            ]

            if not relevant:
                return []

            # Find champions via USES/WORKS_ON relationships
            rels = await self._graph.list_relationships(
                channel_id=channel_id,
                limit=200,
            )
            tech_champions: dict[str, str] = {}
            for r in rels:
                if r.type in ("USES", "WORKS_ON"):
                    for tech in relevant:
                        if r.target.lower() == tech.name.lower():
                            # First person found becomes champion
                            if tech.name not in tech_champions:
                                tech_champions[tech.name] = r.source

            for e in relevant:
                category = ""
                if e.properties:
                    category = (
                        e.properties.get("category", "") or e.properties.get("language", "") or ""
                    )
                technologies.append(
                    {
                        "name": e.name,
                        "category": category,
                        "champion": tech_champions.get(e.name, ""),
                    }
                )
        except Exception:
            logger.debug("Technology enrichment failed for channel %s", channel_id)

        return technologies

    async def _enrich_projects(
        self,
        entity_tags: list[str],
        channel_id: str,
    ) -> list[dict[str, Any]]:
        """Query Neo4j for Project-type entities with status, owner, and blockers."""
        if not self._graph:
            return []

        projects: list[dict[str, Any]] = []
        try:
            entities = await self._graph.list_entities(
                channel_id=channel_id,
                entity_type="Project",
                limit=100,
            )
            tag_lower = {t.lower() for t in entity_tags}
            relevant = [
                e
                for e in entities
                if e.name.lower() in tag_lower or any(a.lower() in tag_lower for a in e.aliases)
            ]

            if not relevant:
                return []

            # Find owners and blockers via relationships
            rels = await self._graph.list_relationships(
                channel_id=channel_id,
                limit=200,
            )
            project_owners: dict[str, str] = {}
            project_blockers: dict[str, list[str]] = {}
            for r in rels:
                for proj in relevant:
                    proj_lower = proj.name.lower()
                    if r.type in ("WORKS_ON", "OWNS") and r.target.lower() == proj_lower:
                        if proj.name not in project_owners:
                            project_owners[proj.name] = r.source
                    elif r.type == "BLOCKED_BY" and r.source.lower() == proj_lower:
                        project_blockers.setdefault(proj.name, []).append(r.target)

            for e in relevant:
                status = ""
                if e.properties:
                    status = e.properties.get("status", "") or ""
                projects.append(
                    {
                        "name": e.name,
                        "status": status or e.status,
                        "owner": project_owners.get(e.name, ""),
                        "blockers": project_blockers.get(e.name, []),
                    }
                )
        except Exception:
            logger.debug("Project enrichment failed for channel %s", channel_id)

        return projects

    # ------------------------------------------------------------------
    # Cross-cluster links
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_cross_cluster_links(
        clusters: list[TopicCluster],
        all_members: dict[str, list[AtomicFact]],
    ) -> dict[str, list[str]]:
        """Compute related_cluster_ids for clusters sharing >=2 entity tags.

        Args:
            clusters: All clusters for a channel.
            all_members: Pre-fetched {cluster_id: [AtomicFact, ...]} mapping.

        Returns:
            {cluster_id: [related_cluster_id, ...]} for clusters that have links.
        """
        # Build {cluster_id: set[normalized entity tags]} from ALL members
        cluster_tags: dict[str, set[str]] = {}
        for cluster in clusters:
            tags: set[str] = set()
            for fact in all_members.get(cluster.id, []):
                for tag in fact.entity_tags:
                    tags.add(tag.lower().strip())
            cluster_tags[cluster.id] = tags

        # Pairwise overlap check
        links: dict[str, list[str]] = {}
        cluster_list = list(clusters)
        for i, a in enumerate(cluster_list):
            for b in cluster_list[i + 1 :]:
                overlap = cluster_tags.get(a.id, set()) & cluster_tags.get(b.id, set())
                if len(overlap) >= 2:
                    links.setdefault(a.id, []).append(b.id)
                    links.setdefault(b.id, []).append(a.id)

        return links

    @staticmethod
    def _compute_cross_cluster_shared_entities(
        clusters: list[TopicCluster],
        all_members: dict[str, list[AtomicFact]],
    ) -> list[dict[str, Any]]:
        """Compute topic_graph_edges with shared entity names between clusters."""
        # Build {cluster_id: set[normalized entity tags]} from ALL members
        cluster_tags: dict[str, set[str]] = {}
        for cluster in clusters:
            tags: set[str] = set()
            for fact in all_members.get(cluster.id, []):
                for tag in fact.entity_tags:
                    tags.add(tag.lower().strip())
            cluster_tags[cluster.id] = tags

        edges: list[dict[str, Any]] = []
        cluster_list = list(clusters)
        for i, a in enumerate(cluster_list):
            for b in cluster_list[i + 1 :]:
                overlap = cluster_tags.get(a.id, set()) & cluster_tags.get(b.id, set())
                if len(overlap) >= 2:
                    edges.append(
                        {
                            "source_cluster_id": a.id,
                            "target_cluster_id": b.id,
                            "source_title": a.title or ", ".join(a.topic_tags[:3]) or "General",
                            "target_title": b.title or ", ".join(b.topic_tags[:3]) or "General",
                            "shared_entities": sorted(overlap),
                        }
                    )
        return edges

    async def _apply_cross_cluster_links(self, channel_id: str) -> None:
        """Fetch all clusters and members, compute links, update clusters and channel summary."""
        clusters = await self._weaviate.list_clusters(channel_id)
        if len(clusters) < 2:
            return

        # Pre-fetch all members for all clusters
        all_members: dict[str, list[AtomicFact]] = {}
        for cluster in clusters:
            members = await self._weaviate.get_cluster_members(cluster.id, limit=200)
            all_members[cluster.id] = members

        links = self._compute_cross_cluster_links(clusters, all_members)

        # Update cluster related_cluster_ids
        for cluster in clusters:
            new_related = links.get(cluster.id, [])
            if sorted(new_related) != sorted(cluster.related_cluster_ids):
                cluster.related_cluster_ids = new_related
                await self._weaviate.upsert_cluster(cluster)

        # Compute and store topic_graph_edges on ChannelSummary
        edges = self._compute_cross_cluster_shared_entities(clusters, all_members)
        try:
            existing_summary = await self._weaviate.get_channel_summary(channel_id)
            if existing_summary:
                existing_summary.topic_graph_edges = edges
                await self._weaviate.upsert_channel_summary(existing_summary)
        except Exception:
            logger.debug("Failed to update topic_graph_edges for %s", channel_id)

    # ------------------------------------------------------------------
    # Summary generation
    # ------------------------------------------------------------------

    async def _generate_summaries(
        self,
        channel_id: str,
        cluster_ids: list[str],
        result: ConsolidationResult,
    ) -> None:
        """Generate LLM summaries for touched clusters."""
        sem = asyncio.Semaphore(self._settings.consolidation_max_concurrent_llm)

        # Pre-fetch clusters and members OUTSIDE semaphore
        prefetched: list[tuple[TopicCluster, list[AtomicFact]]] = []
        for cluster_id in cluster_ids:
            cluster = await self._weaviate.get_cluster(cluster_id)
            if not cluster:
                continue
            members = await self._weaviate.get_cluster_members(cluster_id, limit=50)
            if not members:
                continue
            prefetched.append((cluster, members))

        # Build contexts OUTSIDE semaphore
        contexts: list[tuple[TopicCluster, list[AtomicFact], ClusterContext]] = []
        for cluster, members in prefetched:
            ctx = await self._build_cluster_context(members, channel_id)
            contexts.append((cluster, members, ctx))

        async def _summarize_one(
            cluster: TopicCluster,
            members: list[AtomicFact],
            ctx: ClusterContext,
        ) -> None:
            async with sem:
                try:
                    prompt = self._format_topic_prompt(ctx)
                    llm_result = await self._call_topic_llm(prompt)

                    # Populate multi-angle summary fields from structured LLM output
                    cluster.title = llm_result.get("title", "")
                    cluster.summary = llm_result.get("summary_text", "")
                    cluster.current_state = llm_result.get("current_state", "")
                    cluster.open_questions = llm_result.get("open_questions", "")
                    cluster.impact_note = llm_result.get("impact_note", "")

                    # Use LLM-selected focused topic tags (max 3)
                    llm_tags = llm_result.get("topic_tags", [])
                    if llm_tags and isinstance(llm_tags, list):
                        cluster.topic_tags = llm_tags[:3]
                    else:
                        # Fallback: merge all member tags
                        all_tags: set[str] = set()
                        for m in members:
                            all_tags.update(m.topic_tags)
                        cluster.topic_tags = sorted(all_tags)[:3]

                    # FAQ candidates from LLM
                    raw_faqs = llm_result.get("faq_candidates", [])
                    cluster.faq_candidates = [
                        {"question": faq.get("question", ""), "answer": faq.get("answer", "")}
                        for faq in raw_faqs
                        if isinstance(faq, dict) and faq.get("question")
                    ]

                    # Key facts: top 5 by quality_score from active members
                    active_members = [f for f in members if f.superseded_by is None]
                    active_members.sort(key=lambda f: f.quality_score, reverse=True)
                    cluster.key_facts = [
                        {
                            "fact_id": f.id,
                            "memory_text": f.memory_text,
                            "author_name": f.author_name,
                            "message_ts": f.message_ts,
                            "fact_type": f.fact_type,
                            "importance": f.importance,
                            "quality_score": f.quality_score,
                            "source_message_id": f.source_message_id,
                        }
                        for f in active_members[:5]
                    ]

                    # Graph-derived enrichment
                    cluster.decisions = await self._enrich_decisions(
                        ctx.aggregated_entity_tags,
                        channel_id,
                    )
                    cluster.people = await self._enrich_people(
                        ctx.aggregated_entity_tags,
                        ctx.authors,
                        channel_id,
                    )
                    cluster.technologies = await self._enrich_technologies(
                        ctx.aggregated_entity_tags,
                        channel_id,
                    )
                    cluster.projects = await self._enrich_projects(
                        ctx.aggregated_entity_tags,
                        channel_id,
                    )

                    # Populate existing enrichment fields from context
                    cluster.key_entities = ctx.graph_entities
                    cluster.key_relationships = ctx.graph_relationships
                    cluster.date_range_start = ctx.date_range_start
                    cluster.date_range_end = ctx.date_range_end
                    cluster.authors = ctx.authors
                    cluster.media_refs = ctx.media_refs
                    cluster.media_names = ctx.media_names
                    cluster.link_refs = ctx.link_refs
                    cluster.high_importance_count = ctx.high_importance_count
                    cluster.fact_type_counts = ctx.fact_type_counts

                    # Compute staleness and status
                    cluster.staleness_score = self._compute_staleness(
                        ctx.date_range_start,
                        ctx.date_range_end,
                        cluster.member_count,
                    )
                    cluster.status = self._derive_status(
                        cluster.staleness_score,
                        ctx.fact_type_counts,
                        ctx.aggregated_action_tags,
                    )

                    await self._weaviate.upsert_cluster(cluster)
                    result.summaries_generated += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to generate summary for cluster %s: %s",
                        cluster.id,
                        exc,
                    )
                    result.errors.append(f"summary:{cluster.id}:{exc}")

        await asyncio.gather(
            *[_summarize_one(cluster, members, ctx) for cluster, members, ctx in contexts]
        )

    async def _generate_channel_summary(
        self,
        channel_id: str,
        result: ConsolidationResult,
        channel_name: str = "",
    ) -> None:
        """Generate a Tier 0 channel overview from cluster summaries."""
        clusters = await self._weaviate.list_clusters(channel_id)
        if not clusters:
            return

        ctx = await self._build_channel_context(clusters, channel_id)
        prompt = self._format_channel_prompt(ctx)

        llm_result = await self._call_channel_llm(prompt)

        total_facts = sum(c.member_count for c in clusters)
        worst_staleness = max(
            (c.staleness_score for c in clusters),
            default=0.0,
        )

        # Aggregate top_decisions from all clusters
        all_decisions: list[dict[str, Any]] = []
        for c in clusters:
            for d in c.decisions:
                entry = dict(d)
                entry["topic_cluster_id"] = c.id
                all_decisions.append(entry)
        # Deduplicate by name, keep first occurrence
        seen_decisions: set[str] = set()
        top_decisions: list[dict[str, Any]] = []
        for d in all_decisions:
            name = d.get("name", "")
            if name and name not in seen_decisions:
                seen_decisions.add(name)
                top_decisions.append(d)

        # Aggregate top_people from all clusters (highest role wins)
        role_priority = {"decision_maker": 4, "expert": 3, "contributor": 2, "mentioned": 1}
        people_map: dict[str, dict[str, Any]] = {}  # name -> {role, topic_count, expertise_topics}
        for c in clusters:
            cluster_title = c.title or ", ".join(c.topic_tags[:3]) or "General"
            for p in c.people:
                name = p.get("name", "")
                if not name:
                    continue
                if name not in people_map:
                    people_map[name] = {
                        "name": name,
                        "role": p.get("role", "mentioned"),
                        "topic_count": 1,
                        "expertise_topics": [cluster_title],
                    }
                else:
                    existing = people_map[name]
                    existing["topic_count"] += 1
                    existing["expertise_topics"].append(cluster_title)
                    # Keep highest role
                    new_role = p.get("role", "mentioned")
                    if role_priority.get(new_role, 0) > role_priority.get(existing["role"], 0):
                        existing["role"] = new_role
        # Sort by role priority desc, then topic_count desc
        top_people = sorted(
            people_map.values(),
            key=lambda p: (role_priority.get(p["role"], 0), p["topic_count"]),
            reverse=True,
        )

        # Aggregate tech_stack (deduplicate by name)
        tech_map: dict[str, dict[str, Any]] = {}
        for c in clusters:
            for t in c.technologies:
                name = t.get("name", "")
                if not name:
                    continue
                if name not in tech_map:
                    tech_map[name] = {
                        "name": name,
                        "category": t.get("category", ""),
                        "champion": t.get("champion", ""),
                        "topic_count": 1,
                    }
                else:
                    tech_map[name]["topic_count"] += 1

        # Aggregate active_projects (deduplicate by name, keep most recent)
        project_map: dict[str, dict[str, Any]] = {}
        for c in clusters:
            for p in c.projects:
                name = p.get("name", "")
                if not name:
                    continue
                entry = dict(p)
                entry["topic_cluster_id"] = c.id
                project_map[name] = entry  # last one wins (most recent cluster)

        # Compute recent_activity_summary (last 7 days)
        recent_activity = await self._compute_recent_activity(channel_id, clusters)

        # Glossary terms from LLM
        raw_glossary = llm_result.get("glossary_terms", [])
        glossary_terms = [
            {
                "term": g.get("term", ""),
                "definition": g.get("definition", ""),
                "first_mentioned_by": g.get("first_mentioned_by", ""),
                "related_topics": g.get("related_topics", []),
            }
            for g in raw_glossary
            if isinstance(g, dict) and g.get("term")
        ]

        channel_summary = ChannelSummary(
            channel_id=channel_id,
            channel_name=channel_name,
            text=llm_result.get("summary_text", ""),
            description=llm_result.get("description", ""),
            themes=llm_result.get("themes", ""),
            momentum=llm_result.get("momentum", ""),
            team_dynamics=llm_result.get("team_dynamics", ""),
            cluster_count=len(clusters),
            fact_count=total_facts,
            # Existing enrichment fields
            key_decisions=ctx.graph_decisions,
            key_entities=ctx.graph_entities,
            key_topics=[
                {
                    "tags": c.topic_tags,
                    "title": c.title,
                    "member_count": c.member_count,
                    "status": c.status,
                }
                for c in sorted(clusters, key=lambda c: c.member_count, reverse=True)
            ],
            date_range_start=ctx.date_range_start,
            date_range_end=ctx.date_range_end,
            media_count=ctx.total_media,
            author_count=ctx.total_authors,
            worst_staleness=worst_staleness,
            # Wiki-ready enrichment fields
            top_decisions=top_decisions,
            top_people=list(top_people),
            tech_stack=list(tech_map.values()),
            active_projects=list(project_map.values()),
            glossary_terms=glossary_terms,
            recent_activity_summary=recent_activity,
            # topic_graph_edges computed in _apply_cross_cluster_links
        )
        await self._weaviate.upsert_channel_summary(channel_summary)

    async def _compute_recent_activity(
        self,
        channel_id: str,
        clusters: list[TopicCluster],
    ) -> dict[str, Any]:
        """Compute recent activity summary for the last 7 days."""
        from beever_atlas.models.api import MemoryFilters

        now = datetime.now(tz=UTC)
        seven_days_ago = now.timestamp() - (7 * 24 * 3600)
        seven_days_ago_str = str(seven_days_ago)

        activity: dict[str, Any] = {
            "facts_added_7d": 0,
            "decisions_added_7d": 0,
            "entities_added_7d": 0,
            "new_topics": [],
            "updated_topics": [],
            "highlights": [],
        }

        try:
            recent_page = await self._weaviate.list_facts(
                channel_id=channel_id,
                filters=MemoryFilters(since=seven_days_ago_str),
                page=1,
                limit=200,
            )
            recent_facts = recent_page.memories

            activity["facts_added_7d"] = len(recent_facts)
            activity["decisions_added_7d"] = sum(
                1 for f in recent_facts if f.fact_type == "decision"
            )

            # Top 3 high-importance highlights
            highlights = sorted(
                [f for f in recent_facts if f.importance in ("high", "critical")],
                key=lambda f: f.quality_score,
                reverse=True,
            )[:3]
            activity["highlights"] = [
                {
                    "memory_text": f.memory_text,
                    "author_name": f.author_name,
                    "fact_type": f.fact_type,
                    "message_ts": f.message_ts,
                }
                for f in highlights
            ]
        except Exception:
            logger.debug("Recent activity computation failed for %s", channel_id)

        # New/updated topics in last 7 days
        for c in clusters:
            title = c.title or ", ".join(c.topic_tags[:3]) or "General"
            if c.created_at and c.created_at.timestamp() > seven_days_ago:
                activity["new_topics"].append(title)
            elif c.updated_at and c.updated_at.timestamp() > seven_days_ago:
                activity["updated_topics"].append(title)

        return activity

    async def _call_topic_llm(self, prompt: str) -> dict[str, Any]:
        """Call LLM for topic summary with structured TopicSummaryResult output."""
        from beever_atlas.agents.consolidation.summarizer import create_topic_summarizer
        from beever_atlas.agents.runner import run_agent

        agent = create_topic_summarizer(instruction=prompt)
        state = await run_agent(agent)

        result = state.get("summary_result") or {}
        if isinstance(result, dict):
            return result
        # Fallback: treat as flat text
        return {"summary_text": str(result)} if result else {}

    async def _call_channel_llm(self, prompt: str) -> dict[str, Any]:
        """Call LLM for channel summary with structured ChannelSummaryResult output."""
        from beever_atlas.agents.consolidation.summarizer import create_channel_summarizer
        from beever_atlas.agents.runner import run_agent

        agent = create_channel_summarizer(instruction=prompt)
        state = await run_agent(agent)

        result = state.get("summary_result") or {}
        if isinstance(result, dict):
            return result
        # Fallback: treat as flat text
        return {"summary_text": str(result)} if result else {}

    async def _call_llm(self, prompt: str) -> str:
        """Legacy: Call LLM for summary generation via ADK summarizer agent."""
        from beever_atlas.agents.consolidation.summarizer import create_summarizer
        from beever_atlas.agents.runner import run_agent

        agent = create_summarizer(instruction=prompt)
        state = await run_agent(agent)

        raw = state.get("summary_result") or {}
        return raw.get("summary_text", "") if isinstance(raw, dict) else ""

    # ------------------------------------------------------------------
    # Health checks (UNCHANGED)
    # ------------------------------------------------------------------

    async def _health_check(
        self,
        channel_id: str,
        result: ConsolidationResult,
    ) -> None:
        """Split oversized clusters, merge similar ones, delete empty ones."""
        clusters = await self._weaviate.list_clusters(channel_id)

        # Delete empty clusters
        for cluster in clusters:
            if cluster.member_count == 0:
                await self._weaviate.delete_cluster(cluster.id)
                result.clusters_deleted += 1

        # Remove deleted from working list
        clusters = [c for c in clusters if c.member_count > 0]

        # Split oversized clusters
        to_split = [c for c in clusters if c.member_count > self._max_cluster_size]
        for cluster in to_split:
            try:
                await self._split_cluster(channel_id, cluster, result)
            except Exception as exc:
                logger.warning("Failed to split cluster %s: %s", cluster.id, exc)

        # Merge similar clusters
        if len(clusters) >= 2:
            await self._merge_similar_clusters(channel_id, clusters, result)

    async def _split_cluster(
        self,
        channel_id: str,
        cluster: TopicCluster,
        result: ConsolidationResult,
    ) -> None:
        """Split an oversized cluster into two by partitioning members."""
        members = await self._weaviate.get_cluster_members(
            cluster.id, limit=self._max_cluster_size + 50
        )

        if len(members) < 2:
            return

        # Simple split: divide by first/second half (approximation of k-means k=2)
        mid = len(members) // 2
        group_a = members[:mid]
        group_b = members[mid:]

        # Create new cluster for group B
        new_cluster = TopicCluster(
            channel_id=channel_id,
            member_ids=[m.id for m in group_b],
            member_count=len(group_b),
            topic_tags=list(cluster.topic_tags),
        )

        # Update original cluster to only have group A
        cluster.member_ids = [m.id for m in group_a]
        cluster.member_count = len(group_a)

        # Reassign facts
        updates = [(m.id, new_cluster.id) for m in group_b]
        await self._weaviate.batch_update_fact_clusters(updates)

        await self._weaviate.upsert_cluster(cluster)
        await self._weaviate.upsert_cluster(new_cluster)

        result.clusters_split += 1

    async def _merge_similar_clusters(
        self,
        channel_id: str,
        clusters: list[TopicCluster],
        result: ConsolidationResult,
    ) -> None:
        """Merge clusters whose centroids are very similar."""
        merged_ids: set[str] = set()

        for i, a in enumerate(clusters):
            if a.id in merged_ids:
                continue
            for b in clusters[i + 1 :]:
                if b.id in merged_ids:
                    continue
                if not a.centroid_vector or not b.centroid_vector:
                    continue
                sim = self._cosine_similarity(a.centroid_vector, b.centroid_vector)
                if sim > self._merge_threshold:
                    # Merge b into a
                    a.member_ids.extend(b.member_ids)
                    a.member_count = len(a.member_ids)

                    # Reassign b's facts to a
                    updates = [(mid, a.id) for mid in b.member_ids]
                    await self._weaviate.batch_update_fact_clusters(updates)

                    # Recompute centroid (average of both)
                    if a.centroid_vector and b.centroid_vector:
                        a.centroid_vector = self._compute_centroid(
                            [a.centroid_vector, b.centroid_vector]
                        )

                    await self._weaviate.upsert_cluster(a)
                    await self._weaviate.delete_cluster(b.id)
                    merged_ids.add(b.id)
                    result.clusters_merged += 1
