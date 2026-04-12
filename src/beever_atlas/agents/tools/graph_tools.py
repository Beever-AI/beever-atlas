"""Graph memory tools: entity relationships, decision history, expert ranking."""

from __future__ import annotations

import logging
from typing import Any

from beever_atlas.agents.tools._citation_decorator import cite_tool_output

logger = logging.getLogger(__name__)


@cite_tool_output(kind="graph_relationship")
async def search_relationships(
    channel_id: str,
    entities: list[str],
    hops: int = 2,
) -> dict:
    """Traverse Neo4j graph for relationships between named entities.

    Cost: ~$0.005. Target latency: ~500ms.

    Args:
        channel_id: Scope traversal context (used for logging/filtering).
        entities: List of entity names to resolve and traverse from.
        hops: Number of graph hops (default 2).

    Returns:
        Dict with nodes, edges, and entities_searched.
    """
    try:
        from beever_atlas.stores import get_stores

        graph = get_stores().graph
        all_nodes: list[dict] = []
        all_edges: list[dict] = []
        seen_nodes: set[str] = set()
        seen_edges: set[str] = set()

        for entity_name in entities:
            # fuzzy_match_entities takes one name at a time → list[tuple[str, float]]
            matches = await graph.fuzzy_match_entities(entity_name, threshold=0.6)
            if not matches:
                continue
            canonical_name, _score = matches[0]

            # Resolve to a graph entity
            entity = await graph.find_entity_by_name(canonical_name)
            if entity is None:
                continue

            # entity_id is the Neo4j element ID
            entity_id = entity.id if hasattr(entity, "id") and entity.id else entity.name

            # Get neighborhood subgraph
            subgraph = await graph.get_neighbors(entity_id, hops=hops)

            for node in subgraph.nodes:
                if node.name not in seen_nodes:
                    seen_nodes.add(node.name)
                    _n: Any = node
                    all_nodes.append({
                        "name": _n.name,
                        "type": getattr(_n, "entity_type", None) or getattr(_n, "type", None),
                    })

            for edge in subgraph.edges:
                edge_key = f"{edge.source}-{edge.type}-{edge.target}"
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    all_edges.append({
                        "source": edge.source,
                        "target": edge.target,
                        "type": edge.type,
                        "confidence": edge.confidence,
                        "context": getattr(edge, "context", ""),
                    })

        if not all_nodes and not all_edges:
            return [{"_empty": True, "entity": entities[0] if entities else "", "reason": "no_edges"}]

        # Build a human-readable summary for the citation excerpt.
        edge_summary = "; ".join(
            f"{e['source']} -{e['type']}-> {e['target']}" for e in all_edges[:5]
        ) or f"No relationships found for {', '.join(entities)}"

        return {
            "entities_searched": entities,
            "nodes": all_nodes[:50],
            "edges": all_edges[:100],
            # Citation-decorator fields (single-source dict path):
            "text": edge_summary,
            "subject_id": entities[0] if entities else "",
            "predicate": "RELATED_TO",
            "object_id": channel_id,
            "channel_id": channel_id,
        }
    except Exception:
        logger.exception("search_relationships failed for entities=%s", entities)
        return {"entities_searched": entities, "nodes": [], "edges": []}


@cite_tool_output(kind="decision_record")
async def trace_decision_history(channel_id: str, topic: str) -> list[dict]:
    """Trace temporal evolution of decisions about a topic via Neo4j SUPERSEDES chain.

    Cost: ~$0.005. Target latency: ~500ms.

    Args:
        channel_id: Scope context (for logging).
        topic: Topic or entity name to trace.

    Returns:
        List of decision nodes and SUPERSEDES relationships, ordered by traversal.
    """
    try:
        from beever_atlas.stores import get_stores

        graph = get_stores().graph

        # Fuzzy-match the topic to a canonical entity name
        matches = await graph.fuzzy_match_entities(topic, threshold=0.6)
        if not matches:
            return []
        canonical_name, _ = matches[0]

        entity = await graph.find_entity_by_name(canonical_name)
        if entity is None:
            return []

        entity_id = entity.id if hasattr(entity, "id") and entity.id else entity.name

        # 3-hop traversal to capture the full SUPERSEDES chain
        subgraph = await graph.get_neighbors(entity_id, hops=3)

        # Extract SUPERSEDES edges and build timeline
        timeline: list[dict] = []
        supersedes_edges = [e for e in subgraph.edges if e.type == "SUPERSEDES"]

        for edge in supersedes_edges:
            ctx_text = getattr(edge, "context", "") or f"{edge.target} superseded by {edge.source}"
            timeline.append({
                "entity": edge.target,
                "superseded_by": edge.source,
                "relationship": "SUPERSEDES",
                "confidence": edge.confidence,
                "context": ctx_text,
                # Citation-decorator fields:
                "text": ctx_text,
                "decision_id": f"{channel_id}:{edge.target}:{edge.source}",
                "channel_id": channel_id,
                "topic": topic,
            })

        # If no SUPERSEDES edges, return the empty sentinel
        if not timeline:
            return [{"_empty": True, "entity": topic, "reason": "no_edges"}]

        return timeline
    except Exception:
        logger.exception("trace_decision_history failed for topic=%s", topic)
        return []


@cite_tool_output(kind="graph_relationship")
async def find_experts(channel_id: str, topic: str, limit: int = 5) -> list[dict]:
    """Find top contributors for a topic by Neo4j expertise ranking.

    Cost: ~$0.005. Target latency: ~500ms.

    Args:
        channel_id: Scope to this channel.
        topic: Topic to rank expertise for.
        limit: Max people to return (default 5).

    Returns:
        List of {handle, expertise_score, fact_count} ordered by expertise_score desc.
    """
    try:
        from beever_atlas.stores import get_stores

        graph = get_stores().graph

        # List all Person entities — then score each by topic relevance
        rels = await graph.list_relationships(channel_id=channel_id, limit=500)

        topic_lower = topic.lower()
        person_scores: dict[str, dict] = {}

        for rel in rels:
            # Score Person nodes connected to topic-related nodes
            for endpoint in (rel.source, rel.target):
                if endpoint and topic_lower in endpoint.lower():
                    # The other endpoint may be a Person
                    other = rel.target if endpoint == rel.source else rel.source
                    if other:
                        if other not in person_scores:
                            person_scores[other] = {"handle": other, "expertise_score": 0, "fact_count": 0}
                        person_scores[other]["expertise_score"] += 1
                        person_scores[other]["fact_count"] += 1

        scored = sorted(person_scores.values(), key=lambda x: x["expertise_score"], reverse=True)
        results = scored[:limit]
        if not results:
            return [{"_empty": True, "entity": topic, "reason": "no_edges"}]
        # Inject citation-decorator fields per item (non-destructive to callers).
        for item in results:
            item.setdefault("text", f"{item['handle']} has {item['fact_count']} facts about {topic}")
            item.setdefault("subject_id", item["handle"])
            item.setdefault("predicate", "EXPERT_IN")
            item.setdefault("object_id", topic)
            item.setdefault("channel_id", channel_id)
        return results
    except Exception:
        logger.exception("find_experts failed for topic=%s", topic)
        return []
