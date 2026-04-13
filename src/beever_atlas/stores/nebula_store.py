"""NebulaGraph async store implementing the GraphStore protocol.

Uses nebula3-python ConnectionPool + Session.  All sync nebula3-python calls
are wrapped with ``asyncio.to_thread`` (one session per thread call).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import jellyfish
from nebula3.Config import Config as NebulaConfig
from nebula3.gclient.net import ConnectionPool

from beever_atlas.models import GraphEntity, GraphRelationship, Subgraph

logger = logging.getLogger(__name__)

# Maximum vertices/edges per single INSERT statement (Nebula practical limit).
_BATCH_CHUNK_SIZE = 500

# Pre-registered edge type vocabulary — created during ensure_schema().
_EDGE_TYPE_VOCABULARY: list[str] = [
    "DECIDED",
    "WORKS_ON",
    "USES",
    "OWNS",
    "BLOCKED_BY",
    "REPORTS_TO",
    "DEPENDS_ON",
    "CREATED",
    "REVIEWED",
    "MERGED",
    "DEPLOYED",
    "SCHEDULED",
    "MENTIONED_IN",
    "REFERENCES_MEDIA",
    "RELATED_TO",
    "HAS_ALIAS",
    "PARTICIPATED_IN",
    "APPROVED",
    "REJECTED",
    "ASSIGNED_TO",
    "CONTRIBUTED_TO",
]

# Properties shared by all edge types.
_EDGE_PROPS = (
    "confidence double DEFAULT 0.0, "
    "valid_from string DEFAULT '', "
    "valid_until string DEFAULT '', "
    "context string DEFAULT '', "
    "source_message_id string DEFAULT '', "
    "source_fact_id string DEFAULT '', "
    "created_at string DEFAULT ''"
)


def _vid(name: str, vtype: str, scope: str, channel_id: str | None) -> str:
    """Deterministic vertex ID: SHA-256 of the composite key, truncated to 128 chars."""
    seed = f"{name}:{vtype}:{scope}:{channel_id or ''}"
    return hashlib.sha256(seed.encode()).hexdigest()[:128]


def _media_vid(url: str) -> str:
    """Deterministic VID for a Media node."""
    return hashlib.sha256(f"media:{url}".encode()).hexdigest()[:128]


def _event_vid(weaviate_id: str) -> str:
    """Deterministic VID for an Event node."""
    return hashlib.sha256(f"event:{weaviate_id}".encode()).hexdigest()[:128]


def _escape(value: str) -> str:
    """Escape a string for nGQL.  Nebula uses backslash escaping inside
    double-quoted strings."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _quote(value: str) -> str:
    """Return a double-quoted, escaped nGQL string literal."""
    return f'"{_escape(value)}"'


