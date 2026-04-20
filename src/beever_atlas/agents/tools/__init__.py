"""ADK FunctionTool stubs for all store operations.

Each function has the correct signature and docstring for its target store method.
In M1, all raise NotImplementedError — implementations arrive in M3/M4.
"""

from typing import Literal, TypedDict

# --- QA Agent tools (implemented) ---
from beever_atlas.agents.tools.wiki_tools import get_wiki_page, get_topic_overview
from beever_atlas.agents.tools.memory_tools import (
    search_qa_history,
    search_channel_facts,
    search_media_references,
    get_recent_activity,
)
from beever_atlas.agents.tools.graph_tools import (
    search_relationships,
    trace_decision_history,
    find_experts,
)
from beever_atlas.agents.tools.external_tools import search_external_knowledge

# All 10 QA tools in priority order (wiki → overview → history → facts → media → activity → graph → decisions → experts → external)
QA_TOOLS = [
    get_wiki_page,
    get_topic_overview,
    search_qa_history,
    search_channel_facts,
    search_media_references,
    get_recent_activity,
    search_relationships,
    trace_decision_history,
    find_experts,
    search_external_knowledge,
]


ToolDescriptor = TypedDict(
    "ToolDescriptor",
    {
        "name": str,
        "category": Literal["wiki", "memory", "graph", "external", "orchestration"],
        "description": str,
    },
)


# UI-facing registry: one entry per tool in QA_TOOLS (order preserved).
# Descriptions are short (one-line) paraphrases of the tool docstrings —
# suitable for rendering in the /api/ask/tools panel.
QA_TOOL_DESCRIPTORS: list[ToolDescriptor] = [
    {
        "name": "get_wiki_page",
        "category": "wiki",
        "description": "Retrieve a pre-compiled wiki page from the channel cache.",
    },
    {
        "name": "get_topic_overview",
        "category": "wiki",
        "description": "Retrieve a channel-level summary or a topic cluster summary.",
    },
    {
        "name": "search_qa_history",
        "category": "memory",
        "description": "Search past Q&A pairs for similar questions in this channel.",
    },
    {
        "name": "search_channel_facts",
        "category": "memory",
        "description": "BM25 keyword search over atomic facts in the channel.",
    },
    {
        "name": "search_media_references",
        "category": "memory",
        "description": "Search for images, PDFs, and links shared in the channel.",
    },
    {
        "name": "get_recent_activity",
        "category": "memory",
        "description": "Return recent facts from the channel, optionally filtered by topic.",
    },
    {
        "name": "search_relationships",
        "category": "graph",
        "description": "Traverse the knowledge graph for relationships between entities.",
    },
    {
        "name": "trace_decision_history",
        "category": "graph",
        "description": "Trace temporal evolution of decisions about a topic.",
    },
    {
        "name": "find_experts",
        "category": "graph",
        "description": "Find top contributors for a topic by expertise ranking.",
    },
    {
        "name": "search_external_knowledge",
        "category": "external",
        "description": "Search external web knowledge via the Tavily API.",
    },
    # Orchestration tools — available in deep mode only. Surfaced here so the
    # Tools panel can disable them per request via AskRequest.disabled_tools.
    {
        "name": "list_connections_tool",
        "category": "orchestration",
        "description": "List platform connections available to the caller.",
    },
    {
        "name": "list_channels_tool",
        "category": "orchestration",
        "description": "List channels for a given connection.",
    },
    {
        "name": "trigger_sync_tool",
        "category": "orchestration",
        "description": "Kick off a background sync for a channel (deep mode only; filtered from untrusted contexts).",
    },
    {
        "name": "refresh_wiki_tool",
        "category": "orchestration",
        "description": "Rebuild the wiki for a channel (deep mode only; 5-min cooldown; filtered from untrusted contexts).",
    },
    {
        "name": "get_job_status_tool",
        "category": "orchestration",
        "description": "Poll a sync or wiki-refresh job by id.",
    },
]


# --- Semantic Memory (Weaviate) tools ---


def search_weaviate_hybrid(
    query: str,
    channel_id: str,
    tier: str = "all",
    limit: int = 15,
    alpha: float | None = None,
) -> list[dict]:
    """Hybrid BM25+vector search across Weaviate 3-tier memory.

    Args:
        query: Search query text.
        channel_id: Target channel ID.
        tier: Which tier to search — "all", "summary", "topic", or "atomic".
        limit: Maximum results to return.
        alpha: BM25/vector balance (None = adaptive).

    Returns:
        List of memory objects with scores.
    """
    raise NotImplementedError("Weaviate store not yet implemented (M3)")


async def get_tier0_summary(channel_id: str) -> dict | None:
    """Get the Tier 0 channel summary from Weaviate.

    Args:
        channel_id: Target channel ID.

    Returns:
        Summary dict with text, cluster_count, fact_count, or None if not found.
    """
    from beever_atlas.stores import get_stores

    store = get_stores().weaviate
    summary = await store.get_channel_summary(channel_id)
    if summary is None:
        return None
    return {
        "text": summary.text,
        "cluster_count": summary.cluster_count,
        "fact_count": summary.fact_count,
    }


