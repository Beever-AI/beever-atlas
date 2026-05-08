"""Stage 5: EmbedderAgent — generate text vectors via the embedding shim.

Reads ``session.state["classified_facts"]`` (written by ClassifierAgent) and
writes ``session.state["embedded_facts"]`` — the same facts with
``text_vector`` populated.

All HTTP / retry / chunking / rate-limit concerns live in
``llm.embeddings.embed_texts``; this agent is now a thin orchestrator that
unwraps the session state, calls the shim, and zips vectors back onto the
fact dicts. The previous ``_jina_embed_batch`` block was deleted along
with the inline ``httpx`` import.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from beever_atlas.llm.embeddings import embed_texts

logger = logging.getLogger(__name__)


class EmbedderAgent(BaseAgent):
    """Calls the embedding shim to populate ``text_vector`` on each fact.

    Reads  : ``session.state["classified_facts"]``
    Writes : ``session.state["embedded_facts"]``
    """

    model_config = {"arbitrary_types_allowed": True}

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
        # The shim handles chunking (100 / batch), retries, and the rate
        # limiter. Per-chunk telemetry already flows through ``embed_log``
        # so this agent only logs start / done.
        from beever_atlas.llm.embedding_runtime import EmbeddingMigrationInProgress

        try:
            vectors = await embed_texts(texts)
        except EmbeddingMigrationInProgress:
            # The re-embed migration is in flight. We must NOT call the
            # new model on these facts because the result would be a
            # mixed-dim collection. Skip vectors for this batch — the
            # persister tolerates ``text_vector=[]`` and will produce
            # rows that the next sync (after migration completes) can
            # patch up via the back-fill flow.
            logger.warning(
                "EmbedderAgent: migration in progress — emitting empty "
                "vectors for batch=%s job_id=%s channel=%s facts=%d. "
                "Re-run sync after migration completes to back-fill.",
                batch_num,
                sync_job_id,
                channel_id,
                len(texts),
            )
            empty: list[dict[str, Any]] = []
            for fact in facts:
                enriched = dict(fact)
                enriched["text_vector"] = []
                empty.append(enriched)
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                actions=EventActions(state_delta={"embedded_facts": empty}),
            )
            return

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
