"""Wiki generation API endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.infra.config import get_settings
from beever_atlas.stores import get_stores
from beever_atlas.wiki.cache import WikiCache


def _render_citations_markdown(citations: list[dict]) -> list[str]:
    """Render a citation list to markdown lines (Sources section).

    Skips the trailing ``[](permalink)`` tail when ``permalink`` is
    empty so the export doesn't show an ugly ``[]()`` artifact for
    extractions that lacked thread URLs (#10 in post-export wiki review).
    Returns an empty list when ``citations`` is empty so callers can
    branch on truthiness without conditional formatting.
    """
    if not citations:
        return []
    lines: list[str] = ["\n\n### Sources\n"]
    for cit in citations:
        author = cit.get("author", "")
        ts = cit.get("timestamp", "")
        excerpt = cit.get("text_excerpt", "")
        link = (cit.get("permalink") or "").strip()
        cit_id = cit.get("id", "")
        head = f"- {cit_id} @{author} · {ts} — {excerpt}".rstrip()
        if link:
            lines.append(f"{head} [{link}]({link})")
        else:
            lines.append(head)
    return lines


def _build_download_headers(filename: str, fallback: str) -> dict[str, str]:
    """Build the Content-Disposition + nosniff headers for a markdown
    download response. Strips CRLF / quotes from the ASCII fallback to
    prevent header-injection. ``fallback`` is used when the filename
    drops to empty after ASCII coercion."""
    safe_ascii = (
        filename.encode("ascii", "ignore")
        .decode()
        .replace('"', "")
        .replace("\r", "")
        .replace("\n", "")
    ) or fallback
    encoded = quote(filename, safe="")
    return {
        "Content-Disposition": (
            f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{encoded}"
        ),
        "X-Content-Type-Options": "nosniff",
    }


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels/{channel_id}/wiki", tags=["wiki"])


_wiki_cache: WikiCache | None = None


def _get_cache() -> WikiCache:
    global _wiki_cache
    if _wiki_cache is None:
        settings = get_settings()
        _wiki_cache = WikiCache(settings.mongodb_uri)
    return _wiki_cache


async def _resolve_target_lang(channel_id: str, requested: str | None) -> str:
    """Resolve and validate the requested target language for a channel."""
    settings = get_settings()
    default_lang = settings.default_target_language

    if requested is None:
        return default_lang

    # Allow-list is the global supported language set (union with default).
    # Fresh channels with no sync state must not be pinned to "en".
    allowed = set(settings.supported_languages_list) | {default_lang}

    stores = get_stores()
    try:
        state = await stores.mongodb.get_channel_sync_state(channel_id)
    except Exception:  # noqa: BLE001
        state = None
    if state is not None:
        primary_language = getattr(state, "primary_language", None)
        if primary_language:
            allowed.add(primary_language)

    if requested in allowed:
        return requested
    raise HTTPException(
        status_code=400,
        detail={"error": "unsupported_target_lang", "allowed": sorted(allowed)},
    )


@router.get("")
async def get_wiki(
    channel_id: str,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Return the full cached wiki for a channel."""
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    lang = await _resolve_target_lang(channel_id, target_lang)
    doc = await cache.get_wiki(channel_id, target_lang=lang)
    if doc is None:
        raise HTTPException(status_code=404, detail="No wiki available yet")
    return doc


@router.get("/pages/{page_id}")
async def get_wiki_page(
    channel_id: str,
    page_id: str,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Return a single wiki page from cache."""
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    lang = await _resolve_target_lang(channel_id, target_lang)
    page = await cache.get_page(channel_id, page_id, target_lang=lang)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page {page_id!r} not found")
    return page


@router.get("/entity-pages")
async def list_entity_pages(
    channel_id: str,
    target_lang: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    principal: Principal = Depends(require_user),
) -> dict:
    """Debug view: list raw ``kind=entity`` wiki pages for a channel.

    Post wiki-redesign-gap-fill the per-entity page set is being cleaned up:
    the archive script flips ``archived=true`` on legacy rows. This endpoint
    filters those out by default so operators see the post-cleanup surface.
    Pass ``?include_archived=true`` to inspect archived rows during the
    retention window. The default response includes ``archived_count`` so
    operators can confirm the archive script ran.
    """
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    await cache._ensure_db()
    lang = await _resolve_target_lang(channel_id, target_lang)

    from beever_atlas.wiki.page_store import WikiPageStore

    page_store = WikiPageStore(db=cache._db)
    pages = await page_store.list_pages_by_kind(
        channel_id=channel_id,
        kind="entity",
        target_lang=lang,
        scope="all",
        include_archived=include_archived,
    )
    archived_count = 0
    if not include_archived:
        all_pages = await page_store.list_pages_by_kind(
            channel_id=channel_id,
            kind="entity",
            target_lang=lang,
            scope="all",
            include_archived=True,
        )
        archived_count = sum(1 for p in all_pages if getattr(p, "archived", False))
    return {
        "channel_id": channel_id,
        "target_lang": lang,
        "count": len(pages),
        "archived_count": archived_count,
        "include_archived": include_archived,
        "pages": [
            {
                "page_id": p.page_id,
                "title": p.title or p.page_id,
                "slug": p.slug,
                "fact_count": len(p.last_facts_seen or []),
                "archived": bool(getattr(p, "archived", False)),
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in pages
        ],
    }


@router.get("/structure")
async def get_wiki_structure(
    channel_id: str,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Return the wiki sidebar structure without page content."""
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    lang = await _resolve_target_lang(channel_id, target_lang)
    doc = await cache.get_structure(channel_id, target_lang=lang)
    if doc is None:
        raise HTTPException(status_code=404, detail="No wiki structure available yet")
    return doc


@router.get("/versions")
async def list_wiki_versions(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> list[dict]:
    """Return a list of archived wiki version summaries for a channel."""
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    return await cache.version_store.list_versions(channel_id)


@router.get("/versions/{version_number}")
async def get_wiki_version(
    channel_id: str,
    version_number: int,
    principal: Principal = Depends(require_user),
) -> dict:
    """Return the full content of a specific archived wiki version."""
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    version = await cache.version_store.get_version(channel_id, version_number)
    if version is None:
        raise HTTPException(
            status_code=404,
            detail=f"Version {version_number} not found for this channel",
        )
    return version


@router.get("/versions/{version_number}/pages/{page_id}")
async def get_wiki_version_page(
    channel_id: str,
    version_number: int,
    page_id: str,
    principal: Principal = Depends(require_user),
) -> dict:
    """Return a single page from an archived wiki version."""
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    page = await cache.version_store.get_version_page(channel_id, version_number, page_id)
    if page is None:
        raise HTTPException(
            status_code=404,
            detail=f"Page {page_id!r} not found in version {version_number}",
        )
    return page


@router.get("/download")
async def download_wiki_markdown(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> PlainTextResponse:
    """Export the full wiki as a single Markdown file."""
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    doc = await cache.get_wiki(channel_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="No wiki available yet")

    structure = doc.get("structure", {})
    pages_dict = doc.get("pages", {})
    channel_name = structure.get("channel_name", channel_id)

    # Build page order from structure
    page_order: list[str] = []
    for node in structure.get("pages", []):
        page_order.append(node["id"])
        for child in node.get("children", []):
            page_order.append(child["id"])

    # Assemble Markdown
    from beever_atlas.wiki.modules.narrative_markdown import (
        narrative_sections_to_markdown,
    )
    from beever_atlas.wiki.modules.modules_markdown import modules_to_markdown

    parts: list[str] = [f"# {channel_name} — Wiki\n"]
    for page_id in page_order:
        page = pages_dict.get(page_id)
        if not page:
            continue
        title = page.get("title", page_id)
        section = page.get("section_number", "")
        prefix = f"{section} " if section else ""
        parts.append(f"\n---\n\n## {prefix}{title}\n")
        # ``narrative_sections`` is the structured spotlight body — render
        # it BEFORE the legacy module-substituted content so the export
        # matches what the user sees on the rendered page (article first,
        # then Reference & Evidence appendix). Pages without a narrative
        # payload skip this branch and the export carries only the legacy
        # content as before (backward compat).
        narrative_md = narrative_sections_to_markdown(page.get("narrative_sections") or [])
        if narrative_md:
            parts.append(narrative_md)
            # Separator between the narrative article and the Reference
            # & Evidence (legacy modules) section, mirroring the live UI.
            parts.append("\n---\n")
        # Serialize ``page.modules[]`` — the structured Reference & Evidence
        # appendix (key_facts, decision_log, timeline, etc.). Previously
        # silently dropped; now emitted between the narrative body and the
        # legacy ``content`` blob so the export mirrors the rendered page.
        modules_md = modules_to_markdown(page.get("modules") or [])
        if modules_md:
            parts.append(modules_md)
            parts.append("\n---\n")
        parts.append(page.get("content", ""))
        parts.extend(_render_citations_markdown(page.get("citations", [])))
        parts.append("\n")

    md_content = "\n".join(parts)
    filename = f"{channel_name.replace(' ', '-').lower()}-wiki.md"

    return PlainTextResponse(
        content=md_content,
        media_type="text/markdown",
        headers=_build_download_headers(filename, fallback="wiki.md"),
    )


@router.get("/pages/{page_id}/download")
async def download_wiki_page_markdown(
    channel_id: str,
    page_id: str,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> PlainTextResponse:
    """Export a single wiki page as a Markdown file.

    Serializes narrative_sections, modules (Reference & Evidence), the
    legacy content blob, and citations — the same content the full-wiki
    download uses for each page, scoped to the requested page_id.
    """
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    lang = await _resolve_target_lang(channel_id, target_lang)
    page = await cache.get_page(channel_id, page_id, target_lang=lang)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page {page_id!r} not found")

    from beever_atlas.wiki.modules.narrative_markdown import (
        narrative_sections_to_markdown,
    )
    from beever_atlas.wiki.modules.modules_markdown import modules_to_markdown

    title = page.get("title", page_id)
    section = page.get("section_number", "")
    prefix = f"{section} " if section else ""

    parts: list[str] = [f"# {prefix}{title}\n"]

    narrative_md = narrative_sections_to_markdown(page.get("narrative_sections") or [])
    if narrative_md:
        parts.append(narrative_md)
        parts.append("\n---\n")

    modules_md = modules_to_markdown(page.get("modules") or [])
    if modules_md:
        parts.append(modules_md)
        parts.append("\n---\n")

    parts.append(page.get("content", ""))
    parts.extend(_render_citations_markdown(page.get("citations", [])))

    md_content = "\n".join(parts)

    slug = page.get("slug") or page_id
    filename = f"{slug}-wiki.md"

    return PlainTextResponse(
        content=md_content,
        media_type="text/markdown",
        headers=_build_download_headers(filename, fallback="wiki-page.md"),
    )


@router.get("/status")
async def get_wiki_status(
    channel_id: str,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Return the current wiki generation status for a channel."""
    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    lang = await _resolve_target_lang(channel_id, target_lang)
    status = await cache.get_generation_status(channel_id, target_lang=lang)
    if status is None:
        return {"status": "idle", "channel_id": channel_id}
    return status


@router.post("/regenerate-overview", status_code=202)
async def regenerate_overview(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> dict:
    """Force-restart a stuck auto-overview build.

    Backs the WikiTab "Retry overview generation" affordance shown when
    the loading screen has been stuck for too long (e.g. a hung Gemini
    call upstream of the subscriber's timeout). The handler is
    idempotent — calling it while a build is genuinely running will
    drop the in-flight state but the subscriber's gate-check inside
    ``on_extraction_done`` re-verifies before scheduling new work, so
    two parallel builds cannot fire from a fat-fingered double-click.
    """
    await assert_channel_access(principal, channel_id)
    from beever_atlas.services.auto_overview_subscriber import (
        get_auto_overview_subscriber,
    )

    subscriber = get_auto_overview_subscriber()
    if subscriber is None:
        raise HTTPException(
            status_code=503,
            detail="AutoOverviewSubscriber not initialised — restart the worker.",
        )

    logger.info(
        "wiki_regenerate_overview principal=%s channel=%s — force_reset + re-trigger",
        getattr(principal, "id", "unknown"),
        channel_id,
    )
    subscriber.force_reset(channel_id)
    # Re-trigger generation as a fire-and-forget task. The empty
    # fact_ids list is fine — the gate logic doesn't require facts to
    # act, it only cares about the extraction-done / overview-row /
    # threshold gates. ``on_extraction_done`` awaits the full build
    # inline (multi-minute), so we must NOT await it here or the
    # endpoint would block the user's retry click for the entire
    # rebuild.
    import asyncio as _asyncio

    _asyncio.create_task(subscriber.on_extraction_done(channel_id, fact_ids=[]))
    return {"status": "triggered", "channel_id": channel_id}


@router.post("/refresh", status_code=202)
async def refresh_wiki(
    channel_id: str,
    background_tasks: BackgroundTasks,
    target_lang: str | None = Query(default=None),
    mode: str = Query(
        default="update",
        description=(
            "Wiki action intent: 'update' refreshes pages from current "
            "memories, keeping folder structure intact; 'reorganize' "
            "additionally re-runs the structure planner; 'rebuild' "
            "snapshots the current wiki to history, wipes the cache, "
            "then regenerates from scratch with a fresh folder plan. "
            "Each value maps to a distinct user-facing button."
        ),
    ),
    force: bool = Query(
        default=False,
        description=(
            "When true, bypass the build-input hash skip (wiki-redesign-gap-fill "
            "task 3.6). Use this when a prompt edit landed but the corpus "
            "is unchanged — without ``force=true`` the Builder would reuse "
            "the prior cache."
        ),
    ),
    principal: Principal = Depends(require_user),
) -> dict:
    """Trigger async wiki generation for a channel.

    Unknown ``mode`` values fall back to ``update`` (defensive default so
    a stale frontend never escalates a request unintentionally).
    """
    await assert_channel_access(principal, channel_id)
    from beever_atlas.wiki.builder import WikiBuilder

    valid_modes = {"update", "reorganize", "rebuild"}
    effective_mode = mode if mode in valid_modes else "update"

    stores = get_stores()
    cache = _get_cache()
    lang = await _resolve_target_lang(channel_id, target_lang)
    builder = WikiBuilder(stores.weaviate, stores.graph, cache)

    # ``rebuild`` is destructive. Preserve the current wiki as a
    # recoverable version BEFORE the background task fires so the user
    # always has a rollback point — even if the generator never starts.
    # The actual cache WIPE is deferred to ``_run_generation`` (inside
    # the background task) so a crash between request return and task
    # start can never leave the user with an empty wiki.
    if effective_mode == "rebuild":
        try:
            existing = await cache.get_wiki(channel_id, target_lang=lang)
            if existing:
                archived_version = await cache.version_store.archive(
                    channel_id, existing, target_lang=lang
                )
                await cache.version_store.cleanup(channel_id)
                # Audit log: rebuild is the only mode any authenticated
                # channel member can use to wipe shared content. Emit a
                # structured event so admins can trace who triggered it.
                logger.info(
                    "wiki_rebuild_snapshot principal=%s channel=%s lang=%s archived_version=%d",
                    getattr(principal, "id", "unknown"),
                    channel_id,
                    lang,
                    archived_version,
                )
        except Exception as exc:  # noqa: BLE001 — never block rebuild on archive failure
            logger.exception(
                "wiki_rebuild_snapshot_failed channel=%s lang=%s err=%s",
                channel_id,
                lang,
                exc,
            )

    force_restructure = effective_mode in ("reorganize", "rebuild")
    wipe_before_run = effective_mode == "rebuild"

    # Set status to "running" immediately so the frontend sees it on first poll
    await cache.set_generation_status(
        channel_id,
        status="running",
        stage="starting",
        stage_detail="Initiating wiki generation…",
        target_lang=lang,
    )

    # Rebuild wipes the cache, so the build-input hash check finds nothing
    # to compare against — force_recompile is implied. For update +
    # reorganize, the operator must opt-in via ``force=true``.
    force_recompile = force or (effective_mode == "rebuild")

    background_tasks.add_task(
        _run_generation,
        builder,
        channel_id,
        cache,
        lang,
        force_restructure,
        wipe_before_run,
        force_recompile,
    )
    return {
        "status": "started",
        "channel_id": channel_id,
        "mode": effective_mode,
        "force_recompile": force_recompile,
    }


async def _run_generation(
    builder,
    channel_id: str,
    cache: WikiCache,
    target_lang: str = "en",
    force_restructure: bool = False,
    wipe_before_run: bool = False,
    force_recompile: bool = False,
) -> None:
    from beever_atlas.capabilities.errors import WikiNotReadyError

    try:
        # Wipe is performed HERE (inside the background task) rather than
        # synchronously in the request handler. This eliminates the
        # window where the wiki is empty but the generator hasn't
        # started — if the task scheduler drops the task before this
        # function runs, the existing cache row remains intact.
        if wipe_before_run:
            try:
                await cache.delete_wiki(channel_id, target_lang=target_lang)
                logger.info(
                    "wiki_rebuild_wipe channel=%s lang=%s — cache cleared, generator starting",
                    channel_id,
                    target_lang,
                )
            except Exception as exc:  # noqa: BLE001
                # Wipe failure is recoverable — the generator will
                # overwrite the existing cache via save_wiki anyway. Log
                # for diagnostics but proceed with generation.
                logger.exception(
                    "wiki_rebuild_wipe_failed channel=%s lang=%s err=%s — proceeding with generation",
                    channel_id,
                    target_lang,
                    exc,
                )
        await builder.refresh_wiki(
            channel_id,
            target_lang=target_lang,
            force_restructure=force_restructure,
            force_recompile=force_recompile,
        )
    except WikiNotReadyError as exc:
        # Consolidation was still in progress when the wiki refresh ran.
        # Surface as a retriable "not ready" status so the frontend can
        # show "Wiki generation still warming up — retry in a moment"
        # rather than a generic failure banner.
        logger.warning(
            "Wiki generation not ready channel=%s: %s — consolidation still in progress",
            channel_id,
            exc,
        )
        await cache.set_generation_status(
            channel_id,
            status="not_ready",
            stage="error",
            error=str(exc),
            target_lang=target_lang,
        )
    except Exception as exc:
        logger.error("Wiki generation failed channel=%s: %s", channel_id, exc, exc_info=True)
        await cache.set_generation_status(
            channel_id,
            status="failed",
            stage="error",
            error=str(exc),
            target_lang=target_lang,
        )


# ---------------------------------------------------------------------------
# Wiki health endpoints — lint + on-demand maintenance
# ---------------------------------------------------------------------------


@router.post("/lint")
async def lint_wiki(
    channel_id: str,
    target_lang: str | None = Query(default=None),
    run_coherence_check: bool = Query(default=True),
    principal: Principal = Depends(require_user),
) -> dict:
    """Run wiki lint checks for a channel and return findings.

    Three deterministic checks (orphan / stale / duplicate-section)
    + one bounded LLM coherence pass per page (max 1 call per page).
    Returns ``{channel_id, target_lang, pages_scanned, findings}``.
    Always 200 — an empty findings list is the healthy-channel response.
    """
    from beever_atlas.services.wiki_lint import lint_channel_wiki
    from beever_atlas.wiki.page_store import WikiPageStore

    await assert_channel_access(principal, channel_id)
    cache = _get_cache()
    await cache._ensure_db()
    lang = await _resolve_target_lang(channel_id, target_lang)

    page_store = WikiPageStore(db=cache._db)
    # Live cluster ids — orphan detection compares against the channel's
    # current TopicCluster set in Weaviate.
    stores = get_stores()
    live_cluster_ids: set[str] = set()
    try:
        clusters = await stores.weaviate.list_clusters(channel_id)
        live_cluster_ids = {str(getattr(c, "id", "") or "") for c in clusters}
    except Exception:  # noqa: BLE001
        logger.warning(
            "lint_wiki: could not enumerate live clusters channel=%s — orphan detection will skip",
            channel_id,
        )

    report = await lint_channel_wiki(
        channel_id=channel_id,
        page_store=page_store,
        target_lang=lang,
        live_cluster_ids=live_cluster_ids,
        run_coherence_check=run_coherence_check,
        llm_provider=None,  # Production wires LLMProvider here
    )
    return report.model_dump(mode="json")


@router.post("/maintain")
async def maintain_wiki(
    channel_id: str,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Drain the dirty page queue for one channel.

    Manual mode receiver for the "Maintain Wiki" button. The maintainer
    reads pages where ``is_dirty=True`` and runs the per-page LLM
    section-patch on each one, preserving title / slug / unaffected
    sections byte-identical so page voice does not drift.

    Returns ``{rewritten, errors}`` counters. Always 200 — a degraded
    channel returns ``errors > 0`` rather than failing the request.
    """
    from beever_atlas.services.wiki_maintainer import get_wiki_maintainer

    await assert_channel_access(principal, channel_id)
    lang = await _resolve_target_lang(channel_id, target_lang)

    maintainer = get_wiki_maintainer()
    if maintainer is None:
        # Maintainer not wired in this deployment — manual mode is the
        # default but the singleton is initialised by the FastAPI lifespan.
        # Once the lifespan wires the singleton, this branch falls away.
        return {"rewritten": 0, "errors": 0, "reason": "maintainer_not_initialized"}

    counters = await maintainer.maintain_now(channel_id, target_lang=lang)
    logger.info(
        "wiki_maintain channel=%s rewritten=%d errors=%d",
        channel_id,
        counters.get("rewritten", 0),
        counters.get("errors", 0),
    )
    return counters


# ---------------------------------------------------------------------------
# wiki-llm-native-redesign — curation endpoints (§5.1–§5.4)
# ---------------------------------------------------------------------------


class _PinBody(BaseModel):
    """POST /pin body."""

    reason: str = Field(default="", max_length=512)
    # ``pinned`` defaults to True so a vanilla POST pins. The frontend
    # can flip it to False to unpin without needing a separate endpoint.
    pinned: bool = True


class _HideBody(BaseModel):
    """POST /hide body."""

    reason: str = Field(default="", max_length=512)
    hidden: bool = True


class _SplitBody(BaseModel):
    """POST /split body — extract a subset of facts to a new page."""

    new_title: str = Field(min_length=1, max_length=256)
    fact_ids: list[str] = Field(default_factory=list, max_length=1000)


class _MergeBody(BaseModel):
    """POST /merge body — collapse ``source_slug`` into the URL slug."""

    source_slug: str = Field(min_length=1, max_length=256)


class _CurationBody(BaseModel):
    """PATCH /pages/{slug}/curation body.

    The ``Literal`` type makes Pydantic reject any other value with a 422
    before the handler runs. The endpoint also re-checks at runtime as
    defense-in-depth so the page-store layer never sees a bad value.
    """

    curation_mode: Literal["auto", "manual", "frozen"] = Field(default="auto")


def _slugify_title(title: str) -> str:
    """Thin wrapper over the shared ``beever_atlas.wiki.slugify.slugify``.

    Kept as a private helper so call sites in this module read locally;
    the shared utility owns the canonical derivation so the curation
    API and the offline migration script never diverge (HIGH-priority
    code-review finding §M).
    """
    from beever_atlas.wiki.slugify import slugify

    return slugify(title)


async def _load_page_store():
    """Centralised page-store handle for the curation endpoints.

    Mirrors the ``cache._db`` access pattern used by ``lint_wiki`` so
    the new endpoints reuse the same Mongo connection pool.
    """
    from beever_atlas.wiki.page_store import WikiPageStore

    cache = _get_cache()
    await cache._ensure_db()
    return WikiPageStore(db=cache._db)


@router.post("/pages/{slug}/pin")
async def pin_wiki_page(
    channel_id: str,
    slug: str,
    body: _PinBody,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Mark a page as pinned (or unpinned when ``pinned=false``).

    Pinned pages still receive content updates from the maintainer but
    the prompt addendum constrains them to ``do not restructure
    sections, do not rename`` — preserving the operator's editorial
    intent. ``version`` is NOT bumped (curation, not content).
    """
    await assert_channel_access(principal, channel_id)
    lang = await _resolve_target_lang(channel_id, target_lang)
    store = await _load_page_store()
    existing = await store.get_page_by_slug(channel_id, slug, target_lang=lang)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"No wiki page slug={slug!r}")
    new_state: dict[str, object] = {
        "pinned": bool(body.pinned),
        "hidden": bool(existing.pin_state.get("hidden", False)),
        "reason": body.reason or existing.pin_state.get("reason", ""),
        "set_by": principal.id,
        "set_at": datetime.now(tz=UTC).isoformat(),
    }
    updated = await store.update_pin_state(channel_id, slug, new_state, target_lang=lang)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"No wiki page slug={slug!r}")
    return updated.model_dump(mode="json")


@router.post("/pages/{slug}/hide")
async def hide_wiki_page(
    channel_id: str,
    slug: str,
    body: _HideBody,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Mark a page hidden from human nav (still indexed for agents).

    Sets ``pin_state.hidden = body.hidden``. Hidden pages are excluded
    from ``list_pages_by_kind(scope="human")`` but ``scope="all"``
    still returns them so the MCP read tools (with the appropriate
    scope) can serve them to agents.
    """
    await assert_channel_access(principal, channel_id)
    lang = await _resolve_target_lang(channel_id, target_lang)
    store = await _load_page_store()
    existing = await store.get_page_by_slug(channel_id, slug, target_lang=lang)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"No wiki page slug={slug!r}")
    new_state: dict[str, object] = {
        "pinned": bool(existing.pin_state.get("pinned", False)),
        "hidden": bool(body.hidden),
        "reason": body.reason or existing.pin_state.get("reason", ""),
        "set_by": principal.id,
        "set_at": datetime.now(tz=UTC).isoformat(),
    }
    updated = await store.update_pin_state(channel_id, slug, new_state, target_lang=lang)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"No wiki page slug={slug!r}")
    return updated.model_dump(mode="json")


@router.patch("/pages/{slug}/curation")
async def update_curation_mode(
    channel_id: str,
    slug: str,
    body: _CurationBody,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Set per-page curation mode to auto / manual / frozen.

    Curation is metadata, not content — ``version`` is NOT bumped, only
    ``updated_at``. The maintainer reads ``curation_mode`` on every
    ``apply_update`` to decide whether to skip the LLM call (frozen),
    mark dirty without rewriting (manual), or rewrite normally (auto).
    """
    if body.curation_mode not in {"auto", "manual", "frozen"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"curation_mode must be one of 'auto'|'manual'|'frozen', got {body.curation_mode!r}"
            ),
        )
    await assert_channel_access(principal, channel_id)
    lang = await _resolve_target_lang(channel_id, target_lang)
    store = await _load_page_store()
    updated = await store.update_curation_mode(
        channel_id, slug, body.curation_mode, target_lang=lang
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"No wiki page slug={slug!r}")
    return {
        "slug": updated.slug,
        "curation_mode": updated.curation_mode,
        "updated_at": (updated.updated_at.isoformat() if updated.updated_at else None),
    }


@router.post("/apply-pending-updates")
async def apply_pending_updates(
    channel_id: str,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Flush manual-mode pages with pending dirty facts.

    Walks every ``curation_mode="manual" AND is_dirty=true`` page in the
    channel and triggers a maintainer rewrite. Pages flush one-at-a-time
    via the existing maintainer pacing primitives — no rate-limit storm.
    Auto-mode dirty pages are NOT flushed by this endpoint; they ride
    the normal debounce path.

    Mirrors :meth:`WikiMaintainer.maintain_now` but scoped to manual-mode
    dirty pages only — the maintainer's per-page gate normally short-circuits
    on ``curation_mode="manual"``, so the operator-triggered flush
    temporarily bumps each page to ``auto`` for the duration of its rewrite
    and restores ``manual`` afterwards via try/finally so failure mid-flight
    cannot leave a page in auto state.
    """
    await assert_channel_access(principal, channel_id)
    lang = await _resolve_target_lang(channel_id, target_lang)
    store = await _load_page_store()
    manual_dirty = await store.list_manual_dirty_pages(channel_id, target_lang=lang)
    if not manual_dirty:
        return {"flushed": 0, "pages": []}

    from beever_atlas.services.wiki_maintainer import get_wiki_maintainer

    maintainer = get_wiki_maintainer()
    if maintainer is None:
        raise HTTPException(
            status_code=503,
            detail="WikiMaintainer is not initialised — restart the worker.",
        )

    # The maintainer doesn't track which facts triggered the dirty flag,
    # so we compute new_fact_ids the same way ``maintain_now`` does:
    # load every fact for the channel and diff against
    # ``page.last_facts_seen``. This matches the well-tested production
    # debounce path.
    channel_facts = await maintainer._load_facts(channel_id, None)  # noqa: SLF001
    all_fact_ids = {str(f.get("id") or "") for f in channel_facts}
    flushed: list[str] = []
    for page in manual_dirty:
        already_seen = set(page.last_facts_seen)
        new_fact_ids = sorted(fid for fid in all_fact_ids if fid and fid not in already_seen)
        try:
            await store.update_curation_mode(channel_id, page.slug, "auto", target_lang=lang)
            applied = await maintainer.apply_update(
                channel_id,
                page.page_id,
                new_fact_ids,
                target_lang=lang,
            )
            if applied:
                flushed.append(page.page_id)
        finally:
            await store.update_curation_mode(channel_id, page.slug, "manual", target_lang=lang)
    return {"flushed": len(flushed), "pages": flushed}


@router.post("/pages/{slug}/split", status_code=201)
async def split_wiki_page(
    channel_id: str,
    slug: str,
    body: _SplitBody,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Split a subset of a page's facts into a new page.

    Creates a placeholder page seeded with the operator's title, kind
    derived from the source's kind, and ``last_facts_seen`` carrying
    the moved fact ids. The source page's ``last_facts_seen`` shrinks
    by the same set so the maintainer doesn't keep treating those
    facts as "already integrated here". The new page is is_dirty=True
    so the maintainer's next pass rewrites it from the moved facts.
    """
    from beever_atlas.models.persistence import WikiPage, WikiPageSection

    await assert_channel_access(principal, channel_id)
    lang = await _resolve_target_lang(channel_id, target_lang)
    if not body.fact_ids:
        raise HTTPException(status_code=400, detail="fact_ids must not be empty for split")
    store = await _load_page_store()
    source = await store.get_page_by_slug(channel_id, slug, target_lang=lang)
    if source is None:
        raise HTTPException(status_code=404, detail=f"No wiki page slug={slug!r}")

    new_slug = _slugify_title(body.new_title)
    if new_slug == source.slug:
        raise HTTPException(
            status_code=400, detail="new_title must yield a slug different from source"
        )
    # Reject if the destination slug already exists — splits must not
    # silently merge into an unrelated page.
    if await store.get_page_by_slug(channel_id, new_slug, target_lang=lang):
        raise HTTPException(
            status_code=409,
            detail=f"slug {new_slug!r} already exists; choose a different title",
        )

    moved = [fid for fid in body.fact_ids if fid in set(source.last_facts_seen)]
    new_page = WikiPage(
        channel_id=channel_id,
        target_lang=lang,
        page_id=f"topic:{new_slug}",
        title=body.new_title,
        slug=new_slug,
        kind=source.kind,
        sections=[WikiPageSection(id="overview", title="Overview", content_md="")],
        last_facts_seen=moved,
        is_dirty=True,
        page_voice_seed=source.page_voice_seed,
    )
    await store.save_page(new_page)
    if moved:
        await store.remove_facts_from_page(channel_id, slug, moved, target_lang=lang)
    # The new page is always created by ``save_page`` above (even if zero
    # facts were moved — the operator may want to seed the title now and
    # move facts later), so the response always carries it. Drop the
    # ``model_dump`` to None branch — it was a leftover ternary that
    # was never actually reachable since ``moved`` is a list, not None.
    persisted = await store.get_page_by_slug(channel_id, new_slug, target_lang=lang)
    return {
        "source_slug": slug,
        "new_slug": new_slug,
        "moved_fact_count": len(moved),
        "new_page": persisted.model_dump(mode="json") if persisted is not None else None,
    }


@router.post("/pages/{slug}/merge")
async def merge_wiki_page(
    channel_id: str,
    slug: str,
    body: _MergeBody,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Mark ``body.source_slug`` as merged into the URL ``slug``.

    The source page is hidden from human nav and its ``merged_into``
    field carries the target slug. Future fact routing in
    ``WikiMaintainer.plan_updates`` re-routes any plan_updates entry
    keyed to the source's page_id to the target so subsequent batches
    flow into the canonical page.
    """
    await assert_channel_access(principal, channel_id)
    lang = await _resolve_target_lang(channel_id, target_lang)
    if body.source_slug == slug:
        raise HTTPException(status_code=400, detail="source_slug must differ from target slug")
    store = await _load_page_store()
    target = await store.get_page_by_slug(channel_id, slug, target_lang=lang)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No target wiki page slug={slug!r}")
    source = await store.get_page_by_slug(channel_id, body.source_slug, target_lang=lang)
    if source is None:
        raise HTTPException(
            status_code=404,
            detail=f"No source wiki page slug={body.source_slug!r}",
        )
    updated = await store.record_merged_into(channel_id, body.source_slug, slug, target_lang=lang)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"No source wiki page slug={body.source_slug!r}",
        )
    return {
        "source_slug": body.source_slug,
        "target_slug": slug,
        "source": updated.model_dump(mode="json"),
    }


@router.get("/pages-by-slug/{slug}")
async def get_wiki_page_by_slug(
    channel_id: str,
    slug: str,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Resolve a slug to its page (or follow a merge redirect).

    When the page has ``merged_into`` set, the response is a 301
    redirect to the canonical target's slug — the operator UI follows
    the redirect transparently.
    """
    await assert_channel_access(principal, channel_id)
    lang = await _resolve_target_lang(channel_id, target_lang)
    store = await _load_page_store()
    page = await store.get_page_by_slug(channel_id, slug, target_lang=lang)
    if page is None:
        raise HTTPException(status_code=404, detail=f"No wiki page slug={slug!r}")
    if page.merged_into:
        # Validate BOTH path components before embedding into the 301
        # redirect URL — closes ``codeql:py/url-redirection-from-remote``
        # which otherwise treats both ``channel_id`` (from the URL path)
        # and ``page.merged_into`` (from the page store) as untrusted
        # sources that flow into the ``Location`` header. ``channel_id``
        # is also pre-validated by ``assert_channel_access`` above, but
        # CodeQL's taint propagator doesn't recognise that helper as a
        # sanitiser; the explicit ``re.fullmatch`` here is the
        # syntactic anchor.
        import re

        _SLUG_RE = re.compile(r"\A[A-Za-z0-9_:-]{1,128}\Z")
        if not _SLUG_RE.match(channel_id):
            raise HTTPException(
                status_code=400,
                detail="Invalid channel_id — refusing to redirect",
            )
        if not _SLUG_RE.match(page.merged_into):
            raise HTTPException(
                status_code=500,
                detail="Invalid merge target slug — refusing to redirect",
            )
        # quote() applies one more layer of belt-and-suspenders for
        # any future relaxation of the regex. The path is now built
        # entirely from validated, percent-encoded components.
        safe_channel = quote(channel_id, safe="")
        safe_target = quote(page.merged_into, safe="")
        return RedirectResponse(  # type: ignore[return-value]
            url=f"/api/channels/{safe_channel}/wiki/pages-by-slug/{safe_target}",
            status_code=301,
        )
    return page.model_dump(mode="json")


# ---------------------------------------------------------------------------
# wiki-llm-native-redesign §6 — wiki graph view
# ---------------------------------------------------------------------------


def _page_has_renderable_content(page: object) -> bool:
    """Return True iff a wiki page has body content the UI can render.

    The structure planner pre-allocates ``wiki_pages`` rows for fixed
    pages (overview, people, activity, faq) before the compiler runs.
    On small / sparse channels, the compiler skips populating some of
    these (insufficient signal to write a meaningful FAQ, etc.) and
    leaves the row with empty ``content`` / no ``modules`` / empty
    ``narrative_sections``. Including these in the graph or any UI
    listing creates ghost nodes that resolve to a black page when
    clicked. This predicate is the single check that separates
    "page exists in the store" from "page has anything to show".

    Renderable means at least one of:
      * ``page.content`` is a non-empty string after stripping, OR
      * ``page.narrative_sections`` is a non-empty list, OR
      * ``page.modules`` contains at least one module whose ``data``
        dict is non-empty (an empty ``data`` is what the compiler
        emits for unpopulated module slots).
    """
    content = getattr(page, "content", None)
    if isinstance(content, str) and content.strip():
        return True
    narrative_sections = getattr(page, "narrative_sections", None)
    if isinstance(narrative_sections, list) and narrative_sections:
        return True
    modules = getattr(page, "modules", None)
    if isinstance(modules, list):
        for m in modules:
            if not isinstance(m, dict):
                continue
            data = m.get("data")
            if isinstance(data, dict) and data:
                return True
    return False


@router.get("/graph")
async def get_wiki_graph(
    channel_id: str,
    target_lang: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Return the channel's wiki graph in Cytoscape.js format.

    Always 200 — empty channel returns ``{nodes: [], edges: []}`` so
    the frontend can render an empty-state without a 404 round-trip.

    Source of truth: ``wiki_pages`` in Mongo. Every page in the channel
    becomes a node; every entry in each page's ``cross_links`` dict
    (title → slug) becomes a ``references_wiki`` edge. This works on
    legacy installs without requiring Neo4j to have been backfilled,
    and works the day ``WIKI_LLM_NATIVE_REDESIGN`` flips ON because
    the cross-link resolver writes to ``cross_links`` directly.

    Neo4j is read only for ENRICHMENT — entity cross-edges
    (``references_entity``) are pulled from there when the graph
    backend exposes ``get_wiki_graph``. Backends without that method
    (NullGraphStore, etc.) silently skip the entity layer; the wiki
    page-only graph still renders.
    """
    await assert_channel_access(principal, channel_id)
    lang = await _resolve_target_lang(channel_id, target_lang)
    store = await _load_page_store()
    pages = await store.list_pages_by_kind(channel_id, target_lang=lang, scope="human")

    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    seen_node_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    # 1) Wiki page nodes — every visible page in the channel that has
    # actual content. Pages exist in ``wiki_pages`` for all human-scope
    # rows including fixed pages (overview, people, activity, faq) that
    # the structure planner created as placeholders. On small channels
    # those placeholders are never populated by the compiler — they
    # come back with empty content / empty modules / no narrative — and
    # appear in the graph as ghost nodes that resolve to blank pages
    # when clicked. Filter them out here so the graph reflects what
    # the user can actually read, not what the planner pre-allocated.
    #
    # Build a parallel ``page_id → slug`` index so the parent_id chain
    # (which references the persistent page_id, NOT the slug used as
    # node id) can be resolved when emitting `child_of` edges below.
    page_id_to_slug: dict[str, str] = {}
    for page in pages:
        slug = page.slug or page.page_id.replace(":", "-")
        if not slug or slug in seen_node_ids:
            continue
        if not _page_has_renderable_content(page):
            continue
        seen_node_ids.add(slug)
        if page.page_id:
            page_id_to_slug[page.page_id] = slug
        nodes.append(
            {
                "data": {
                    "id": slug,
                    "page_id": page.page_id,
                    "slug": slug,
                    "label": page.title or slug,
                    "kind": "wiki",
                    "page_kind": page.kind or "topic",
                    "version": page.version,
                    "last_updated": page.updated_at.isoformat() if page.updated_at else "",
                }
            }
        )

    # 1b) Hierarchy edges — every page with ``parent_id`` gets a
    # ``child_of`` edge to its parent. Without these, the planner-
    # produced folder tree is invisible in the graph view (folders
    # render as isolated amber squares with no visible link to the
    # topics they group). Mirrors the legacy fallback's edge logic.
    # ``getattr`` defends against partial test mocks that omit
    # parent_id; real WikiPage instances default it to None.
    for page in pages:
        parent_id = getattr(page, "parent_id", None)
        if not parent_id:
            continue
        src_slug = page_id_to_slug.get(page.page_id)
        dst_slug = page_id_to_slug.get(parent_id)
        if not src_slug or not dst_slug or src_slug == dst_slug:
            continue
        key = (src_slug, dst_slug, "child_of")
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(
            {
                "data": {
                    "id": f"e:{src_slug}->{dst_slug}",
                    "source": src_slug,
                    "target": dst_slug,
                    "kind": "child_of",
                }
            }
        )

    # 2) References_wiki edges — from each page's ``cross_links`` dict.
    for page in pages:
        src_slug = page.slug or page.page_id.replace(":", "-")
        cross_links = page.cross_links or {}
        if not isinstance(cross_links, dict):
            continue
        for dst_slug in cross_links.values():
            if not dst_slug or dst_slug == src_slug:
                continue
            # Skip dangling edges to slugs that don't exist as nodes —
            # they would break the graph layout. The cross_links_broken
            # field already tracks these for the renderer's broken-link
            # affordance; the graph view stays focused on resolved
            # references.
            if dst_slug not in seen_node_ids:
                continue
            key = (src_slug, dst_slug, "references_wiki")
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(
                {
                    "data": {
                        "id": f"e:{src_slug}->{dst_slug}",
                        "source": src_slug,
                        "target": dst_slug,
                        "kind": "references_wiki",
                    }
                }
            )

    # 2b) Legacy ``wiki_cache`` fallback — when ``wiki_pages`` is empty
    # (existing installs that pre-date the per-page store, OR new
    # channels whose wiki was generated by the legacy WikiBuilder
    # path), fall back to the legacy single-doc cache so the graph
    # view shows SOMETHING useful instead of an empty canvas. The
    # legacy doc carries a ``pages`` map keyed by page_id with
    # ``id``/``slug``/``title``/``page_type``/``parent_id``/``section_number``;
    # the parent_id chain becomes the edge set, mirroring the wiki
    # tab's hierarchy view.
    channel_label = ""
    if not nodes:
        try:
            cache = _get_cache()
            legacy_doc = await cache.get_wiki(channel_id, target_lang=lang)
        except Exception as exc:  # noqa: BLE001 — soft-fail to empty graph
            logger.exception(
                "event=wiki_graph_legacy_cache_failed channel_id=%s err=%s",
                channel_id,
                exc,
            )
            legacy_doc = None
        legacy_pages = (legacy_doc or {}).get("pages") if isinstance(legacy_doc, dict) else None
        if isinstance(legacy_doc, dict):
            structure = legacy_doc.get("structure") or {}
            if isinstance(structure, dict):
                channel_label = (
                    structure.get("channel_name") or legacy_doc.get("channel_name") or ""
                )
        if isinstance(legacy_pages, dict):
            for page_id, page in legacy_pages.items():
                if not isinstance(page, dict):
                    continue
                # Use the page_id (not slug) as the graph node id so
                # the frontend can route to the wiki tab + select the
                # right page via ``?page={page_id}`` deep link.
                node_id = str(page_id)
                slug = page.get("slug") or node_id
                if not node_id or node_id in seen_node_ids:
                    continue
                seen_node_ids.add(node_id)
                # Short summary: the first 240 chars of content_md /
                # content / summary so the side panel can show
                # something useful without a second round-trip.
                raw = page.get("summary") or page.get("content") or ""
                if isinstance(raw, str):
                    excerpt = raw.strip()[:240].rstrip()
                    if len(raw) > 240:
                        excerpt += "…"
                else:
                    excerpt = ""
                nodes.append(
                    {
                        "data": {
                            "id": node_id,
                            "slug": slug,
                            "page_id": node_id,
                            "label": page.get("title") or slug,
                            "kind": "wiki",
                            "page_kind": page.get("page_type") or "topic",
                            "version": 0,
                            "last_updated": "",
                            "section_number": page.get("section_number") or "",
                            "summary": excerpt,
                            "memory_count": page.get("memory_count") or 0,
                            "legacy": True,
                        }
                    }
                )
            # Hierarchical edges from parent_id (legacy compiler emits
            # this for sub-topics under parent topics). The legacy
            # cache does NOT carry [[wikilink]] cross-references — those
            # only land under WIKI_LLM_NATIVE_REDESIGN.
            #
            # The structure planner ALSO commits folder→child links as
            # an inline ``children: [{id, title, slug, ...}]`` array on
            # each folder doc, but does NOT round-trip ``parent_id`` back
            # onto each child page. Walking only ``parent_id`` therefore
            # misses every planner-produced folder→topic edge. Walk both
            # the parent_id chain AND each folder's children array so
            # the graph reflects the same hierarchy the sidebar shows.
            for page_id, page in legacy_pages.items():
                if not isinstance(page, dict):
                    continue
                src_id = str(page_id)
                # Path A — child_of from parent_id (sub-topics under
                # parent topics; nested folder under root folder).
                parent_id = page.get("parent_id")
                if (
                    parent_id
                    and isinstance(parent_id, str)
                    and parent_id in seen_node_ids
                    and src_id in seen_node_ids
                    and src_id != parent_id
                ):
                    key = (src_id, parent_id, "child_of")
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(
                            {
                                "data": {
                                    "id": f"e:{src_id}->{parent_id}",
                                    "source": src_id,
                                    "target": parent_id,
                                    "kind": "child_of",
                                    "legacy": True,
                                }
                            }
                        )
                # Path B — child_of from inline ``children`` array (the
                # planner-produced folder→topic links). Each entry is
                # ``{id, title, slug, ...}`` where ``id`` is the child's
                # page_id used as the graph node id.
                children = page.get("children")
                if not isinstance(children, list):
                    continue
                for child in children:
                    if not isinstance(child, dict):
                        continue
                    child_id = child.get("id")
                    if not child_id or not isinstance(child_id, str):
                        continue
                    if child_id not in seen_node_ids or child_id == src_id:
                        continue
                    key = (child_id, src_id, "child_of")
                    if key in seen_edges:
                        continue
                    seen_edges.add(key)
                    edges.append(
                        {
                            "data": {
                                "id": f"e:{child_id}->{src_id}",
                                "source": child_id,
                                "target": src_id,
                                "kind": "child_of",
                                "legacy": True,
                            }
                        }
                    )

    # 3) Entity cross-edges — best-effort enrichment from Neo4j. When the
    # graph backend doesn't expose ``get_wiki_graph`` (NullGraphStore,
    # NebulaStore until parity), or the call fails, the wiki page-only
    # graph above is what the operator sees.
    stores_obj = get_stores()
    graph_backend = stores_obj.graph
    if hasattr(graph_backend, "get_wiki_graph"):
        try:
            neo_payload = await graph_backend.get_wiki_graph(channel_id)
            neo_nodes_by_id: dict[str, dict[str, object]] = {}
            for n in neo_payload.get("nodes", []):
                nd = n.get("data", {}) if isinstance(n, dict) else {}
                nid = nd.get("id")
                if isinstance(nid, str):
                    neo_nodes_by_id[nid] = n
            for edge in neo_payload.get("edges", []):
                if not isinstance(edge, dict):
                    continue
                ed = edge.get("data", {}) or {}
                if ed.get("kind") != "references_entity":
                    continue
                src = ed.get("source")
                tgt = ed.get("target")
                if not isinstance(src, str) or not isinstance(tgt, str):
                    continue
                # Only emit entity edges whose wiki source is on the
                # graph (otherwise we'd attach an entity to a node
                # that isn't visible).
                if src not in seen_node_ids:
                    continue
                if tgt not in seen_node_ids:
                    candidate = neo_nodes_by_id.get(tgt)
                    if candidate is not None:
                        nodes.append(candidate)
                        seen_node_ids.add(tgt)
                key = (src, tgt, "references_entity")
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edges.append(edge)
        except Exception as exc:  # noqa: BLE001 — soft-fail to wiki-only
            logger.exception(
                "event=wiki_graph_entity_enrichment_failed channel_id=%s err=%s",
                channel_id,
                exc,
            )

    # 4) Central channel hub — gives the cose-bilkent / concentric
    # layouts a meaningful pivot. Every wiki node that has no in-edges
    # (no parent / no incoming reference) gets a ``belongs_to`` edge
    # to the hub so disconnected pages still anchor to the channel.
    if nodes:
        hub_id = f"channel:{channel_id}"
        if hub_id not in seen_node_ids:
            seen_node_ids.add(hub_id)
            nodes.append(
                {
                    "data": {
                        "id": hub_id,
                        "label": channel_label or "Channel",
                        "kind": "channel",
                        "page_kind": "channel",
                        "summary": (
                            f"All wiki pages for this channel "
                            f"({sum(1 for n in nodes if n['data'].get('kind') == 'wiki')} pages)."
                        ),
                    }
                }
            )
        # Find every node with no incoming edge among the wiki/entity
        # node set and connect it to the hub.
        targets_with_incoming: set[str] = set()
        for edge in edges:
            tgt = edge.get("data", {}).get("target")
            if isinstance(tgt, str):
                targets_with_incoming.add(tgt)
        for node in nodes:
            nd = node.get("data", {})
            if nd.get("kind") != "wiki":
                continue
            nid = nd.get("id")
            if not isinstance(nid, str) or nid == hub_id:
                continue
            # Only top-level orphans — pages with NO parent edge — anchor
            # to the hub. Pages already inside the hierarchy chain reach
            # the hub transitively via their parent.
            has_parent_edge = any(
                e.get("data", {}).get("source") == nid
                and e.get("data", {}).get("kind") == "child_of"
                for e in edges
            )
            if has_parent_edge:
                continue
            key = (nid, hub_id, "belongs_to")
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(
                {
                    "data": {
                        "id": f"e:{nid}->{hub_id}",
                        "source": nid,
                        "target": hub_id,
                        "kind": "belongs_to",
                    }
                }
            )

    return {"channel_id": channel_id, "nodes": nodes, "edges": edges}
