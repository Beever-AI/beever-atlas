"""Stage 5: EmbedderAgent — generate text vectors via the Jina API.

Reads ``session.state["classified_facts"]`` (written by ClassifierAgent) and
writes ``session.state["embedded_facts"]`` — the same facts with
``text_vector`` populated from Jina's embeddings API.

Implemented as a ``BaseAgent`` subclass (no LLM calls); all I/O is via Jina's
REST API using ``httpx.AsyncClient``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, AsyncGenerator

import httpx

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from beever_atlas.infra.config import get_settings
from beever_atlas.infra.rate_limit import JINA_LIMITER

logger = logging.getLogger(__name__)

# URL loaded from settings.jina_api_url at runtime
_BATCH_SIZE = 100
_MAX_RETRIES = 3
# Retry on rate-limits (429) AND transient 5xx. Jina has observed 500s that
# resolve on retry; losing an entire batch over a single blip wastes the
# already-spent LLM extraction cost upstream.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class EmbedderAgent(BaseAgent):
    """Calls the Jina Embeddings API to populate ``text_vector`` on each fact.

    Reads  : ``session.state["classified_facts"]``
    Writes : ``session.state["embedded_facts"]``
    """

    model_config = {"arbitrary_types_allowed": True}

    async def _jina_embed_batch(
        self,
        texts: list[str],
        *,
        sync_job_id: str,
        channel_id: str,
        batch_num: str | int,
    ) -> list[list[float]]:
        """Send texts to the Jina API in sub-batches and return embedding vectors.

        Chunks input into batches of up to ``_BATCH_SIZE`` entries and applies
        exponential backoff on HTTP 429 responses (up to ``_MAX_RETRIES``).
        """
        settings = get_settings()
        headers = {
            "Authorization": f"Bearer {settings.jina_api_key}",
            "Content-Type": "application/json",
        }
        all_vectors: list[list[float]] = []

        async with httpx.AsyncClient(timeout=60.0) as client:
            for chunk_start in range(0, len(texts), _BATCH_SIZE):
                chunk = texts[chunk_start : chunk_start + _BATCH_SIZE]
                chunk_index = (chunk_start // _BATCH_SIZE) + 1
                total_chunks = ((len(texts) - 1) // _BATCH_SIZE) + 1
                logger.info(
                    "EmbedderAgent: chunk start job_id=%s channel=%s batch=%s chunk=%d/%d size=%d",
                    sync_job_id,
                    channel_id,
                    batch_num,
                    chunk_index,
                    total_chunks,
                    len(chunk),
                )
                payload: dict[str, Any] = {
                    "model": settings.jina_model,
                    "input": chunk,
                    "dimensions": settings.jina_dimensions,
                    "task": "text-matching",
                }

                attempt = 0
                while True:
                    try:
                        async with JINA_LIMITER:
                            response = await client.post(
                                settings.jina_api_url,
                                headers=headers,
                                json=payload,
                            )
                    except (
                        httpx.ConnectError,
                        httpx.ReadTimeout,
                        httpx.RemoteProtocolError,
                    ) as transient_err:
                        attempt += 1
                        if attempt > _MAX_RETRIES:
                            raise
                        wait = (2**attempt) * (1 + random.uniform(-0.2, 0.2))
                        logger.warning(
                            "EmbedderAgent: transient %s job_id=%s channel=%s batch=%s chunk=%d/%d retry_in=%.1fs attempt=%d/%d",
                            type(transient_err).__name__,
                            sync_job_id,
                            channel_id,
                            batch_num,
                            chunk_index,
                            total_chunks,
                            wait,
                            attempt,
                            _MAX_RETRIES,
                        )
                        await asyncio.sleep(wait)
                        continue

                    if response.status_code in _RETRYABLE_STATUS:
                        attempt += 1
                        if attempt > _MAX_RETRIES:
                            response.raise_for_status()
                        # ±20% jitter to decorrelate concurrent batches
                        wait = (2**attempt) * (1 + random.uniform(-0.2, 0.2))
                        logger.warning(
                            "EmbedderAgent: retryable status=%d job_id=%s channel=%s batch=%s chunk=%d/%d retry_in=%.1fs attempt=%d/%d",
                            response.status_code,
                            sync_job_id,
                            channel_id,
                            batch_num,
                            chunk_index,
                            total_chunks,
                            wait,
                            attempt,
                            _MAX_RETRIES,
                        )
                        await asyncio.sleep(wait)
                        continue

                    response.raise_for_status()
                    data = response.json()
                    # Jina returns {"data": [{"embedding": [...], ...}, ...]}
                    for item in data["data"]:
                        all_vectors.append(item["embedding"])
                    logger.info(
                        "EmbedderAgent: chunk done job_id=%s channel=%s batch=%s chunk=%d/%d embedded=%d",
                        sync_job_id,
                        channel_id,
                        batch_num,
                        chunk_index,
                        total_chunks,
                        len(data["data"]),
                    )
                    break

        return all_vectors

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        """Embed all classified facts and write results to session state."""
        from beever_atlas.agents.callbacks.checkpoint_skip import should_skip_stage

        if should_skip_stage(ctx.session.state, "embedded_facts", self.name):
            yield Event(author=self.name, invocation_id=ctx.invocation_id)
            return

        sync_job_id = ctx.session.state.get("sync_job_id", "unknown")
        channel_id = ctx.session.state.get("channel_id", "unknown")
        batch_num = ctx.session.state.get("batch_num", "?")

        raw_classified = ctx.session.state.get("classified_facts")
        if isinstance(raw_classified, dict):
            facts_raw = raw_classified.get("facts") or []
        elif isinstance(raw_classified, list):
            facts_raw = raw_classified
        else:
            logger.warning(
                "EmbedderAgent: classified_facts is %s, not dict/list; skipping batch=%s",
                type(raw_classified).__name__,
                batch_num,
            )
            facts_raw = []
        facts: list[dict[str, Any]] = [f for f in facts_raw if isinstance(f, dict)]

        if not facts:
            logger.warning(
                "EmbedderAgent: no classified_facts job_id=%s channel=%s batch=%s; "
                "writing empty embedded_facts.",
                sync_job_id,
                channel_id,
                batch_num,
            )
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                actions=EventActions(state_delta={"embedded_facts": []}),
            )
            return

        texts = [fact.get("memory_text", "") for fact in facts]

        logger.info(
            "EmbedderAgent: start job_id=%s channel=%s batch=%s facts=%d",
            sync_job_id,
            channel_id,
            batch_num,
            len(texts),
        )
        vectors = await self._jina_embed_batch(
            texts,
            sync_job_id=sync_job_id,
            channel_id=channel_id,
            batch_num=batch_num,
        )

        embedded: list[dict[str, Any]] = []
        for fact, vector in zip(facts, vectors, strict=True):
            enriched = dict(fact)
            enriched["text_vector"] = vector
            embedded.append(enriched)

        logger.info(
            "EmbedderAgent: done job_id=%s channel=%s batch=%s embedded=%d",
            sync_job_id,
            channel_id,
            batch_num,
            len(embedded),
        )

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(state_delta={"embedded_facts": embedded}),
        )
