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
        description="Metadata for a single platform connection owned by the calling MCP principal.",
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
        description="All channels selected for sync under a connection owned by the calling principal.",
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
            "Wiki structure index for a channel: overview summary and available page types. "
            "Returns a stub if the wiki cache has not been populated yet."
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

            overview = await wiki_cap.get_topic_overview(principal_id, channel_id)
            if overview is None:
                logger.warning(
                    "event=mcp_resource_wiki_stub channel_id=%s "
                    "detail='wiki structure index awaits Phase 6 wiki-cache integration'",
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
            "Pre-compiled wiki page content for a channel. page_id is one of: "
            "overview, faq, decisions, people, glossary, activity, topics."
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
            "Status of a long-running sync or wiki-refresh job. Returns job_not_found "
            "for jobs not owned by the calling principal (no information leak)."
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
