"""Atlas MCP resources (Phase 4, tasks 4.1–4.5)."""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from beever_atlas.api.mcp_server._helpers import (
    _get_principal_id_from_resource,
    _validate_id,
)

logger = logging.getLogger(__name__)


def register_resources(mcp: FastMCP) -> None:
    """Register all atlas:// URI resources."""

    # 4.1 atlas://connection/{connection_id}
    @mcp.resource(
        "atlas://connection/{connection_id}",
        name="connection",
        description=(
            "Read metadata for one platform connection (e.g. a linked Slack or "
            "Discord workspace) owned by the calling principal. connection_id "
            "comes from list_connections (e.g. 'conn_abc123'). Returns the "
            "connection's JSON record, or a structured error: "
            "connection_not_found if the id is unknown, connection_access_denied "
            "if it belongs to another principal. Instant read, no side effects."
        ),
        mime_type="application/json",
    )
    async def get_connection(connection_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(connection_id, "connection_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import connections as conn_cap
            from beever_atlas.capabilities.errors import ConnectionAccessDenied

            conns = await conn_cap.list_connections(principal_id)
            for conn in conns:
                if conn.get("connection_id") == connection_id:
                    return conn
            return {"error": "connection_not_found", "connection_id": connection_id}
        except ConnectionAccessDenied:
            return {"error": "connection_access_denied", "connection_id": connection_id}
        except Exception:
            logger.exception(
                "resource get_connection: failed principal=%s connection_id=%s",
                principal_id,
                connection_id,
            )
            return {"error": "internal_error", "connection_id": connection_id}

    # 4.2 atlas://connection/{connection_id}/channels
    @mcp.resource(
        "atlas://connection/{connection_id}/channels",
        name="connection-channels",
        description=(
            "List the channels selected for sync under one connection owned by "
            "the calling principal. connection_id comes from list_connections "
            "(e.g. 'conn_abc123'). Returns {channels: [...], connection_id}, or "
            "connection_access_denied if the connection belongs to another "
            "principal. Equivalent to the list_channels tool scoped to a single "
            "connection. Instant read, no side effects."
        ),
        mime_type="application/json",
    )
    async def get_connection_channels(connection_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(connection_id, "connection_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import connections as conn_cap
            from beever_atlas.capabilities.errors import ConnectionAccessDenied

            channels = await conn_cap.list_channels(principal_id, connection_id)
            return {"channels": channels, "connection_id": connection_id}
        except ConnectionAccessDenied:
            return {"error": "connection_access_denied", "connection_id": connection_id}
        except Exception:
            logger.exception(
                "resource get_connection_channels: failed principal=%s connection_id=%s",
                principal_id,
                connection_id,
            )
            return {"error": "internal_error", "connection_id": connection_id}

    # 4.3 atlas://channel/{channel_id}/wiki
    @mcp.resource(
        "atlas://channel/{channel_id}/wiki",
        name="channel-wiki-index",
        description=(
            "Read the wiki table of contents for a channel: the list of pages "
            "(or page types) and a short overview. Use this to discover what "
            "wiki content exists before fetching a specific page. channel_id "
            "comes from list_channels (e.g. 'C12345'). Returns {channel_id, "
            "pages|page_types, overview, stub}; stub is true when the wiki has "
            "not been generated yet (run refresh_wiki to populate it). Returns "
            "channel_access_denied if the channel belongs to another principal. "
            "Instant read, no side effects."
        ),
        mime_type="application/json",
    )
    async def get_channel_wiki_index(channel_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import wiki as wiki_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied
            from beever_atlas.infra.channel_access import assert_channel_access
            from beever_atlas.infra.config import get_settings
            from beever_atlas.stores import get_stores
            from beever_atlas.wiki.page_store import WikiPageStore

            # Per-page resource path (PR-E + production-wiring §16):
            # when ``PER_PAGE_WIKI=ON`` AND the new ``wiki_pages`` collection
            # has rows for this channel, return a real per-page index. The
            # legacy ``get_topic_overview`` fallback covers the other cases.
            settings = get_settings()
            if settings.per_page_wiki:
                try:
                    await assert_channel_access(principal_id, channel_id)
                    stores = get_stores()
                    page_store = WikiPageStore(db=stores.mongodb.db)
                    pages = await page_store.list_pages(channel_id)
                except ChannelAccessDenied:
                    return {"error": "channel_access_denied", "channel_id": channel_id}
                except Exception:  # noqa: BLE001 — fall through to legacy path
                    logger.warning(
                        "event=mcp_resource_wiki_pages_lookup_failed channel_id=%s",
                        channel_id,
                    )
                    pages = []
                if pages:
                    last_run_ts = max(
                        (p.updated_at for p in pages if p.updated_at is not None),
                        default=None,
                    )
                    return {
                        "channel_id": channel_id,
                        "page_count": len(pages),
                        "pages": [
                            {
                                "page_id": p.page_id,
                                "title": p.title,
                                "version": p.version,
                                "is_dirty": p.is_dirty,
                                "tensions_count": len(p.tensions or []),
                                "updated_at": (
                                    p.updated_at.isoformat() if p.updated_at is not None else None
                                ),
                            }
                            for p in pages
                        ],
                        "maintainer_last_run_ts": (
                            last_run_ts.isoformat() if last_run_ts is not None else None
                        ),
                        "stub": False,
                    }

            overview = await wiki_cap.get_topic_overview(principal_id, channel_id)
            if overview is None:
                logger.warning(
                    "event=mcp_resource_wiki_stub channel_id=%s "
                    "detail='wiki structure index empty across both legacy and per-page stores'",
                    channel_id,
                )
                return {
                    "channel_id": channel_id,
                    "page_types": list(wiki_cap.SUPPORTED_PAGE_TYPES),
                    "overview": None,
                    "stub": True,
                }
            return {
                "channel_id": channel_id,
                "page_types": list(wiki_cap.SUPPORTED_PAGE_TYPES),
                "overview": overview,
                "stub": False,
            }
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "resource get_channel_wiki_index: failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"error": "internal_error", "channel_id": channel_id}

    # 4.4 atlas://channel/{channel_id}/wiki/page/{page_id}
    @mcp.resource(
        "atlas://channel/{channel_id}/wiki/page/{page_id}",
        name="channel-wiki-page",
        description=(
            "Read one pre-compiled wiki page for a channel. channel_id comes "
            "from list_channels (e.g. 'C12345'); page_id is one of: overview, "
            "faq, decisions, people, glossary, activity, topics. Returns "
            "{channel_id, page_type, content, generated_at, citations}; content "
            "is null when that page has not been generated yet (run refresh_wiki "
            "to populate it). Returns channel_access_denied if the channel "
            "belongs to another principal. Instant read, no side effects."
        ),
        mime_type="application/json",
    )
    async def get_channel_wiki_page(channel_id: str, page_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id") or _validate_id(page_id, "page_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import wiki as wiki_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            page = await wiki_cap.get_wiki_page(principal_id, channel_id, page_type=page_id)
            if page is None:
                return {
                    "channel_id": channel_id,
                    "page_type": page_id,
                    "content": None,
                    "generated_at": None,
                    "citations": [],
                }
            return {
                **page,
                "generated_at": None,
                "citations": [],
            }
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "resource get_channel_wiki_page: failed principal=%s channel_id=%s page_id=%s",
                principal_id,
                channel_id,
                page_id,
            )
            return {
                "error": "internal_error",
                "channel_id": channel_id,
                "page_type": page_id,
            }

    # 4.5 atlas://job/{job_id}
    @mcp.resource(
        "atlas://job/{job_id}",
        name="job-status",
        description=(
            "Read the status of a long-running sync or wiki-refresh job started "
            "by trigger_sync or refresh_wiki. job_id is the value those tools "
            "return (e.g. 'job_789'). Returns the job status record (state, "
            "progress, timestamps). Returns job_not_found both for unknown ids "
            "and for jobs owned by another principal, so it never leaks the "
            "existence of others' jobs. This is the resource form of the "
            "get_job_status tool. Instant read, no side effects."
        ),
        mime_type="application/json",
    )
    async def get_job(job_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(job_id, "job_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import jobs as jobs_cap
            from beever_atlas.capabilities.errors import JobNotFound

            status = await jobs_cap.get_job_status(principal_id, job_id)
            return status
        except JobNotFound:
            return {"error": "job_not_found", "job_id": job_id}
        except Exception:
            logger.exception(
                "resource get_job: failed principal=%s job_id=%s",
                principal_id,
                job_id,
            )
            return {"error": "job_not_found", "job_id": job_id}
