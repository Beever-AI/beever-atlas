"""Weaviate-backed store for QA history (separate from MemoryFact collection)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.query import Filter

logger = logging.getLogger(__name__)

QA_HISTORY_COLLECTION = "QAHistory"

_QA_HISTORY_PROPERTIES: list[tuple[str, DataType]] = [
    ("question", DataType.TEXT),
    ("answer", DataType.TEXT),
    ("citations_json", DataType.TEXT),
    ("channel_id", DataType.TEXT),
    ("user_id", DataType.TEXT),
    ("session_id", DataType.TEXT),
    ("timestamp", DataType.TEXT),
    ("is_deleted", DataType.BOOL),
    ("answer_kind", DataType.TEXT),
]

_REFUSAL_MARKERS = [
    "no record",
    "no information",
    "I don't have",
    "not identified",
    "couldn't find",
    "no evidence",
]
_REFUSAL_LENGTH_THRESHOLD = 400


def _classify_answer(answer: str) -> str:
    """Classify answer as 'refused' if it contains a refusal marker and is short."""
    try:
        if len(answer) < _REFUSAL_LENGTH_THRESHOLD and any(
            marker.lower() in answer.lower() for marker in _REFUSAL_MARKERS
        ):
            return "refused"
        return "answered"
    except Exception:
        return "answered"


class QAHistoryStore:
    """Manages the QAHistory collection in Weaviate for searchable Q&A history.

    Separate from MemoryFact — Q&A entries never appear in channel fact searches.
    """

    def __init__(self, url: str, api_key: str = "") -> None:
        self._url = url
        self._api_key = api_key
        self._client: weaviate.WeaviateClient | None = None

    async def startup(self) -> None:
        """Connect to Weaviate and ensure QAHistory schema exists."""

        def _connect() -> weaviate.WeaviateClient:
            from urllib.parse import urlparse

            parsed = urlparse(self._url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 8080)
            secure = parsed.scheme == "https"

            if host in ("localhost", "127.0.0.1") and not secure:
                return weaviate.connect_to_local(port=port, grpc_port=50051)

            headers: dict[str, str] = {}
            if self._api_key:
                headers["X-Weaviate-Api-Key"] = self._api_key
            return weaviate.connect_to_custom(
                http_host=host,
                http_port=port,
                http_secure=secure,
                grpc_host=host,
                grpc_port=50051,
                grpc_secure=secure,
                headers=headers,
            )

        self._client = await asyncio.to_thread(_connect)
        await self.ensure_schema()

    async def shutdown(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None

    async def ensure_schema(self) -> None:
        """Create QAHistory collection if it does not exist."""

        def _ensure() -> None:
            assert self._client is not None
            if self._client.collections.exists(QA_HISTORY_COLLECTION):
                # Migrate: add missing properties
                collection = self._client.collections.get(QA_HISTORY_COLLECTION)
                existing = {p.name for p in collection.config.get().properties}
                for name, dtype in _QA_HISTORY_PROPERTIES:
                    if name not in existing:
                        collection.config.add_property(Property(name=name, data_type=dtype))
                        logger.info("QAHistoryStore: added property %r to QAHistory", name)
                return
            self._client.collections.create(
                name=QA_HISTORY_COLLECTION,
                vectorizer_config=Configure.Vectorizer.none(),
                vector_index_config=Configure.VectorIndex.hnsw(),
                properties=[
                    Property(name=name, data_type=dtype) for name, dtype in _QA_HISTORY_PROPERTIES
                ],
            )
            logger.info("QAHistoryStore: created QAHistory collection")

        await asyncio.to_thread(_ensure)

    def _collection(self):
        assert self._client is not None, "QAHistoryStore not started"
        if not self._client.collections.exists(QA_HISTORY_COLLECTION):
            logger.warning("QAHistoryStore: QAHistory collection missing, recreating")
            self._client.collections.create(
                name=QA_HISTORY_COLLECTION,
                vectorizer_config=Configure.Vectorizer.none(),
                vector_index_config=Configure.VectorIndex.hnsw(),
                properties=[
                    Property(name=name, data_type=dtype) for name, dtype in _QA_HISTORY_PROPERTIES
                ],
            )
        return self._client.collections.get(QA_HISTORY_COLLECTION)

    async def write_qa_entry(
        self,
        question: str,
        answer: str,
        citations: list[dict] | dict,
        channel_id: str,
        user_id: str,
        session_id: str,
    ) -> str:
        """Write a Q&A pair to QAHistory. Returns the Weaviate UUID.

        `citations` may be either a legacy flat list or the Phase 1
        envelope dict `{items, sources, refs}`. It is always stored in
        envelope form; reads flatten back for legacy consumers.
        """
        from beever_atlas.agents.citations.persistence import upgrade_envelope

        entry_id = str(uuid.uuid4())
        envelope = upgrade_envelope(citations)
        props = {
            "question": question,
            "answer": answer,
            "citations_json": json.dumps(envelope),
            "channel_id": channel_id,
            "user_id": user_id,
            "session_id": session_id,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "is_deleted": False,
            "answer_kind": _classify_answer(answer),
        }

        def _write() -> str:
            collection = self._collection()
            collection.data.insert(properties=props, uuid=entry_id)
            return entry_id

        return await asyncio.to_thread(_write)

    def _parse_qa_objects(self, objects) -> list[dict]:
        """Convert Weaviate result objects to QA history dicts."""
        from beever_atlas.agents.citations.persistence import as_legacy_items

        entries = []
        for obj in objects:
            props = obj.properties
            try:
                raw = json.loads(str(props.get("citations_json") or "[]"))
            except (json.JSONDecodeError, TypeError):
                raw = []
            # Envelope-aware: collapse `{items,sources,refs}` → items,
            # passthrough bare lists (legacy rows).
            citations = as_legacy_items(raw)
            entries.append(
                {
                    "question": props.get("question", ""),
                    "answer": props.get("answer", ""),
                    "citations": citations,
                    "timestamp": props.get("timestamp", ""),
                    "session_id": props.get("session_id", ""),
                    "id": str(obj.uuid),
                    # NULL on historical rows treated as "answered" (backfill-safe)
                    "answer_kind": props.get("answer_kind") or "answered",
                }
            )
        return entries

    async def true_hybrid_search(
        self,
        channel_id: str,
        query: str,
        query_vector: list[float],
        limit: int = 5,
        alpha: float | None = None,
    ) -> list[dict]:
        """Weaviate v4 native hybrid search over QAHistory.

        Combines BM25 (keyword) and vector search server-side.  Filters out
        ``is_deleted=True`` entries.  Returns the same shape as
        ``search_qa_history``.

        Args:
            channel_id: Scope search to this channel.
            query: Raw text for the BM25 side.
            query_vector: Pre-computed embedding for the vector side.
            limit: Maximum entries to return.
            alpha: BM25/vector blend (0.0 = pure BM25, 1.0 = pure vector).
                Defaults to ``settings.weaviate_hybrid_alpha`` (0.6).
        """
        from beever_atlas.infra.config import get_settings
        from weaviate.classes.query import MetadataQuery

        resolved_alpha = alpha if alpha is not None else get_settings().weaviate_hybrid_alpha

        def _search() -> list[dict]:
            collection = self._collection()
            channel_filter = Filter.by_property("channel_id").equal(channel_id)
            not_deleted = Filter.by_property("is_deleted").equal(False)
            combined = channel_filter & not_deleted
            result = collection.query.hybrid(
                query=query,
                vector=query_vector,
                alpha=resolved_alpha,
                limit=limit,
                filters=combined,
                return_metadata=MetadataQuery(score=True),
            )
            return self._parse_qa_objects(result.objects)

        try:
            return await asyncio.to_thread(_search)
        except Exception:
            logger.exception("QAHistoryStore.true_hybrid_search failed")
            return []

    async def search_qa_history(
        self,
        channel_id: str,
        query: str,
        limit: int = 5,
        query_vector: list[float] | None = None,
    ) -> list[dict]:
        """Hybrid search over QAHistory scoped to a channel.

        When ``query_vector`` is provided, uses Weaviate native hybrid search
        (BM25 + vector).  Falls back to BM25-only when no vector is given or
        when the hybrid call fails.

        Filters out is_deleted=True entries. Returns list of
        {question, answer, citations, timestamp}.
        """
        if query_vector is not None:
            try:
                return await self.true_hybrid_search(
                    channel_id=channel_id,
                    query=query,
                    query_vector=query_vector,
                    limit=limit,
                )
            except Exception:
                logger.warning(
                    "QAHistoryStore.search_qa_history: hybrid failed, falling back to bm25"
                )

        def _bm25_search() -> list[dict]:
            collection = self._collection()
            channel_filter = Filter.by_property("channel_id").equal(channel_id)
            not_deleted = Filter.by_property("is_deleted").equal(False)
            combined = channel_filter & not_deleted
            result = collection.query.bm25(
                query=query,
                limit=limit,
                filters=combined,
            )
            return self._parse_qa_objects(result.objects)

        try:
            return await asyncio.to_thread(_bm25_search)
        except Exception:
            logger.exception("QAHistoryStore.search_qa_history failed")
            return []

    async def soft_delete(self, entry_id: str) -> None:
        """Mark a QAHistory entry as deleted (is_deleted=True)."""

        def _delete() -> None:
            collection = self._collection()
            collection.data.update(
                uuid=entry_id,
                properties={"is_deleted": True},
            )

        await asyncio.to_thread(_delete)

    #: Maximum Weaviate objects scanned per call. Surfaced on the result
    #: so callers can tell whether they hit the cap and should narrow
    #: their query (or introduce pagination in a future phase).
    FIND_QA_SCAN_CAP: int = 1000

    async def find_qa_entries_citing_source(self, source_id: str, limit: int = 20) -> dict:
        """Return past QA entries whose stored envelope cites `source_id`.

        Weaviate can't query nested JSON natively, so we scan entries and
        filter in Python. This is an ops-query path, not a hot runtime path.

        Returns a dict `{entries, truncated, scanned}`:
        - `entries`: list of matching entries (`id, question, answer, ...`)
        - `truncated`: True when the scan hit `FIND_QA_SCAN_CAP`, meaning
          there may be un-inspected objects that could match.
        - `scanned`: number of Weaviate objects actually examined.
        """
        from beever_atlas.agents.citations.persistence import upgrade_envelope

        cap = self.FIND_QA_SCAN_CAP

        def _scan() -> dict:
            entries: list[dict] = []
            scanned = 0
            collection = self._collection()
            not_deleted = Filter.by_property("is_deleted").equal(False)
            result = collection.query.fetch_objects(
                limit=cap,
                filters=not_deleted,
            )
            for obj in result.objects:
                scanned += 1
                if len(entries) >= limit:
                    continue  # keep counting `scanned` even after we have enough matches
                props = obj.properties
                try:
                    raw = json.loads(str(props.get("citations_json") or "[]"))
                except (json.JSONDecodeError, TypeError):
                    continue
                env = upgrade_envelope(raw)
                if any(
                    isinstance(s, dict) and s.get("id") == source_id
                    for s in (env.get("sources") or [])
                ):
                    entries.append(
                        {
                            "id": str(obj.uuid),
                            "question": props.get("question", ""),
                            "answer": props.get("answer", ""),
                            "timestamp": props.get("timestamp", ""),
                            "session_id": props.get("session_id", ""),
                            "channel_id": props.get("channel_id", ""),
                        }
                    )
            truncated = scanned >= cap
            if truncated:
                logger.warning(
                    "find_qa_entries_citing_source hit scan cap (%d) for source_id=%s",
                    cap,
                    source_id,
                )
            return {
                "entries": entries,
                "truncated": truncated,
                "scanned": scanned,
            }

        try:
            return await asyncio.to_thread(_scan)
        except Exception:
            logger.exception(
                "QAHistoryStore.find_qa_entries_citing_source failed for source_id=%s",
                source_id,
            )
            return {"entries": [], "truncated": False, "scanned": 0}
