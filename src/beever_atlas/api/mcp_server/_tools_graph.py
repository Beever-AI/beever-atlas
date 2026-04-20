"""Graph tools: find_experts, search_relationships, trace_decision_history
(Phase 3, task 3.6)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastmcp import Context, FastMCP

from beever_atlas.api.mcp_server._helpers import (
    _get_principal_id,
    _validate_id,
)

logger = logging.getLogger(__name__)


def register_graph_tools(mcp: FastMCP) -> None:

    @mcp.tool(name="find_experts")
    async def find_experts(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        topic: Annotated[str, "Topic or keyword to find subject-matter experts for"],
        ctx: Context,
        limit: Annotated[int, "Maximum number of experts to return (1–20)"] = 5,
    ) -> dict:
        """Identify the most knowledgeable people about a topic in a channel.

        Scores channel members by graph-edge frequency for the topic and returns
        a ranked list. Each entry includes ``handle``, ``expertise_score``,
        ``fact_count``, and ``top_topics``.

        When to use: to answer "who knows the most about X in #channel?" or
        to find the right person to ask for a specific domain. Use
        ``search_channel_facts`` to find facts, not people.

        Returns: ``{experts: [...]}`` or ``{error: "channel_access_denied", ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        # Fix #8: clamp limit to documented 1–20 bound.
        limit = max(1, min(limit, 20))

        try:
            from beever_atlas.capabilities import graph as graph_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            experts = await graph_cap.find_experts(principal_id, channel_id, topic, limit=limit)
            return {"experts": experts}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "find_experts: failed principal=%s channel_id=%s topic=%s",
                principal_id,
                channel_id,
                topic,
            )
            return {"experts": []}

    @mcp.tool(name="search_relationships")
    async def search_relationships(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        entities: Annotated[
            list[str],
            "List of entity names to find relationships for",
        ],
        ctx: Context,
        hops: Annotated[int, "Number of graph hops to traverse (1–4)"] = 2,
    ) -> dict:
        """Traverse the knowledge graph to find relationships between entities.

        Returns a subgraph of nodes and edges connecting the requested entities.
        Each node has ``name`` and ``type``; each edge has ``source``, ``target``,
        ``type``, ``confidence``, and ``context``.

        When to use: to answer "how is X related to Y?" or to explore entity
        connections in a channel's knowledge graph. Use ``find_experts`` to
        find people, not relationships.

        Returns: ``{nodes, edges, text, entities_searched}`` or ``{error: ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        # Fix #8: clamp hops to documented 1–4 bound.
        hops = max(1, min(hops, 4))

        try:
            from beever_atlas.capabilities import graph as graph_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            result = await graph_cap.search_relationships(
                principal_id, channel_id, entities, hops=hops
            )
            if isinstance(result, dict):
                return result
            return {"edges": result, "channel_id": channel_id}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "search_relationships: failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"nodes": [], "edges": [], "channel_id": channel_id}

    @mcp.tool(name="trace_decision_history")
    async def trace_decision_history(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        topic: Annotated[
            str,
            "Topic or decision to trace (e.g. 'database choice', 'API versioning')",
        ],
        ctx: Context,
    ) -> dict:
        """Trace the history of decisions made about a topic in a channel.

        Follows ``SUPERSEDES`` edges in the knowledge graph to reconstruct
        the decision timeline. Each item includes ``entity``, ``superseded_by``,
        ``relationship``, ``confidence``, ``context``, and ``position``.

        When to use: to answer "how did the team arrive at the current approach
        for X?" or "what earlier decisions were overridden?" Use
        ``search_channel_facts`` to find facts about the current state without
        historical context.

        Returns: ``{decisions: [...]}`` or ``{error: "channel_access_denied", ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import graph as graph_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            decisions = await graph_cap.trace_decision_history(principal_id, channel_id, topic)
            if isinstance(decisions, list):
                return {"decisions": decisions}
            return decisions  # type: ignore[return-value]
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "trace_decision_history: failed principal=%s channel_id=%s topic=%s",
                principal_id,
                channel_id,
                topic,
            )
            return {"decisions": []}
