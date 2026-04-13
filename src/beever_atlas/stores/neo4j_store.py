"""Neo4j async store for the Beever Atlas knowledge graph."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from neo4j import AsyncGraphDatabase

from beever_atlas.models import GraphEntity, GraphRelationship, Subgraph

if TYPE_CHECKING:
    from beever_atlas.stores.graph_protocol import GraphStore


class Neo4jStore:
    """Manages a Neo4j knowledge graph with Entity nodes, Event nodes, and
    flexible relationship types."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Verify connectivity and create required indexes/schema."""
        await self._driver.verify_connectivity()
        await self.ensure_schema()

    async def ensure_schema(self) -> None:
        """Create indexes and backfill optional fields.  Idempotent."""
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
            await session.run(
                "CREATE INDEX media_url IF NOT EXISTS "
                "FOR (m:Media) ON (m.url)"
            )
            await session.run(
                "MATCH (e:Entity) WHERE e.aliases IS NULL SET e.aliases = []"
            )
            await session.run(
                "MATCH (e:Entity) WHERE e.status IS NULL SET e.status = 'active'"
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
                        e.status         = $status,
                        e.pending_since  = $pending_since,
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
                    status=entity.status,
                    pending_since=entity.pending_since.isoformat() if entity.pending_since else None,
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
                        e.status         = $status,
                        e.pending_since  = $pending_since,
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
                    status=entity.status,
                    pending_since=entity.pending_since.isoformat() if entity.pending_since else None,
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
        media_urls: list[str] | None = None,
        link_urls: list[str] | None = None,
    ) -> None:
        """MERGE an Event node and link the named entity to it via MENTIONED_IN.

        Optionally stores media_urls and link_urls on the Event node for
        graph-traversable media references.
        """
        async with self._driver.session() as session:
            await session.run(
                """
                MATCH (e:Entity {name: $entity_name})
                MERGE (ev:Event {weaviate_id: $weaviate_id})
                    ON CREATE SET
                        ev.message_ts  = $message_ts,
                        ev.channel_id  = $channel_id,
                        ev.media_urls  = $media_urls,
                        ev.link_urls   = $link_urls
                    ON MATCH SET
                        ev.media_urls  = CASE WHEN ev.media_urls IS NULL THEN $media_urls ELSE ev.media_urls END,
                        ev.link_urls   = CASE WHEN ev.link_urls IS NULL THEN $link_urls ELSE ev.link_urls END
                MERGE (e)-[:MENTIONED_IN]->(ev)
                """,
                entity_name=entity_name,
                weaviate_id=weaviate_fact_id,
                message_ts=message_ts,
                channel_id=channel_id,
                media_urls=media_urls or [],
                link_urls=link_urls or [],
            )

    # ------------------------------------------------------------------
    # Write — media nodes
    # ------------------------------------------------------------------

    async def upsert_media(
        self,
        url: str,
        media_type: str,
        title: str = "",
        channel_id: str = "",
        message_ts: str = "",
    ) -> None:
        """MERGE a Media node by URL. Idempotent."""
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (m:Media {url: $url})
                    ON CREATE SET
                        m.media_type  = $media_type,
                        m.title       = $title,
                        m.channel_id  = $channel_id,
                        m.message_ts  = $message_ts
                    ON MATCH SET
                        m.title       = CASE WHEN $title <> '' THEN $title ELSE m.title END
                """,
                url=url,
                media_type=media_type,
                title=title,
                channel_id=channel_id,
                message_ts=message_ts,
            )

    async def link_entity_to_media(
        self, entity_name: str, media_url: str
    ) -> None:
        """Create REFERENCES_MEDIA relationship from Entity to Media."""
        async with self._driver.session() as session:
            await session.run(
                """
                MATCH (e:Entity {name: $entity_name})
                MATCH (m:Media {url: $media_url})
                MERGE (e)-[:REFERENCES_MEDIA]->(m)
                """,
                entity_name=entity_name,
                media_url=media_url,
            )

    # ------------------------------------------------------------------
    # Delete — channel scoped
    # ------------------------------------------------------------------

    async def delete_channel_data(self, channel_id: str) -> dict[str, int]:
        """Delete all entities, events, media, and relationships for a channel.

        Returns counts of deleted nodes and relationships.
        """
        async with self._driver.session() as session:
            # Delete Event nodes and their relationships for this channel
            result = await session.run(
                "MATCH (ev:Event {channel_id: $channel_id}) "
                "DETACH DELETE ev RETURN count(ev) AS n",
                channel_id=channel_id,
            )
            record = await result.single()
            events_deleted = int(record["n"]) if record else 0

            # Delete Media nodes for this channel
            result = await session.run(
                "MATCH (m:Media {channel_id: $channel_id}) "
                "DETACH DELETE m RETURN count(m) AS n",
                channel_id=channel_id,
            )
            record = await result.single()
            media_deleted = int(record["n"]) if record else 0

            # Delete channel-scoped entities
            result = await session.run(
                "MATCH (e:Entity {channel_id: $channel_id}) "
                "DETACH DELETE e RETURN count(e) AS n",
                channel_id=channel_id,
            )
            record = await result.single()
            entities_deleted = int(record["n"]) if record else 0

            # Clean up orphaned global entities that have no remaining relationships
            result = await session.run(
                "MATCH (e:Entity) WHERE e.scope = 'global' "
                "AND NOT EXISTS { MATCH (e)-[]-() } "
                "DELETE e RETURN count(e) AS n",
            )
            record = await result.single()
            orphans_deleted = int(record["n"]) if record else 0

        return {
            "events_deleted": events_deleted,
            "media_deleted": media_deleted,
            "entities_deleted": entities_deleted + orphans_deleted,
        }

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def list_entities(
        self,
        channel_id: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
        include_pending: bool = False,
    ) -> list[GraphEntity]:
        """Return entities, optionally filtered by channel and/or type.

        When channel_id is provided, returns entities that either:
        - Have channel_id matching directly, OR
        - Have at least one episodic link (MENTIONED_IN) to an Event in that channel
        This ensures only entities actually referenced in the channel appear.

        By default excludes pending entities. Set include_pending=True to include them.
        """
        params: dict[str, Any] = {"limit": limit}

        if channel_id is not None:
            # Use episodic links to scope entities to the channel
            match_clause = (
                "MATCH (e:Entity) "
                "WHERE (e.channel_id = $channel_id "
                "OR EXISTS { MATCH (e)-[:MENTIONED_IN]->(ev:Event) WHERE ev.channel_id = $channel_id })"
            )
            params["channel_id"] = channel_id
        else:
            match_clause = "MATCH (e:Entity)"

        # Filter out pending entities by default
        if not include_pending:
            pending_filter = "(e.status = 'active' OR e.status IS NULL)"
            match_clause += f" AND {pending_filter}" if "WHERE" in match_clause else f" WHERE {pending_filter}"

        if entity_type is not None:
            match_clause += " AND e.type = $entity_type" if "WHERE" in match_clause else " WHERE e.type = $entity_type"
            params["entity_type"] = entity_type

        query = f"{match_clause} RETURN e LIMIT $limit"  # noqa: S608

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

    async def find_entity_by_name(self, name: str) -> GraphEntity | None:
        """Return an entity by its name, or None if not found."""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity {name: $name}) RETURN e LIMIT 1",
                name=name,
            )
            record = await result.single()
        if record is None:
            return None
        return self._entity_from_record(record["e"])

    async def get_neighbors(
        self, entity_id: str, hops: int = 1, limit: int = 50
    ) -> Subgraph:
        """Return the neighborhood subgraph up to `hops` hops from an entity."""
        hops = max(1, min(int(hops), 4))
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
        """Return relationships between entities, optionally scoped to a channel.

        When channel_id is provided, only returns relationships where at least
        one endpoint entity has an episodic link to an Event in that channel.
        """
        if channel_id is not None:
            where = (
                "WHERE (a.channel_id = $channel_id "
                "OR EXISTS { MATCH (a)-[:MENTIONED_IN]->(ev:Event) WHERE ev.channel_id = $channel_id }) "
                "AND (b.channel_id = $channel_id "
                "OR EXISTS { MATCH (b)-[:MENTIONED_IN]->(ev2:Event) WHERE ev2.channel_id = $channel_id })"
            )
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

    async def list_media_relationships(
        self,
        channel_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return REFERENCES_MEDIA relationships between entities and media."""
        params: dict[str, Any] = {"limit": limit}
        if channel_id is not None:
            where = "WHERE m.channel_id = $channel_id"
            params["channel_id"] = channel_id
        else:
            where = ""
        query = (
            f"MATCH (e:Entity)-[r:REFERENCES_MEDIA]->(m:Media) {where} "  # noqa: S608
            "RETURN e.name AS src, m.title AS tgt_title, m.url AS tgt_url, "
            "m.media_type AS media_type, type(r) AS rel_type "
            "LIMIT $limit"
        )
        async with self._driver.session() as session:
            result = await session.run(query, **params)
            records = await result.data()
        rels: list[dict[str, Any]] = []
        for row in records:
            # Use title or derive name from URL for the target
            tgt_name = row.get("tgt_title") or ""
            if not tgt_name:
                url = row.get("tgt_url", "")
                media_type = row.get("media_type", "")
                if media_type == "link":
                    try:
                        tgt_name = url.split("//")[-1].split("/")[0]
                    except Exception:
                        tgt_name = url
                else:
                    tgt_name = url.split("/")[-1] if "/" in url else url
            rels.append({
                "source": row.get("src", ""),
                "target": tgt_name,
                "type": row.get("rel_type", "REFERENCES_MEDIA"),
            })
        return rels

    async def list_media(
        self,
        channel_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return Media nodes, optionally filtered by channel."""
        params: dict[str, Any] = {"limit": limit}
        if channel_id is not None:
            where = "WHERE m.channel_id = $channel_id"
            params["channel_id"] = channel_id
        else:
            where = ""
        query = f"MATCH (m:Media) {where} RETURN m LIMIT $limit"  # noqa: S608

        async with self._driver.session() as session:
            result = await session.run(query, **params)
            records = [record async for record in result]
        media_list: list[dict[str, Any]] = []
        for r in records:
            node = r["m"]
            props = dict(node)
            media_list.append({
                "id": getattr(node, "element_id", None) or props.get("url", ""),
                "url": props.get("url", ""),
                "media_type": props.get("media_type", ""),
                "title": props.get("title", ""),
                "channel_id": props.get("channel_id", ""),
                "message_ts": props.get("message_ts", ""),
            })
        return media_list

    async def get_decisions(self, channel_id: str, limit: int = 20) -> list[GraphEntity]:
        """Return entities of type 'Decision' visible in a channel."""
        return await self.list_entities(
            channel_id=channel_id, entity_type="Decision", limit=limit
        )

    async def list_person_entities_with_edges(
        self, channel_id: str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return Person entities with their edge types and connected entity names.

        Each result contains: name, properties, edges list with
        {type, target_name, target_type} for DECIDED/WORKS_ON/OWNS edges.
        """
        query = """
        MATCH (p:Entity {type: 'Person'})
        WHERE p.channel_id = $channel_id
           OR EXISTS { MATCH (p)-[:MENTIONED_IN]->(ev:Event) WHERE ev.channel_id = $channel_id }
        OPTIONAL MATCH (p)-[r]->(t:Entity)
        WHERE type(r) IN ['DECIDED', 'WORKS_ON', 'OWNS', 'USES']
        WITH p, collect({
            type: type(r),
            target_name: t.name,
            target_type: t.type
        }) AS edges
        RETURN p, edges
        LIMIT $limit
        """
        async with self._driver.session() as session:
            result = await session.run(query, channel_id=channel_id, limit=limit)
            records = await result.data()
        persons: list[dict[str, Any]] = []
        for row in records:
            entity = self._entity_from_record(row["p"])
            edges = [e for e in row.get("edges", []) if e.get("type")]
            persons.append({
                "entity": entity,
                "edges": edges,
            })
        return persons

    async def list_technology_entities(
        self, channel_id: str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return Technology entities visible in a channel with USES edges."""
        query = """
        MATCH (t:Entity {type: 'Technology'})
        WHERE t.channel_id = $channel_id
           OR EXISTS { MATCH (t)-[:MENTIONED_IN]->(ev:Event) WHERE ev.channel_id = $channel_id }
        OPTIONAL MATCH (user:Entity)-[r:USES]->(t)
        WITH t, collect(user.name) AS used_by
        RETURN t, used_by
        LIMIT $limit
        """
        async with self._driver.session() as session:
            result = await session.run(query, channel_id=channel_id, limit=limit)
            records = await result.data()
        techs: list[dict[str, Any]] = []
        for row in records:
            entity = self._entity_from_record(row["t"])
            techs.append({
                "entity": entity,
                "used_by": row.get("used_by", []),
            })
        return techs

    async def list_project_entities(
        self, channel_id: str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return Project entities with BLOCKED_BY/DEPENDS_ON edges."""
        query = """
        MATCH (p:Entity {type: 'Project'})
        WHERE p.channel_id = $channel_id
           OR EXISTS { MATCH (p)-[:MENTIONED_IN]->(ev:Event) WHERE ev.channel_id = $channel_id }
        OPTIONAL MATCH (p)-[r]->(dep:Entity)
        WHERE type(r) IN ['BLOCKED_BY', 'DEPENDS_ON']
        WITH p, collect({type: type(r), target: dep.name}) AS deps
        OPTIONAL MATCH (owner:Entity)-[:OWNS]->(p)
        WITH p, deps, collect(owner.name) AS owners
        RETURN p, deps, owners
        LIMIT $limit
        """
        async with self._driver.session() as session:
            result = await session.run(query, channel_id=channel_id, limit=limit)
            records = await result.data()
        projects: list[dict[str, Any]] = []
        for row in records:
            entity = self._entity_from_record(row["p"])
            deps = [d for d in row.get("deps", []) if d.get("type")]
            projects.append({
                "entity": entity,
                "dependencies": deps,
                "owners": row.get("owners", []),
            })
        return projects

    async def get_decisions_with_chains(
        self, channel_id: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return Decision entities with SUPERSEDES chains and DECIDED-by persons."""
        query = """
        MATCH (d:Entity {type: 'Decision'})
        WHERE d.channel_id = $channel_id
           OR EXISTS { MATCH (d)-[:MENTIONED_IN]->(ev:Event) WHERE ev.channel_id = $channel_id }
        OPTIONAL MATCH (person:Entity)-[:DECIDED]->(d)
        OPTIONAL MATCH (d)-[:SUPERSEDES]->(old:Entity)
        OPTIONAL MATCH (newer:Entity)-[:SUPERSEDES]->(d)
        WITH d,
             collect(DISTINCT person.name) AS decided_by,
             collect(DISTINCT old.name) AS supersedes,
             collect(DISTINCT newer.name) AS superseded_by
        RETURN d, decided_by, supersedes, superseded_by
        LIMIT $limit
        """
        async with self._driver.session() as session:
            result = await session.run(query, channel_id=channel_id, limit=limit)
            records = await result.data()
        decisions: list[dict[str, Any]] = []
        for row in records:
            entity = self._entity_from_record(row["d"])
            decisions.append({
                "entity": entity,
                "decided_by": [n for n in row.get("decided_by", []) if n],
                "supersedes": [n for n in row.get("supersedes", []) if n],
                "superseded_by": [n for n in row.get("superseded_by", []) if n],
            })
        return decisions

    async def count_entities(self, channel_id: str | None = None) -> int:
        """Return total entity count, optionally scoped to a channel."""
        params: dict[str, Any] = {}
        if channel_id is not None:
            where = (
                "WHERE e.channel_id = $channel_id "
                "OR EXISTS { MATCH (e)-[:MENTIONED_IN]->(ev:Event) WHERE ev.channel_id = $channel_id }"
            )
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
                "WHERE (a.channel_id = $channel_id "
                "OR EXISTS { MATCH (a)-[:MENTIONED_IN]->(ev:Event) WHERE ev.channel_id = $channel_id }) "
                "AND (b.channel_id = $channel_id "
                "OR EXISTS { MATCH (b)-[:MENTIONED_IN]->(ev2:Event) WHERE ev2.channel_id = $channel_id }) "
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

    # ------------------------------------------------------------------
    # Soft orphan handling
    # ------------------------------------------------------------------

    async def promote_pending_entity(self, entity_name: str) -> None:
        """Promote a pending entity to active status."""
        async with self._driver.session() as session:
            await session.run(
                "MATCH (e:Entity {name: $name}) "
                "WHERE e.status = 'pending' "
                "SET e.status = 'active', e.pending_since = null",
                name=entity_name,
            )

    async def prune_expired_pending(self, grace_period_days: int = 7) -> int:
        """Delete pending entities older than the grace period.

        Returns count of pruned entities.
        """
        from datetime import timedelta
        cutoff = (datetime.now(tz=UTC) - timedelta(days=grace_period_days)).isoformat()
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) "
                "WHERE e.status = 'pending' AND e.pending_since IS NOT NULL "
                "AND e.pending_since < $cutoff "
                "DETACH DELETE e RETURN count(e) AS n",
                cutoff=cutoff,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def fuzzy_match_entity(
        self, name: str, threshold: float = 0.8
    ) -> list[GraphEntity]:
        """Find entities whose name is similar to `name` using Jaro-Winkler distance.

        Internal method kept for backwards compatibility.  The protocol-level
        method is :meth:`fuzzy_match_entities`.
        """
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

    # ------------------------------------------------------------------
    # Entity-registry support (protocol methods)
    # ------------------------------------------------------------------

    async def find_entity_by_name_or_alias(self, name: str) -> str | None:
        """Find an entity by exact name or alias.  Returns canonical name."""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) "
                "WHERE e.name = $name OR $name IN coalesce(e.aliases, []) "
                "RETURN e.name AS canonical LIMIT 1",
                name=name,
            )
            record = await result.single()
        if record is None:
            return None
        return record["canonical"]

    async def get_all_entities_summary(self) -> list[dict[str, Any]]:
        """Return all entities as dicts with name, type, aliases."""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) "
                "RETURN e.name AS name, e.type AS type, "
                "coalesce(e.aliases, []) AS aliases "
                "ORDER BY e.name"
            )
            records = await result.data()
        return [
            {"name": r["name"], "type": r["type"], "aliases": list(r["aliases"])}
            for r in records
        ]

    async def register_alias(
        self, canonical: str, alias: str, entity_type: str
    ) -> None:
        """Append alias to the aliases list of the named entity."""
        async with self._driver.session() as session:
            await session.run(
                "MATCH (e:Entity {name: $canonical, type: $entity_type}) "
                "SET e.aliases = CASE "
                "  WHEN $alias IN coalesce(e.aliases, []) THEN e.aliases "
                "  ELSE coalesce(e.aliases, []) + [$alias] "
                "END",
                canonical=canonical,
                entity_type=entity_type,
                alias=alias,
            )

    async def fuzzy_match_entities(
        self, name: str, threshold: float = 0.8
    ) -> list[tuple[str, float]]:
        """Return (canonical_name, score) pairs using jellyfish Jaro-Winkler."""
        import jellyfish  # lazy import — optional dependency

        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) RETURN e.name AS name"
            )
            records = await result.data()
        matches: list[tuple[str, float]] = []
        for r in records:
            entity_name = r["name"]
            if not entity_name:
                continue
            score = jellyfish.jaro_winkler_similarity(name, entity_name)
            if score >= threshold:
                matches.append((entity_name, score))
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches

    async def get_entities_with_name_vectors(self) -> list[dict[str, Any]]:
        """Return dicts with name and vec for entities that have name_vector."""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) WHERE e.name_vector IS NOT NULL "
                "RETURN e.name AS name, e.name_vector AS vec"
            )
            records = await result.data()
        return [{"name": r["name"], "vec": r["vec"]} for r in records]

    async def get_entities_missing_name_vectors(self) -> list[str]:
        """Return entity names that do not have a name_vector."""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) WHERE e.name_vector IS NULL "
                "RETURN e.name AS name"
            )
            records = await result.data()
        return [r["name"] for r in records if r.get("name")]

    async def store_name_vector(
        self, entity_name: str, vector: list[float]
    ) -> None:
        """Persist a name-embedding vector on an entity node."""
        async with self._driver.session() as session:
            await session.run(
                "MATCH (e:Entity {name: $name}) SET e.name_vector = $vector",
                name=entity_name,
                vector=vector,
            )

    # ------------------------------------------------------------------
    # Batch operations (optimised for persister pipeline)
    # ------------------------------------------------------------------

    async def batch_create_episodic_links(self, links: list[dict[str, Any]]) -> int:
        if not links:
            return 0
        async with self._driver.session() as session:
            result = await session.run(
                "UNWIND $links AS link "
                "MATCH (e:Entity {name: link.entity_name}) "
                "MERGE (ep:Event {weaviate_id: link.weaviate_fact_id}) "
                "ON CREATE SET ep.message_ts = link.message_ts, ep.channel_id = link.channel_id "
                "MERGE (e)-[:MENTIONED_IN]->(ep) "
                "RETURN count(*) AS created",
                links=links,
            )
            record = await result.single()
            return int(record["created"]) if record else 0

    async def batch_upsert_media(self, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        async with self._driver.session() as session:
            result = await session.run(
                "UNWIND $items AS item "
                "MERGE (m:Media {url: item.url}) "
                "ON CREATE SET m.media_type = item.media_type, m.title = item.title, "
                "m.channel_id = item.channel_id, m.message_ts = item.message_ts "
                "RETURN count(*) AS upserted",
                items=items,
            )
            record = await result.single()
            return int(record["upserted"]) if record else 0

    async def batch_link_entities_to_media(self, links: list[dict[str, Any]]) -> int:
        if not links:
            return 0
        async with self._driver.session() as session:
            result = await session.run(
                "UNWIND $links AS link "
                "MATCH (e:Entity {name: link.entity_name}) "
                "MATCH (m:Media {url: link.media_url}) "
                "MERGE (e)-[:REFERENCES_MEDIA]->(m) "
                "RETURN count(*) AS linked",
                links=links,
            )
            record = await result.single()
            return int(record["linked"]) if record else 0

    async def batch_promote_pending(self, names: list[str]) -> int:
        if not names:
            return 0
        async with self._driver.session() as session:
            result = await session.run(
                "UNWIND $names AS name "
                "MATCH (e:Entity {name: name, status: 'pending'}) "
                "SET e.status = 'active', e.pending_since = null "
                "RETURN count(*) AS promoted",
                names=names,
            )
            record = await result.single()
            return int(record["promoted"]) if record else 0

    async def batch_find_entities_by_name(self, names: list[str]) -> set[str]:
        if not names:
            return set()
        async with self._driver.session() as session:
            result = await session.run(
                "UNWIND $names AS name "
                "MATCH (e:Entity {name: name}) "
                "RETURN e.name AS found",
                names=list(names),
            )
            found: set[str] = set()
            async for record in result:
                found.add(record["found"])
            return found
