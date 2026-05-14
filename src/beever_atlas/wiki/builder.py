"""WikiBuilder orchestrates the gather → compile → cache pipeline."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from beever_atlas.llm import get_llm_provider
from beever_atlas.models.domain import WikiMetadata, WikiResponse
from beever_atlas.wiki.compiler import WikiCompiler
from beever_atlas.wiki.data_gatherer import WikiDataGatherer

logger = logging.getLogger(__name__)


# Module-level per-channel lock registry. Because the API layer constructs a
# fresh WikiBuilder per request, instance-level locks cannot serialize
# concurrent generations. These module-level structures survive across
# WikiBuilder instances and ensure only one generation runs per channel at a
# time (regardless of target_lang).
def _detect_platform(channel_id: str) -> str:
    """Infer platform from channel_id format.

    Mirrors ``beever_atlas.api.channels._detect_platform_from_channel_id``
    but is duplicated here to avoid a wiki→api import cycle. Falls back to
    ``"unknown"`` rather than hardcoding ``"slack"``.
    """
    if re.match(r"^[CDG][A-Z0-9]{8,}$", channel_id):
        return "slack"
    if re.match(r"^\d{17,20}$", channel_id):
        return "discord"
    return "unknown"


_CHANNEL_LOCKS: dict[str, asyncio.Lock] = {}
_CHANNEL_LOCKS_GUARD = asyncio.Lock()
_ACTIVE_GENERATIONS: set[str] = set()


async def _get_channel_lock(channel_id: str) -> asyncio.Lock:
    async with _CHANNEL_LOCKS_GUARD:
        lock = _CHANNEL_LOCKS.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            _CHANNEL_LOCKS[channel_id] = lock
        return lock


def _cache_key_for_lang(channel_id: str, target_lang: str) -> str:
    """Mirror :func:`WikiCache._cache_key` so the build-input hash lookup
    finds the same row the cache uses (suffixed key for non-default lang)."""
    if target_lang and target_lang != "en":
        return f"{channel_id}__{target_lang}"
    return channel_id


def _compute_build_input_hash(gathered: dict) -> str:
    """Stable SHA-256 over the parts of ``gathered`` that drive the
    Builder's compile output. When this hash matches the prior build,
    the wiki cannot have changed (modulo prompt edits, which the
    operator handles via ``force=true``).

    Inputs hashed:
      * Sorted cluster ids + their member_count + summary
      * Channel summary fact_count + glossary terms count
      * Sorted decision ids
      * Sorted cluster_facts fact_id sets (unordered per cluster)

    Excluded: timestamps, generated_at, monotonic counters.
    """
    import hashlib

    parts: list[str] = []
    clusters = gathered.get("clusters") or []
    cluster_keys = sorted(
        f"{getattr(c, 'id', '')}|{getattr(c, 'member_count', 0)}|{getattr(c, 'summary', '') or ''}"
        for c in clusters
    )
    parts.extend(cluster_keys)
    cs = gathered.get("channel_summary")
    if cs is not None:
        parts.append(f"summary_facts={getattr(cs, 'fact_count', 0)}")

        # ``glossary_terms`` shape changed from ``list[str]`` (legacy
        # extraction schema) to ``list[GlossaryTerm]`` (consolidation
        # schema) — and Weaviate deserialises whatever JSON was last
        # written. Coerce to comparable strings before sorting so the
        # hash works regardless of which shape is in flight.
        def _glossary_key(t):
            if isinstance(t, str):
                return t
            if isinstance(t, dict):
                return str(t.get("term") or "")
            return str(getattr(t, "term", "") or "")

        _glossary_keys = sorted(
            _glossary_key(t) for t in (getattr(cs, "glossary_terms", None) or [])
        )
        parts.append("glossary=" + ",".join(_glossary_keys))
    decisions = gathered.get("decisions") or []
    parts.append("decisions=" + ",".join(sorted(getattr(d, "id", "") for d in decisions)))
    cluster_facts = gathered.get("cluster_facts") or {}
    for cid in sorted(cluster_facts.keys()):
        facts = cluster_facts.get(cid) or []
        parts.append(f"facts:{cid}=" + ",".join(sorted(getattr(f, "id", "") for f in facts)))
    media_facts = gathered.get("media_facts") or []
    parts.append("media=" + ",".join(sorted(getattr(f, "id", "") for f in media_facts)))
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class WikiBuilder:
    """Orchestrates the three-phase wiki generation pipeline."""

    def __init__(self, weaviate_store, graph_store, wiki_cache) -> None:
        self._gatherer = WikiDataGatherer(weaviate_store, graph_store)
        # Compiler is recreated per-request to carry target_lang/source_lang.
        self._gatherer_bound = True
        self._cache = wiki_cache

    def _make_compiler(self, target_lang: str, source_lang: str) -> WikiCompiler:
        return WikiCompiler(target_lang=target_lang, source_lang=source_lang)

    async def generate_wiki(
        self,
        channel_id: str,
        *,
        target_lang: str | None = None,
        source_lang: str | None = None,
        force_restructure: bool = False,
        force_recompile: bool = False,
    ) -> WikiResponse:
        """Full pipeline: gather → compile → cache. Returns the WikiResponse.

        Args:
            channel_id: Channel to generate for.
            target_lang: BCP-47 tag for the rendered output. Defaults to
                settings.default_target_language when None.
            source_lang: BCP-47 tag of the underlying memory. When None, the
                builder will look it up from the channel's sync state (falling
                back to "en" for channels that predate language detection).
            force_recompile: When True, bypass the kind_schema_hash skip
                check and recompile every page even if the canonical input
                hasn't changed since the last build. Used by the Regenerate
                button's "Force recompile" override.
        """
        # Resolve languages.
        from beever_atlas.infra.config import get_settings as _get_settings

        _settings = _get_settings()
        if target_lang is None:
            target_lang = _settings.default_target_language or "en"
        resolved_source_lang: str = "en"
        if source_lang is not None:
            resolved_source_lang = source_lang
        else:
            # Try to read the channel's primary_language from sync state.
            try:
                from beever_atlas.stores import get_stores as _gs

                _state = await _gs().mongodb.get_channel_sync_state(channel_id)
                if _state is not None:
                    resolved_source_lang = getattr(_state, "primary_language", "en") or "en"
            except Exception:  # noqa: BLE001
                pass
        source_lang = resolved_source_lang

        # Serialize generations per-channel via module-level lock (API layer
        # creates a fresh WikiBuilder per request, so instance locks won't
        # serialize concurrent requests). One run at a time per channel,
        # regardless of target_lang.
        channel_lock = await _get_channel_lock(channel_id)
        async with channel_lock:
            return await self._generate_wiki_locked(
                channel_id=channel_id,
                target_lang=target_lang,
                source_lang=source_lang,
                force_restructure=force_restructure,
                force_recompile=force_recompile,
            )

    async def _generate_wiki_locked(
        self,
        *,
        channel_id: str,
        target_lang: str,
        source_lang: str,
        force_restructure: bool = False,
        force_recompile: bool = False,
    ) -> WikiResponse:
        _ACTIVE_GENERATIONS.add(channel_id)
        compiler = self._make_compiler(target_lang=target_lang, source_lang=source_lang)
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
                target_lang=target_lang,
            )
            data = await self._gatherer.gather(channel_id)

            # wiki-redesign-gap-fill / Group 3 — build-input hash skip.
            # Compute a stable fingerprint of the gathered input. If the
            # prior cached wiki has the same fingerprint and the operator
            # didn't pass force_recompile, return the cached wiki and emit
            # a cost summary with calls_total=0. Operators get the
            # observable benefit ("stable Regenerate makes zero LLM calls")
            # without restructuring the per-page compile loop.
            build_input_hash = _compute_build_input_hash(data)
            if not force_recompile:
                try:
                    prior_doc = await self._cache._collection.find_one(  # noqa: SLF001
                        {"channel_id": _cache_key_for_lang(channel_id, target_lang)},
                        {"_id": 0, "build_input_hash": 1, "pages": 1},
                    )
                    if (
                        prior_doc is not None
                        and prior_doc.get("build_input_hash") == build_input_hash
                        and prior_doc.get("pages")
                    ):
                        prior_page_count = len(prior_doc.get("pages") or {})
                        duration_ms_skip = int((time.monotonic() - start) * 1000)
                        try:
                            from beever_atlas.services.pipeline_events import (
                                EVENT_TYPE_COST_SUMMARY,
                                get_pipeline_events,
                            )

                            get_pipeline_events().record(
                                channel_id=channel_id,
                                stage="wiki_build",
                                label=(
                                    f"Build skipped: 0 LLM calls "
                                    f"({prior_page_count} pages reused, "
                                    f"{duration_ms_skip / 1000:.1f}s)"
                                ),
                                event_type=EVENT_TYPE_COST_SUMMARY,
                                payload={
                                    "calls_total": 0,
                                    "calls_skipped": prior_page_count,
                                    "duration_ms": duration_ms_skip,
                                    "skip_reason": "build_input_hash_match",
                                },
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        await self._cache.set_generation_status(
                            channel_id=channel_id,
                            status="done",
                            stage="done",
                            stage_detail=(
                                f"No corpus changes — reused {prior_page_count} "
                                "cached pages (zero LLM calls)"
                            ),
                            pages_total=prior_page_count,
                            pages_done=prior_page_count,
                            pages_completed=list(prior_doc.get("pages", {}).keys()),
                            model=model_name,
                            target_lang=target_lang,
                        )
                        # Reconstruct WikiResponse from the prior cache doc.
                        return WikiResponse.model_validate(
                            {
                                k: v
                                for k, v in prior_doc.items()
                                if k not in ("pages", "build_input_hash")
                            }
                        )
                except Exception:  # noqa: BLE001 — fall through to a real build
                    logger.exception(
                        "WikiBuilder: build_input_hash skip lookup failed channel=%s — "
                        "falling through to a full compile",
                        channel_id,
                    )

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
                target_lang=target_lang,
            )

            async def on_page_compiled(
                page_id: str, pages_done: int, pages_completed: list[str]
            ) -> None:
                await self._cache.set_generation_status(
                    channel_id=channel_id,
                    status="running",
                    stage="compiling",
                    stage_detail=f"Compiled {page_id}",
                    pages_total=total_pages,
                    pages_done=pages_done,
                    pages_completed=pages_completed,
                    model=model_name,
                    target_lang=target_lang,
                )

            # wiki-redesign-gap-fill / Group 3+4 — pre-compile pass: detect
            # frozen pages so the Builder can preserve their existing prose
            # byte-identical and emit a skip event per frozen page. Done as
            # a best-effort lookup so a wiki_pages-store hiccup never blocks
            # the build itself.
            frozen_pages: dict[str, Any] = {}
            try:
                from beever_atlas.wiki.page_store import WikiPageStore

                _ps = WikiPageStore(db=self._cache._db)  # noqa: SLF001
                _existing = await _ps.list_pages(channel_id, target_lang=target_lang)
                for _ep in _existing or []:
                    if getattr(_ep, "curation_mode", "auto") == "frozen":
                        frozen_pages[_ep.page_id] = _ep
            except Exception:  # noqa: BLE001 — frozen detection is best-effort
                pass

            pages = await compiler.compile(data, on_page_compiled=on_page_compiled)

            # Apply frozen overrides: a frozen page's existing content is
            # restored byte-identical from wiki_pages, replacing whatever the
            # compiler produced. Emit a skip event so operators see the
            # cleanup surface in the SyncMonitor.
            if frozen_pages:
                from beever_atlas.services.pipeline_events import (
                    EVENT_TYPE_WIKI_UPDATE,
                    get_pipeline_events,
                )

                for _fp_id, _fp in frozen_pages.items():
                    if _fp_id in pages:
                        pages[_fp_id] = _fp
                    try:
                        get_pipeline_events().record(
                            channel_id=channel_id,
                            stage="wiki_build",
                            label=f"Skipped (frozen): {_fp_id}",
                            event_type=EVENT_TYPE_WIKI_UPDATE,
                            payload={
                                "page_id": _fp_id,
                                "page_title": getattr(_fp, "title", _fp_id),
                                "action": "skipped_frozen",
                            },
                        )
                    except Exception:  # noqa: BLE001
                        pass

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
                target_lang=target_lang,
            )

            channel_summary = data["channel_summary"]
            platform = _detect_platform(channel_id)
            structure = compiler.build_structure(
                channel_id=channel_id,
                channel_name=channel_summary.channel_name,
                platform=platform,
                pages=pages,
            )

            # ``llm-wiki-folder-structure`` Phase C — optional folder
            # plan + folder index synthesis layered on top of the
            # already-built flat structure. Runs only when the
            # ``WIKI_FOLDER_PLANNER`` flag is ON AND the channel has
            # enough topics to warrant folders. Failures (planner
            # falls back to flat, no folders produced) silently
            # leave the structure unchanged.
            try:
                from beever_atlas.infra.config import get_settings as _gs2
                from beever_atlas.wiki.structure import WikiStructurePlanner

                _settings2 = _gs2()
                if _settings2.wiki_folder_planner or force_restructure:
                    # CRITICAL: send PAGE SLUGS (not cluster UUIDs) as
                    # the planner's cluster ids. The planner output's
                    # ``child_slugs`` must round-trip through the
                    # ``leaves_by_slug`` dict below — and that dict is
                    # keyed by ``page.slug`` (the slugified title), not
                    # the cluster UUID. Sending the page slug as the
                    # planner identifier ensures the LLM's response
                    # references real page slugs the consumer can
                    # resolve. The cluster UUID is opaque to the LLM
                    # anyway; the slug is the human-meaningful handle.
                    from beever_atlas.wiki.compiler import _slugify

                    cluster_dicts: list[dict] = []
                    for c in clusters:
                        c_id = getattr(c, "id", "") or ""
                        c_title = getattr(c, "title", "") or ""
                        page_slug = _slugify(c_title) or c_id
                        cluster_dicts.append(
                            {
                                "id": page_slug,
                                "title": c_title,
                                "summary": getattr(c, "summary", "") or "",
                                "member_count": getattr(c, "member_count", 0) or 0,
                                "key_entities": [
                                    e.model_dump() if hasattr(e, "model_dump") else e
                                    for e in (getattr(c, "key_entities", []) or [])
                                ],
                            }
                        )

                    # Use the compiler's existing async LLM helper as
                    # the planner's injected callable. This piggy-backs
                    # on the compiler's retry/parsing/safety logic and
                    # avoids re-implementing provider invocation.
                    async def _llm_call(prompt: str) -> str:
                        return await compiler._llm_generate_json(  # type: ignore[attr-defined]
                            prompt, temperature=0.2, page_kind="topic"
                        )

                    planner = WikiStructurePlanner(
                        llm=_llm_call,
                        min_topics_for_folders=_settings2.wiki_min_topics_for_folders,
                    )
                    plan = await planner.plan_async(
                        channel_summary=getattr(channel_summary, "summary", "")
                        or getattr(channel_summary, "description", "")
                        or "",
                        clusters=cluster_dicts,
                        fact_graph=None,
                    )
                    logger.info(
                        "wiki_folder_planner_result channel=%s folders=%d leaves=%d fallback=%s",
                        channel_id,
                        len(plan.folders),
                        len(plan.leaves),
                        plan.fallback_reason or "ok",
                    )

                    if plan.folders:
                        # Build a slug → page lookup so compile_folders
                        # can find the leaves to feed into FOLDER_INDEX_PROMPT.
                        leaves_by_slug: dict[str, Any] = {}
                        for p in pages.values():
                            if getattr(p, "slug", None):
                                leaves_by_slug[p.slug] = p
                        # Surface the compiled-topic set so the folder
                        # synthesis path can constrain ``[[Title]]`` wikilink
                        # targets the same way Glossary / People / Topic do.
                        # Fall back to every cluster title when the key is
                        # absent (legacy/test entry points).
                        compiled_topic_titles_for_folders = data.get("_compiled_topic_titles") or [
                            getattr(c, "title", "") or "" for c in data.get("clusters", []) or []
                        ]
                        compiled_topic_titles_for_folders = [
                            t for t in compiled_topic_titles_for_folders if t
                        ]
                        folder_pages = await compiler.compile_folders(
                            plan=plan,
                            leaves_by_slug=leaves_by_slug,
                            compiled_topic_titles=compiled_topic_titles_for_folders,
                        )
                        # Add folder pages to the page dict so they
                        # round-trip through the cache.
                        for fp_id, fp in folder_pages.items():
                            pages[fp_id] = fp
                        # Re-shape the structure to put folders at root
                        # with their leaves nested inside.
                        structure = compiler.apply_folder_plan_to_structure(
                            structure,
                            plan=plan,
                            folder_pages=folder_pages,
                        )
            except Exception:  # noqa: BLE001 — planner is best-effort
                logger.exception(
                    "wiki_folder_planner_unhandled channel=%s — falling back to flat structure",
                    channel_id,
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
                platform=platform,
                generated_at=now,
                is_stale=False,
                structure=structure,
                overview=overview,
                metadata=metadata,
            )

            wiki_dict = wiki.model_dump(mode="json")
            # Flatten pages into the cache doc
            wiki_dict["pages"] = {p_id: p.model_dump(mode="json") for p_id, p in pages.items()}
            # wiki-redesign-gap-fill / Group 3 — stash the build-input
            # fingerprint so the next Regenerate can short-circuit when
            # gathered data hasn't changed.
            wiki_dict["build_input_hash"] = build_input_hash

            await self._cache.save_wiki(channel_id, wiki_dict, target_lang=target_lang)

            # First-sync gate fix: seed the ``wiki_pages`` collection with
            # one minimal row per compiled page. The maintainer's
            # incremental path reads from ``wiki_pages`` and DEFERS every
            # dirty entry until rows exist there. Before this seed, Builder
            # only wrote to ``wiki_cache`` so the maintainer's first-sync
            # gate stayed permanently active — ``_rewrite_page`` (the only
            # writer to ``wiki_pages``) was never reachable because the
            # gate blocks it. Result was a chicken-and-egg deadlock where
            # every memory_settled logged ``Builder hasn't run yet —
            # first-sync gate`` even after a successful build.
            #
            # Note the type translation: ``compiler.compile`` returns
            # ``models.domain.WikiPage`` (rendered-content shape used by
            # the UI cache), while ``WikiPageStore`` persists
            # ``models.persistence.WikiPage`` (incremental-maintenance
            # shape). The seed copies the identity fields (page_id, slug,
            # title) plus a derived ``kind``. The maintainer will fill
            # ``sections`` / ``last_facts_seen`` on its first
            # ``apply_update`` of each page; the UI doesn't read this
            # collection so empty sections are safe in the meantime.
            try:
                from beever_atlas.wiki.page_store import WikiPageStore
                from beever_atlas.models.persistence import (
                    WikiPage as _PersistedWikiPage,
                )
                from beever_atlas.services.wiki_maintainer import (
                    derive_kind_from_page_id as _derive_kind,
                )

                _seed_ps = WikiPageStore(db=self._cache._db)  # noqa: SLF001
                _seeded = 0
                for _dp_id, _dp in pages.items():
                    try:
                        _seed = _PersistedWikiPage(
                            channel_id=channel_id,
                            target_lang=target_lang,
                            page_id=_dp_id,
                            title=getattr(_dp, "title", "") or "",
                            slug=getattr(_dp, "slug", "") or _dp_id.replace(":", "-"),
                            kind=_derive_kind(_dp_id),
                        )
                        await _seed_ps.save_page(_seed)
                        _seeded += 1
                    except Exception:  # noqa: BLE001 — per-page isolation
                        logger.exception(
                            "WikiBuilder: wiki_pages seed failed channel=%s page=%s",
                            channel_id,
                            _dp_id,
                        )
                logger.info(
                    "WikiBuilder: seeded wiki_pages channel=%s rows=%d/%d",
                    channel_id,
                    _seeded,
                    len(pages),
                )
            except Exception:  # noqa: BLE001 — seeding is best-effort
                logger.exception(
                    "WikiBuilder: wiki_pages seed step crashed channel=%s",
                    channel_id,
                )

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
                target_lang=target_lang,
            )

            logger.info(
                "WikiBuilder: generated wiki channel=%s pages=%d duration_ms=%d",
                channel_id,
                len(pages),
                duration_ms,
            )
            # wiki-redesign-gap-fill / Group 3 — emit per-build cost summary
            # so operators can see the recompile-skip savings live in the
            # SyncMonitor's right pane. Best-effort.
            try:
                from beever_atlas.services.pipeline_events import (
                    EVENT_TYPE_COST_SUMMARY,
                    get_pipeline_events,
                )

                _calls_skipped = len(frozen_pages)
                get_pipeline_events().record(
                    channel_id=channel_id,
                    stage="wiki_build",
                    label=(
                        f"Build complete: {len(pages)} pages "
                        f"({_calls_skipped} skipped, {duration_ms / 1000:.1f}s)"
                    ),
                    event_type=EVENT_TYPE_COST_SUMMARY,
                    payload={
                        "calls_total": len(pages),
                        "calls_skipped": _calls_skipped,
                        "duration_ms": duration_ms,
                    },
                )
            except Exception:  # noqa: BLE001
                pass
            return wiki

        except Exception as exc:
            await self._cache.set_generation_status(
                channel_id=channel_id,
                status="failed",
                stage="error",
                stage_detail=str(exc)[:200],
                model=model_name,
                error=str(exc)[:500],
                target_lang=target_lang,
            )
            raise

        finally:
            _ACTIVE_GENERATIONS.discard(channel_id)

    async def refresh_wiki(
        self,
        channel_id: str,
        *,
        target_lang: str | None = None,
        force_restructure: bool = False,
        force_recompile: bool = False,
    ) -> None:
        """Async wrapper for background generation.

        Serialized per-channel via module-level lock; concurrent invocations
        await rather than rejecting.

        ``force_restructure`` (Phase E of llm-wiki-folder-structure) bypasses
        the ``WIKI_FOLDER_PLANNER`` flag and forces the structure planner
        to run on this single regenerate. Used by the "Restructure tree"
        operator action without flipping the flag globally.

        ``force_recompile`` (wiki-redesign-gap-fill task 3.6) bypasses the
        build-input hash skip so a corpus-unchanged Regenerate still walks
        the full compile path. Use when a prompt edit needs to be picked up.
        """
        await self.generate_wiki(
            channel_id,
            target_lang=target_lang,
            force_restructure=force_restructure,
            force_recompile=force_recompile,
        )
