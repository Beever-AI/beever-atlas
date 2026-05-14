"""Gathers all data needed for wiki generation from Weaviate and Neo4j."""

from __future__ import annotations

import asyncio
import logging

from beever_atlas.capabilities.errors import WikiNotReadyError

logger = logging.getLogger(__name__)

# How long to wait for consolidation to catch up before giving up.
# Wait up to this many seconds for ``channel_summary`` to appear before
# raising WikiNotReadyError. The Tier-0 channel summary is produced by the
# summarize_settled flow which fires AFTER consolidation completes — it
# involves 4+ TopicSummaryResult LLM calls (~3-5s each) plus a final
# ChannelSummaryResult call. On a fresh channel with 4 topics that's
# typically 20-25s of LLM time. The previous 15s timeout fired BEFORE the
# summarize chain finished, surfacing
# ``WikiNotReadyError: Consolidation is still in progress`` to the
# AutoOverviewSubscriber on every fresh sync. 60s comfortably covers the
# 4-topic case and leaves headroom for slower LLMs / rate-limited paths.
# The outer ``AutoOverviewSubscriber._GENERATION_TIMEOUT_SECONDS`` (600s)
# still bounds the total wait, so a genuinely stuck consolidation still
# fails the wiki build, just with more useful slack on the happy path.
_CONSOLIDATION_WAIT_SECONDS = 60
_CONSOLIDATION_POLL_INTERVAL = 0.5
_CONSOLIDATION_POLL_STEPS = int(_CONSOLIDATION_WAIT_SECONDS / _CONSOLIDATION_POLL_INTERVAL)  # 30


class WikiDataGatherer:
    """Collects channel data from Weaviate and Neo4j stores in parallel."""

    def __init__(self, weaviate_store, graph_store) -> None:
        self._weaviate = weaviate_store
        self._graph = graph_store

    async def gather(self, channel_id: str) -> dict:
        """Gather all data for wiki generation. Returns a structured dict."""
        (
            channel_summary,
            clusters,
            recent_facts,
            media_facts,
            total_facts,
            persons,
            decisions,
            technologies,
            projects,
            total_entities,
        ) = await asyncio.gather(
            self._weaviate.get_channel_summary(channel_id),
            self._weaviate.list_clusters(channel_id),
            self._weaviate.fetch_recent_facts(channel_id, days=7),
            self._weaviate.fetch_media_facts(channel_id),
            self._weaviate.count_facts(channel_id),
            self._graph.list_person_entities_with_edges(channel_id),
            self._graph.get_decisions_with_chains(channel_id),
            self._graph.list_technology_entities(channel_id),
            self._graph.list_project_entities(channel_id),
            self._graph.count_entities(channel_id),
        )

        if channel_summary is None:
            # channel_summary is the Tier-0 consolidated row. If it is absent,
            # consolidation may still be running (e.g. recovering from a 503).
            # Wait up to 15 s for it to appear before raising.
            for _ in range(_CONSOLIDATION_POLL_STEPS):
                await asyncio.sleep(_CONSOLIDATION_POLL_INTERVAL)
                # Re-check clusters and channel summary together so we can also
                # detect "never consolidated" (zero clusters) quickly.
                _clusters_check = await self._weaviate.list_clusters(channel_id)
                if not _clusters_check:
                    # No clusters at all — channel has never been consolidated.
                    raise WikiNotReadyError(
                        "Channel has not been consolidated yet. Run a sync first."
                    )
                channel_summary = await self._weaviate.get_channel_summary(channel_id)
                if channel_summary is not None:
                    clusters = _clusters_check
                    break
            else:
                raise WikiNotReadyError(
                    "Consolidation is still in progress. Retry in a few seconds."
                )

        # Fetch cluster members in parallel
        cluster_facts: dict = {}
        if clusters:
            member_lists = await asyncio.gather(
                *[self._weaviate.fetch_all_cluster_members(channel_id, c.id) for c in clusters]
            )
            cluster_facts = {c.id: members for c, members in zip(clusters, member_lists)}

        return {
            "channel_summary": channel_summary,
            "clusters": clusters,
            "cluster_facts": cluster_facts,
            "persons": persons,
            "decisions": decisions,
            "technologies": technologies,
            "projects": projects,
            "recent_facts": recent_facts,
            "media_facts": media_facts,
            "total_facts": total_facts,
            "total_entities": total_entities,
        }
