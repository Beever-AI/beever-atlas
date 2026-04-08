"""WikiBuilder orchestrates the gather → compile → cache pipeline."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from beever_atlas.llm import get_llm_provider
from beever_atlas.models.domain import WikiMetadata, WikiResponse
from beever_atlas.wiki.compiler import WikiCompiler
from beever_atlas.wiki.data_gatherer import WikiDataGatherer

logger = logging.getLogger(__name__)


class WikiBuilder:
    """Orchestrates the three-phase wiki generation pipeline."""

    def __init__(self, weaviate_store, graph_store, wiki_cache) -> None:
        self._gatherer = WikiDataGatherer(weaviate_store, graph_store)
        self._compiler = WikiCompiler()
        self._cache = wiki_cache
        self._active_generations: set[str] = set()

    async def generate_wiki(self, channel_id: str) -> WikiResponse:
        """Full pipeline: gather → compile → cache. Returns the WikiResponse."""
        if channel_id in self._active_generations:
            raise RuntimeError("already_running")

        self._active_generations.add(channel_id)
        model_name = get_llm_provider().get_model_string("wiki_compiler")

        try:
            start = time.monotonic()

            # Phase 1: gather
            await self._cache.set_generation_status(
                channel_id=channel_id,
                status="running",
                stage="gathering",
                stage_detail="Fetching memories, entities, and topics from stores",
                model=model_name,
            )
            data = await self._gatherer.gather(channel_id)

            # Phase 2: compile (with progress tracking)
            clusters = data.get("clusters", [])
            # Match compiler's conditional fixed-page plan so progress totals stay accurate.
            total_faq = sum(len(c.faq_candidates) for c in clusters)
            has_decisions = len(data.get("decisions", [])) > 0
            has_faq = total_faq > 0
            has_glossary = len((data["channel_summary"].glossary_terms or [])) > 0
            has_resources = any(
                (fact.source_media_urls or fact.source_link_urls)
                for fact in data.get("media_facts", [])
            )
            fixed_pages_total = (
                3  # overview, people, activity (always generated)
                + (1 if has_decisions else 0)
                + (1 if has_faq else 0)
                + (1 if has_glossary else 0)
                + (1 if has_resources else 0)
            )
            total_pages = fixed_pages_total + len(clusters)

            await self._cache.set_generation_status(
                channel_id=channel_id,
                status="running",
                stage="compiling",
                stage_detail="Starting page compilation",
                pages_total=total_pages,
                pages_done=0,
                pages_completed=[],
                model=model_name,
            )

            async def on_page_compiled(page_id: str, pages_done: int, pages_completed: list[str]) -> None:
                await self._cache.set_generation_status(
                    channel_id=channel_id,
                    status="running",
                    stage="compiling",
                    stage_detail=f"Compiled {page_id}",
                    pages_total=total_pages,
                    pages_done=pages_done,
                    pages_completed=pages_completed,
                    model=model_name,
                )

            pages = await self._compiler.compile(data, on_page_compiled=on_page_compiled)

            # Phase 3: assemble & save
            await self._cache.set_generation_status(
                channel_id=channel_id,
                status="running",
                stage="saving",
                stage_detail="Saving wiki to cache",
                pages_total=total_pages,
                pages_done=len(pages),
                pages_completed=list(pages.keys()),
                model=model_name,
            )

            channel_summary = data["channel_summary"]
            structure = self._compiler.build_structure(
                channel_id=channel_id,
                channel_name=channel_summary.channel_name,
                platform=channel_summary.channel_id and "slack",
                pages=pages,
            )

            duration_ms = int((time.monotonic() - start) * 1000)
            overview = pages.get("overview")
            if overview is None:
                raise RuntimeError("overview page compilation failed")

            metadata = WikiMetadata(
                memory_count=data["total_facts"],
                entity_count=data["total_entities"],
                media_count=channel_summary.media_count,
                page_count=len(pages),
                generation_duration_ms=duration_ms,
            )

            now = datetime.now(tz=UTC)
            wiki = WikiResponse(
                channel_id=channel_id,
                channel_name=channel_summary.channel_name,
                platform="slack",
                generated_at=now,
                is_stale=False,
                structure=structure,
                overview=overview,
                metadata=metadata,
            )

            wiki_dict = wiki.model_dump(mode="json")
            # Flatten pages into the cache doc
            wiki_dict["pages"] = {p_id: p.model_dump(mode="json") for p_id, p in pages.items()}

            await self._cache.save_wiki(channel_id, wiki_dict)

            # Mark generation complete
            await self._cache.set_generation_status(
                channel_id=channel_id,
                status="done",
                stage="done",
                stage_detail=f"Generated {len(pages)} pages in {duration_ms / 1000:.1f}s",
                pages_total=len(pages),
                pages_done=len(pages),
                pages_completed=list(pages.keys()),
                model=model_name,
            )

            logger.info(
                "WikiBuilder: generated wiki channel=%s pages=%d duration_ms=%d",
                channel_id, len(pages), duration_ms,
            )
            return wiki

        except Exception as exc:
            await self._cache.set_generation_status(
                channel_id=channel_id,
                status="failed",
                stage="error",
                stage_detail=str(exc)[:200],
                model=model_name,
                error=str(exc)[:500],
            )
            raise

        finally:
            self._active_generations.discard(channel_id)

    async def refresh_wiki(self, channel_id: str) -> None:
        """Async wrapper for background generation."""
        try:
            await self.generate_wiki(channel_id)
        except RuntimeError as exc:
            if "already_running" in str(exc):
                logger.info("WikiBuilder: generation already in progress for %s", channel_id)
            else:
                raise
