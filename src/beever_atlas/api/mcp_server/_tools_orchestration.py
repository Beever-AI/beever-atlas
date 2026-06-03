"""Long-running-job orchestration tools: trigger_sync, refresh_wiki, get_job_status."""

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
            "Ingest a chat channel's messages into the Atlas knowledge base so they "
            "become searchable by the retrieval tools (ask_channel, "
            "search_channel_facts, search_memory). This is the WRITE/ingestion entry "
            "point; it does NOT answer questions — use the retrieval tools for that. "
            "It is distinct from refresh_wiki, which only re-renders wiki pages from "
            "already-ingested facts.\n\n"
            "WHEN TO USE: only when the user EXPLICITLY asks to sync/refresh a "
            "channel, OR when retrieval tools return empty/stale results AND the "
            "channel was last synced over 24h ago. WHEN NOT TO USE: do not call "
            "before every question or as a precautionary warm-up — prefer the data "
            "already indexed. Sync is expensive and rate-limited (cooldown) per "
            "channel.\n\n"
            "PREREQUISITES: get a valid channel_id (and ideally connection_id) from "
            "list_channels first. The calling principal must have access to the "
            "channel.\n\n"
            "LATENCY & SIDE EFFECTS: asynchronous. Returns within ~5s with a job "
            "envelope while ingestion runs in the background; this WRITES facts to "
            "the knowledge base. Shape: "
            "{job_id: 'job_abc123', status_uri: 'atlas://job/job_abc123', "
            "status: 'queued'}. Track progress by calling get_job_status(job_id) or "
            "reading the atlas://job/<job_id> resource.\n\n"
            "IDEMPOTENT: if a queued or running sync already exists for the channel, "
            "its existing job_id is returned instead of starting a duplicate. A new "
            "job is created only when no active job exists, or after the prior one "
            "completed or failed.\n\n"
            "ERROR MODES (returned as {error: ...}, never raised): "
            "'authentication_missing' (no principal); "
            "'invalid_parameter' (malformed channel_id/connection_id); "
            "'channel_access_denied' (principal lacks access); "
            "'cooldown_active' (synced too recently; includes retry_after_seconds); "
            "'service_unavailable' (backing service down; includes service); "
            "'internal_error' (unexpected failure)."
        ),
    )
    async def trigger_sync(
        channel_id: Annotated[
            str,
            "Channel to sync, e.g. 'ch_eng_backend'. Get it from list_channels. Required.",
        ],
        ctx: Context,
        sync_type: Annotated[
            str,
            "Sync mode. 'incremental' (default) fetches only messages newer than the "
            "last sync (cheap); 'full' re-fetches the entire history (expensive); "
            "'auto' lets the server pick based on sync history. Valid values: "
            "'incremental' | 'full' | 'auto'.",
        ] = "incremental",
        connection_id: Annotated[
            str | None,
            "Platform connection that owns the channel, e.g. 'conn_slack_acme'. Get "
            "it from list_channels or list_connections. Default None. Optional but "
            "STRONGLY RECOMMENDED when multiple same-platform connections exist (e.g. "
            "two Slack workspaces): without it the server matches the channel against "
            "each connection's selected_channels pick-list and may mis-route the sync "
            "if the channel was never added to a pick-list.",
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
            "Re-render a channel's pre-compiled wiki pages (overview, FAQ, decisions, "
            "etc.) from facts ALREADY ingested into the knowledge base. Use this to "
            "rebuild stale wiki content; the refreshed pages are then read with "
            "get_wiki_page / read_wiki_page / list_wiki_pages. It does NOT ingest new "
            "messages — that is trigger_sync's job — and it does NOT answer questions "
            "(use the retrieval tools for that).\n\n"
            "WHEN TO USE: after a sync has added new facts (i.e. after trigger_sync "
            "completes), or when the user explicitly asks to regenerate the wiki. "
            "WHEN NOT TO USE: do not call routinely — the standard sync pipeline "
            "already rebuilds wiki pages automatically, so calling this after a normal "
            "sync is usually redundant.\n\n"
            "PREREQUISITES: a valid channel_id from list_channels; the channel must "
            "have ingested facts (run trigger_sync first if it has none); the calling "
            "principal must have access.\n\n"
            "LATENCY & SIDE EFFECTS: asynchronous and expensive (runs an LLM "
            "generation pass). Returns within ~5s with a job envelope while "
            "generation runs in the background; this WRITES/overwrites the channel's "
            "wiki pages. Shape: "
            "{job_id: 'job_def456', status_uri: 'atlas://job/job_def456', "
            "status: 'queued'}. Track progress with get_job_status(job_id) or the "
            "atlas://job/<job_id> resource.\n\n"
            "ERROR MODES (returned as {error: ...}, never raised): "
            "'authentication_missing'; 'invalid_parameter' (malformed channel_id); "
            "'channel_access_denied'; "
            "'cooldown_active' (refreshed too recently; includes retry_after_seconds); "
            "'service_unavailable' (includes service); 'internal_error'."
        ),
    )
    async def refresh_wiki(
        channel_id: Annotated[
            str,
            "Channel whose wiki pages to regenerate, e.g. 'ch_eng_backend'. Get it "
            "from list_channels. Required.",
        ],
        ctx: Context,
        page_types: Annotated[
            list[str] | None,
            "Subset of wiki page types to regenerate, e.g. ['overview', 'faq']. "
            "Valid values: 'overview' | 'faq' | 'decisions' | 'people' | 'glossary' | "
            "'activity' | 'topics'. Default None, which regenerates ALL page types.",
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
            "Check the progress and result of a background job started by "
            "trigger_sync or refresh_wiki. Call this AFTER one of those tools "
            "returns a job_id, to learn whether the sync/wiki-generation has "
            "finished. This is read-only and instant; it neither starts work nor "
            "answers channel questions (use the retrieval tools for that).\n\n"
            "WHEN TO USE: poll after trigger_sync/refresh_wiki to wait for "
            "completion before reading the freshly ingested/regenerated data. "
            "POLLING CADENCE: wait ~2–3s between polls and back off on repeats; do "
            "NOT hot-loop. Sync/wiki jobs typically take seconds to a few minutes — "
            "stop once status is a terminal value (done / error / cancelled).\n\n"
            "PREREQUISITES: a job_id previously returned by trigger_sync or "
            "refresh_wiki; the job must belong to the calling principal.\n\n"
            "LATENCY & SIDE EFFECTS: instant, no side effects.\n\n"
            "RETURNS a dict: {job_id, kind ('sync' | 'wiki'), status, progress, "
            "started_at, updated_at, ended_at, result, error, target}. "
            "status: 'queued' | 'running' | 'done' | 'error' | 'cancelled'. "
            "progress: float 0.0–1.0, or null when not yet available. "
            "result/error are populated only once the job reaches a terminal state.\n\n"
            "ERROR MODES (returned as {error: ...}): 'authentication_missing'; "
            "'invalid_parameter' (malformed job_id); 'job_not_found' — returned both "
            "for ids that do not exist AND for jobs owned by another principal, so no "
            "cross-principal job information is disclosed.\n\n"
            "Reading the atlas://job/<job_id> resource is an equivalent alternative "
            "for clients that prefer resources/read over tool calls."
        ),
    )
    async def get_job_status(
        job_id: Annotated[
            str,
            "Job to inspect, e.g. 'job_abc123'. This is the job_id returned by "
            "trigger_sync or refresh_wiki. Required.",
        ],
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
