"""Gathers all data needed for wiki generation from Weaviate and Neo4j."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


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
            raise ValueError("Channel has no consolidated data")

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
