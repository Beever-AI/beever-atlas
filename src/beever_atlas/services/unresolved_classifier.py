"""Second-pass entity-type classifier for Unresolved stubs (PR-A).

Subscribes to ``ExtractionWorker.memory_settled`` events. When a
channel's extraction queue drains, the classifier:

1. Lists every ``Unresolved`` stub that's still ``awaiting_type=true``
   AND reachable from the channel via a MENTIONED_IN→Event{channel_id}
   pivot (A-2 scope filter).
2. Fetches ≤3 incident-edge contexts per stub in a single
   ``UNWIND``-based Cypher round-trip (A-4 batching).
3. Bundles batches of ≤25 names → Gemini Flash 2.5 JSON-mode call.
4. Validates the inferred type (PascalCase; new types gated by
   confidence ≥ 0.8 — A-5).
5. For accepted classifications, calls ``upsert_entity`` with the
   inferred type — the existing symmetric heal-path at
   :func:`Neo4jStore.upsert_entity` absorbs the stub into the typed
   row via ``apoc.refactor.mergeNodes``.
6. For low-confidence rows, bumps ``classifier_attempts`` so the stub
   parks for 7 days before retry.

Per-channel ``asyncio.Lock`` guards each ``memory_settled`` emit
against double-spend (A-6 fix).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from beever_atlas.models import GraphEntity

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Canonical types from entity_extractor.py:42-56 (11 types). Used as
# the "preferred" set; new PascalCase types from the LLM are allowed
# only when confidence ≥ 0.8 (A-5 gate).
CANONICAL_TYPES: frozenset[str] = frozenset(
    {
        "Person",
        "Technology",
        "Project",
        "Team",
        "Decision",
        "Meeting",
        "Artifact",
        "Organization",
        "Concept",
        "Location",
        "Event",
    }
)

# Confidence thresholds — per plan §A.3.1.
ACCEPT_THRESHOLD: float = 0.55
NEW_TYPE_THRESHOLD: float = 0.8
LOW_CONFIDENCE_TTL_DAYS: int = 7

# Batch size for the LLM call — plan §A.5 pins ~25 names per batch so
# the prompt stays under 2.5k input tokens.
BATCH_SIZE: int = 25

# Per-channel locks — module-level so concurrent ``memory_settled``
# emits for the same channel queue serially (A-6 fix).
_locks: dict[str, asyncio.Lock] = {}


_PASCAL_CASE = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def _coerce_pascal(raw: str) -> str:
    """Coerce ``raw`` to PascalCase if possible.

    ``person`` → ``Person``; ``DATA_OFFICER`` → ``DataOfficer``.
    Returns the raw string unchanged if it cannot be coerced cleanly.
    """
    s = (raw or "").strip()
    if not s:
        return s
    if _PASCAL_CASE.match(s):
        return s
    if "_" in s or s.islower() or s.isupper():
        parts = [p for p in re.split(r"[_\s-]+", s) if p]
        return "".join(p[:1].upper() + p[1:].lower() for p in parts)
    # Mixed-case unknown shape — just capitalise the first letter.
    return s[:1].upper() + s[1:]


@dataclass
class Classification:
    """Single LLM classification result."""

    name: str
    type: str
    confidence: float


@dataclass
class ClassifyReport:
    """Return shape for :meth:`UnresolvedClassifier.classify_channel`."""

    channel_id: str
    processed: int = 0
    classified: int = 0
    low_confidence: int = 0
    new_types_accepted: int = 0
    errors: list[str] = field(default_factory=list)
    llm_calls: int = 0
    est_cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "processed": self.processed,
            "classified": self.classified,
            "low_confidence": self.low_confidence,
            "new_types_accepted": self.new_types_accepted,
            "errors": list(self.errors),
            "llm_calls": self.llm_calls,
            "est_cost_usd": round(self.est_cost_usd, 6),
        }


class ClassifyReportModel(BaseModel):
    """Pydantic wrapper for the admin endpoint response."""

    channel_id: str
    processed: int = 0
    classified: int = 0
    low_confidence: int = 0
    new_types_accepted: int = 0
    errors: list[str] = []
    llm_calls: int = 0
    est_cost_usd: float = 0.0


class UnresolvedClassifier:
    """Drives the post-extraction second pass.

    The classifier never speaks Cypher directly beyond the three new
    methods on :class:`Neo4jStore` (``list_unresolved_stubs``,
    ``fetch_incident_contexts_batch``, ``mark_unresolved_attempt``).
    Typed writes flow through ``upsert_entity`` so the existing heal-
    path absorbs the stub via ``apoc.refactor.mergeNodes``.
    """

    def __init__(
        self,
        *,
        stores: Any,
        settings: Any | None = None,
        llm_dispatcher: Any | None = None,
        provider: str = "gemini",
        model: str = "gemini-2.5-flash",
        endpoint_id: str | None = None,
    ) -> None:
        self._stores = stores
        self._settings = settings
        # Caller may inject a mock dispatcher for tests. Production
        # path resolves lazily so the LLM provider isn't imported at
        # lifespan time.
        self._dispatcher = llm_dispatcher
        self._provider = provider
        self._model = model
        self._endpoint_id = endpoint_id

    async def classify_channel(
        self,
        channel_id: str,
        *,
        limit: int = 500,
        force: bool = False,
    ) -> ClassifyReport:
        """Classify every eligible Unresolved stub reachable from
        ``channel_id``. Returns a :class:`ClassifyReport`.

        ``force=true`` bypasses the 7-day ``classifier_low_confidence_at``
        defer gate — used by the admin endpoint.
        """
        lock = _locks.setdefault(channel_id, asyncio.Lock())
        report = ClassifyReport(channel_id=channel_id)
        async with lock:
            try:
                stubs = await self._stores.graph.list_unresolved_stubs(
                    channel_id=channel_id, limit=limit
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "UnresolvedClassifier: list_unresolved_stubs failed channel=%s: %s",
                    channel_id,
                    exc,
                )
                report.errors.append(f"list_unresolved_stubs: {exc}")
                return report

            now = datetime.now(tz=UTC)
            cutoff = now - timedelta(days=LOW_CONFIDENCE_TTL_DAYS)
            eligible: list[dict[str, Any]] = []
            for stub in stubs:
                if not force:
                    raw = stub.get("low_confidence_at")
                    if raw:
                        try:
                            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=UTC)
                            if ts > cutoff:
                                continue
                        except (TypeError, ValueError):
                            # Unparseable timestamp — treat as old.
                            pass
                eligible.append(stub)

            report.processed = len(eligible)
            if not eligible:
                return report

            # Single batched context fetch (A-4 optimisation).
            names = [s["name"] for s in eligible if s.get("name")]
            try:
                contexts_map = await self._stores.graph.fetch_incident_contexts_batch(
                    names, limit_per_name=3
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "UnresolvedClassifier: fetch_incident_contexts_batch failed channel=%s: %s",
                    channel_id,
                    exc,
                )
                report.errors.append(f"fetch_incident_contexts_batch: {exc}")
                return report

            # Observed type catalog for this channel — used as
            # context in the prompt + for the new-type gate.
            observed_types = await self._observed_channel_types(channel_id)

            # LLM dispatch in batches of ≤25.
            for batch_start in range(0, len(eligible), BATCH_SIZE):
                batch = eligible[batch_start : batch_start + BATCH_SIZE]
                candidates = [
                    {
                        "name": s["name"],
                        "contexts": contexts_map.get(s["name"], [])[:3],
                    }
                    for s in batch
                ]
                try:
                    classifications = await self._dispatch_batch(candidates, observed_types)
                    report.llm_calls += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "UnresolvedClassifier: LLM dispatch failed channel=%s: %s",
                        channel_id,
                        exc,
                    )
                    report.errors.append(f"dispatch: {exc}")
                    continue

                by_name = {c.name: c for c in classifications}
                for stub in batch:
                    name = stub["name"]
                    cls = by_name.get(name)
                    if cls is None:
                        continue
                    inferred = _coerce_pascal(cls.type)
                    confidence = float(cls.confidence)
                    # Gate: new type acceptance requires high
                    # confidence OR presence in channel/catalog.
                    is_known = inferred in CANONICAL_TYPES or inferred in observed_types
                    if not is_known and confidence < NEW_TYPE_THRESHOLD:
                        # Parked as low-confidence; stub stays
                        # Unresolved.
                        await self._mark_low(stub, report)
                        continue
                    if confidence < ACCEPT_THRESHOLD:
                        await self._mark_low(stub, report)
                        continue
                    # Accept — typed write through the heal-path.
                    try:
                        await self._apply_classification(stub, inferred, confidence)
                        report.classified += 1
                        if not is_known:
                            report.new_types_accepted += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "UnresolvedClassifier: upsert_entity failed name=%s: %s",
                            name,
                            exc,
                        )
                        report.errors.append(f"upsert:{name}: {exc}")

            # Cost estimate — plan §A.5: ~$0.00025 / batch.
            report.est_cost_usd = report.llm_calls * 0.00025
            return report

    async def _observed_channel_types(self, channel_id: str) -> set[str]:
        """Return the set of distinct typed entity types currently
        visible in the channel — used as auxiliary context for the LLM
        and as half of the new-type gate (A-5).
        """
        try:
            entities = await self._stores.graph.list_entities(channel_id=channel_id, limit=500)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "UnresolvedClassifier: observed-types lookup failed channel=%s: %s",
                channel_id,
                exc,
            )
            return set()
        return {e.type for e in entities if e.type and e.type not in {"Unresolved", "Topic"}}

    async def _dispatch_batch(
        self,
        candidates: list[dict[str, Any]],
        observed_types: set[str],
    ) -> list[Classification]:
        """Run one LLM call. Returns a list of :class:`Classification`."""
        from beever_atlas.agents.prompts.unresolved_classifier import (
            UNRESOLVED_CLASSIFIER_INSTRUCTION,
        )

        prompt = UNRESOLVED_CLASSIFIER_INSTRUCTION.format(
            channel_observed_types=(
                ", ".join(sorted(observed_types)) if observed_types else "(none)"
            ),
            candidates_json=json.dumps(candidates, ensure_ascii=False),
        )
        if self._dispatcher is not None:
            raw = await self._dispatcher(prompt)
        else:
            from beever_atlas.services.llm_dispatch import dispatch_completion

            response = await dispatch_completion(
                provider=self._provider,
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                endpoint_id=self._endpoint_id,
                response_format={"type": "json_object"},
                temperature=0.2,
                _log_consumer="unresolved_classifier",
            )
            raw = response.choices[0].message.content or "{}"

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "UnresolvedClassifier: LLM returned non-JSON: %s",
                exc,
            )
            return []
        rows = data.get("classifications") or []
        out: list[Classification] = []
        for row in rows:
            try:
                out.append(
                    Classification(
                        name=str(row.get("name", "")).strip(),
                        type=str(row.get("type", "")).strip(),
                        confidence=float(row.get("confidence", 0.0)),
                    )
                )
            except (TypeError, ValueError) as exc:
                logger.debug("UnresolvedClassifier: bad row %r: %s", row, exc)
        return out

    async def _apply_classification(
        self,
        stub: dict[str, Any],
        inferred_type: str,
        confidence: float,
    ) -> None:
        """Typed upsert — the heal-path at neo4j_store.upsert_entity
        absorbs the original Unresolved row via APOC.

        For ``scope='channel'`` stubs (legacy data) we pass both
        ``channel_id`` and ``scope='channel'`` so the channel branch
        of the heal-path is taken. For ``scope='global'`` stubs
        (production case) ``channel_id=None`` triggers the global
        branch.
        """
        name = stub["name"]
        scope = stub.get("scope") or "global"
        channel_id = stub.get("channel_id")
        entity = GraphEntity(
            name=name,
            type=inferred_type,
            scope=scope,
            channel_id=channel_id if scope == "channel" else None,
            properties={
                "classifier_source": "unresolved_classifier",
                "classifier_confidence": round(confidence, 4),
            },
        )
        await self._stores.graph.upsert_entity(entity)

    async def _mark_low(self, stub: dict[str, Any], report: ClassifyReport) -> None:
        """Bump ``classifier_attempts`` and stamp the defer timestamp."""
        try:
            await self._stores.graph.mark_unresolved_attempt(
                name=stub["name"],
                scope=stub.get("scope") or "global",
                channel_id=stub.get("channel_id"),
            )
            report.low_confidence += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "UnresolvedClassifier: mark_unresolved_attempt failed name=%s: %s",
                stub.get("name"),
                exc,
            )
            report.errors.append(f"mark_low:{stub.get('name')}: {exc}")
