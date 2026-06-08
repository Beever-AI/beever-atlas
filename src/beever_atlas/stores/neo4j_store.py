"""Neo4j async store for the Beever Atlas knowledge graph."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging

logger = logging.getLogger(__name__)
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from neo4j import AsyncGraphDatabase
from neo4j import exceptions as neo4j_exc

from beever_atlas.models import GraphEntity, GraphRelationship, Subgraph
from beever_atlas.stores.graph_errors import (
    GraphBackendUnavailable,
    GraphConflict,
    GraphStoreError,
)

if TYPE_CHECKING:
    pass


@asynccontextmanager
async def _translate_errors() -> AsyncIterator[None]:
    """Translate raw ``neo4j.exceptions.*`` into :mod:`graph_errors` types.

    ``GraphNotFound`` is NOT raised here — point-query methods raise it
    explicitly when a ``result.single()`` yields ``None``.
    """
    try:
        yield
    except (neo4j_exc.ConstraintError, neo4j_exc.Forbidden) as exc:
        raise GraphConflict(str(exc)) from exc
    except (
        neo4j_exc.ServiceUnavailable,
        neo4j_exc.SessionExpired,
        neo4j_exc.TransientError,
    ) as exc:
        raise GraphBackendUnavailable(str(exc)) from exc
    except GraphStoreError:
        raise
    except neo4j_exc.Neo4jError as exc:
        # Catch-all for other driver errors — surface as generic backend error
        raise GraphStoreError(str(exc)) from exc


def _wrap_async_methods(cls: type) -> type:
    """Decorator: wrap every public async method on *cls* with
    :func:`_translate_errors` so callers see ``GraphStoreError`` subclasses
    instead of raw ``neo4j.exceptions.*``.
    """
    for attr_name, attr in list(vars(cls).items()):
        if attr_name.startswith("_"):
            continue
        if not inspect.iscoroutinefunction(attr):
            continue

        def _make(fn):  # noqa: ANN001 — local closure
            @functools.wraps(fn)
            async def _wrapped(*args: Any, **kwargs: Any) -> Any:
                async with _translate_errors():
                    return await fn(*args, **kwargs)

            return _wrapped

        setattr(cls, attr_name, _make(attr))
    return cls


@_wrap_async_methods
class Neo4jStore:
    """Manages a Neo4j knowledge graph with Entity nodes, Event nodes, and
    flexible relationship types."""

    # Issue #37 — bound concurrent Neo4j sessions per batch upsert. Peak
    # concurrent sessions per process =
    #   ingest_batch_concurrency * Neo4jStore._BATCH_CONCURRENCY
    # Default: 4 (ingest_batch_concurrency) * 16 = 64. Keep the product
    # below the driver's `max_connection_pool_size` (default 100) to
    # avoid pool exhaustion under tuned `ingest_batch_concurrency`.
    _BATCH_CONCURRENCY: int = 16

    def __init__(self, uri: str, user: str, password: str) -> None:
        # Filter informational Neo4j notifications (e.g. SUPERSEDES relationship
        # type missing on OPTIONAL MATCH — pre-existing harmless noise). The
        # kwargs below landed in neo4j-driver 5.7+; fall back to a plain driver
        # if they are unsupported by the installed driver version.
        try:
            from neo4j import NotificationDisabledCategory, NotificationMinimumSeverity

            self._driver = AsyncGraphDatabase.driver(
                uri,
                auth=(user, password),
                notifications_min_severity=NotificationMinimumSeverity.WARNING,
                notifications_disabled_categories=[NotificationDisabledCategory.UNRECOGNIZED],
            )
        except (ImportError, TypeError):  # pragma: no cover - defensive
            # TODO: neo4j-driver < 5.7 lacks notification filtering kwargs.
            self._driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Verify connectivity and create required indexes/schema."""
        await self._driver.verify_connectivity()
        await self.ensure_schema()
        await self.ensure_entity_name_type_scope_unique_constraint()

    async def ensure_schema(self) -> None:
        """Create indexes and backfill optional fields.  Idempotent."""
        async with self._driver.session() as session:
            await session.run("CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)")
            await session.run("CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)")
            await session.run(
                "CREATE INDEX event_weaviate_id IF NOT EXISTS FOR (ev:Event) ON (ev.weaviate_id)"
            )
            await session.run("CREATE INDEX media_url IF NOT EXISTS FOR (m:Media) ON (m.url)")
            # Parallel index on the new ``(channel_id, target_lang, slug)``
            # composite that upsert_wiki_page_node now MERGEs on, so the
            # MERGE remains O(1). The legacy ``(channel_id, slug)`` index
            # (if present from earlier deployments) is left alone — it
            # still services any pre-existing rows.
            await session.run(
                "CREATE INDEX wiki_page_channel_lang_slug IF NOT EXISTS "
                "FOR (w:WikiPage) ON (w.channel_id, w.target_lang, w.slug)"
            )
            await session.run("MATCH (e:Entity) WHERE e.aliases IS NULL SET e.aliases = []")
            await session.run("MATCH (e:Entity) WHERE e.status IS NULL SET e.status = 'active'")

    async def ensure_entity_name_type_scope_unique_constraint(self) -> None:
        """Ensure the composite UNIQUE constraint over ``(name, type, scope)``.

        This is the PR-2 schema migration. The constraint guarantees that the
        in-Cypher ``MERGE`` stub-endpoint creation in :meth:`upsert_relationship`
        and :meth:`batch_create_episodic_links` is serialised by Neo4j under
        concurrent batches — without it, two batches racing on the same
        unknown endpoint name would each create their own stub.

        The constraint creation will fail if pre-existing duplicate
        ``(name, type, scope)`` triples exist (past races, before the fix).
        We therefore run the discovery query first, and if duplicates are
        found we attempt an APOC-based dedup (``apoc.refactor.mergeNodes``).
        If APOC is unavailable we raise a clear error pointing to the
        :file:`runbooks/entity-dedup.md` runbook for the manual procedure.

        Idempotent — ``CREATE CONSTRAINT ... IF NOT EXISTS`` is safe to call
        repeatedly.
        """
        discovery_query = (
            "MATCH (e:Entity) "
            "WITH e.name AS n, e.type AS t, e.scope AS s, collect(e) AS dups "
            "WHERE size(dups) > 1 "
            "RETURN n, t, s, [d IN dups | elementId(d)] AS ids, size(dups) AS cnt"
        )
        async with self._driver.session() as session:
            # 1. Discover pre-existing duplicates.
            result = await session.run(discovery_query)
            duplicates = await result.data()
            if duplicates:
                logger.warning(
                    "Neo4jStore: found %d (name,type,scope) duplicate groups "
                    "before constraint creation; attempting APOC dedup",
                    len(duplicates),
                )
                # 2. Probe for APOC availability and dedup.
                try:
                    dedup_result = await session.run(
                        "MATCH (e:Entity) "
                        "WITH e.name AS n, e.type AS t, e.scope AS s, collect(e) AS dups "
                        "WHERE size(dups) > 1 "
                        "CALL apoc.refactor.mergeNodes(dups, "
                        "{properties: 'discard', mergeRels: true}) "
                        "YIELD node "
                        "RETURN n, t, s, elementId(node) AS kept"
                    )
                    merged = await dedup_result.data()
                    logger.info(
                        "Neo4jStore: APOC dedup merged %d duplicate groups",
                        len(merged),
                    )
                except neo4j_exc.Neo4jError as exc:
                    raise GraphStoreError(
                        f"Neo4jStore: cannot create composite UNIQUE constraint "
                        f"entity_name_type_scope_unique — {len(duplicates)} "
                        f"pre-existing (name,type,scope) duplicate groups found "
                        f"and APOC dedup is unavailable ({exc}). See "
                        f"runbooks/entity-dedup.md for the manual procedure."
                    ) from exc

            # 3. Create the constraint.
            await session.run(
                "CREATE CONSTRAINT entity_name_type_scope_unique IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE (e.name, e.type, e.scope) IS UNIQUE"
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

    def _rel_from_record(
        self, rel: Any, source_name: str = "", target_name: str = ""
    ) -> GraphRelationship:
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

        Symmetric heal-path: for typed writes (``entity.type != 'Unresolved'``),
        after the typed MERGE we look for every sibling row sharing
        ``(name, scope)`` whose type is ``Unresolved`` or ``Topic`` and
        absorb them into the typed row via
        ``apoc.refactor.mergeNodes([typed] + sibs, {properties: 'discard',
        mergeRels: true})``. This collapses parallel rows created by the
        in-Cypher stub MERGE paths in
        :meth:`_upsert_relationship_with_stub_flag` and
        :meth:`batch_create_episodic_links`, regardless of which write
        arrives first. ``mergeRels: true`` also dedupes parallel edges
        (e.g., two ``MENTIONED_IN→Event(X)`` edges collapse into one).
        ``properties: 'discard'`` keeps the typed row's properties
        authoritative; stub flags such as ``awaiting_type`` get dropped.
        """
        now_iso = datetime.now(tz=UTC).isoformat()
        props_json = json.dumps(entity.properties)

        async with self._driver.session() as session:
            if entity.type != "Unresolved":
                # Symmetric heal: MERGE the typed row, then absorb every
                # Unresolved/Topic sibling for the same (name, scope) into
                # it via apoc.refactor.mergeNodes. The CASE guard avoids
                # calling mergeNodes with a single-element list when no
                # stubs exist (APOC emits a noisy log line otherwise).
                if entity.scope == "channel" and entity.channel_id:
                    heal_result = await session.run(
                        """
                        MERGE (typed:Entity {name: $name, type: $type, channel_id: $channel_id})
                          ON CREATE SET
                            typed.scope             = $scope,
                            typed.properties        = $properties,
                            typed.aliases           = $aliases,
                            typed.source_message_id = $source_message_id,
                            typed.message_ts        = $message_ts,
                            typed.status            = $status,
                            typed.pending_since     = $pending_since,
                            typed.created_at        = $now,
                            typed.updated_at        = $now
                          ON MATCH SET
                            typed.scope             = $scope,
                            typed.properties        = $properties,
                            typed.aliases           = $aliases,
                            typed.source_message_id = $source_message_id,
                            typed.message_ts        = $message_ts,
                            typed.updated_at        = $now
                        WITH typed
                        OPTIONAL MATCH (sib:Entity {name: $name, scope: $scope})
                          WHERE sib.type IN ['Unresolved', 'Topic']
                            AND elementId(sib) <> elementId(typed)
                        WITH typed, collect(sib) AS sibs
                        CALL apoc.refactor.mergeNodes(
                          CASE WHEN size(sibs) > 0 THEN [typed] + sibs ELSE [typed] END,
                          {properties: 'discard', mergeRels: true}
                        ) YIELD node
                        RETURN elementId(node) AS eid
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
                        pending_since=entity.pending_since.isoformat()
                        if entity.pending_since
                        else None,
                        now=now_iso,
                    )
                else:
                    heal_result = await session.run(
                        """
                        MERGE (typed:Entity {name: $name, type: $type, scope: 'global'})
                          ON CREATE SET
                            typed.channel_id        = null,
                            typed.properties        = $properties,
                            typed.aliases           = $aliases,
                            typed.source_message_id = $source_message_id,
                            typed.message_ts        = $message_ts,
                            typed.status            = $status,
                            typed.pending_since     = $pending_since,
                            typed.created_at        = $now,
                            typed.updated_at        = $now
                          ON MATCH SET
                            typed.properties        = $properties,
                            typed.aliases           = $aliases,
                            typed.source_message_id = $source_message_id,
                            typed.message_ts        = $message_ts,
                            typed.updated_at        = $now
                        WITH typed
                        OPTIONAL MATCH (sib:Entity {name: $name, scope: 'global'})
                          WHERE sib.type IN ['Unresolved', 'Topic']
                            AND elementId(sib) <> elementId(typed)
                        WITH typed, collect(sib) AS sibs
                        CALL apoc.refactor.mergeNodes(
                          CASE WHEN size(sibs) > 0 THEN [typed] + sibs ELSE [typed] END,
                          {properties: 'discard', mergeRels: true}
                        ) YIELD node
                        RETURN elementId(node) AS eid
                        """,
                        name=entity.name,
                        type=entity.type,
                        properties=props_json,
                        aliases=entity.aliases,
                        source_message_id=entity.source_message_id,
                        message_ts=entity.message_ts,
                        status=entity.status,
                        pending_since=entity.pending_since.isoformat()
                        if entity.pending_since
                        else None,
                        now=now_iso,
                    )
                record = await heal_result.single()
                return record["eid"]  # type: ignore[index]

            # Unresolved write — preserve the legacy stub-creation MERGE so
            # the in-Cypher relationship/episodic-link stub paths keep
            # working. Typed writes will later absorb these via the
            # symmetric heal above.
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
                    pending_since=entity.pending_since.isoformat()
                    if entity.pending_since
                    else None,
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
                    pending_since=entity.pending_since.isoformat()
                    if entity.pending_since
                    else None,
                    now=now_iso,
                )
            record = await result.single()
            return record["eid"]  # type: ignore[index]

    async def batch_upsert_entities(self, entities: list[GraphEntity]) -> list[str]:
        """Upsert multiple entities in parallel. Returns element IDs.

        Issue #37 — concurrent sessions are bounded by
        ``self._BATCH_CONCURRENCY`` (default 16) so a large batch can't
        exhaust the Neo4j driver's connection pool. Per-entity failures
        are tolerated via ``return_exceptions=True``: the failure is
        logged and its slot in the returned list is an empty string,
        sibling entities still persist (matches the existing
        ``batch_upsert_relationships`` pattern).

        Circuit-breaker: when EVERY entity fails (e.g. Neo4j fully
        unreachable), this raises rather than returning an all-empty
        list. Otherwise callers in ``persister.py`` /  ``reconciler.py``
        would call ``mark_intent_neo4j_done`` after a no-op write, and
        the reconciler's next pass would skip the intent — silently
        dropping every entity in the batch. Partial failures (≥1
        success) keep the best-effort behavior.
        """
        if not entities:
            return []
        sem = asyncio.Semaphore(self._BATCH_CONCURRENCY)

        async def _bounded(e: GraphEntity) -> str:
            async with sem:
                return await self.upsert_entity(e)

        results = await asyncio.gather(
            *[_bounded(e) for e in entities],
            return_exceptions=True,
        )
        if all(isinstance(r, BaseException) for r in results):
            first_exc = next(r for r in results if isinstance(r, BaseException))
            raise RuntimeError(
                f"Neo4jStore: all {len(entities)} entity upserts failed; first error: {first_exc!r}"
            ) from first_exc
        ids: list[str] = []
        for entity, res in zip(entities, results):
            if isinstance(res, BaseException):
                logger.warning(
                    "Neo4jStore: entity upsert failed (name=%s): %s",
                    entity.name,
                    res,
                )
                ids.append("")
            else:
                ids.append(res)
        return ids

    # ------------------------------------------------------------------
    # Write — relationships
    # ------------------------------------------------------------------

    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        """MERGE a relationship between two entities using apoc.merge.relationship.

        Returns the relationship element ID, or empty string when either
        endpoint entity does not exist in the graph (legacy MATCH-and-skip
        path only — the MERGE path always returns a relationship).

        Behaviour depends on the ``NEO4J_RELATIONSHIP_STUB_ENDPOINTS`` env
        flag (PR-2):

        * ``true`` (default) — uses ``MERGE`` on both endpoint entities. If
          an endpoint name does not exist as ``(name, 'Unresolved', 'global')``
          a stub Entity is auto-created with ``properties`` containing
          ``"stub": true, "reason": "rel_endpoint", "awaiting_type": true``
          plus a top-level ``awaiting_type=true`` node property. A later
          typed ``upsert_entity`` for the same name promotes the row in
          place via the heal-path (see :meth:`upsert_entity`). The
          composite UNIQUE constraint at ``(name, type, scope)``
          serialises concurrent stub creation under racing batches.
        * ``false`` — legacy ``MATCH`` semantics; relationships referencing
          unknown endpoints are silently skipped and a warning is logged.
        """
        eid, _stub_created = await self._upsert_relationship_with_stub_flag(rel)
        return eid

    async def _upsert_relationship_with_stub_flag(self, rel: GraphRelationship) -> tuple[str, int]:
        """Internal helper — same as :meth:`upsert_relationship` but also
        returns the number of stub endpoint Entity nodes created (0, 1,
        or 2). Used by :meth:`batch_upsert_relationships` to apply the
        fail-closed stub-explosion cap per batch.
        """
        from beever_atlas.infra.config import get_settings

        now_iso = datetime.now(tz=UTC).isoformat()
        use_merge = get_settings().neo4j_relationship_stub_endpoints

        async with self._driver.session() as session:
            if use_merge:
                # PR-2 MERGE path — auto-creates stub Entity nodes for
                # unknown endpoints. Stub creation is detected by comparing
                # node ``created_at`` to ``$now``: stubs created in THIS
                # query have created_at == $now exactly; pre-existing nodes
                # have an older value.
                #
                # Earlier marker-property approach (_created_by_rel_stub +
                # REMOVE) tripped a Neo4j 5+ Cypher syntax error around the
                # WITH/REMOVE/CALL fence. This pure-MERGE form avoids that
                # by computing the count from a property already being set.
                # Stubs are typed ``Unresolved`` (backend-only synthetic
                # type, never emitted by the LLM) plus
                # ``awaiting_type=true`` so a subsequent typed
                # ``upsert_entity`` for the same name can promote the row
                # in place. The flag lives BOTH inside the JSON
                # ``properties`` string (for application code that reads
                # entities back) and as a top-level node property (for
                # Cypher filtering by ``prune_stub_orphans`` and the
                # heal-path). See ``upsert_entity`` heal-path for details.
                stub_props = '{"stub": true, "reason": "rel_endpoint", "awaiting_type": true}'
                result = await session.run(
                    """
                    MERGE (a_raw:Entity {name: $source, type: 'Unresolved', scope: 'global'})
                      ON CREATE SET
                        a_raw.channel_id = null,
                        a_raw.properties = $stub_props,
                        a_raw.aliases    = [],
                        a_raw.status     = 'active',
                        a_raw.awaiting_type = true,
                        a_raw.created_at = $now,
                        a_raw.updated_at = $now
                    WITH a_raw
                    OPTIONAL MATCH (a_typed:Entity {name: $source, scope: 'global'})
                      WHERE NOT a_typed.type IN ['Unresolved', 'Topic']
                        AND elementId(a_typed) <> elementId(a_raw)
                    WITH a_raw, head(collect(a_typed)) AS a_typed_pick
                    CALL apoc.refactor.mergeNodes(
                      CASE WHEN a_typed_pick IS NOT NULL THEN [a_typed_pick, a_raw] ELSE [a_raw] END,
                      {properties: 'discard', mergeRels: true}
                    ) YIELD node AS a
                    MERGE (b_raw:Entity {name: $target, type: 'Unresolved', scope: 'global'})
                      ON CREATE SET
                        b_raw.channel_id = null,
                        b_raw.properties = $stub_props,
                        b_raw.aliases    = [],
                        b_raw.status     = 'active',
                        b_raw.awaiting_type = true,
                        b_raw.created_at = $now,
                        b_raw.updated_at = $now
                    WITH a, b_raw
                    OPTIONAL MATCH (b_typed:Entity {name: $target, scope: 'global'})
                      WHERE NOT b_typed.type IN ['Unresolved', 'Topic']
                        AND elementId(b_typed) <> elementId(b_raw)
                    WITH a, b_raw, head(collect(b_typed)) AS b_typed_pick
                    CALL apoc.refactor.mergeNodes(
                      CASE WHEN b_typed_pick IS NOT NULL THEN [b_typed_pick, b_raw] ELSE [b_raw] END,
                      {properties: 'discard', mergeRels: true}
                    ) YIELD node AS b
                    WITH a, b,
                         (CASE WHEN a.type = 'Unresolved' AND a.created_at = $now THEN 1 ELSE 0 END
                          + CASE WHEN b.type = 'Unresolved' AND b.created_at = $now THEN 1 ELSE 0 END) AS stubs_created
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
                    RETURN elementId(rel) AS eid, stubs_created
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
                    stub_props=stub_props,
                    now=now_iso,
                )
                record = await result.single()
                if record is None:
                    # Should not happen on the MERGE path, but defensive.
                    logger.warning(
                        "Neo4jStore: relationship MERGE returned no row "
                        "(source=%s target=%s type=%s)",
                        rel.source,
                        rel.target,
                        rel.type,
                    )
                    return "", 0
                return record["eid"], int(record["stubs_created"])

            # Legacy MATCH-and-skip path.
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
            if record is None:
                logger.warning(
                    "Neo4jStore: relationship skipped — entity not found (source=%s target=%s type=%s)",
                    rel.source,
                    rel.target,
                    rel.type,
                )
                return "", 0
            return record["eid"], 0

    # Fail-closed cap: a batch creating more than this many stub Entity
    # nodes for unknown relationship endpoints triggers an ERROR log and
    # the ``stub_explosion_detected`` sync_summary metric. Pollution
    # signal — not fatal; the batch still commits. See Task 1, criterion
    # #6 in .omc/plans/pipeline-realign-v2.md.
    _STUB_EXPLOSION_THRESHOLD: int = 50

    async def batch_upsert_relationships(
        self,
        rels: list[GraphRelationship],
        *,
        channel_id: str = "",
        sync_job_id: str = "",
        batch_idx: int | None = None,
    ) -> list[str]:
        """Upsert multiple relationships in parallel.

        Uses return_exceptions=True so one failing relationship does not
        poison the whole batch — the failure is logged, its slot in the
        returned list is an empty string, and sibling relationships still
        persist.

        Issue #37 — concurrent sessions are bounded by
        ``self._BATCH_CONCURRENCY`` (default 16) so a large batch can't
        exhaust the Neo4j driver's connection pool.

        Circuit-breaker: when EVERY relationship fails (e.g. Neo4j fully
        unreachable), this raises rather than returning an all-empty
        list — same rationale as ``batch_upsert_entities``: prevents
        ``mark_intent_neo4j_done`` running after a no-op write and the
        reconciler silently skipping the intent on retry.

        PR-2 stub-explosion cap — when ``NEO4J_RELATIONSHIP_STUB_ENDPOINTS``
        is true, counts the stub Entity nodes auto-created across the
        batch. If the count exceeds :attr:`_STUB_EXPLOSION_THRESHOLD`, an
        ERROR is logged and the ``stub_explosion_detected`` sync_summary
        metric is set (when ``channel_id`` + ``sync_job_id`` are provided).
        The batch still commits — pollution, not fatal.
        """
        if not rels:
            return []
        sem = asyncio.Semaphore(self._BATCH_CONCURRENCY)

        async def _bounded(r: GraphRelationship) -> tuple[str, int]:
            async with sem:
                return await self._upsert_relationship_with_stub_flag(r)

        results = await asyncio.gather(
            *[_bounded(r) for r in rels],
            return_exceptions=True,
        )
        if all(isinstance(r, BaseException) for r in results):
            first_exc = next(r for r in results if isinstance(r, BaseException))
            raise RuntimeError(
                f"Neo4jStore: all {len(rels)} relationship upserts failed; "
                f"first error: {first_exc!r}"
            ) from first_exc
        ids: list[str] = []
        stubs_created = 0
        for rel, res in zip(rels, results):
            if isinstance(res, BaseException):
                logger.warning(
                    "Neo4jStore: relationship upsert failed (source=%s target=%s type=%s): %s",
                    rel.source,
                    rel.target,
                    rel.type,
                    res,
                )
                ids.append("")
            else:
                eid, stub_count = res
                ids.append(eid)
                stubs_created += stub_count

        # Fail-closed cap on stub creation (PR-2 Task 1 criterion #6).
        if stubs_created > self._STUB_EXPLOSION_THRESHOLD:
            # Sample up to 5 (rel_type, source, target) triples for ops triage.
            samples = [(r.type, r.source, r.target) for r in rels[:5]]
            logger.error(
                "Neo4jStore: stub explosion detected — batch created %d stub "
                "Entity nodes (threshold=%d); sample rel_types/endpoints=%s",
                stubs_created,
                self._STUB_EXPLOSION_THRESHOLD,
                samples,
            )
            if channel_id and sync_job_id:
                try:
                    from beever_atlas.services.batch_processor import (
                        increment_sync_metric,
                    )

                    # boolean-as-flag: set value to count for triage; the
                    # >0 read from the metric registry signals "detected".
                    increment_sync_metric(
                        channel_id,
                        sync_job_id,
                        "stub_explosion_detected",
                        stubs_created,
                    )
                except Exception:  # noqa: BLE001 — metrics must never break writes
                    logger.debug(
                        "Neo4jStore: stub_explosion_detected metric increment "
                        "failed (channel=%s job=%s)",
                        channel_id,
                        sync_job_id,
                        exc_info=True,
                    )

        return ids

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

    async def link_entity_to_media(self, entity_name: str, media_url: str) -> None:
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
                "MATCH (ev:Event {channel_id: $channel_id}) DETACH DELETE ev RETURN count(ev) AS n",
                channel_id=channel_id,
            )
            record = await result.single()
            events_deleted = int(record["n"]) if record else 0

            # Delete Media nodes for this channel
            result = await session.run(
                "MATCH (m:Media {channel_id: $channel_id}) DETACH DELETE m RETURN count(m) AS n",
                channel_id=channel_id,
            )
            record = await result.single()
            media_deleted = int(record["n"]) if record else 0

            # Delete channel-scoped entities
            result = await session.run(
                "MATCH (e:Entity {channel_id: $channel_id}) DETACH DELETE e RETURN count(e) AS n",
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

    async def delete_channel_wiki_graph(self, channel_id: str) -> int:
        """Drop ``:WikiPage`` nodes (and their relationships) for a channel.

        Per-channel reset wipes wiki rows in MongoDB; the matching graph
        nodes (with their ``REFERENCES_ENTITY`` / ``REFERENCES`` edges)
        would otherwise linger as dangling references. ``DETACH DELETE``
        removes the page node plus every incident edge in one statement.
        Kept separate from :meth:`delete_channel_data` so that method's
        Entity/Event/Media semantics stay unchanged.
        """
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (w:WikiPage {channel_id: $channel_id}) DETACH DELETE w RETURN count(w) AS n",
                channel_id=channel_id,
            )
            record = await result.single()
            return int(record["n"]) if record else 0

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
            match_clause += (
                f" AND {pending_filter}" if "WHERE" in match_clause else f" WHERE {pending_filter}"
            )

        if entity_type is not None:
            match_clause += (
                " AND e.type = $entity_type"
                if "WHERE" in match_clause
                else " WHERE e.type = $entity_type"
            )
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

    async def get_neighbors(self, entity_id: str, hops: int = 1, limit: int = 50) -> Subgraph:
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
            node_map: dict[str, GraphEntity] = {}
            edges: list[GraphRelationship] = []

            # Iterate RAW records — NOT result.data(). result.data() serialises a
            # Relationship value into a 3-tuple (start_node, "TYPE", end_node),
            # which (a) drops the relationship's own properties
            # (confidence/context/source_fact_id) and (b) crashes
            # _rel_from_record, whose dict(rel) then sees a tuple →
            # "dictionary update sequence element #0 has length 12; 2 is
            # required", so EVERY deep-mode relationship traversal silently
            # returned no edges. Raw records preserve real Node/Relationship
            # objects, which the _from_record helpers already handle.
            async for row in result:
                src = self._entity_from_record(row["src_node"])
                tgt = self._entity_from_record(row["tgt_node"])
                node_map[src.name] = src
                node_map[tgt.name] = tgt
                edges.append(
                    self._rel_from_record(row["rel"], source_name=src.name, target_name=tgt.name)
                )

        return Subgraph(nodes=list(node_map.values()), edges=edges)

    async def list_relationships(
        self,
        channel_id: str | None = None,
        limit: int = 1000,
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
            "r.confidence AS confidence, r.context AS context, "
            "r.valid_from AS valid_from "
            "LIMIT $limit"
        )
        async with self._driver.session() as session:
            result = await session.run(query, **params)
            records = await result.data()
        rels: list[GraphRelationship] = []
        for row in records:
            rels.append(
                GraphRelationship(
                    type=row.get("rel_type", "RELATED_TO"),
                    source=row.get("src", ""),
                    target=row.get("tgt", ""),
                    confidence=float(row.get("confidence") or 0.0),
                    context=row.get("context") or "",
                    valid_from=row.get("valid_from"),
                )
            )
        return rels

    async def list_co_mention_edges(
        self,
        channel_id: str,
        min_shared: int = 2,
        limit: int = 500,
    ) -> list[GraphRelationship]:
        """Return synthetic CO_MENTIONED edges between entity pairs that
        share at least ``min_shared`` ``Event`` nodes in this channel.

        Used by the Memory Graph UI to surface implicit co-occurrence
        between entities when explicit LLM-extracted relationships are
        sparse (common in casual channels). Each pair appears once,
        ordered by ``elementId(a) < elementId(b)`` so the same pair is
        not returned in both directions.

        Confidence is set to ``min(1.0, shared / 5.0)`` so the UI can
        scale opacity / edge weight. Verb is the synthetic constant
        ``CO_MENTIONED``. ``valid_from`` is the most-recent shared event
        timestamp (so the time-window filter still drops stale pairs).
        """
        if not channel_id:
            return []
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (a:Entity)-[:MENTIONED_IN]->(ev:Event {channel_id: $channel_id})
                MATCH (b:Entity)-[:MENTIONED_IN]->(ev)
                WHERE elementId(a) < elementId(b)
                WITH a, b, count(ev) AS shared, max(ev.message_ts) AS most_recent
                WHERE shared >= $min_shared
                RETURN a.name AS src, b.name AS tgt, shared, most_recent
                ORDER BY shared DESC
                LIMIT $limit
                """,
                channel_id=channel_id,
                min_shared=min_shared,
                limit=limit,
            )
            records = await result.data()
        out: list[GraphRelationship] = []
        for row in records:
            shared = int(row.get("shared") or 0)
            confidence = min(1.0, shared / 5.0)
            out.append(
                GraphRelationship(
                    type="CO_MENTIONED",
                    source=row.get("src", ""),
                    target=row.get("tgt", ""),
                    confidence=confidence,
                    context=f"co-mentioned in {shared} events",
                    valid_from=row.get("most_recent"),
                )
            )
        return out

    # ------------------------------------------------------------------
    # Unresolved-classifier helpers (PR-A)
    # ------------------------------------------------------------------

    async def list_unresolved_stubs(
        self,
        channel_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return Unresolved stub entities awaiting type classification.

        Scoping (A-2 fix): when ``channel_id`` is provided, accepts a
        stub that either has ``channel_id`` set directly OR is reachable
        via a ``MENTIONED_IN→Event{channel_id}`` edge — mirrors the
        per-channel pivot in :meth:`list_relationships` so workspace-
        wide stubs without an incident event in the channel are filtered
        out.

        Excludes stubs with ``classifier_attempts >= 2`` (idempotency
        gate — see :meth:`mark_unresolved_attempt`). Caller layers a
        ``classifier_low_confidence_at`` recency check in Python so the
        7-day TTL stays operator-tunable without a redeploy.
        """
        if channel_id is not None:
            where = (
                "WHERE e.type = 'Unresolved' "
                "AND e.awaiting_type = true "
                "AND coalesce(e.classifier_attempts, 0) < 2 "
                "AND (e.channel_id = $channel_id "
                "OR EXISTS { MATCH (e)-[:MENTIONED_IN]->(ev:Event) "
                "WHERE ev.channel_id = $channel_id })"
            )
            params: dict[str, Any] = {"channel_id": channel_id, "limit": limit}
        else:
            where = (
                "WHERE e.type = 'Unresolved' "
                "AND e.awaiting_type = true "
                "AND coalesce(e.classifier_attempts, 0) < 2"
            )
            params = {"limit": limit}
        query = (
            f"MATCH (e:Entity) {where} "  # noqa: S608
            "RETURN e.name AS name, e.scope AS scope, "
            "e.channel_id AS channel_id, "
            "coalesce(e.classifier_attempts, 0) AS attempts, "
            "e.classifier_low_confidence_at AS low_confidence_at "
            "ORDER BY e.created_at ASC "
            "LIMIT $limit"
        )
        async with self._driver.session() as session:
            result = await session.run(query, **params)
            records = await result.data()
        return [
            {
                "name": row.get("name", ""),
                "scope": row.get("scope") or "global",
                "channel_id": row.get("channel_id"),
                "attempts": int(row.get("attempts") or 0),
                "low_confidence_at": row.get("low_confidence_at"),
            }
            for row in records
        ]

    async def fetch_incident_contexts_batch(
        self,
        names: list[str],
        limit_per_name: int = 3,
    ) -> dict[str, list[str]]:
        """Return up to ``limit_per_name`` incident-edge contexts per
        candidate name in a SINGLE Cypher round-trip (A-4 fix).

        Used by the unresolved classifier to gather the disambiguating
        signal for each stub.  Empty/missing contexts are filtered out
        in Cypher so the LLM never sees zero-signal rows.
        """
        if not names:
            return {}
        query = """
        UNWIND $names AS target_name
        CALL (target_name) {
          MATCH (e:Entity {name: target_name})-[r]-(other:Entity)
          WHERE r.context IS NOT NULL AND r.context <> ''
          RETURN r.context AS ctx
          ORDER BY coalesce(r.valid_from, '') DESC
          LIMIT $limit_per_name
        }
        RETURN target_name AS name, collect(ctx) AS contexts
        """
        async with self._driver.session() as session:
            result = await session.run(query, names=names, limit_per_name=limit_per_name)
            records = await result.data()
        return {
            row.get("name", ""): [c for c in (row.get("contexts") or []) if c] for row in records
        }

    async def mark_unresolved_attempt(
        self,
        name: str,
        scope: str,
        channel_id: str | None,
    ) -> None:
        """Bump ``classifier_attempts`` and stamp
        ``classifier_low_confidence_at = now`` on the stub.

        Idempotent — repeated calls just increment. Channel-scoped
        stubs match by ``(name, scope='channel', channel_id)``; global
        stubs match by ``(name, scope='global')``.
        """
        now_iso = datetime.now(tz=UTC).isoformat()
        if scope == "channel" and channel_id:
            query = (
                "MATCH (e:Entity {name: $name, scope: 'channel', "
                "channel_id: $channel_id}) "
                "WHERE e.type = 'Unresolved' "
                "SET e.classifier_attempts = coalesce(e.classifier_attempts, 0) + 1, "
                "e.classifier_low_confidence_at = $now"
            )
            params = {"name": name, "channel_id": channel_id, "now": now_iso}
        else:
            query = (
                "MATCH (e:Entity {name: $name, scope: 'global'}) "
                "WHERE e.type = 'Unresolved' "
                "SET e.classifier_attempts = coalesce(e.classifier_attempts, 0) + 1, "
                "e.classifier_low_confidence_at = $now"
            )
            params = {"name": name, "now": now_iso}
        async with self._driver.session() as session:
            await session.run(query, **params)

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
            rels.append(
                {
                    "source": row.get("src", ""),
                    "target": tgt_name,
                    "type": row.get("rel_type", "REFERENCES_MEDIA"),
                }
            )
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
            media_list.append(
                {
                    "id": getattr(node, "element_id", None) or props.get("url", ""),
                    "url": props.get("url", ""),
                    "media_type": props.get("media_type", ""),
                    "title": props.get("title", ""),
                    "channel_id": props.get("channel_id", ""),
                    "message_ts": props.get("message_ts", ""),
                }
            )
        return media_list

    async def get_decisions(self, channel_id: str, limit: int = 20) -> list[GraphEntity]:
        """Return entities of type 'Decision' visible in a channel."""
        return await self.list_entities(channel_id=channel_id, entity_type="Decision", limit=limit)

    async def list_person_entities_with_edges(
        self,
        channel_id: str,
        limit: int = 100,
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
            persons.append(
                {
                    "entity": entity,
                    "edges": edges,
                }
            )
        return persons

    async def list_technology_entities(
        self,
        channel_id: str,
        limit: int = 100,
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
            techs.append(
                {
                    "entity": entity,
                    "used_by": row.get("used_by", []),
                }
            )
        return techs

    async def list_project_entities(
        self,
        channel_id: str,
        limit: int = 100,
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
            projects.append(
                {
                    "entity": entity,
                    "dependencies": deps,
                    "owners": row.get("owners", []),
                }
            )
        return projects

    async def get_decisions_with_chains(
        self,
        channel_id: str,
        limit: int = 50,
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
            decisions.append(
                {
                    "entity": entity,
                    "decided_by": [n for n in row.get("decided_by", []) if n],
                    "supersedes": [n for n in row.get("supersedes", []) if n],
                    "superseded_by": [n for n in row.get("superseded_by", []) if n],
                }
            )
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

    async def prune_stub_orphans(self, ttl_hours: int = 24) -> int:
        """Delete Unresolved stub entities that have no edges and are older than ``ttl_hours``.

        Stubs are created by :meth:`_upsert_relationship_with_stub_flag`
        and :meth:`batch_create_episodic_links` for unknown endpoint
        names. They are typed ``Unresolved`` or (in legacy syncs) ``Topic``
        with ``status='active'`` (so ``prune_expired_pending`` does not
        catch them). Once a stub has any incident edge it is kept — a
        later typed write absorbs it via the ``upsert_entity`` symmetric
        heal-path. Only truly orphaned Unresolved-or-Topic stubs older
        than the TTL are purged.

        Note: the ``awaiting_type`` flag is intentionally NOT required —
        legacy Topic stubs from pre-fix syncs do not have that flag set.
        The remaining filters (no edges, older than cutoff) are
        sufficient guard rails for edge-less stubs.

        Returns the number purged.
        """
        from datetime import timedelta

        cutoff = (datetime.now(tz=UTC) - timedelta(hours=ttl_hours)).isoformat()
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) "
                "WHERE e.type IN ['Unresolved', 'Topic'] "
                "AND NOT EXISTS { MATCH (e)--() } "
                "AND e.created_at < $cutoff "
                "DETACH DELETE e RETURN count(e) AS n",
                cutoff=cutoff,
            )
            record = await result.single()
        return int(record["n"]) if record else 0

    async def fuzzy_match_entity(self, name: str, threshold: float = 0.8) -> list[GraphEntity]:
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
        """Find an entity by exact name or alias.  Returns canonical name.

        Prefers typed siblings over Unresolved/Topic stubs so that wiki
        citation resolution paths bind to the authoritative row when both
        a typed entity and a stub coexist for the same name.
        """
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (e:Entity) "
                "WHERE e.name = $name OR $name IN coalesce(e.aliases, []) "
                "RETURN e.name AS canonical "
                "ORDER BY CASE WHEN e.type IN ['Unresolved', 'Topic'] THEN 1 ELSE 0 END, "
                "         e.updated_at DESC "
                "LIMIT 1",
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
            {"name": r["name"], "type": r["type"], "aliases": list(r["aliases"])} for r in records
        ]

    async def register_alias(self, canonical: str, alias: str, entity_type: str) -> None:
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
            result = await session.run("MATCH (e:Entity) RETURN e.name AS name")
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
                "MATCH (e:Entity) WHERE e.name_vector IS NULL RETURN e.name AS name"
            )
            records = await result.data()
        return [r["name"] for r in records if r.get("name")]

    async def store_name_vector(self, entity_name: str, vector: list[float]) -> None:
        """Persist a name-embedding vector on an entity node."""
        async with self._driver.session() as session:
            await session.run(
                "MATCH (e:Entity {name: $name}) SET e.name_vector = $vector",
                name=entity_name,
                vector=vector,
            )

    async def batch_store_name_vectors(self, items: list[tuple[str, list[float]]]) -> int:
        """Persist name-embedding vectors for multiple entities in one Cypher call.

        Uses UNWIND + MATCH (not MERGE) — only updates entities that already exist.
        Returns the number of items submitted (not matched, since SET returns no count).
        """
        if not items:
            return 0
        params = [{"name": name, "vector": vector} for name, vector in items]
        async with self._driver.session() as session:
            await session.run(
                "UNWIND $items AS item "
                "MATCH (e:Entity {name: item.name}) "
                "SET e.name_vector = item.vector",
                items=params,
            )
        return len(items)

    # ------------------------------------------------------------------
    # Batch operations (optimised for persister pipeline)
    # ------------------------------------------------------------------

    async def batch_create_episodic_links(self, links: list[dict[str, Any]]) -> int:
        """Create ``:MENTIONED_IN`` edges from Entity to Event in bulk.

        Behaviour depends on the ``NEO4J_RELATIONSHIP_STUB_ENDPOINTS`` env
        flag (PR-2):

        * ``true`` (default) — MERGEs the Entity by
          ``(name, 'Unresolved', 'global')`` so unknown entity_tags from a
          fact (whose owning entity may not have committed yet) still
          link to the Event via a stub Entity. Stubs are tagged
          ``{"stub": true, "reason": "episodic_link", "awaiting_type": true}``.
          A later typed ``upsert_entity`` for the same name promotes
          the row in place via the heal-path.
        * ``false`` — legacy MATCH semantics; links to unknown entity
          names are silently dropped.
        """
        from beever_atlas.infra.config import get_settings

        if not links:
            return 0
        use_merge = get_settings().neo4j_relationship_stub_endpoints
        async with self._driver.session() as session:
            if use_merge:
                # Per link: MERGE the Unresolved stub, then immediately
                # absorb it into any existing typed sibling for the same
                # name via apoc.refactor.mergeNodes — same symmetric-heal
                # pattern as upsert_entity and _upsert_relationship_with_stub_flag.
                # The MENTIONED_IN edge then attaches to the surviving node
                # (typed sibling if present, else the just-created stub),
                # keeping the channel-scoped view consistent.
                result = await session.run(
                    "UNWIND $links AS link "
                    "MERGE (e_raw:Entity {name: link.entity_name, type: 'Unresolved', scope: 'global'}) "
                    "  ON CREATE SET "
                    "    e_raw.channel_id = null, "
                    '    e_raw.properties = \'{"stub": true, "reason": "episodic_link", "awaiting_type": true}\', '
                    "    e_raw.aliases    = [], "
                    "    e_raw.status     = 'active', "
                    "    e_raw.awaiting_type = true, "
                    "    e_raw.created_at = toString(datetime()), "
                    "    e_raw.updated_at = toString(datetime()) "
                    "WITH link, e_raw "
                    "OPTIONAL MATCH (e_typed:Entity {name: link.entity_name, scope: 'global'}) "
                    "  WHERE NOT e_typed.type IN ['Unresolved', 'Topic'] "
                    "    AND elementId(e_typed) <> elementId(e_raw) "
                    "WITH link, e_raw, head(collect(e_typed)) AS e_typed_pick "
                    "CALL apoc.refactor.mergeNodes( "
                    "  CASE WHEN e_typed_pick IS NOT NULL THEN [e_typed_pick, e_raw] ELSE [e_raw] END, "
                    "  {properties: 'discard', mergeRels: true} "
                    ") YIELD node AS e "
                    "MERGE (ep:Event {weaviate_id: link.weaviate_fact_id}) "
                    "  ON CREATE SET ep.message_ts = link.message_ts, ep.channel_id = link.channel_id "
                    "MERGE (e)-[:MENTIONED_IN]->(ep) "
                    "RETURN count(*) AS created",
                    links=links,
                )
            else:
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
                "UNWIND $names AS name MATCH (e:Entity {name: name}) RETURN e.name AS found",
                names=list(names),
            )
            found: set[str] = set()
            async for record in result:
                found.add(record["found"])
            return found

    # ------------------------------------------------------------------
    # wiki-llm-native-redesign — WikiPage nodes + REFERENCES edges
    # ------------------------------------------------------------------

    async def upsert_wiki_page_node(
        self,
        *,
        channel_id: str,
        slug: str,
        kind: str,
        title: str,
        version: int,
        last_updated: datetime,
        target_lang: str = "en",
    ) -> str:
        """MERGE a WikiPage node keyed by ``(channel_id, target_lang, slug)``.

        Returns the node element ID. Idempotent — the maintainer calls
        this on every successful ``apply_update`` so existing nodes
        update their ``kind``/``title``/``version``/``last_updated``
        in place. ``target_lang`` is part of the MERGE key so ``:en``
        and ``:zh-HK`` runs against the same ``page_id`` do not stomp
        each other.
        """
        now_iso = datetime.now(tz=UTC).isoformat()
        last_updated_iso = (
            last_updated.isoformat() if isinstance(last_updated, datetime) else str(last_updated)
        )
        async with self._driver.session() as session:
            result = await session.run(
                """
                MERGE (w:WikiPage {channel_id: $channel_id, target_lang: $target_lang, slug: $slug})
                ON CREATE SET
                    w.kind         = $kind,
                    w.title        = $title,
                    w.version      = $version,
                    w.last_updated = $last_updated,
                    w.created_at   = $now,
                    w.updated_at   = $now
                ON MATCH SET
                    w.kind         = $kind,
                    w.title        = $title,
                    w.version      = $version,
                    w.last_updated = $last_updated,
                    w.updated_at   = $now
                RETURN elementId(w) AS eid
                """,
                channel_id=channel_id,
                target_lang=target_lang,
                slug=slug,
                kind=kind,
                title=title,
                version=version,
                last_updated=last_updated_iso,
                now=now_iso,
            )
            record = await result.single()
            return record["eid"] if record else ""

    async def upsert_wiki_reference_edge(
        self,
        *,
        channel_id: str,
        src_slug: str,
        dst_slug: str,
        target_lang: str = "en",
    ) -> None:
        """MERGE a (:WikiPage)-[:REFERENCES]->(:WikiPage) edge.

        Idempotent. Both endpoints are MERGEd by
        ``(channel_id, target_lang, slug)`` so calling this with a
        destination slug whose node does not yet exist still succeeds —
        Neo4j creates a placeholder node that ``upsert_wiki_page_node``
        will subsequently enrich on the next apply_update against that
        page.
        """
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (src:WikiPage {channel_id: $channel_id, target_lang: $target_lang, slug: $src_slug})
                MERGE (dst:WikiPage {channel_id: $channel_id, target_lang: $target_lang, slug: $dst_slug})
                MERGE (src)-[:REFERENCES]->(dst)
                """,
                channel_id=channel_id,
                target_lang=target_lang,
                src_slug=src_slug,
                dst_slug=dst_slug,
            )

    async def upsert_wiki_reference_entity_edge(
        self,
        *,
        channel_id: str,
        target_lang: str,
        src_slug: str,
        entity_name: str,
    ) -> None:
        """MERGE a (:WikiPage)-[:REFERENCES_ENTITY]->(:Entity) edge.

        Idempotent. The WikiPage is MATCHed by
        ``(channel_id, target_lang, slug)`` — it must already exist
        (the caller invokes :meth:`upsert_wiki_page_node` first). The
        Entity is also MATCHed (not MERGEd) so a wikilink targeting a
        name with no real entity behind it does not manufacture a stub
        — the edge is silently dropped instead. The caller is expected
        to resolve the title via :meth:`find_entity_by_name_or_alias`
        before invoking this method.
        """
        async with self._driver.session() as session:
            await session.run(
                """
                MATCH (src:WikiPage {channel_id: $channel_id, target_lang: $target_lang, slug: $src_slug})
                MATCH (e:Entity {name: $entity_name})
                MERGE (src)-[:REFERENCES_ENTITY]->(e)
                """,
                channel_id=channel_id,
                target_lang=target_lang,
                src_slug=src_slug,
                entity_name=entity_name,
            )

    async def get_wiki_graph(self, channel_id: str) -> dict[str, Any]:
        """Return the channel's wiki graph in Cytoscape.js format.

        Shape:
            {
              "channel_id": str,
              "nodes": [
                {"data": {"id": str, "label": str, "kind": "wiki" | "entity",
                          "page_kind"?: str, "version"?: int, "last_updated"?: str}}
              ],
              "edges": [
                {"data": {"id": str, "source": str, "target": str,
                          "kind": "references_wiki" | "references_entity"}}
              ]
            }

        Includes:
          - Every ``(:WikiPage {channel_id})`` node;
          - Every ``REFERENCES`` edge between two WikiPage nodes in the
            channel;
          - Cross-edges from WikiPage to ``(:Entity)`` nodes that the
            page references via the existing entity-graph wiring (when
            those edges exist).
        """
        out: dict[str, Any] = {
            "channel_id": channel_id,
            "nodes": [],
            "edges": [],
        }
        async with self._driver.session() as session:
            # 1) WikiPage nodes
            page_result = await session.run(
                """
                MATCH (w:WikiPage {channel_id: $channel_id})
                RETURN w.slug AS slug, w.title AS title, w.kind AS kind,
                       w.version AS version, w.last_updated AS last_updated
                """,
                channel_id=channel_id,
            )
            seen_node_ids: set[str] = set()
            async for record in page_result:
                slug = record["slug"]
                if not slug or slug in seen_node_ids:
                    continue
                seen_node_ids.add(slug)
                out["nodes"].append(
                    {
                        "data": {
                            "id": slug,
                            "label": record["title"] or slug,
                            "kind": "wiki",
                            "page_kind": record["kind"] or "topic",
                            "version": record["version"] or 0,
                            "last_updated": record["last_updated"] or "",
                        }
                    }
                )

            # 2) REFERENCES edges between WikiPage nodes in the channel
            edge_result = await session.run(
                """
                MATCH (src:WikiPage {channel_id: $channel_id})
                       -[:REFERENCES]->
                      (dst:WikiPage {channel_id: $channel_id})
                RETURN src.slug AS src_slug, dst.slug AS dst_slug
                """,
                channel_id=channel_id,
            )
            seen_edges: set[tuple[str, str, str]] = set()
            async for record in edge_result:
                src_slug = record["src_slug"]
                dst_slug = record["dst_slug"]
                if not src_slug or not dst_slug:
                    continue
                key = (src_slug, dst_slug, "references_wiki")
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                out["edges"].append(
                    {
                        "data": {
                            "id": f"e:{src_slug}->{dst_slug}",
                            "source": src_slug,
                            "target": dst_slug,
                            "kind": "references_wiki",
                        }
                    }
                )

            # 3) Cross-edges to Entity nodes the page references via the
            #    existing entity-graph wiring. Edge type is intentionally
            #    permissive (any directed edge from a WikiPage to an
            #    Entity counts) so the graph picks up future relation
            #    types without code changes.
            entity_result = await session.run(
                """
                MATCH (w:WikiPage {channel_id: $channel_id})-[]->(e:Entity)
                WHERE e.channel_id = $channel_id OR e.scope = 'global'
                RETURN DISTINCT w.slug AS src_slug, e.name AS entity_name,
                                e.type AS entity_type
                """,
                channel_id=channel_id,
            )
            async for record in entity_result:
                src_slug = record["src_slug"]
                entity_name = record["entity_name"]
                if not src_slug or not entity_name:
                    continue
                # Entity node id is namespaced so it cannot collide with
                # a WikiPage slug ("entity:" prefix is reserved on the
                # wiki side too — see _slug_for_entity in wiki_maintainer).
                entity_id = f"entity:{entity_name}"
                if entity_id not in seen_node_ids:
                    seen_node_ids.add(entity_id)
                    out["nodes"].append(
                        {
                            "data": {
                                "id": entity_id,
                                "label": entity_name,
                                "kind": "entity",
                                "entity_type": record["entity_type"] or "",
                            }
                        }
                    )
                key = (src_slug, entity_id, "references_entity")
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                out["edges"].append(
                    {
                        "data": {
                            "id": f"e:{src_slug}->{entity_id}",
                            "source": src_slug,
                            "target": entity_id,
                            "kind": "references_entity",
                        }
                    }
                )

        return out