class NebulaStore:
    """NebulaGraph backend implementing :class:`GraphStore`."""

    def __init__(
        self,
        hosts: str,
        user: str,
        password: str,
        space: str,
    ) -> None:
        self._hosts_str = hosts
        self._user = user
        self._password = password
        self._space = space

        self._pool: ConnectionPool | None = None
        self._session: Any | None = None  # persistent session with USE <space>
        self._registered_edge_types: set[str] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            raise RuntimeError("NebulaStore not started — call startup() first")
        return self._pool

    def _execute_sync(self, ngql: str) -> Any:
        """Execute an nGQL statement synchronously in a fresh session.

        Callers should wrap this with ``asyncio.to_thread``.
        """
        pool = self._get_pool()
        session = pool.get_session(self._user, self._password)
        try:
            resp = session.execute(ngql)
            if not resp.is_succeeded():
                raise RuntimeError(f"nGQL error: {resp.error_msg()} | query: {ngql[:300]}")
            return resp
        finally:
            session.release()

    def _execute_on_session_sync(self, ngql: str) -> Any:
        """Execute on the persistent session (which already has USE <space>)."""
        if self._session is None:
            raise RuntimeError("Persistent session not initialized")
        resp = self._session.execute(ngql)
        if not resp.is_succeeded():
            raise RuntimeError(f"nGQL error: {resp.error_msg()} | query: {ngql[:300]}")
        return resp

    async def _execute(self, ngql: str) -> Any:
        """Execute an nGQL statement asynchronously (fresh session, no space)."""
        return await asyncio.to_thread(self._execute_sync, ngql)

    async def _execute_with_space(self, ngql: str, *, retries: int = 5) -> Any:
        """Execute on the persistent session that has USE <space> set.

        Falls back to USE prefix with retry if persistent session isn't ready.
        If the persistent session fails with a retryable error (SpaceNotFound,
        session gone, etc.), it is invalidated so subsequent retries use the
        fresh-session fallback path which is self-contained.  After a
        successful fallback recovery, the persistent session is re-initialized
        so future calls return to the fast path.
        """
        session_was_invalidated = False
        result: Any = None
        async with self._lock:
            for attempt in range(retries + 1):
                try:
                    if self._session is not None:
                        return await asyncio.to_thread(self._execute_on_session_sync, ngql)
                    else:
                        # Fallback: no persistent session yet (during ensure_schema)
                        # or persistent session was invalidated by a prior retry.
                        result = await asyncio.to_thread(
                            self._execute_sync, f"USE `{_escape(self._space)}`; {ngql}"
                        )
                        break  # success — exit retry loop
                except RuntimeError as exc:
                    err = str(exc)
                    retryable = (
                        "SpaceNotFound" in err
                        or "No schema found" in err
                        or "Session not existed" in err
                        or "connection is lost" in err
                    )
                    if retryable and attempt < retries:
                        # Invalidate the persistent session so the next retry
                        # uses a fresh session with an explicit USE prefix.
                        # This avoids retrying the same broken session state.
                        if self._session is not None:
                            logger.warning(
                                "Invalidating persistent session after retryable error: %s",
                                err.split("|")[0].strip(),
                            )
                            try:
                                self._session.release()
                            except Exception:
                                pass
                            self._session = None
                            session_was_invalidated = True
                        wait = 5.0
                        logger.warning("Nebula not ready (%s), retrying in %.0fs (attempt %d/%d)...",
                                       err.split("|")[0].strip(), wait, attempt + 1, retries)
                        await asyncio.sleep(wait)
                        continue
                    raise

        # If we recovered via the fallback path after invalidating the
        # persistent session, re-initialize it for future calls.
        if session_was_invalidated and self._session is None:
            try:
                await self._init_persistent_session()
            except Exception:
                logger.warning("Could not re-init persistent session; will keep using fallback path")

        return result

    async def _init_persistent_session(self) -> None:
        """Create a persistent session with USE <space> pre-configured.

        Called after ensure_schema when the space is confirmed ready.
        """
        def _init() -> Any:
            pool = self._get_pool()
            session = pool.get_session(self._user, self._password)
            resp = session.execute(f"USE `{_escape(self._space)}`")
            if not resp.is_succeeded():
                session.release()
                raise RuntimeError(f"USE {self._space} failed: {resp.error_msg()}")
            return session
        self._session = await asyncio.to_thread(_init)
        logger.info("Persistent session initialized for space '%s'", self._space)

    def _entity_from_row(self, row: dict[str, Any]) -> GraphEntity:
        """Construct a GraphEntity from a Nebula result row."""
        raw_properties = row.get("properties", "{}")
        if isinstance(raw_properties, str):
            try:
                parsed_properties: dict[str, Any] = json.loads(raw_properties)
            except (json.JSONDecodeError, ValueError):
                parsed_properties = {}
        else:
            parsed_properties = raw_properties or {}

        raw_aliases = row.get("aliases", "[]")
        if isinstance(raw_aliases, str):
            try:
                aliases = json.loads(raw_aliases)
            except (json.JSONDecodeError, ValueError):
                aliases = []
        else:
            aliases = list(raw_aliases) if raw_aliases else []

        def _parse_dt(val: Any) -> datetime:
            if val is None or val == "":
                return datetime.now(tz=UTC)
            if isinstance(val, datetime):
                return val if val.tzinfo else val.replace(tzinfo=UTC)
            return datetime.fromisoformat(str(val)).replace(tzinfo=UTC)

        return GraphEntity(
            id=row.get("vid", ""),
            name=row.get("name", ""),
            type=row.get("type", ""),
            scope=row.get("scope", "global"),
            channel_id=row.get("channel_id") or None,
            properties=parsed_properties,
            aliases=aliases,
            status=row.get("status", "active"),
            pending_since=_parse_dt(row.get("pending_since")) if row.get("pending_since") else None,
            source_fact_ids=[],
            source_message_id=row.get("source_message_id", ""),
            message_ts=row.get("message_ts", ""),
            created_at=_parse_dt(row.get("created_at")),
            updated_at=_parse_dt(row.get("updated_at")),
        )

    def _parse_result_to_dicts(self, resp: Any) -> list[dict[str, Any]]:
        """Convert a nebula3-python ResultSet into a list of plain dicts."""
        if resp is None or not resp.is_succeeded():
            return []
        rows: list[dict[str, Any]] = []
        col_names = resp.keys()
        for i in range(resp.row_size()):
            row: dict[str, Any] = {}
            for col in col_names:
                val_wrapper = resp.row_values(i)[col_names.index(col)]
                row[col] = self._unwrap_value(val_wrapper)
            rows.append(row)
        return rows

    @staticmethod
    def _unwrap_value(val: Any) -> Any:
        """Unwrap a nebula3-python ValueWrapper to a Python primitive."""
        if val.is_empty() or val.is_null():
            return None
        if val.is_string():
            return val.as_string()
        if val.is_int():
            return val.as_int()
        if val.is_double():
            return val.as_double()
        if val.is_bool():
            return val.as_bool()
        if val.is_list():
            return [NebulaStore._unwrap_value(v) for v in val.as_list()]
        if val.is_map():
            return {k: NebulaStore._unwrap_value(v) for k, v in val.as_map().items()}
        if val.is_vertex():
            return str(val.as_node().get_id().as_string())
        if val.is_edge():
            return str(val.as_relationship())
        if val.is_path():
            return str(val.as_path())
        # Fallback
        return str(val)

    # ------------------------------------------------------------------
    # Schema propagation helper
    # ------------------------------------------------------------------

    async def _wait_for_schema(self, check_ngql: str, max_retries: int = 6) -> None:
        """Retry *check_ngql* with exponential backoff until it succeeds.

        Nebula schema DDL is asynchronous; newly created TAGs/EDGEs may not
        be visible immediately.  Default waits up to ~30s.
        """
        wait = 3.0
        for attempt in range(max_retries):
            try:
                await self._execute_with_space(check_ngql)
                return
            except RuntimeError:
                if attempt == max_retries - 1:
                    raise
                logger.warning(
                    "Schema not yet propagated (attempt %d/%d), waiting %.1fs…",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                await asyncio.sleep(wait)
                wait = min(wait * 2, 10.0)

    async def _ensure_edge_type(self, edge_type: str) -> None:
        """Register a dynamic edge type if not already known."""
        if edge_type in self._registered_edge_types:
            return

        logger.warning(
            "Dynamically creating edge type %r on hot path — "
            "consider adding it to the vocabulary list.",
            edge_type,
        )
        safe_type = _escape(edge_type)
        await self._execute_with_space(
            f"CREATE EDGE IF NOT EXISTS `{safe_type}` ({_EDGE_PROPS})"
        )
        # Backoff: wait for schema propagation
        wait = 5.0
        for attempt in range(2):
            try:
                await self._execute_with_space(f"DESCRIBE EDGE `{safe_type}`")
                break
            except RuntimeError:
                if attempt == 1:
                    raise
                logger.warning(
                    "Dynamic edge %r not propagated yet, waiting %.1fs…",
                    edge_type,
                    wait,
                )
                await asyncio.sleep(wait)
                wait = min(wait * 2, 10.0)

        self._registered_edge_types.add(edge_type)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Initialize the ConnectionPool and verify connectivity."""
        config = NebulaConfig()
        config.max_connection_pool_size = 10

        host_list: list[tuple[str, int]] = []
        for entry in self._hosts_str.split(","):
            entry = entry.strip()
            if ":" in entry:
                host, port_str = entry.rsplit(":", 1)
                host_list.append((host, int(port_str)))
            else:
                host_list.append((entry, 9669))

        pool = ConnectionPool()
        ok = await asyncio.to_thread(pool.init, host_list, config)
        if not ok:
            raise RuntimeError(f"Failed to connect to NebulaGraph at {self._hosts_str}")
        self._pool = pool
        logger.info("NebulaStore connected to %s", self._hosts_str)

    async def shutdown(self) -> None:
        """Release persistent session and close the ConnectionPool."""
        if self._session is not None:
            try:
                await asyncio.to_thread(self._session.release)
            except Exception:
                pass
            self._session = None
        if self._pool is not None:
            await asyncio.to_thread(self._pool.close)
            self._pool = None
            logger.info("NebulaStore connection pool closed")

    async def ensure_schema(self) -> None:
        """Create the graph space, tags, and edge types.  Idempotent."""
        space = _escape(self._space)

        # CREATE SPACE
        await self._execute(
            f"CREATE SPACE IF NOT EXISTS `{space}` "
            f"(vid_type=FIXED_STRING(128), partition_num=10, replica_factor=1)"
        )
        # Wait for space to propagate — NebulaGraph needs ~2 heartbeat cycles
        # (default 10s each = ~20-25s) for storage partition assignment after
        # CREATE SPACE.  We poll every 5s for up to 60s.
        for attempt in range(12):
            await asyncio.sleep(5.0)
            try:
                await self._execute(f"USE `{space}`; SHOW TAGS")
                logger.info("Space '%s' ready after ~%ds", self._space, (attempt + 1) * 5)
                break
            except RuntimeError:
                logger.info("Waiting for space '%s' partition assignment (attempt %d/12)...", self._space, attempt + 1)
        else:
            raise RuntimeError(
                f"Space '{self._space}' did not become ready after 60s. "
                f"Check NebulaGraph storaged status with SHOW HOSTS."
            )

        # Entity tag
        await self._execute_with_space(
            "CREATE TAG IF NOT EXISTS Entity ("
            "  name string DEFAULT '', "
            "  type string DEFAULT '', "
            "  scope string DEFAULT 'global', "
            "  channel_id string DEFAULT '', "
            "  properties string DEFAULT '{}', "
            "  aliases string DEFAULT '[]', "
            "  status string DEFAULT 'active', "
            "  pending_since string DEFAULT '', "
            "  source_message_id string DEFAULT '', "
            "  message_ts string DEFAULT '', "
            "  name_vector string DEFAULT '', "
            "  created_at string DEFAULT '', "
            "  updated_at string DEFAULT ''"
            ")"
        )

        # Event tag
        await self._execute_with_space(
            "CREATE TAG IF NOT EXISTS Event ("
            "  weaviate_id string DEFAULT '', "
            "  message_ts string DEFAULT '', "
            "  channel_id string DEFAULT '', "
            "  media_urls string DEFAULT '[]', "
            "  link_urls string DEFAULT '[]'"
            ")"
        )

        # Media tag
        await self._execute_with_space(
            "CREATE TAG IF NOT EXISTS Media ("
            "  url string DEFAULT '', "
            "  media_type string DEFAULT '', "
            "  title string DEFAULT '', "
            "  channel_id string DEFAULT '', "
            "  message_ts string DEFAULT ''"
            ")"
        )

        # Edge types from vocabulary
        for etype in _EDGE_TYPE_VOCABULARY:
            safe = _escape(etype)
            await self._execute_with_space(
                f"CREATE EDGE IF NOT EXISTS `{safe}` ({_EDGE_PROPS})"
            )

        # Wait for schema propagation, then verify
        await self._wait_for_schema("DESCRIBE TAG Entity")
        await self._wait_for_schema("DESCRIBE TAG Event")
        await self._wait_for_schema("DESCRIBE TAG Media")

        self._registered_edge_types = set(_EDGE_TYPE_VOCABULARY)

        # Create indexes for common lookups
        _indexes = [
            "CREATE TAG INDEX IF NOT EXISTS idx_entity_name ON Entity(name(128))",
            "CREATE TAG INDEX IF NOT EXISTS idx_entity_type ON Entity(type(64))",
            "CREATE TAG INDEX IF NOT EXISTS idx_entity_channel ON Entity(channel_id(128))",
            "CREATE TAG INDEX IF NOT EXISTS idx_entity_status ON Entity(status(16))",
            "CREATE TAG INDEX IF NOT EXISTS idx_event_weaviate ON Event(weaviate_id(128))",
            "CREATE TAG INDEX IF NOT EXISTS idx_event_channel ON Event(channel_id(128))",
            "CREATE TAG INDEX IF NOT EXISTS idx_media_url ON Media(url(256))",
            "CREATE TAG INDEX IF NOT EXISTS idx_media_channel ON Media(channel_id(128))",
        ]
        for stmt in _indexes:
            await self._execute_with_space(stmt)

        # Rebuild indexes — NebulaGraph DDL is asynchronous, so CREATE INDEX
        # may not have propagated to storaged yet.  We poll until the first
        # index is visible in SHOW TAG INDEXES output, then rebuild all.
        _index_names = [
            "idx_entity_name", "idx_entity_type", "idx_entity_channel",
            "idx_entity_status", "idx_event_weaviate", "idx_event_channel",
            "idx_media_url", "idx_media_channel",
        ]

        # Wait for indexes to propagate (poll up to ~60s).
        for wait_attempt in range(12):
            try:
                await self._execute_with_space(f"REBUILD TAG INDEX {_index_names[0]}")
                # First index rebuild succeeded — propagation is done.
                remaining = _index_names[1:]
                break
            except RuntimeError:
                logger.info(
                    "Waiting for index propagation (attempt %d/12)...",
                    wait_attempt + 1,
                )
                await asyncio.sleep(5.0)
        else:
            logger.warning(
                "Index propagation timed out — skipping REBUILD (will retry next startup)"
            )
            remaining = []

        for idx_name in remaining:
            try:
                await self._execute_with_space(f"REBUILD TAG INDEX {idx_name}")
            except RuntimeError as exc:
                logger.warning("Index %s rebuild skipped: %s", idx_name, exc)

        # Final stabilization — verify INSERT works (NebulaGraph partition
        # assignment can lag behind DDL success by 10-20s).
        sentinel_vid = "__nebula_schema_probe__"
        for probe_attempt in range(6):
            try:
                await self._execute_with_space(
                    f'INSERT VERTEX IF NOT EXISTS Entity (name) VALUES "{sentinel_vid}":("{sentinel_vid}")'
                )
                # Clean up probe vertex
                await self._execute_with_space(
                    f'DELETE VERTEX "{sentinel_vid}" WITH EDGE'
                )
                break
            except RuntimeError:
                logger.info("Schema probe INSERT failed, waiting 5s (attempt %d/6)...", probe_attempt + 1)
                await asyncio.sleep(5.0)
        else:
            logger.warning("Schema probe never succeeded — space may not be fully operational yet")

        # Initialize persistent session now that the space is confirmed ready.
        # This avoids per-call USE <space> and sidesteps propagation edge cases.
        await self._init_persistent_session()

        logger.info(
            "NebulaStore schema ensured for space %r (%d edge types registered)",
            self._space,
            len(self._registered_edge_types),
        )

    # ------------------------------------------------------------------
    # Entity CRUD
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: GraphEntity) -> str:
        vid = _vid(entity.name, entity.type, entity.scope, entity.channel_id)
        now_iso = datetime.now(tz=UTC).isoformat()
        props_json = json.dumps(entity.properties)
        aliases_json = json.dumps(entity.aliases)
        pending_since = entity.pending_since.isoformat() if entity.pending_since else ""

        await self._execute_with_space(
            f"INSERT VERTEX IF NOT EXISTS Entity ("
            f"name, type, scope, channel_id, properties, aliases, status, "
            f"pending_since, source_message_id, message_ts, created_at, updated_at"
            f") VALUES {_quote(vid)}:("
            f"{_quote(entity.name)}, {_quote(entity.type)}, {_quote(entity.scope)}, "
            f"{_quote(entity.channel_id or '')}, {_quote(props_json)}, "
            f"{_quote(aliases_json)}, {_quote(entity.status)}, "
            f"{_quote(pending_since)}, {_quote(entity.source_message_id)}, "
            f"{_quote(entity.message_ts)}, {_quote(now_iso)}, {_quote(now_iso)})"
        )

        # UPDATE for the ON MATCH path (idempotent upsert)
        await self._execute_with_space(
            f'UPDATE VERTEX ON Entity {_quote(vid)} SET '
            f'properties = {_quote(props_json)}, '
            f'aliases = {_quote(aliases_json)}, '
            f'source_message_id = {_quote(entity.source_message_id)}, '
            f'message_ts = {_quote(entity.message_ts)}, '
            f'updated_at = {_quote(now_iso)}'
        )

        return vid

    async def batch_upsert_entities(self, entities: list[GraphEntity]) -> list[str]:
        if not entities:
            return []

        now_iso = datetime.now(tz=UTC).isoformat()
        vids: list[str] = []

        for chunk_start in range(0, len(entities), _BATCH_CHUNK_SIZE):
            chunk = entities[chunk_start : chunk_start + _BATCH_CHUNK_SIZE]
            values_parts: list[str] = []

            for entity in chunk:
                vid = _vid(entity.name, entity.type, entity.scope, entity.channel_id)
                vids.append(vid)
                props_json = json.dumps(entity.properties)
                aliases_json = json.dumps(entity.aliases)
                pending_since = entity.pending_since.isoformat() if entity.pending_since else ""

                values_parts.append(
                    f'{_quote(vid)}:('
                    f'{_quote(entity.name)}, {_quote(entity.type)}, {_quote(entity.scope)}, '
                    f'{_quote(entity.channel_id or "")}, {_quote(props_json)}, '
                    f'{_quote(aliases_json)}, {_quote(entity.status)}, '
                    f'{_quote(pending_since)}, {_quote(entity.source_message_id)}, '
                    f'{_quote(entity.message_ts)}, {_quote(now_iso)}, {_quote(now_iso)})'
                )

            values_str = ", ".join(values_parts)
            await self._execute_with_space(
                f"INSERT VERTEX IF NOT EXISTS Entity ("
                f"name, type, scope, channel_id, properties, aliases, status, "
                f"pending_since, source_message_id, message_ts, created_at, updated_at"
                f") VALUES {values_str}"
            )

            # Batch UPDATE for existing vertices
            for entity in chunk:
                vid = _vid(entity.name, entity.type, entity.scope, entity.channel_id)
                props_json = json.dumps(entity.properties)
                aliases_json = json.dumps(entity.aliases)
                await self._execute_with_space(
                    f'UPDATE VERTEX ON Entity {_quote(vid)} SET '
                    f'properties = {_quote(props_json)}, '
                    f'aliases = {_quote(aliases_json)}, '
                    f'source_message_id = {_quote(entity.source_message_id)}, '
                    f'message_ts = {_quote(entity.message_ts)}, '
                    f'updated_at = {_quote(now_iso)}'
                )

        return vids

    async def get_entity(self, entity_id: str) -> GraphEntity | None:
        resp = await self._execute_with_space(
            f"FETCH PROP ON Entity {_quote(entity_id)} "
            f"YIELD id(vertex) AS vid, "
            f"properties(vertex).name AS name, "
            f"properties(vertex).type AS type, "
            f"properties(vertex).scope AS scope, "
            f"properties(vertex).channel_id AS channel_id, "
            f"properties(vertex).properties AS properties, "
            f"properties(vertex).aliases AS aliases, "
            f"properties(vertex).status AS status, "
            f"properties(vertex).pending_since AS pending_since, "
            f"properties(vertex).source_message_id AS source_message_id, "
            f"properties(vertex).message_ts AS message_ts, "
            f"properties(vertex).created_at AS created_at, "
            f"properties(vertex).updated_at AS updated_at"
        )
        rows = self._parse_result_to_dicts(resp)
        if not rows:
            return None
        return self._entity_from_row(rows[0])

    async def find_entity_by_name(self, name: str) -> GraphEntity | None:
        resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.name == {_quote(name)} "
            f"YIELD id(vertex) AS vid, "
            f"properties(vertex).name AS name, "
            f"properties(vertex).type AS type, "
            f"properties(vertex).scope AS scope, "
            f"properties(vertex).channel_id AS channel_id, "
            f"properties(vertex).properties AS properties, "
            f"properties(vertex).aliases AS aliases, "
            f"properties(vertex).status AS status, "
            f"properties(vertex).pending_since AS pending_since, "
            f"properties(vertex).source_message_id AS source_message_id, "
            f"properties(vertex).message_ts AS message_ts, "
            f"properties(vertex).created_at AS created_at, "
            f"properties(vertex).updated_at AS updated_at "
            f"| LIMIT 1"
        )
        rows = self._parse_result_to_dicts(resp)
        if not rows:
            return None
        return self._entity_from_row(rows[0])

    async def list_entities(
        self,
        channel_id: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
        include_pending: bool = False,
    ) -> list[GraphEntity]:
        # Build WHERE conditions for LOOKUP
        conditions: list[str] = []
        if channel_id is not None:
            conditions.append(f"Entity.channel_id == {_quote(channel_id)}")
        if entity_type is not None:
            conditions.append(f"Entity.type == {_quote(entity_type)}")
        if not include_pending:
            conditions.append(f"Entity.status == \"active\"")

        if conditions:
            where = " AND ".join(conditions)
            query = f"LOOKUP ON Entity WHERE {where} "
        else:
            query = "LOOKUP ON Entity "

        query += (
            "YIELD id(vertex) AS vid, "
            "properties(vertex).name AS name, "
            "properties(vertex).type AS type, "
            "properties(vertex).scope AS scope, "
            "properties(vertex).channel_id AS channel_id, "
            "properties(vertex).properties AS properties, "
            "properties(vertex).aliases AS aliases, "
            "properties(vertex).status AS status, "
            "properties(vertex).pending_since AS pending_since, "
            "properties(vertex).source_message_id AS source_message_id, "
            "properties(vertex).message_ts AS message_ts, "
            "properties(vertex).created_at AS created_at, "
            "properties(vertex).updated_at AS updated_at "
            f"| LIMIT {limit}"
        )

        # If channel_id is provided, also find entities linked via episodic events.
        # Nebula doesn't support EXISTS subqueries like Neo4j, so we do a second
        # query and merge results.
        resp = await self._execute_with_space(query)
        rows = self._parse_result_to_dicts(resp)
        entities_by_vid: dict[str, GraphEntity] = {}
        for row in rows:
            e = self._entity_from_row(row)
            entities_by_vid[e.id] = e

        if channel_id is not None:
            # Find entities with episodic links to events in this channel
            episodic_resp = await self._execute_with_space(
                f"LOOKUP ON Event WHERE Event.channel_id == {_quote(channel_id)} "
                f"YIELD id(vertex) AS ev_vid "
                f"| GO FROM $-.ev_vid OVER `MENTIONED_IN` REVERSELY "
                f"YIELD id($^) AS ev_vid, id($$) AS entity_vid "
                f"| YIELD DISTINCT $-.entity_vid AS vid"
            )
            episodic_rows = self._parse_result_to_dicts(episodic_resp)
            if episodic_rows:
                ep_vids = [_quote(str(r["vid"])) for r in episodic_rows if r.get("vid")]
                if ep_vids:
                    vids_str = ", ".join(ep_vids)
                    fetch_resp = await self._execute_with_space(
                        f"FETCH PROP ON Entity {vids_str} "
                        f"YIELD id(vertex) AS vid, "
                        f"properties(vertex).name AS name, "
                        f"properties(vertex).type AS type, "
                        f"properties(vertex).scope AS scope, "
                        f"properties(vertex).channel_id AS channel_id, "
                        f"properties(vertex).properties AS properties, "
                        f"properties(vertex).aliases AS aliases, "
                        f"properties(vertex).status AS status, "
                        f"properties(vertex).pending_since AS pending_since, "
                        f"properties(vertex).source_message_id AS source_message_id, "
                        f"properties(vertex).message_ts AS message_ts, "
                        f"properties(vertex).created_at AS created_at, "
                        f"properties(vertex).updated_at AS updated_at"
                    )
                    for row in self._parse_result_to_dicts(fetch_resp):
                        e = self._entity_from_row(row)
                        if not include_pending and e.status != "active":
                            continue
                        entities_by_vid.setdefault(e.id, e)

        result = list(entities_by_vid.values())
        return result[:limit]

    async def count_entities(self, channel_id: str | None = None) -> int:
        entities = await self.list_entities(channel_id=channel_id, limit=1_000_000)
        return len(entities)

    async def promote_pending_entity(self, entity_name: str) -> None:
        resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.name == {_quote(entity_name)} "
            f"AND Entity.status == \"pending\" "
            f"YIELD id(vertex) AS vid"
        )
        rows = self._parse_result_to_dicts(resp)
        for row in rows:
            vid = row["vid"]
            await self._execute_with_space(
                f'UPDATE VERTEX ON Entity {_quote(str(vid))} SET '
                f'status = "active", pending_since = ""'
            )

    async def prune_expired_pending(self, grace_period_days: int = 7) -> int:
        cutoff = (datetime.now(tz=UTC) - timedelta(days=grace_period_days)).isoformat()
        resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.status == \"pending\" "
            f"YIELD id(vertex) AS vid, "
            f"properties(vertex).pending_since AS pending_since"
        )
        rows = self._parse_result_to_dicts(resp)
        pruned = 0
        for row in rows:
            ps = row.get("pending_since", "")
            if ps and ps < cutoff:
                vid = row["vid"]
                await self._execute_with_space(
                    f"DELETE VERTEX {_quote(str(vid))} WITH EDGE"
                )
                pruned += 1
        return pruned

    # ------------------------------------------------------------------
    # Relationship CRUD
    # ------------------------------------------------------------------

    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        await self._ensure_edge_type(rel.type)
        now_iso = datetime.now(tz=UTC).isoformat()

        # Find source and target VIDs by name
        src_resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.name == {_quote(rel.source)} "
            f"YIELD id(vertex) AS vid | LIMIT 1"
        )
        tgt_resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.name == {_quote(rel.target)} "
            f"YIELD id(vertex) AS vid | LIMIT 1"
        )

        src_rows = self._parse_result_to_dicts(src_resp)
        tgt_rows = self._parse_result_to_dicts(tgt_resp)

        if not src_rows or not tgt_rows:
            logger.warning(
                "Cannot create relationship %s -> %s: entity not found",
                rel.source,
                rel.target,
            )
            return ""

        src_vid = str(src_rows[0]["vid"])
        tgt_vid = str(tgt_rows[0]["vid"])
        safe_type = _escape(rel.type)

        await self._execute_with_space(
            f"INSERT EDGE `{safe_type}` ("
            f"confidence, valid_from, valid_until, context, "
            f"source_message_id, source_fact_id, created_at"
            f") VALUES {_quote(src_vid)} -> {_quote(tgt_vid)}:("
            f"{rel.confidence}, {_quote(rel.valid_from or '')}, "
            f"{_quote(rel.valid_until or '')}, {_quote(rel.context)}, "
            f"{_quote(rel.source_message_id)}, {_quote(rel.source_fact_id)}, "
            f"{_quote(now_iso)})"
        )

        # Return a composite edge ID
        return f"{src_vid}->{rel.type}->{tgt_vid}"

    async def batch_upsert_relationships(
        self, rels: list[GraphRelationship]
    ) -> list[str]:
        if not rels:
            return []

        # Ensure all edge types exist first
        unique_types = {r.type for r in rels}
        for etype in unique_types:
            await self._ensure_edge_type(etype)

        # Build a name -> VID cache
        all_names = {r.source for r in rels} | {r.target for r in rels}
        name_to_vid: dict[str, str] = {}
        for name in all_names:
            resp = await self._execute_with_space(
                f"LOOKUP ON Entity WHERE Entity.name == {_quote(name)} "
                f"YIELD id(vertex) AS vid | LIMIT 1"
            )
            rows = self._parse_result_to_dicts(resp)
            if rows:
                name_to_vid[name] = str(rows[0]["vid"])

        now_iso = datetime.now(tz=UTC).isoformat()
        ids: list[str] = []

        # Group by edge type for batched inserts
        by_type: dict[str, list[GraphRelationship]] = {}
        for rel in rels:
            by_type.setdefault(rel.type, []).append(rel)

        for etype, typed_rels in by_type.items():
            safe_type = _escape(etype)
            for chunk_start in range(0, len(typed_rels), _BATCH_CHUNK_SIZE):
                chunk = typed_rels[chunk_start : chunk_start + _BATCH_CHUNK_SIZE]
                values_parts: list[str] = []

                for rel in chunk:
                    src_vid = name_to_vid.get(rel.source)
                    tgt_vid = name_to_vid.get(rel.target)
                    if not src_vid or not tgt_vid:
                        ids.append("")
                        continue

                    values_parts.append(
                        f'{_quote(src_vid)} -> {_quote(tgt_vid)}:('
                        f'{rel.confidence}, {_quote(rel.valid_from or "")}, '
                        f'{_quote(rel.valid_until or "")}, {_quote(rel.context)}, '
                        f'{_quote(rel.source_message_id)}, {_quote(rel.source_fact_id)}, '
                        f'{_quote(now_iso)})'
                    )
                    ids.append(f"{src_vid}->{etype}->{tgt_vid}")

                if values_parts:
                    values_str = ", ".join(values_parts)
                    await self._execute_with_space(
                        f"INSERT EDGE `{safe_type}` ("
                        f"confidence, valid_from, valid_until, context, "
                        f"source_message_id, source_fact_id, created_at"
                        f") VALUES {values_str}"
                    )

        return ids

    async def list_relationships(
        self, channel_id: str | None = None, limit: int = 200
    ) -> list[GraphRelationship]:
        # Get entity VIDs in scope
        entities = await self.list_entities(channel_id=channel_id, limit=10000)
        if not entities:
            return []

        rels: list[GraphRelationship] = []
        entity_names = {e.name for e in entities}
        vids = [_quote(e.id) for e in entities]

        # Query edges from these vertices across all registered edge types
        for etype in self._registered_edge_types:
            if etype in ("MENTIONED_IN", "REFERENCES_MEDIA"):
                continue  # Skip non-entity edges
            safe_type = _escape(etype)
            vids_str = ", ".join(vids)
            resp = await self._execute_with_space(
                f"GO FROM {vids_str} OVER `{safe_type}` "
                f"YIELD id($^) AS src_vid, id($$) AS tgt_vid, "
                f"`{safe_type}`.confidence AS confidence, "
                f"`{safe_type}`.context AS context "
                f"| LIMIT {limit}"
            )
            rows = self._parse_result_to_dicts(resp)
            for row in rows:
                # Resolve VIDs back to names
                src_name = ""
                tgt_name = ""
                for e in entities:
                    if e.id == str(row.get("src_vid", "")):
                        src_name = e.name
                    if e.id == str(row.get("tgt_vid", "")):
                        tgt_name = e.name
                if src_name in entity_names and tgt_name in entity_names:
                    rels.append(GraphRelationship(
                        type=etype,
                        source=src_name,
                        target=tgt_name,
                        confidence=float(row.get("confidence") or 0.0),
                        context=row.get("context") or "",
                    ))

            if len(rels) >= limit:
                break

        return rels[:limit]

    async def count_relationships(self, channel_id: str | None = None) -> int:
        rels = await self.list_relationships(channel_id=channel_id, limit=1_000_000)
        return len(rels)

    # ------------------------------------------------------------------
    # Episodic + Media
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
        ev_vid = _event_vid(weaviate_fact_id)
        media_json = json.dumps(media_urls or [])
        link_json = json.dumps(link_urls or [])

        # Upsert Event vertex
        await self._execute_with_space(
            f"INSERT VERTEX IF NOT EXISTS Event ("
            f"weaviate_id, message_ts, channel_id, media_urls, link_urls"
            f") VALUES {_quote(ev_vid)}:("
            f"{_quote(weaviate_fact_id)}, {_quote(message_ts)}, "
            f"{_quote(channel_id)}, {_quote(media_json)}, {_quote(link_json)})"
        )

        # Find entity VID
        resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.name == {_quote(entity_name)} "
            f"YIELD id(vertex) AS vid | LIMIT 1"
        )
        rows = self._parse_result_to_dicts(resp)
        if not rows:
            return

        entity_vid = str(rows[0]["vid"])

        # Create MENTIONED_IN edge
        now_iso = datetime.now(tz=UTC).isoformat()
        await self._execute_with_space(
            f"INSERT EDGE `MENTIONED_IN` ("
            f"confidence, valid_from, valid_until, context, "
            f"source_message_id, source_fact_id, created_at"
            f") VALUES {_quote(entity_vid)} -> {_quote(ev_vid)}:("
            f"1.0, \"\", \"\", \"\", \"\", {_quote(weaviate_fact_id)}, {_quote(now_iso)})"
        )

    async def upsert_media(
        self,
        url: str,
        media_type: str,
        title: str = "",
        channel_id: str = "",
        message_ts: str = "",
    ) -> None:
        vid = _media_vid(url)
        await self._execute_with_space(
            f"INSERT VERTEX IF NOT EXISTS Media ("
            f"url, media_type, title, channel_id, message_ts"
            f") VALUES {_quote(vid)}:("
            f"{_quote(url)}, {_quote(media_type)}, {_quote(title)}, "
            f"{_quote(channel_id)}, {_quote(message_ts)})"
        )
        # Update title if provided (ON MATCH equivalent)
        if title:
            await self._execute_with_space(
                f'UPDATE VERTEX ON Media {_quote(vid)} SET '
                f'title = {_quote(title)}'
            )

    async def link_entity_to_media(self, entity_name: str, media_url: str) -> None:
        resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.name == {_quote(entity_name)} "
            f"YIELD id(vertex) AS vid | LIMIT 1"
        )
        rows = self._parse_result_to_dicts(resp)
        if not rows:
            return

        entity_vid = str(rows[0]["vid"])
        media_vid = _media_vid(media_url)
        now_iso = datetime.now(tz=UTC).isoformat()

        await self._execute_with_space(
            f"INSERT EDGE `REFERENCES_MEDIA` ("
            f"confidence, valid_from, valid_until, context, "
            f"source_message_id, source_fact_id, created_at"
            f") VALUES {_quote(entity_vid)} -> {_quote(media_vid)}:("
            f"1.0, \"\", \"\", \"\", \"\", \"\", {_quote(now_iso)})"
        )

    async def list_media(
        self, channel_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if channel_id is not None:
            query = (
                f"LOOKUP ON Media WHERE Media.channel_id == {_quote(channel_id)} "
                f"YIELD id(vertex) AS vid, "
                f"properties(vertex).url AS url, "
                f"properties(vertex).media_type AS media_type, "
                f"properties(vertex).title AS title, "
                f"properties(vertex).channel_id AS channel_id, "
                f"properties(vertex).message_ts AS message_ts "
                f"| LIMIT {limit}"
            )
        else:
            query = (
                f"LOOKUP ON Media "
                f"YIELD id(vertex) AS vid, "
                f"properties(vertex).url AS url, "
                f"properties(vertex).media_type AS media_type, "
                f"properties(vertex).title AS title, "
                f"properties(vertex).channel_id AS channel_id, "
                f"properties(vertex).message_ts AS message_ts "
                f"| LIMIT {limit}"
            )

        resp = await self._execute_with_space(query)
        rows = self._parse_result_to_dicts(resp)
        return [
            {
                "id": row.get("vid", ""),
                "url": row.get("url", ""),
                "media_type": row.get("media_type", ""),
                "title": row.get("title", ""),
                "channel_id": row.get("channel_id", ""),
                "message_ts": row.get("message_ts", ""),
            }
            for row in rows
        ]

    async def list_media_relationships(
        self, channel_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        entities = await self.list_entities(channel_id=channel_id, limit=10000)
        if not entities:
            return []

        vids = [_quote(e.id) for e in entities]
        vids_str = ", ".join(vids)

        resp = await self._execute_with_space(
            f"GO FROM {vids_str} OVER `REFERENCES_MEDIA` "
            f"YIELD id($^) AS src_vid, id($$) AS media_vid "
            f"| LIMIT {limit}"
        )
        rows = self._parse_result_to_dicts(resp)

        rels: list[dict[str, Any]] = []
        entity_map = {e.id: e.name for e in entities}

        for row in rows:
            src_name = entity_map.get(str(row.get("src_vid", "")), "")
            media_vid = str(row.get("media_vid", ""))

            # Fetch media details
            media_resp = await self._execute_with_space(
                f"FETCH PROP ON Media {_quote(media_vid)} "
                f"YIELD properties(vertex).title AS title, "
                f"properties(vertex).url AS url, "
                f"properties(vertex).media_type AS media_type"
            )
            media_rows = self._parse_result_to_dicts(media_resp)
            tgt_name = ""
            if media_rows:
                mr = media_rows[0]
                tgt_name = mr.get("title") or ""
                if not tgt_name:
                    url = mr.get("url", "")
                    media_type = mr.get("media_type", "")
                    if media_type == "link":
                        try:
                            tgt_name = url.split("//")[-1].split("/")[0]
                        except Exception:
                            tgt_name = url
                    else:
                        tgt_name = url.split("/")[-1] if "/" in url else url

            rels.append({
                "source": src_name,
                "target": tgt_name,
                "type": "REFERENCES_MEDIA",
            })

        return rels[:limit]

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    async def get_neighbors(
        self, entity_id: str, hops: int = 1, limit: int = 50
    ) -> Subgraph:
        hops = max(1, hops)
        node_map: dict[str, GraphEntity] = {}
        edges: list[GraphRelationship] = []

        # Use GO with variable-length steps over all edge types
        resp = await self._execute_with_space(
            f"GO 1 TO {hops} STEPS FROM {_quote(entity_id)} OVER * BIDIRECT "
            f"YIELD id($^) AS src_vid, id($$) AS tgt_vid, type(edge) AS etype, "
            f"properties(edge).confidence AS confidence, "
            f"properties(edge).context AS context "
            f"| LIMIT {limit}"
        )
        rows = self._parse_result_to_dicts(resp)

        # Collect all unique VIDs
        all_vids: set[str] = {entity_id}
        for row in rows:
            src = str(row.get("src_vid", ""))
            tgt = str(row.get("tgt_vid", ""))
            if src:
                all_vids.add(src)
            if tgt:
                all_vids.add(tgt)

        # Fetch all vertex props in one go
        if all_vids:
            vids_str = ", ".join(_quote(v) for v in all_vids)
            fetch_resp = await self._execute_with_space(
                f"FETCH PROP ON Entity {vids_str} "
                f"YIELD id(vertex) AS vid, "
                f"properties(vertex).name AS name, "
                f"properties(vertex).type AS type, "
                f"properties(vertex).scope AS scope, "
                f"properties(vertex).channel_id AS channel_id, "
                f"properties(vertex).properties AS properties, "
                f"properties(vertex).aliases AS aliases, "
                f"properties(vertex).status AS status, "
                f"properties(vertex).pending_since AS pending_since, "
                f"properties(vertex).source_message_id AS source_message_id, "
                f"properties(vertex).message_ts AS message_ts, "
                f"properties(vertex).created_at AS created_at, "
                f"properties(vertex).updated_at AS updated_at"
            )
            for frow in self._parse_result_to_dicts(fetch_resp):
                e = self._entity_from_row(frow)
                node_map[e.id] = e

        # Build edges
        for row in rows:
            src_vid = str(row.get("src_vid", ""))
            tgt_vid = str(row.get("tgt_vid", ""))
            src_entity = node_map.get(src_vid)
            tgt_entity = node_map.get(tgt_vid)
            edges.append(GraphRelationship(
                type=row.get("etype") or "RELATED_TO",
                source=src_entity.name if src_entity else src_vid,
                target=tgt_entity.name if tgt_entity else tgt_vid,
                confidence=float(row.get("confidence") or 0.0),
                context=row.get("context") or "",
            ))

        return Subgraph(nodes=list(node_map.values()), edges=edges)

    async def get_decisions(
        self, channel_id: str, limit: int = 20
    ) -> list[GraphEntity]:
        return await self.list_entities(
            channel_id=channel_id, entity_type="Decision", limit=limit
        )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_channel_data(self, channel_id: str) -> dict[str, int]:
        # Delete events for this channel
        ev_resp = await self._execute_with_space(
            f"LOOKUP ON Event WHERE Event.channel_id == {_quote(channel_id)} "
            f"YIELD id(vertex) AS vid"
        )
        ev_rows = self._parse_result_to_dicts(ev_resp)
        events_deleted = len(ev_rows)
        for row in ev_rows:
            await self._execute_with_space(
                f"DELETE VERTEX {_quote(str(row['vid']))} WITH EDGE"
            )

        # Delete media for this channel
        media_resp = await self._execute_with_space(
            f"LOOKUP ON Media WHERE Media.channel_id == {_quote(channel_id)} "
            f"YIELD id(vertex) AS vid"
        )
        media_rows = self._parse_result_to_dicts(media_resp)
        media_deleted = len(media_rows)
        for row in media_rows:
            await self._execute_with_space(
                f"DELETE VERTEX {_quote(str(row['vid']))} WITH EDGE"
            )

        # Delete channel-scoped entities
        entity_resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.channel_id == {_quote(channel_id)} "
            f"YIELD id(vertex) AS vid"
        )
        entity_rows = self._parse_result_to_dicts(entity_resp)
        entities_deleted = len(entity_rows)
        for row in entity_rows:
            await self._execute_with_space(
                f"DELETE VERTEX {_quote(str(row['vid']))} WITH EDGE"
            )

        # Clean up orphaned global entities with no edges
        global_resp = await self._execute_with_space(
            "LOOKUP ON Entity WHERE Entity.scope == \"global\" "
            "YIELD id(vertex) AS vid"
        )
        global_rows = self._parse_result_to_dicts(global_resp)
        orphans_deleted = 0
        for row in global_rows:
            vid = str(row["vid"])
            # Check if vertex has any edges
            edge_resp = await self._execute_with_space(
                f"GO FROM {_quote(vid)} OVER * BIDIRECT "
                f"YIELD id($$) AS neighbor | LIMIT 1"
            )
            edge_rows = self._parse_result_to_dicts(edge_resp)
            if not edge_rows:
                await self._execute_with_space(
                    f"DELETE VERTEX {_quote(vid)} WITH EDGE"
                )
                orphans_deleted += 1

        return {
            "events_deleted": events_deleted,
            "media_deleted": media_deleted,
            "entities_deleted": entities_deleted + orphans_deleted,
        }

    # ------------------------------------------------------------------
    # Entity-registry support
    # ------------------------------------------------------------------

    async def find_entity_by_name_or_alias(self, name: str) -> str | None:
        # Try exact name match first
        resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.name == {_quote(name)} "
            f"YIELD properties(vertex).name AS name | LIMIT 1"
        )
        rows = self._parse_result_to_dicts(resp)
        if rows:
            return rows[0]["name"]

        # Search through aliases (stored as JSON strings).
        # Nebula doesn't support searching within JSON-encoded lists natively,
        # so we fetch all and filter in Python.
        all_resp = await self._execute_with_space(
            "LOOKUP ON Entity "
            "YIELD properties(vertex).name AS name, "
            "properties(vertex).aliases AS aliases"
        )
        for row in self._parse_result_to_dicts(all_resp):
            raw = row.get("aliases", "[]")
            try:
                aliases = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except (json.JSONDecodeError, ValueError):
                aliases = []
            if name in aliases:
                return row["name"]

        return None

    async def get_all_entities_summary(self) -> list[dict[str, Any]]:
        resp = await self._execute_with_space(
            "LOOKUP ON Entity "
            "YIELD properties(vertex).name AS name, "
            "properties(vertex).type AS type, "
            "properties(vertex).aliases AS aliases"
        )
        rows = self._parse_result_to_dicts(resp)
        result: list[dict[str, Any]] = []
        for row in rows:
            raw_aliases = row.get("aliases", "[]")
            try:
                aliases = json.loads(raw_aliases) if isinstance(raw_aliases, str) else (raw_aliases or [])
            except (json.JSONDecodeError, ValueError):
                aliases = []
            result.append({
                "name": row.get("name", ""),
                "type": row.get("type", ""),
                "aliases": aliases,
            })
        result.sort(key=lambda x: x["name"])
        return result

    async def register_alias(
        self, canonical: str, alias: str, entity_type: str
    ) -> None:
        resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.name == {_quote(canonical)} "
            f"AND Entity.type == {_quote(entity_type)} "
            f"YIELD id(vertex) AS vid, "
            f"properties(vertex).aliases AS aliases | LIMIT 1"
        )
        rows = self._parse_result_to_dicts(resp)
        if not rows:
            return

        vid = str(rows[0]["vid"])
        raw_aliases = rows[0].get("aliases", "[]")
        try:
            aliases = json.loads(raw_aliases) if isinstance(raw_aliases, str) else (raw_aliases or [])
        except (json.JSONDecodeError, ValueError):
            aliases = []

        if alias not in aliases:
            aliases.append(alias)
            aliases_json = json.dumps(aliases)
            await self._execute_with_space(
                f'UPDATE VERTEX ON Entity {_quote(vid)} SET '
                f'aliases = {_quote(aliases_json)}'
            )

    async def fuzzy_match_entities(
        self, name: str, threshold: float = 0.8
    ) -> list[tuple[str, float]]:
        resp = await self._execute_with_space(
            "LOOKUP ON Entity "
            "YIELD properties(vertex).name AS name"
        )
        rows = self._parse_result_to_dicts(resp)
        results: list[tuple[str, float]] = []
        for row in rows:
            entity_name = row.get("name", "")
            if not entity_name:
                continue
            score = jellyfish.jaro_winkler_similarity(name, entity_name)
            if score >= threshold:
                results.append((entity_name, round(score, 4)))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    async def get_entities_with_name_vectors(self) -> list[dict[str, Any]]:
        resp = await self._execute_with_space(
            "LOOKUP ON Entity "
            "YIELD properties(vertex).name AS name, "
            "properties(vertex).name_vector AS vec"
        )
        rows = self._parse_result_to_dicts(resp)
        results: list[dict[str, Any]] = []
        for row in rows:
            raw_vec = row.get("vec", "")
            if not raw_vec or raw_vec == "":
                continue
            try:
                vec = json.loads(raw_vec) if isinstance(raw_vec, str) else raw_vec
            except (json.JSONDecodeError, ValueError):
                continue
            if vec and isinstance(vec, list):
                results.append({"name": row.get("name", ""), "vec": vec})
        return results

    async def get_entities_missing_name_vectors(self) -> list[str]:
        resp = await self._execute_with_space(
            "LOOKUP ON Entity "
            "YIELD properties(vertex).name AS name, "
            "properties(vertex).name_vector AS vec"
        )
        rows = self._parse_result_to_dicts(resp)
        names: list[str] = []
        for row in rows:
            raw_vec = row.get("vec", "")
            if not raw_vec or raw_vec == "" or raw_vec == "[]":
                name = row.get("name", "")
                if name:
                    names.append(name)
        return names

    async def store_name_vector(
        self, entity_name: str, vector: list[float]
    ) -> None:
        resp = await self._execute_with_space(
            f"LOOKUP ON Entity WHERE Entity.name == {_quote(entity_name)} "
            f"YIELD id(vertex) AS vid | LIMIT 1"
        )
        rows = self._parse_result_to_dicts(resp)
        if not rows:
            return
        vid = str(rows[0]["vid"])
        vec_json = json.dumps(vector)
        await self._execute_with_space(
            f'UPDATE VERTEX ON Entity {_quote(vid)} SET '
            f'name_vector = {_quote(vec_json)}'
        )

    # ------------------------------------------------------------------
    # Batch operations (delegate to individual methods)
    # ------------------------------------------------------------------

    async def batch_create_episodic_links(self, links: list[dict[str, Any]]) -> int:
        count = 0
        for link in links:
            try:
                await self.create_episodic_link(**link)
                count += 1
            except Exception:
                logger.warning("NebulaStore: batch episodic link failed for %s", link.get("entity_name", "?"), exc_info=True)
        return count

    async def batch_upsert_media(self, items: list[dict[str, Any]]) -> int:
        count = 0
        for item in items:
            try:
                await self.upsert_media(**item)
                count += 1
            except Exception:
                logger.warning("NebulaStore: batch upsert_media failed for %s", item.get("url", "?")[:60], exc_info=True)
        return count

    async def batch_link_entities_to_media(self, links: list[dict[str, Any]]) -> int:
        count = 0
        for link in links:
            try:
                await self.link_entity_to_media(**link)
                count += 1
            except Exception:
                logger.warning("NebulaStore: batch link_entity_to_media failed", exc_info=True)
        return count

    async def batch_promote_pending(self, names: list[str]) -> int:
        count = 0
        for name in names:
            try:
                await self.promote_pending_entity(name)
                count += 1
            except Exception:
                pass  # Entity may not be pending
        return count

    async def batch_find_entities_by_name(self, names: list[str]) -> set[str]:
        found: set[str] = set()
        for name in names:
            try:
                entity = await self.find_entity_by_name(name)
                if entity is not None:
                    found.add(name)
            except Exception:
                pass
        return found
