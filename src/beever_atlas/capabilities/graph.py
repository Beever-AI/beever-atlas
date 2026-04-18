"""Graph-memory capabilities: entity relationships, expert ranking, decision history.

Framework-neutral implementations for openspec change ``atlas-mcp-server``
Phase 1 (task 1.4 partial). Logic extracted from
``agents/tools/graph_tools.py``; the ADK wrappers in that module are
preserved as thin shims that delegate to the ``_impl`` helpers here.

Each public capability function:

* Takes ``principal_id: str`` as its first argument.
* Calls :func:`beever_atlas.infra.channel_access.assert_channel_access`
  as its first line (raises :class:`~capabilities.errors.ChannelAccessDenied`).
* Returns the same structured result shape as the existing ADK tool.
"""

from __future__ import annotations

import logging
from typing import Any

from beever_atlas.capabilities.errors import ChannelAccessDenied
from beever_atlas.infra.channel_access import assert_channel_access

logger = logging.getLogger(__name__)

_MAX_NODES = 20
_MAX_EDGES = 50


# ---------------------------------------------------------------------------
# _impl helpers (no access check — called by ADK wrappers and public fns)
# ---------------------------------------------------------------------------


async def _search_relationships_impl(
    channel_id: str,
    entities: list[str],
    hops: int = 2,
) -> dict | list[dict]:
    """Core implementation of entity-relationship search (no access check)."""
    try:
        from beever_atlas.stores import get_stores

        graph = get_stores().graph
        all_nodes: list[dict] = []
        all_edges: list[dict] = []
        seen_nodes: set[str] = set()
        seen_edges: set[str] = set()

        for entity_name in entities:
            matches = await graph.fuzzy_match_entities(entity_name, threshold=0.6)
            if not matches:
                continue
            canonical_name, _score = matches[0]

            entity = await graph.find_entity_by_name(canonical_name)
            if entity is None:
                continue

            entity_id = entity.id if hasattr(entity, "id") and entity.id else entity.name

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
                        "context": getattr(edge, "context", "") or "",
                    })

        if not all_nodes and not all_edges:
            return [{"_empty": True, "entity": entities[0] if entities else "", "reason": "no_edges"}]

        if len(all_edges) > _MAX_EDGES:
            all_edges.sort(key=lambda e: e.get("confidence", 0.0), reverse=True)
            all_edges = all_edges[:_MAX_EDGES]
        if len(all_nodes) > _MAX_NODES:
            all_nodes = all_nodes[:_MAX_NODES]

        edge_summary = "; ".join(
            f"{e['source']} -{e['type']}-> {e['target']}" for e in all_edges[:5]
        ) or f"No relationships found for {', '.join(entities)}"

        return {
            "entities_searched": entities,
            "nodes": all_nodes,
            "edges": all_edges,
            "text": edge_summary,
            "subject_id": entities[0] if entities else "",
            "predicate": "RELATED_TO",
            "object_id": channel_id,
            "channel_id": channel_id,
        }
    except Exception:
        logger.exception("search_relationships failed for entities=%s", entities)
        return {
            "entities_searched": entities,
            "nodes": [],
            "edges": [],
            "text": "",
            "subject_id": entities[0] if entities else "",
            "predicate": "RELATED_TO",
            "object_id": channel_id,
            "channel_id": channel_id,
        }


