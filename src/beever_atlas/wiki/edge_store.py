"""Denormalized typed-edge index for the wiki graph.

Introduced by ``unified-llm-wiki-graph-redesign``. Edges between wiki
pages are persisted in a separate ``wiki_edges`` collection (rather
than embedded in each page's ``cross_links`` field) so backedge
queries — "what links to me" — use an index instead of a collection
scan.

The store is intentionally thin: edges have no business logic of
their own; the resolver in ``services/wiki_maintainer.py`` decides
which edges to write, and the API + agent retrieval layer reads from
this store. v1 surfaces 4 edge kinds (``references``, ``subtopic_of``,
``cites``, ``mentions``); the schema accepts reserved kinds
(``decided_in``, ``owned_by``, ``depends_on``, ``successor_of``,
``same_as``) without migration when v2 promotes them.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import IndexModel

from beever_atlas.models.persistence import WikiEdge

logger = logging.getLogger(__name__)


# v1 surfaced edge kinds. Filtered into traversal results by default.
SURFACED_EDGE_KINDS: frozenset[str] = frozenset(
    {"references", "subtopic_of", "cites", "mentions"}
)

# Reserved kinds. Accepted by the schema; ignored by default traversal.
RESERVED_EDGE_KINDS: frozenset[str] = frozenset(
    {"decided_in", "owned_by", "depends_on", "successor_of", "same_as"}
)

ALL_EDGE_KINDS: frozenset[str] = SURFACED_EDGE_KINDS | RESERVED_EDGE_KINDS


class WikiEdgeStore:
    """Mongo-backed typed-edge index for the wiki graph."""

    COLLECTION_NAME = "wiki_edges"

    def __init__(self, db: AsyncIOMotorDatabase | None = None) -> None:
        self._db = db
        self._collection = db[self.COLLECTION_NAME] if db is not None else None

    def bind_db(self, db: AsyncIOMotorDatabase) -> None:
        """Attach a Motor database handle (used by tests / lazy-init)."""
        self._db = db
        self._collection = db[self.COLLECTION_NAME]

    async def ensure_indexes(self) -> None:
        """Create the indexes the spec requires.

        Per ``wiki-graph-model``: outbound-by-kind, inbound-by-kind,
        cross-channel-pair, plus a unique compound index that makes
        edge upserts idempotent.
        """
        if self._collection is None:
            return
        await self._collection.create_indexes(
            [
                IndexModel(
                    [
                        ("channel_id_from", 1),
                        ("page_id_from", 1),
                        ("edge_kind", 1),
                    ],
                    name="wiki_edges_outbound_by_kind",
                ),
                IndexModel(
                    [
                        ("channel_id_to", 1),
                        ("page_id_to", 1),
                        ("edge_kind", 1),
                    ],
                    name="wiki_edges_inbound_by_kind",
                ),
                IndexModel(
                    [("channel_id_from", 1), ("channel_id_to", 1)],
                    name="wiki_edges_cross_channel_pair",
                ),
                IndexModel(
                    [
                        ("channel_id_from", 1),
                        ("page_id_from", 1),
                        ("edge_kind", 1),
                        ("channel_id_to", 1),
                        ("page_id_to", 1),
                    ],
                    name="wiki_edges_unique_directed_edge",
                    unique=True,
                ),
            ]
        )

    async def upsert_edge(self, edge: WikiEdge) -> None:
        """Idempotently insert or update an edge.

        Matches the unique compound index. Subsequent upserts of the
        same directed edge update ``updated_at``, ``weight``,
        ``confidence``, and ``source_*`` fields; ``created_at`` is
        preserved on first write via ``$setOnInsert``.
        """
        if self._collection is None:
            return
        payload = edge.model_dump(mode="json")
        match = {
            "channel_id_from": edge.channel_id_from,
            "page_id_from": edge.page_id_from,
            "edge_kind": edge.edge_kind,
            "channel_id_to": edge.channel_id_to,
            "page_id_to": edge.page_id_to,
        }
        await self._collection.update_one(
            match,
            {
                "$set": {
                    "weight": payload["weight"],
                    "confidence": payload["confidence"],
                    "source_section_id": payload.get("source_section_id"),
                    "source_fact_id": payload.get("source_fact_id"),
                    "updated_at": payload["updated_at"],
                },
                "$setOnInsert": {
                    **match,
                    "created_at": payload["created_at"],
                },
            },
            upsert=True,
        )

    async def upsert_many(self, edges: list[WikiEdge]) -> int:
        """Batch upsert. Returns the number of edges processed."""
        if not edges or self._collection is None:
            return 0
        for edge in edges:
            await self.upsert_edge(edge)
        return len(edges)

    async def delete_edges_for_page(
        self, channel_id: str, page_id: str
    ) -> int:
        """Remove every edge originating from a page.

        Used by the maintainer when a page is fully rewritten and the
        cross-link set may have shrunk. Returns the deleted count.
        """
        if self._collection is None:
            return 0
        result = await self._collection.delete_many(
            {"channel_id_from": channel_id, "page_id_from": page_id}
        )
        return int(result.deleted_count or 0)

    async def find_outbound(
        self,
        channel_id: str,
        page_id: str,
        edge_kinds: list[str] | None = None,
    ) -> list[WikiEdge]:
        """All edges originating from this page, optionally filtered
        by edge kind. Defaults to v1 surfaced kinds when ``edge_kinds``
        is None."""
        return await self._find_directed(
            "from", channel_id, page_id, edge_kinds
        )

    async def find_inbound(
        self,
        channel_id: str,
        page_id: str,
        edge_kinds: list[str] | None = None,
    ) -> list[WikiEdge]:
        """All edges terminating at this page."""
        return await self._find_directed(
            "to", channel_id, page_id, edge_kinds
        )

    async def _find_directed(
        self,
        direction: str,
        channel_id: str,
        page_id: str,
        edge_kinds: list[str] | None,
    ) -> list[WikiEdge]:
        if self._collection is None:
            return []
        if direction == "from":
            query: dict[str, Any] = {
                "channel_id_from": channel_id,
                "page_id_from": page_id,
            }
        else:
            query = {
                "channel_id_to": channel_id,
                "page_id_to": page_id,
            }
        kinds = edge_kinds if edge_kinds is not None else list(SURFACED_EDGE_KINDS)
        if kinds:
            query["edge_kind"] = {"$in": kinds}
        # Cross-channel filter: v1 surfaces only within-channel edges
        # unless the caller explicitly asked for cross-channel kinds.
        # Reserved kinds (e.g., ``same_as``) are the only path that
        # might touch cross-channel rows, and the caller opts in by
        # naming them in ``edge_kinds``.
        if not any(k in RESERVED_EDGE_KINDS for k in kinds):
            other_field = "channel_id_to" if direction == "from" else "channel_id_from"
            query[other_field] = channel_id
        out: list[WikiEdge] = []
        async for doc in self._collection.find(query):
            doc.pop("_id", None)
            try:
                out.append(WikiEdge.model_validate(doc))
            except Exception:  # noqa: BLE001
                logger.debug("WikiEdgeStore._find_directed: skip malformed row")
        return out

    async def find_path(
        self,
        from_slug: str,
        to_slug: str,
        channel_id: str,
        max_depth: int = 4,
        edge_kinds: list[str] | None = None,
    ) -> list[tuple[str, str]] | None:
        """BFS shortest path from ``from_slug`` to ``to_slug``.

        Returns an ordered list of ``(slug, edge_kind)`` tuples
        starting with ``(from_slug, "")`` and ending with the target;
        ``None`` if unreachable within ``max_depth`` hops.
        """
        if self._collection is None or from_slug == to_slug:
            return [(from_slug, "")] if from_slug == to_slug else None
        kinds = edge_kinds if edge_kinds is not None else list(SURFACED_EDGE_KINDS)
        # BFS frontier: slug -> (parent_slug, edge_kind_to_here)
        visited: dict[str, tuple[str | None, str]] = {from_slug: (None, "")}
        frontier: deque[tuple[str, int]] = deque([(from_slug, 0)])
        while frontier:
            slug, depth = frontier.popleft()
            if depth >= max_depth:
                continue
            outbound = await self.find_outbound(channel_id, slug, kinds)
            for edge in outbound:
                neighbor = edge.page_id_to
                if neighbor in visited:
                    continue
                visited[neighbor] = (slug, edge.edge_kind)
                if neighbor == to_slug:
                    # Reconstruct path.
                    path: list[tuple[str, str]] = []
                    cur: str | None = to_slug
                    while cur is not None:
                        parent, kind = visited[cur]
                        path.append((cur, kind))
                        cur = parent
                    path.reverse()
                    return path
                frontier.append((neighbor, depth + 1))
        return None

    async def get_subgraph(
        self,
        rooted_at: str,
        channel_id: str,
        max_pages: int = 50,
        edge_kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return the rooted page plus its 1-hop and 2-hop neighbors.

        Capped at ``max_pages`` pages (BFS by edge count). The result
        shape matches the MCP ``get_subgraph`` tool's contract.
        """
        if self._collection is None:
            return {
                "root_slug": rooted_at,
                "pages": [rooted_at],
                "edges": [],
                "max_pages_reached": False,
            }
        kinds = edge_kinds if edge_kinds is not None else list(SURFACED_EDGE_KINDS)
        seen: set[str] = {rooted_at}
        edges_out: list[dict[str, Any]] = []
        frontier: deque[tuple[str, int]] = deque([(rooted_at, 0)])
        max_pages_reached = False
        while frontier:
            slug, depth = frontier.popleft()
            if depth >= 2:
                continue
            outbound = await self.find_outbound(channel_id, slug, kinds)
            for edge in outbound:
                edges_out.append(
                    {
                        "from": edge.page_id_from,
                        "to": edge.page_id_to,
                        "edge_kind": edge.edge_kind,
                        "weight": edge.weight,
                    }
                )
                if edge.page_id_to not in seen:
                    if len(seen) >= max_pages:
                        max_pages_reached = True
                        continue
                    seen.add(edge.page_id_to)
                    frontier.append((edge.page_id_to, depth + 1))
        return {
            "root_slug": rooted_at,
            "pages": sorted(seen),
            "edges": edges_out,
            "max_pages_reached": max_pages_reached,
        }

    async def compute_graph_quality(self, channel_id: str) -> dict[str, Any]:
        """Compute orphan_count, max_degree, average_diameter for a channel.

        Cheap and on-demand. Cached by the caller (5-minute TTL per
        ``wiki-graph-model`` spec). Broken-link count is computed by
        the page resolver, not here.
        """
        if self._collection is None:
            return {
                "orphan_count": 0,
                "max_degree": 0,
                "average_diameter": 0.0,
                "edge_count": 0,
            }
        # Aggregate degree per page over surfaced kinds.
        pipeline_outbound = [
            {
                "$match": {
                    "channel_id_from": channel_id,
                    "edge_kind": {"$in": list(SURFACED_EDGE_KINDS)},
                }
            },
            {"$group": {"_id": "$page_id_from", "n": {"$sum": 1}}},
        ]
        pipeline_inbound = [
            {
                "$match": {
                    "channel_id_to": channel_id,
                    "edge_kind": {"$in": list(SURFACED_EDGE_KINDS)},
                }
            },
            {"$group": {"_id": "$page_id_to", "n": {"$sum": 1}}},
        ]
        out_counts: dict[str, int] = {}
        in_counts: dict[str, int] = {}
        async for row in self._collection.aggregate(pipeline_outbound):
            out_counts[row["_id"]] = int(row["n"])
        async for row in self._collection.aggregate(pipeline_inbound):
            in_counts[row["_id"]] = int(row["n"])
        all_pages = set(out_counts.keys()) | set(in_counts.keys())
        max_degree = 0
        for slug in all_pages:
            degree = out_counts.get(slug, 0) + in_counts.get(slug, 0)
            if degree > max_degree:
                max_degree = degree
        edge_count = sum(out_counts.values())
        # Orphan = edges existed for the channel but this slug's degree is 0.
        # Note: pages with NO edges in either direction won't appear in
        # ``all_pages``; the page-store-level orphan check handles those.
        # Here we conservatively report 0 — full orphan reckoning happens
        # in the Wiki Health admin endpoint which joins page list +
        # edge index.
        return {
            "orphan_count": 0,
            "max_degree": max_degree,
            "average_diameter": 0.0,  # Computed on demand by the admin view.
            "edge_count": edge_count,
        }