async def get_tier1_clusters(channel_id: str) -> list[dict]:
    """Get all Tier 1 topic clusters for a channel from Weaviate.

    Args:
        channel_id: Target channel ID.

    Returns:
        List of cluster dicts with summary, topic_tags, member_count.
    """
    from beever_atlas.stores import get_stores

    store = get_stores().weaviate
    clusters = await store.list_clusters(channel_id)
    return [
        {
            "id": cluster.id,
            "summary": cluster.summary,
            "topic_tags": cluster.topic_tags,
            "member_count": cluster.member_count,
        }
        for cluster in clusters
    ]


# --- Graph Memory (Neo4j) tools ---


def traverse_neo4j(
    entity_name: str,
    channel_id: str | None = None,
    depth: int = 2,
) -> dict:
    """Traverse the Neo4j knowledge graph from a named entity.

    Args:
        entity_name: Starting entity name.
        channel_id: Optional channel scope.
        depth: Traversal depth (default 2).

    Returns:
        Dict with nodes and relationships encountered.
    """
    raise NotImplementedError("Neo4j store not yet implemented (M4)")


def temporal_chain(
    entity_name: str,
    channel_id: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Get temporal evolution chain for an entity from Neo4j.

    Args:
        entity_name: Entity to trace over time.
        channel_id: Optional channel scope.
        since: ISO timestamp to start from.

    Returns:
        Chronological list of events/changes for the entity.
    """
    raise NotImplementedError("Neo4j store not yet implemented (M4)")


def comprehensive_traverse(
    entity_names: list[str],
    channel_id: str | None = None,
    depth: int = 2,
) -> dict:
    """Comprehensive multi-entity traversal with bidirectional expansion.

    Args:
        entity_names: List of entity names to start from.
        channel_id: Optional channel scope.
        depth: Traversal depth.

    Returns:
        Dict with all nodes, relationships, and paths found.
    """
    raise NotImplementedError("Neo4j store not yet implemented (M4)")


def get_episodic_weaviate_ids(entity_name: str, channel_id: str | None = None) -> list[str]:
    """Get Weaviate fact IDs linked to a Neo4j entity via episodic edges.

    Args:
        entity_name: Entity name to look up.
        channel_id: Optional channel scope.

    Returns:
        List of Weaviate object UUIDs linked to this entity.
    """
    raise NotImplementedError("Neo4j store not yet implemented (M4)")


# --- External Search tools ---


def search_tavily(query: str, max_results: int = 5) -> list[dict]:
    """Search external web knowledge via Tavily API.

    Args:
        query: Search query.
        max_results: Maximum results.

    Returns:
        List of search result dicts with title, url, content.
    """
    raise NotImplementedError("Tavily integration not yet implemented (M6)")


# --- Write tools (used by persister_agent) ---


def upsert_fact(
    channel_id: str,
    memory: str,
    quality_score: float,
    topic_tags: list[str],
    entity_tags: list[str],
    importance: str,
    user_name: str,
    timestamp: str,
    permalink: str,
    embedding: list[float] | None = None,
    cluster_id: str | None = None,
) -> str:
    """Upsert an atomic fact into Weaviate with deterministic UUID.

    Args:
        channel_id: Source channel.
        memory: Fact text.
        quality_score: Quality gate score (0-10).
        topic_tags: Topic classification tags.
        entity_tags: Entity names mentioned.
        importance: Importance level (low/medium/high/critical).
        user_name: Author attribution.
        timestamp: Original message timestamp.
        permalink: Platform message URL.
        embedding: Pre-computed Jina embedding vector.
        cluster_id: Tier 1 cluster assignment.

    Returns:
        Weaviate object UUID.
    """
    raise NotImplementedError("Weaviate store not yet implemented (M3)")


def upsert_entity(
    name: str,
    entity_type: str,
    channel_id: str,
    properties: dict | None = None,
) -> str:
    """Upsert an entity node into Neo4j (MERGE semantics).

    Args:
        name: Canonical entity name.
        entity_type: Entity type (Person, Decision, Project, Technology, Team, etc.).
        channel_id: Source channel.
        properties: Type-specific properties dict.

    Returns:
        Neo4j node ID.
    """
    raise NotImplementedError("Neo4j store not yet implemented (M4)")


def create_episodic_link(
    entity_name: str,
    weaviate_id: str,
    channel_id: str,
    timestamp: str,
) -> None:
    """Create a MENTIONED_IN episodic link from Neo4j entity to Weaviate fact.

    Args:
        entity_name: Entity name in Neo4j.
        weaviate_id: Weaviate object UUID of the source fact.
        channel_id: Source channel.
        timestamp: When the mention occurred.
    """
    raise NotImplementedError("Neo4j store not yet implemented (M4)")


# Collect all tool functions for easy access
ALL_TOOLS = [
    search_weaviate_hybrid,
    get_tier0_summary,
    get_tier1_clusters,
    traverse_neo4j,
    temporal_chain,
    comprehensive_traverse,
    get_episodic_weaviate_ids,
    search_tavily,
    upsert_fact,
    upsert_entity,
    create_episodic_link,
]
