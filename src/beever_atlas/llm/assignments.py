"""Assignment data model — per-consumer LLM routing.

Each consumer (the 16 agents + ``embedding``) has one Assignment document
that points at an Endpoint by UUID and carries optional per-call overrides
(``temperature``, ``max_tokens``, ``response_format``, ``extra_headers``)
plus an optional ``fallback_endpoint_id`` for circuit-breaker-driven
failover.

See ``openspec/changes/agent-llm-provider-pluggable/design.md`` D2 + D6.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)


# The 17 default consumers — 16 agents + embedding. Mirrors the AGENT_NAMES
# list in ``model_resolver.py`` with the embedding consumer prepended.
DEFAULT_CONSUMERS: tuple[str, ...] = (
    "embedding",
    "fact_extractor",
    "entity_extractor",
    "cross_batch_validator",
    "coreference_resolver",
    "contradiction_detector",
    "image_describer",
    "video_analyzer",
    "audio_transcriber",
    "summarizer",
    "document_digester",
    "echo",
    "wiki_compiler",
    "wiki_maintainer",
    "qa_agent",
    "qa_router",
    "csv_mapper",
)


ResponseFormat = Literal["text", "json"]


@dataclass
class Assignment:
    """The persisted per-consumer routing record.

    Fields prefixed by ``_`` are computed at boot and not persisted. All other
    fields round-trip through MongoDB. The shape is rectangular: embedding-
    specific fields (``dimensions``, ``task``) stay ``None`` for chat consumers
    and vice versa for chat-only fields.
    """

    consumer: str
    endpoint_id: str
    model: str
    # Optional per-call overrides — None means "use provider default".
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: ResponseFormat | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Optional failover Endpoint UUID. When circuit-breaker is open against
    # ``endpoint_id`` AND this is non-null, dispatch routes to the fallback.
    fallback_endpoint_id: str | None = None
    # Embedding-specific fields (unused for chat consumers).
    dimensions: int | None = None
    task: str | None = None
    updated_at: str = ""

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> "Assignment":
        return cls(
            consumer=cast(str, doc["consumer"]),
            endpoint_id=cast(str, doc["endpoint_id"]),
            model=cast(str, doc.get("model") or ""),
            temperature=doc.get("temperature"),
            max_tokens=doc.get("max_tokens"),
            response_format=doc.get("response_format"),
            extra_headers=dict(doc.get("extra_headers") or {}),
            fallback_endpoint_id=doc.get("fallback_endpoint_id"),
            dimensions=doc.get("dimensions"),
            task=doc.get("task"),
            updated_at=cast(str, doc.get("updated_at") or ""),
        )

    def to_document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResolvedAssignment:
    """The post-resolution shape consumed by ``dispatch_completion``.

    Captures everything dispatch needs — provider prefix, LiteLLM-shaped
    model id, decrypted credentials (api_key / aws_iam / google_sa blob),
    base_url, and the per-call override params. Built once per call by the
    resolver and discarded immediately after.
    """

    consumer: str
    endpoint_id: str
    provider: str  # litellm prefix ("gemini", "openai", ...)
    litellm_model: str  # "<provider>/<model>"
    base_url: str | None
    api_key: str | None
    aws_credentials: dict[str, str] | None
    vertex_credentials: dict[str, str] | None
    extra_headers: dict[str, str]
    temperature: float | None
    max_tokens: int | None
    response_format: ResponseFormat | None
    dimensions: int | None
    task: str | None


class AssignmentStore:
    """CRUD over the ``llm_assignments`` Mongo collection."""

    def __init__(self, mongodb_store: Any) -> None:
        self._mongo = mongodb_store

    @property
    def _collection(self) -> Any:
        return self._mongo.db["llm_assignments"]

    async def list(self) -> list[Assignment]:
        cursor = self._collection.find({}, {"_id": 0})
        return [Assignment.from_document(doc) async for doc in cursor]

    async def get(self, consumer: str) -> Assignment | None:
        doc = await self._collection.find_one({"consumer": consumer}, {"_id": 0})
        return Assignment.from_document(doc) if doc else None

    async def upsert(self, assignment: Assignment) -> Assignment:
        """Insert-or-update an Assignment, stamping ``updated_at``."""
        assignment.updated_at = datetime.now(tz=UTC).isoformat()
        await self._collection.update_one(
            {"consumer": assignment.consumer},
            {"$set": assignment.to_document()},
            upsert=True,
        )
        return assignment

    async def delete(self, consumer: str) -> bool:
        result = await self._collection.delete_one({"consumer": consumer})
        return result.deleted_count > 0

    async def list_referencing_endpoint(self, endpoint_id: str) -> list[Assignment]:
        """Return Assignments using ``endpoint_id`` as primary OR fallback.

        Used by the ``DELETE /api/settings/endpoints/{id}`` 409 protection
        in PR-E so operators can't orphan dispatch by deleting a referenced
        Endpoint.
        """
        cursor = self._collection.find(
            {
                "$or": [
                    {"endpoint_id": endpoint_id},
                    {"fallback_endpoint_id": endpoint_id},
                ]
            },
            {"_id": 0},
        )
        return [Assignment.from_document(doc) async for doc in cursor]


__all__ = [
    "Assignment",
    "AssignmentStore",
    "DEFAULT_CONSUMERS",
    "ResolvedAssignment",
    "ResponseFormat",
]
