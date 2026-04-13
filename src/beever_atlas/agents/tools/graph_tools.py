"""Graph memory tools: entity relationships, decision history, expert ranking.

This module exposes three async tools that let the QA agent reason over the
knowledge graph (Neo4j / Nebula). All three are decorated with
``@cite_tool_output`` so the citation registry can assign stable ``_src_id``
handles to every returned row.

Return-shape contracts (see TypedDicts below):
    * ``search_relationships``  -> RelationshipSearchResult  (dict)
    * ``trace_decision_history`` -> list[DecisionEvent]       (list of dicts)
    * ``find_experts``           -> list[ExpertHit]           (list of dicts)

Empty-graph sentinel: list-returning tools emit ``[{"_empty": True, ...}]``
rather than a bare ``[]`` so the citation decorator's list-branch (see
``_citation_decorator.py:68-75``) skips annotation but the agent still has a
structured hint about the failure mode.

Name freeze: the three public coroutine names are locked by Stream 1's
``test_allowed_tools_exist_in_registry`` — do NOT rename.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from beever_atlas.agents.tools._citation_decorator import cite_tool_output

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TypedDict shapes (authoritative return schemas)
# ---------------------------------------------------------------------------


class RelationshipNode(TypedDict):
    """A single node in a relationship subgraph."""

    name: str
    type: str | None


class RelationshipEdge(TypedDict):
    """A single edge in a relationship subgraph."""

    source: str
    target: str
    type: str
    confidence: float
    context: str


class RelationshipSearchResult(TypedDict):
    """Return shape for :func:`search_relationships`.

    Citation-decorator fields (``text``, ``subject_id``, ``predicate``,
    ``object_id``, ``channel_id``) are required: the ``graph_relationship``
    kind derives its ``native_identity`` from ``subject_id:predicate:object_id``
    and its excerpt from ``text``.
    """

    entities_searched: list[str]
    nodes: list[RelationshipNode]
    edges: list[RelationshipEdge]
    text: str
    subject_id: str
    predicate: str
    object_id: str
    channel_id: str


class DecisionEvent(TypedDict):
    """A single SUPERSEDES event returned by :func:`trace_decision_history`."""

    entity: str
    superseded_by: str
    superseded_by_id: str | None
    relationship: str  # always "SUPERSEDES"
    confidence: float
    context: str
    position: int
    text: str
    decision_id: str
    channel_id: str
    topic: str


class ExpertHit(TypedDict):
    """A single ranked expert returned by :func:`find_experts`."""

    handle: str
    expertise_score: float
    fact_count: int
    top_topics: list[str]
    recent_activity_days: int
    text: str
    subject_id: str
    predicate: str  # always "EXPERT_IN"
    object_id: str
    channel_id: str


# Caps enforced on graph traversals to bound token cost / latency.
_MAX_NODES = 20
_MAX_EDGES = 50


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@cite_tool_output(kind="graph_relationship")
async def search_relationships(
    channel_id: str,
    entities: list[str],
    hops: int = 2,
) -> dict | list[dict]:
    """Traverse the knowledge graph for relationships between named entities.

    **Purpose.** Resolve each entity name to a canonical node (fuzzy match),
    then merge the ``hops``-radius neighbourhoods into a single deduplicated
    subgraph of nodes and edges. Useful for answering *"how is X connected to
    Y?"* style questions.

    **When to use.**
    - The question names 1-3 concrete entities (people, projects, tech) and
      asks how they relate.
    - You need structured edges with relationship types and confidence, not
      free-text facts.

    **When NOT to use.**
    - The question is broad / exploratory ("tell me about X"): prefer
      ``search_channel_facts`` or ``get_topic_overview``.
    - You need temporal ordering of decisions: use
      ``trace_decision_history``.
    - You need ranked people by expertise: use ``find_experts``.

    Cost: ~$0.005. Target latency: ~500ms.

    Args:
        channel_id: Scope traversal context (used for logging / citations).
        entities: 1-N entity names to resolve and traverse from.
        hops: Graph-traversal radius (default 2).

    Returns:
        A :class:`RelationshipSearchResult` dict. Nodes capped at 20 and
        edges at 50 — overflow is dropped lowest-confidence first. The
        citation-decorator fields (``text``, ``subject_id``, ``predicate``,
        ``object_id``, ``channel_id``) are always populated.

        On an empty graph the sentinel ``[{"_empty": True, "entity": ...,
        "reason": "no_edges"}]`` is returned instead (kept for parity with
        the list-returning tools so that the citation decorator's list
        branch short-circuits on ``_empty``).

    Example 1 (typical call)::

        >>> # Doctest-ish illustration (stubbed):
        >>> # result = await search_relationships("C1", ["Alice", "Bob"], hops=2)
        >>> # result["entities_searched"]
        >>> # ['Alice', 'Bob']
        >>> # len(result["edges"]) <= 50
        >>> # True

    Example 2 (empty subgraph)::

        >>> # result = await search_relationships("C1", ["Nobody"], hops=1)
        >>> # result
        >>> # [{'_empty': True, 'entity': 'Nobody', 'reason': 'no_edges'}]
    """
    try:
        from beever_atlas.stores import get_stores

        graph = get_stores().graph
        all_nodes: list[RelationshipNode] = []
        all_edges: list[RelationshipEdge] = []
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

        # Cap outputs; drop lowest-confidence edges first, keep node order
        # (no confidence on nodes, so truncate tail).
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
            # Citation-decorator fields (single-source dict path):
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


@cite_tool_output(kind="decision_record")
async def trace_decision_history(channel_id: str, topic: str) -> list:
    """Trace the temporal evolution of decisions about a topic.

    **Purpose.** Walk the knowledge graph's ``SUPERSEDES`` chain starting
    from a fuzzy-matched topic entity, returning each supersession event in
    traversal order (stable ``position`` per event).

    **When to use.**
    - The question asks *"what did we decide about X, and has it changed?"*
    - You need an ordered timeline of replaced/superseded decisions.

    **When NOT to use.**
    - The question is about current state only: prefer
      ``search_channel_facts`` or ``get_topic_overview``.
    - You need generic relationships (not SUPERSEDES): use
      ``search_relationships``.

    Cost: ~$0.005. Target latency: ~500ms.

    Args:
        channel_id: Scope context (for logging and citation identity).
        topic: Topic or entity name to trace. Fuzzy-matched against
            canonical entity names (Jaro-Winkler, threshold 0.6).

    Returns:
        A list of :class:`DecisionEvent` dicts, ordered by traversal order
        (``position`` is 0-indexed). Each event carries
        ``decision_id = f"{channel_id}:{entity}:{superseded_by}"`` for the
        citation decorator's ``decision_record`` kind.

        - Empty-graph sentinel: ``[{"_empty": True, "entity": topic,
          "reason": "no_edges"}]`` when no SUPERSEDES edges are found.
        - No fuzzy match / entity missing: ``[]``.
        - Generic exception: ``[]``.
        - ``ConnectionError`` / ``OSError`` (backend unreachable): the
          legacy dict ``{"result": [], "error": "graph_unavailable"}`` is
          preserved so upstream probes can distinguish *"graph down"* from
          *"no data"*. (See audit note — fully normalising this is
          deferred because callers rely on the ``error`` key.)

    Example 1 (typical call)::

        >>> # events = await trace_decision_history("C1", "Architecture v2")
        >>> # events[0]["relationship"]
        >>> # 'SUPERSEDES'
        >>> # events[0]["position"]
        >>> # 0

    Example 2 (no match)::

        >>> # events = await trace_decision_history("C1", "Nonexistent")
        >>> # events
        >>> # []
    """
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

        # Build a name -> id lookup so we can expose superseded_by_id.
        name_to_id: dict[str, str] = {}
        for n in subgraph.nodes:
            nid = getattr(n, "id", None) or getattr(n, "name", None)
            if n.name and nid:
                name_to_id[n.name] = nid

        timeline: list[DecisionEvent] = []
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
                # Citation-decorator fields:
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
        # Preserved legacy dict so probes can distinguish "graph down" from
        # "no data". Locked by test_trace_decision_history_errors.py.
        return {"result": [], "error": "graph_unavailable"}  # type: ignore[return-value]
    except Exception as e:
        logger.exception("trace_decision_history failed for topic=%s exc=%r", topic, e)
        return []


@cite_tool_output(kind="graph_relationship")
async def find_experts(channel_id: str, topic: str, limit: int = 5) -> list:
    """Rank channel members by expertise on a topic.

    **Purpose.** Scan up to 500 recent relationships in the channel, score
    each person-endpoint that co-occurs with a topic-matching node, and
    return the top ``limit`` contributors ordered by ``expertise_score``
    descending.

    **When to use.**
    - The question asks *"who knows most about X?"* / *"who should I
      ask?"*.

    **When NOT to use.**
    - You need a specific person's facts: use ``search_channel_facts``.
    - You need relationships between named entities:
      ``search_relationships``.
    - Topic lacks any channel facts — prefer
      ``search_external_knowledge``.

    Cost: ~$0.005. Target latency: ~500ms.

    Args:
        channel_id: Scope to this channel.
        topic: Topic to rank expertise for. Case-insensitive substring
            match against relationship endpoints.
        limit: Max people to return (default 5).

    Returns:
        A list of :class:`ExpertHit` dicts ordered by ``expertise_score``
        descending. Each hit includes ``top_topics`` (distinct co-occurring
        endpoints, capped at 5) and ``recent_activity_days`` (placeholder
        ``0`` — populated when the graph store surfaces timestamps).

        Empty-result sentinel: ``[{"_empty": True, "entity": topic,
        "reason": "no_edges"}]``. The citation decorator's list branch
        skips ``_empty`` rows.

    Example 1 (typical call)::

        >>> # experts = await find_experts("C1", "Database", limit=3)
        >>> # [e["handle"] for e in experts]
        >>> # ['alice', 'bob', 'carol']
        >>> # experts[0]["predicate"]
        >>> # 'EXPERT_IN'

    Example 2 (no experts)::

        >>> # experts = await find_experts("C1", "Obscure XYZ")
        >>> # experts
        >>> # [{'_empty': True, 'entity': 'Obscure XYZ', 'reason': 'no_edges'}]
    """
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

        out: list[ExpertHit] = []
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
        return out  # type: ignore[return-value]
    except Exception:
        logger.exception("find_experts failed for topic=%s", topic)
        return []
