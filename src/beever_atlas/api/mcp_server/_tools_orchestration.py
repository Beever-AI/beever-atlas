"""Orchestration tools: trigger_sync, refresh_wiki, get_job_status
(Phase 5b, tasks 5.2–5.4)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastmcp import Context, FastMCP

from beever_atlas.api.mcp_server._helpers import (
    _get_principal_id,
    _validate_id,
)

logger = logging.getLogger(__name__)


def register_orchestration_tools(mcp: FastMCP) -> None:
    """Register long-running-job tools: trigger_sync, refresh_wiki, get_job_status."""

    @mcp.tool(
        name="trigger_sync",
        description=(
            "Trigger an incremental or full sync of a channel's messages into the "
            "Atlas knowledge base. Returns a job envelope "
            "{job_id, status_uri, status: 'queued'} within 5 seconds; the actual "
            "ingestion runs in the background. Poll atlas://job/<job_id> or call "
            "get_job_status to track progress.\n\n"
            "IMPORTANT — only call when the user EXPLICITLY asks to refresh or sync "
            "a channel, OR when retrieval tools return empty/stale results AND the "
            "channel's last_sync_ts is beyond 24 hours ago. Otherwise prefer existing "
            "indexed data — sync is expensive and rate-limited to 5/min per principal. "
            "Do NOT call before every question or as a precautionary step.\n\n"
            "If a queued or running sync job already exists for the same channel, "
            "the existing job_id is returned (idempotent). A new job is only created "
            "when no active job exists, or after a previous job has completed or failed.\n\n"
            "sync_type: 'incremental' (default — fetches only new messages since last "
            "sync), 'full' (re-fetches all messages; expensive), or 'auto' (server "
            "chooses based on sync history).\n\n"
            "connection_id: OPTIONAL but STRONGLY RECOMMENDED when the workspace has "
            "multiple same-platform connections (e.g. two Slack workspaces). Without "
            "it, the server falls back to matching the channel against each "
            "connection's selected_channels pick-list; if the channel hasn't been "
            "added to any pick-list yet, the sync may route to the wrong connection "
            "and fail with channel_not_found. Prefer to pass the connection_id you "
            "got from list_channels — it disambiguates the target connection "
            "deterministically."
        ),
    )
    async def trigger_sync(
        channel_id: Annotated[str, "The channel id to sync (from list_channels)"],
        ctx: Context,
        sync_type: Annotated[
            str, "Sync mode: 'incremental' (default), 'full', or 'auto'"
        ] = "incremental",
        connection_id: Annotated[
            str | None,
            "Optional: the connection the channel belongs to (from list_connections / "
            "list_channels). Pass it when the user has multiple same-platform "
            "connections to avoid mis-routing.",
        ] = None,
    ) -> dict:
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err
        if connection_id is not None:
            err = _validate_id(connection_id, "connection_id")
            if err:
                return err

        try:
            from beever_atlas.capabilities import sync as sync_cap
            from beever_atlas.capabilities.errors import (
                ChannelAccessDenied,
                CooldownActive,
                ServiceUnavailable,
            )

            result = await sync_cap.trigger_sync(
                principal_id,
                channel_id,
                sync_type=sync_type,
                connection_id=connection_id,
            )
            return result
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except CooldownActive as exc:
            return {
                "error": "cooldown_active",
                "retry_after_seconds": exc.retry_after_seconds,
            }
        except ServiceUnavailable as exc:
            return {"error": "service_unavailable", "service": exc.service}
        except Exception:
            logger.exception(
                "trigger_sync: capability failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"error": "internal_error", "channel_id": channel_id}

    @mcp.tool(
        name="refresh_wiki",
        description=(
            "Regenerate pre-compiled wiki pages for a channel from its ingested facts. "
            "Returns a job envelope {job_id, status_uri, status: 'queued'} within 5 "
            "seconds; generation runs in the background.\n\n"
            "Expensive — only call after a fresh sync has added new facts (i.e., after "
            "trigger_sync completes), or when the user explicitly requests wiki "
            "regeneration. Do NOT call routinely — wiki pages are rebuilt automatically "
            "as part of the standard sync pipeline.\n\n"
            "page_types: optional subset of page types to regenerate. Valid values: "
            "overview, faq, decisions, people, glossary, activity, topics. "
            "Omit to regenerate all pages."
        ),
    )
    async def refresh_wiki(
        channel_id: Annotated[
            str,
            "The channel id to regenerate wiki pages for (from list_channels)",
        ],
        ctx: Context,
        page_types: Annotated[
            list[str] | None,
            "Optional list of page types to regenerate: overview, faq, decisions, people, glossary, activity, topics",
        ] = None,
    ) -> dict:
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import wiki as wiki_cap
            from beever_atlas.capabilities.errors import (
                ChannelAccessDenied,
                CooldownActive,
                ServiceUnavailable,
            )

            result = await wiki_cap.refresh_wiki(principal_id, channel_id, page_types=page_types)
            return result
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except CooldownActive as exc:
            return {
                "error": "cooldown_active",
                "retry_after_seconds": exc.retry_after_seconds,
            }
        except ServiceUnavailable as exc:
            return {"error": "service_unavailable", "service": exc.service}
        except Exception:
            logger.exception(
                "refresh_wiki: capability failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"error": "internal_error", "channel_id": channel_id}

    @mcp.tool(
        name="get_job_status",
        description=(
            "Poll the state of a long-running job created by trigger_sync or "
            "refresh_wiki. Returns a structured dict: "
            "{job_id, kind, status, progress, started_at, updated_at, ended_at, "
            "result, error, target}.\n\n"
            "status values: queued, running, done, error, cancelled.\n"
            "progress: fraction 0.0–1.0 or null when not yet available.\n\n"
            "Returns {error: 'job_not_found', job_id: ...} for jobs that do not "
            "exist or are not owned by the calling principal — no information about "
            "other principals' jobs is disclosed.\n\n"
            "Use atlas://job/<id> as a resource-read alternative when your MCP "
            "client prefers resources/read over tool calls."
        ),
    )
    async def get_job_status(
        job_id: Annotated[str, "The job id returned by trigger_sync or refresh_wiki"],
        ctx: Context,
    ) -> dict:
        principal_id = _get_principal_id(ctx)
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
                "get_job_status: capability failed principal=%s job_id=%s",
                principal_id,
                job_id,
            )
            return {"error": "job_not_found", "job_id": job_id}