async def _trace_decision_history_impl(channel_id: str, topic: str) -> list:
    """Core implementation of decision-history trace (no access check)."""
    try:
        from beever_atlas.stores import get_stores

        graph = get_stores().graph

        matches = await graph.fuzzy_match_entities(topic, threshold=0.6)
        if not matches:
            return []
        canonical_name, _ = matches[0]

        entity = await graph.find_entity_by_name(canonical_name)
        if entity is None:
            return []

        entity_id = entity.id if hasattr(entity, "id") and entity.id else entity.name

        subgraph = await graph.get_neighbors(entity_id, hops=3)

        name_to_id: dict[str, str] = {}
        for n in subgraph.nodes:
            nid = getattr(n, "id", None) or getattr(n, "name", None)
            if n.name and nid:
                name_to_id[n.name] = nid

        timeline: list[dict] = []
        supersedes_edges = [e for e in subgraph.edges if e.type == "SUPERSEDES"]

        for position, edge in enumerate(supersedes_edges):
            ctx_text = getattr(edge, "context", "") or f"{edge.target} superseded by {edge.source}"
            timeline.append({
                "entity": edge.target,
                "superseded_by": edge.source,
                "superseded_by_id": name_to_id.get(edge.source),
                "relationship": "SUPERSEDES",
                "confidence": edge.confidence,
                "context": ctx_text,
                "position": position,
                "text": ctx_text,
                "decision_id": f"{channel_id}:{edge.target}:{edge.source}",
                "channel_id": channel_id,
                "topic": topic,
            })

        if not timeline:
            return [{"_empty": True, "entity": topic, "reason": "no_edges"}]

        return timeline
    except (ConnectionError, OSError) as e:
        logger.error(
            "trace_decision_history graph unavailable for topic=%s exc=%r",
            topic,
            e,
        )
        return {"result": [], "error": "graph_unavailable"}  # type: ignore[return-value]
    except Exception as e:
        logger.exception("trace_decision_history failed for topic=%s exc=%r", topic, e)
        return []


async def _find_experts_impl(
    channel_id: str,
    topic: str,
    limit: int = 5,
) -> list:
    """Core implementation of expert ranking (no access check)."""
    try:
        from beever_atlas.stores import get_stores

        graph = get_stores().graph

        rels = await graph.list_relationships(channel_id=channel_id, limit=500)

        topic_lower = topic.lower()
        person_scores: dict[str, dict[str, Any]] = {}

        for rel in rels:
            for endpoint in (rel.source, rel.target):
                if endpoint and topic_lower in endpoint.lower():
                    other = rel.target if endpoint == rel.source else rel.source
                    if other:
                        bucket = person_scores.setdefault(
                            other,
                            {
                                "handle": other,
                                "expertise_score": 0,
                                "fact_count": 0,
                                "_topics": set(),
                            },
                        )
                        bucket["expertise_score"] += 1
                        bucket["fact_count"] += 1
                        bucket["_topics"].add(endpoint)

        scored = sorted(
            person_scores.values(),
            key=lambda x: x["expertise_score"],
            reverse=True,
        )
        results = scored[:limit]
        if not results:
            return [{"_empty": True, "entity": topic, "reason": "no_edges"}]

        out: list[dict] = []
        for item in results:
            topics_list = sorted(item.pop("_topics", set()))[:5]
            out.append({
                "handle": item["handle"],
                "expertise_score": float(item["expertise_score"]),
                "fact_count": int(item["fact_count"]),
                "top_topics": topics_list,
                "recent_activity_days": 0,
                "text": f"{item['handle']} has {item['fact_count']} facts about {topic}",
                "subject_id": item["handle"],
                "predicate": "EXPERT_IN",
                "object_id": topic,
                "channel_id": channel_id,
            })
        return out
    except Exception:
        logger.exception("find_experts failed for topic=%s", topic)
        return []


# ---------------------------------------------------------------------------
# Public capability functions (with access check)
# ---------------------------------------------------------------------------


async def search_relationships(
    principal_id: str,
    channel_id: str,
    entities: list[str],
    hops: int = 2,
) -> dict | list[dict]:
    """Traverse the knowledge graph for entity relationships; enforces channel access."""
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc
    return await _search_relationships_impl(channel_id, entities, hops=hops)


async def trace_decision_history(
    principal_id: str,
    channel_id: str,
    topic: str,
) -> list:
    """Trace decision history for a topic; enforces channel access."""
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc
    return await _trace_decision_history_impl(channel_id, topic)


async def find_experts(
    principal_id: str,
    channel_id: str,
    topic: str,
    limit: int = 5,
) -> list:
    """Rank channel members by expertise on a topic; enforces channel access."""
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc
    return await _find_experts_impl(channel_id, topic, limit=limit)


__all__ = [
    "search_relationships",
    "trace_decision_history",
    "find_experts",
    "_search_relationships_impl",
    "_trace_decision_history_impl",
    "_find_experts_impl",
]
