"""Neo4j async store for the Beever Atlas knowledge graph."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from neo4j import AsyncGraphDatabase

from beever_atlas.models import GraphEntity, GraphRelationship, Subgraph


class Neo4jStore:
    """Manages a Neo4j knowledge graph with Entity nodes, Event nodes, and
    flexible relationship types."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Verify connectivity and create required indexes."""
        await self._driver.verify_connectivity()
        async with self._driver.session() as session:
            await session.run(
                "CREATE INDEX entity_name IF NOT EXISTS "
                "FOR (e:Entity) ON (e.name)"
            )
            await session.run(
                "CREATE INDEX entity_type IF NOT EXISTS "
                "FOR (e:Entity) ON (e.type)"
            )
            await session.run(
                "CREATE INDEX event_weaviate_id IF NOT EXISTS "
                "FOR (ev:Event) ON (ev.weaviate_id)"
            )
            # Backfill optional fields to avoid noisy "property key does not exist"
            # notifications in read queries that project aliases.
            await session.run(
                "MATCH (e:Entity) WHERE e.aliases IS NULL SET e.aliases = []"
            )

    async def shutdown(self) -> None:
        """Close the Neo4j driver."""
        await self._driver.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _entity_from_record(self, node: Any) -> GraphEntity:
        """Construct a GraphEntity from a Neo4j node or plain dict."""
        props = dict(node) if not isinstance(node, dict) else node
        raw_properties = props.get("properties", "{}")
        if isinstance(raw_properties, str):
            try:
                parsed_properties: dict[str, Any] = json.loads(raw_properties)
            except (json.JSONDecodeError, ValueError):
                parsed_properties = {}
        else:
            parsed_properties = raw_properties or {}

        def _parse_dt(val: Any) -> datetime:
            if val is None:
                return datetime.now(tz=UTC)
            if isinstance(val, datetime):
                return val if val.tzinfo else val.replace(tzinfo=UTC)
            return datetime.fromisoformat(str(val)).replace(tzinfo=UTC)

        # Support both Neo4j Node objects (.element_id) and plain dicts.
        node_id = getattr(node, "element_id", None) or props.get("name", str(id(node)))

        return GraphEntity(
            id=node_id,
            name=props.get("name", ""),
            type=props.get("type", ""),
            scope=props.get("scope", "global"),
            channel_id=props.get("channel_id"),
            properties=parsed_properties,
            aliases=list(props.get("aliases") or []),
            source_fact_ids=[],
            source_message_id=props.get("source_message_id", ""),
            message_ts=props.get("message_ts", ""),
            created_at=_parse_dt(props.get("created_at")),
            updated_at=_parse_dt(props.get("updated_at")),
        )

    def _rel_from_record(self, rel: Any, source_name: str = "", target_name: str = "") -> GraphRelationship:
        """Construct a GraphRelationship from a Neo4j relationship."""
        props = dict(rel)

        def _parse_dt(val: Any) -> datetime:
            if val is None:
                return datetime.now(tz=UTC)
            if isinstance(val, datetime):
                return val if val.tzinfo else val.replace(tzinfo=UTC)
            return datetime.fromisoformat(str(val)).replace(tzinfo=UTC)

        return GraphRelationship(
            id=rel.element_id,
            type=rel.type,
            source=source_name or props.get("source", ""),
            target=target_name or props.get("target", ""),
            confidence=float(props.get("confidence", 0.0)),
            valid_from=props.get("valid_from"),
            valid_until=props.get("valid_until"),
            context=props.get("context", ""),
            source_message_id=props.get("source_message_id", ""),
            source_fact_id=props.get("source_fact_id", ""),
            created_at=_parse_dt(props.get("created_at")),
        )

    # ------------------------------------------------------------------
    # Write — entities
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: GraphEntity) -> str:
        """MERGE an Entity node by name+type (and channel_id for channel scope).

        Returns the node element ID.
        """
        now_iso = datetime.now(tz=UTC).isoformat()
        props_json = json.dumps(entity.properties)

        async with self._driver.session() as session:
            if entity.scope == "channel" and entity.channel_id:
                result = await session.run(
                    """
                    MERGE (e:Entity {name: $name, type: $type, channel_id: $channel_id})
                    ON CREATE SET
                        e.scope          = $scope,
                        e.properties     = $properties,
                        e.aliases        = $aliases,
                        e.source_message_id = $source_message_id,
                        e.message_ts     = $message_ts,
                        e.created_at     = $now,
                        e.updated_at     = $now
                    ON MATCH SET
                        e.scope          = $scope,
                        e.properties     = $properties,
                        e.aliases        = $aliases,
                        e.source_message_id = $source_message_id,
                        e.message_ts     = $message_ts,
                        e.updated_at     = $now
                    RETURN elementId(e) AS eid
                    """,
                    name=entity.name,
                    type=entity.type,
                    channel_id=entity.channel_id,
                    scope=entity.scope,
                    properties=props_json,
                    aliases=entity.aliases,
                    source_message_id=entity.source_message_id,
                    message_ts=entity.message_ts,
                    now=now_iso,
                )
            else:
                result = await session.run(
                    """
                    MERGE (e:Entity {name: $name, type: $type, scope: 'global'})
                    ON CREATE SET
                        e.channel_id     = null,
                        e.properties     = $properties,
                        e.aliases        = $aliases,
                        e.source_message_id = $source_message_id,
                        e.message_ts     = $message_ts,
                        e.created_at     = $now,
                        e.updated_at     = $now
                    ON MATCH SET
                        e.properties     = $properties,
                        e.aliases        = $aliases,
                        e.source_message_id = $source_message_id,
                        e.message_ts     = $message_ts,
                        e.updated_at     = $now
                    RETURN elementId(e) AS eid
                    """,
                    name=entity.name,
                    type=entity.type,
                    properties=props_json,
                    aliases=entity.aliases,
                    source_message_id=entity.source_message_id,
                    message_ts=entity.message_ts,
                    now=now_iso,
                )
            record = await result.single()
            return record["eid"]  # type: ignore[index]

    async def batch_upsert_entities(self, entities: list[GraphEntity]) -> list[str]:
        """Upsert multiple entities in parallel. Returns element IDs."""
        return list(await asyncio.gather(*[self.upsert_entity(e) for e in entities]))

    # ------------------------------------------------------------------
    # Write — relationships
    # ------------------------------------------------------------------

    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        """MERGE a relationship between two entities using apoc.merge.relationship.

        Returns the relationship element ID.
        """
        now_iso = datetime.now(tz=UTC).isoformat()
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (a:Entity {name: $source})
                MATCH (b:Entity {name: $target})
                CALL apoc.merge.relationship(
                    a,
                    $rel_type,
                    {},
                    {
                        confidence:        $confidence,
                        valid_from:        $valid_from,
                        valid_until:       $valid_until,
                        context:           $context,
                        source_message_id: $source_message_id,
                        source_fact_id:    $source_fact_id,
                        created_at:        $now
                    },
                    b,
                    {}
                ) YIELD rel
                RETURN elementId(rel) AS eid
                """,
                source=rel.source,
                target=rel.target,
                rel_type=rel.type,
                confidence=rel.confidence,
                valid_from=rel.valid_from,
                valid_until=rel.valid_until,
                context=rel.context,
                source_message_id=rel.source_message_id,
                source_fact_id=rel.source_fact_id,
                now=now_iso,
            )
            record = await result.single()
            return record["eid"]  # type: ignore[index]

    async def batch_upsert_relationships(self, rels: list[GraphRelationship]) -> list[str]:
        """Upsert multiple relationships in parallel. Returns element IDs."""
        return list(await asyncio.gather(*[self.upsert_relationship(r) for r in rels]))

    # ------------------------------------------------------------------
    # Write — episodic links
    # ------------------------------------------------------------------

    async def create_episodic_link(
        self,
        entity_name: str,
        weaviate_fact_id: str,
        message_ts: str,
        channel_id: str = "",
    ) -> None:
        """MERGE an Event node and link the named entity to it via MENTIONED_IN."""
        async with self._driver.session() as session:
            await session.run(
                """
                MATCH (e:Entity {name: $entity_name})
                MERGE (ev:Event {weaviate_id: $weaviate_id})
                    ON CREATE SET
                        ev.message_ts  = $message_ts,
                        ev.channel_id  = $channel_id
                MERGE (e)-[:MENTIONED_IN]->(ev)
                """,
                entity_name=entity_name,
                weaviate_id=weaviate_fact_id,
                message_ts=message_ts,
                channel_id=channel_id,
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def list_entities(
        self,
        channel_id: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
    ) -> list[GraphEntity]:
        """Return entities, optionally filtered by channel and/or type."""
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": limit}

        if channel_id is not None:
            conditions.append("(e.channel_id = $channel_id OR e.scope = 'global')")
            params["channel_id"] = channel_id
        if entity_type is not None:
            conditions.append("e.type = $entity_type")
            params["entity_type"] = entity_type

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"MATCH (e:Entity) {where_clause} RETURN e LIMIT $limit"  # noqa: S608

        async with self._driver.session() as session:
            result = await session.run(query, **params)
            records = [record async for record in result]
        return [self._entity_from_record(r["e"]) for r in records]

    async def get_entity(self, entity_id: str) -> GraphEntity | None:
        """Return an entity by its Neo4j element ID, or None if not found."""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) WHERE elementId(e) = $eid RETURN e",
                eid=entity_id,
            )
            record = await result.single()
        if record is None:
            return None
        return self._entity_from_record(record["e"])

    async def get_neighbors(
        self, entity_id: str, hops: int = 1, limit: int = 50
    ) -> Subgraph:
        """Return the neighborhood subgraph up to `hops` hops from an entity."""
        hops = max(1, hops)
        async with self._driver.session() as session:
            result = await session.run(
                f"""
                MATCH (n:Entity)
                WHERE elementId(n) = $eid
                MATCH path = (n)-[r*1..{hops}]-(m:Entity)
                WITH n, m, r
                UNWIND r AS rel
                WITH DISTINCT n, m, rel
                RETURN
                    startNode(rel) AS src_node,
                    endNode(rel)   AS tgt_node,
                    rel
                LIMIT $limit
                """,
                eid=entity_id,
                limit=limit,
            )
            records = await result.data()

        node_map: dict[str, GraphEntity] = {}
        edges: list[GraphRelationship] = []

        for row in records:
            src_node = row["src_node"]
            tgt_node = row["tgt_node"]
            rel = row["rel"]

            src = self._entity_from_record(src_node)
            tgt = self._entity_from_record(tgt_node)
            node_map[src.name] = src
            node_map[tgt.name] = tgt

            edges.append(self._rel_from_record(rel, source_name=src.name, target_name=tgt.name))

        return Subgraph(nodes=list(node_map.values()), edges=edges)

    async def list_relationships(
        self,
        channel_id: str | None = None,
        limit: int = 200,
    ) -> list[GraphRelationship]:
        """Return relationships between entities, optionally scoped to a channel."""
        if channel_id is not None:
            where = "WHERE a.channel_id = $channel_id OR a.scope = 'global'"
            params: dict[str, Any] = {"channel_id": channel_id, "limit": limit}
        else:
            where = ""
            params = {"limit": limit}
        query = (
            f"MATCH (a:Entity)-[r]->(b:Entity) {where} "  # noqa: S608
            "RETURN a.name AS src, b.name AS tgt, type(r) AS rel_type, "
            "r.confidence AS confidence, r.context AS context "
            "LIMIT $limit"
        )
        async with self._driver.session() as session:
            result = await session.run(query, **params)
            records = await result.data()
        rels: list[GraphRelationship] = []
        for row in records:
            rels.append(GraphRelationship(
                type=row.get("rel_type", "RELATED_TO"),
                source=row.get("src", ""),
                target=row.get("tgt", ""),
                confidence=float(row.get("confidence") or 0.0),
                context=row.get("context") or "",
            ))
        return rels

    async def get_decisions(self, channel_id: str, limit: int = 20) -> list[GraphEntity]:
        """Return entities of type 'Decision' visible in a channel."""
        return await self.list_entities(
            channel_id=channel_id, entity_type="Decision", limit=limit
        )

    async def count_entities(self, channel_id: str | None = None) -> int:
        """Return total entity count, optionally scoped to a channel."""
        params: dict[str, Any] = {}
        if channel_id is not None:
            where = "WHERE e.channel_id = $channel_id OR e.scope = 'global'"
            params["channel_id"] = channel_id
        else:
            where = ""
        async with self._driver.session() as session:
            result = await session.run(
                f"MATCH (e:Entity) {where} RETURN count(e) AS n",  # noqa: S608
                **params,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def count_relationships(self, channel_id: str | None = None) -> int:
        """Return total relationship count, optionally scoped to a channel."""
        if channel_id is not None:
            query = (
                "MATCH (a:Entity)-[r]->(b:Entity) "
                "WHERE a.channel_id = $channel_id OR a.scope = 'global' "
                "RETURN count(r) AS n"
            )
            params: dict[str, Any] = {"channel_id": channel_id}
        else:
            query = "MATCH ()-[r]->() RETURN count(r) AS n"
            params = {}

        async with self._driver.session() as session:
            result = await session.run(query, **params)
            record = await result.single()
        return int(record["n"]) if record else 0

    # ------------------------------------------------------------------
    # Raw query
    # ------------------------------------------------------------------

    async def execute_query(self, query: str, **params) -> list[dict]:
        """Execute a raw Cypher query and return results as dicts."""
        async with self._driver.session() as session:
            result = await session.run(query, params)
            return [record.data() async for record in result]

    # ------------------------------------------------------------------
    # Fuzzy match
    # ------------------------------------------------------------------

    async def fuzzy_match_entity(
        self, name: str, threshold: float = 0.8
    ) -> list[GraphEntity]:
        """Find entities whose name is similar to `name` using Jaro-Winkler distance."""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity)
                WITH e, apoc.text.jaroWinklerDistance(e.name, $name) AS score
                WHERE score >= $threshold
                RETURN e
                ORDER BY score DESC
                """,
                name=name,
                threshold=threshold,
            )
            records = await result.data()
        return [self._entity_from_record(r["e"]) for r in records]
