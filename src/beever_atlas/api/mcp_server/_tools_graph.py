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
        channel_id: Annotated[
            str,
            "Required. The channel id to search within, obtained from "
            "list_channels (e.g. 'ch-eng'). Not a human channel name.",
        ],
        topic: Annotated[
            str,
            "Required. Topic or keyword to rank experts on, e.g. 'kubernetes', "
            "'billing', 'auth'. Matched against knowledge-graph edges, so use a "
            "concept the channel actually discusses.",
        ],
        ctx: Context,
        limit: Annotated[
            int,
            "Maximum number of experts to return. Range 1–20, default 5. Values "
            "outside the range are silently clamped (e.g. 50 -> 20, 0 -> 1).",
        ] = 5,
    ) -> dict:
        """Rank the PEOPLE most knowledgeable about a topic in one channel.

        Call this to answer "who should I ask about X in #channel?" or to
        route a question to the right person. This is the only tool that
        ranks PEOPLE; use ``search_channel_facts`` / ``find_facts`` to find
        FACTS, and ``find_experts`` only when you specifically need a human.

        Prerequisite: a ``channel_id`` from ``list_channels``. Do NOT call
        with a channel display name.

        Returns (instant, read-only, no side effects):
        ``{"experts": [...]}`` — a list ranked by ``expertise_score``
        descending. Each entry has ``handle`` (e.g. '@dana'),
        ``expertise_score`` (relative float, higher = more authoritative;
        not a fixed 0–1 scale, only meaningful for ranking within this
        result), ``fact_count`` (number of contributing facts), and
        ``top_topics`` (list of related topics that person engages with).
        An empty list means no graph signal for that topic — not an error.

        Error modes: ``{"error": "authentication_missing"}`` if the caller
        is unauthenticated; ``{"error": "channel_access_denied",
        "channel_id": ...}`` if the principal cannot read the channel;
        ``{"error": "invalid_parameter", ...}`` for a malformed
        ``channel_id``. Other backend failures degrade gracefully to
        ``{"experts": []}``.
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
        channel_id: Annotated[
            str,
            "Required. The channel id to search within, obtained from "
            "list_channels (e.g. 'ch-eng'). Not a human channel name.",
        ],
        entities: Annotated[
            list[str],
            "Required. One or more entity NAMES to connect, e.g. "
            "['Postgres', 'billing-service'] or ['Dana']. These are knowledge-"
            "graph node names (people, systems, concepts), not channel ids. "
            "Provide at least one; provide two+ to find paths between them.",
        ],
        ctx: Context,
        hops: Annotated[
            int,
            "How many graph edges to traverse out from the entities. Range 1–4, "
            "default 2. Larger values return wider but noisier subgraphs and are "
            "slower. Out-of-range values are silently clamped (e.g. 9 -> 4).",
        ] = 2,
    ) -> dict:
        """Find how named ENTITIES connect in a channel's knowledge graph.

        Call this to answer "how is X related to Y?" or "what touches the
        billing service?" by returning the subgraph of nodes and edges
        around the given entities. This explores the KNOWLEDGE graph of
        entities/relationships — distinct from ``get_wiki_graph``, which
        returns the wiki PAGE-LINK graph (which wiki pages reference which).
        Use ``find_experts`` to rank people and ``trace_decision_history``
        to follow decision supersession.

        Prerequisite: a ``channel_id`` from ``list_channels`` and at least
        one entity name. Names should match how the channel refers to the
        entity; unknown names simply yield an empty subgraph.

        Returns (instant for small hop counts, read-only, no side effects):
        ``{"nodes": [...], "edges": [...], "text": str,
        "entities_searched": [...]}``. Each node has ``name`` and ``type``
        (e.g. 'person', 'system', 'concept'); each edge has ``source``,
        ``target``, ``type`` (relationship label), ``confidence`` (0–1,
        extraction confidence), and ``context`` (snippet explaining the
        edge). ``text`` is a human-readable summary. Empty ``nodes``/
        ``edges`` means no connections were found — not an error.

        Error modes: ``{"error": "authentication_missing"}`` if
        unauthenticated; ``{"error": "channel_access_denied",
        "channel_id": ...}`` if the channel is not readable; ``{"error":
        "invalid_parameter", ...}`` for a malformed ``channel_id``. Other
        backend failures degrade to ``{"nodes": [], "edges": [],
        "channel_id": ...}``.
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
        channel_id: Annotated[
            str,
            "Required. The channel id to trace within, obtained from "
            "list_channels (e.g. 'ch-eng'). Not a human channel name.",
        ],
        topic: Annotated[
            str,
            "Required. The decision area to trace, e.g. 'database choice', "
            "'API versioning', 'auth provider'. Matched against decision "
            "entities in the knowledge graph; use the subject of the decision, "
            "not a yes/no question.",
        ],
        ctx: Context,
    ) -> dict:
        """Reconstruct how a decision EVOLVED over time in a channel.

        Call this to answer "how did the team arrive at the current approach
        for X?" or "what earlier choices were overridden?" It walks
        ``SUPERSEDES`` edges in the knowledge graph to build an ordered
        timeline of superseded → current decisions. Distinct from
        ``find_decisions`` (which lists current decision facts with no
        history) and ``search_channel_facts`` (current state only, no
        chronology); use this tool specifically when you need the
        chronological "why we changed" trail.

        Prerequisite: a ``channel_id`` from ``list_channels``. Best results
        on mature channels where decisions have been revised; new channels
        often have no supersession chain yet (empty result, not an error).

        Returns (instant, read-only, no side effects):
        ``{"decisions": [...]}`` ordered oldest → newest. Each item has
        ``entity`` (the decision that was made), ``superseded_by`` (the
        decision that replaced it, or empty for the current one),
        ``relationship`` (edge label, typically 'SUPERSEDES'),
        ``confidence`` (0–1 extraction confidence), ``context`` (snippet
        explaining the change), and ``position`` (0-based index in the
        timeline). An empty list means no recorded supersession chain.

        Error modes: ``{"error": "authentication_missing"}`` if
        unauthenticated; ``{"error": "channel_access_denied",
        "channel_id": ...}`` if the channel is not readable; ``{"error":
        "invalid_parameter", ...}`` for a malformed ``channel_id``. Other
        backend failures degrade to ``{"decisions": []}``.
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
