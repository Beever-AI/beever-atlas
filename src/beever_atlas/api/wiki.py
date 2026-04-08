"""Wiki generation API endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import PlainTextResponse

from beever_atlas.infra.config import get_settings
from beever_atlas.stores import get_stores
from beever_atlas.wiki.cache import WikiCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels/{channel_id}/wiki", tags=["wiki"])


_wiki_cache: WikiCache | None = None


def _get_cache() -> WikiCache:
    global _wiki_cache
    if _wiki_cache is None:
        settings = get_settings()
        _wiki_cache = WikiCache(settings.mongodb_uri)
    return _wiki_cache


@router.get("")
async def get_wiki(channel_id: str) -> dict:
    """Return the full cached wiki for a channel."""
    cache = _get_cache()
    doc = await cache.get_wiki(channel_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="No wiki available yet")
    return doc


@router.get("/pages/{page_id}")
async def get_wiki_page(channel_id: str, page_id: str) -> dict:
    """Return a single wiki page from cache."""
    cache = _get_cache()
    page = await cache.get_page(channel_id, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page {page_id!r} not found")
    return page


@router.get("/structure")
async def get_wiki_structure(channel_id: str) -> dict:
    """Return the wiki sidebar structure without page content."""
    cache = _get_cache()
    doc = await cache.get_structure(channel_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="No wiki structure available yet")
    return doc


@router.get("/download")
async def download_wiki_markdown(channel_id: str) -> PlainTextResponse:
    """Export the full wiki as a single Markdown file."""
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
    parts: list[str] = [f"# {channel_name} — Wiki\n"]
    for page_id in page_order:
        page = pages_dict.get(page_id)
        if not page:
            continue
        title = page.get("title", page_id)
        section = page.get("section_number", "")
        prefix = f"{section} " if section else ""
        parts.append(f"\n---\n\n## {prefix}{title}\n")
        parts.append(page.get("content", ""))
        # Append citations
        citations = page.get("citations", [])
        if citations:
            parts.append("\n\n### Sources\n")
            for cit in citations:
                author = cit.get("author", "")
                ts = cit.get("timestamp", "")
                excerpt = cit.get("text_excerpt", "")
                link = cit.get("permalink", "")
                parts.append(f"- {cit.get('id', '')} @{author} · {ts} — {excerpt} [{link}]({link})")
        parts.append("\n")

    md_content = "\n".join(parts)
    filename = f"{channel_name.replace(' ', '-').lower()}-wiki.md"

    return PlainTextResponse(
        content=md_content,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/status")
async def get_wiki_status(channel_id: str) -> dict:
    """Return the current wiki generation status for a channel."""
    cache = _get_cache()
    status = await cache.get_generation_status(channel_id)
    if status is None:
        return {"status": "idle", "channel_id": channel_id}
    return status


@router.post("/refresh", status_code=202)
async def refresh_wiki(channel_id: str, background_tasks: BackgroundTasks) -> dict:
    """Trigger async wiki generation for a channel."""
    from beever_atlas.wiki.builder import WikiBuilder

    stores = get_stores()
    cache = _get_cache()
    builder = WikiBuilder(stores.weaviate, stores.graph, cache)

    # Set status to "running" immediately so the frontend sees it on first poll
    await cache.set_generation_status(
        channel_id, status="running", stage="starting",
        stage_detail="Initiating wiki generation…",
    )

    background_tasks.add_task(_run_generation, builder, channel_id, cache)
    return {"status": "started", "channel_id": channel_id}


async def _run_generation(builder, channel_id: str, cache: WikiCache) -> None:
    try:
        await builder.refresh_wiki(channel_id)
    except Exception as exc:
        logger.error("Wiki generation failed channel=%s: %s", channel_id, exc, exc_info=True)
        await cache.set_generation_status(
            channel_id, status="failed", stage="error",
            error=str(exc),
        )
