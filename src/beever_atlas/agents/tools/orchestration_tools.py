"""ADK-style orchestration tools for the QA agent (deep mode).

These tools wrap the framework-neutral capabilities in
``beever_atlas.capabilities`` so the internal QA agent can list
connections, list channels, trigger sync jobs, refresh the wiki, and
poll job status — all with the same access-control guarantees as the
MCP surface.

Principal identity propagation
-------------------------------
ADK tool functions receive only their declared keyword arguments; there
is no implicit session context injected by the framework.  We propagate
the calling user's ``principal_id`` through a request-scoped
``ContextVar`` (``_current_principal_id``) that the ``_run_agent_stream``
runner sets once per QA turn, before handing control to the LLM.

Callers (``api/ask.py``) must call ``bind_principal(user_id)`` and reset
the token at turn end — the same pattern used by ``follow_ups_tool.py``
for its collector.

Write-side safety
-----------------
``trigger_sync_tool`` and ``refresh_wiki_tool`` are intentionally named
so that the ``_UNTRUSTED_TOOL_DENYLIST_FRAGMENTS`` filter in
``qa_agent.py`` (extended in Phase 6 to include ``"sync"`` and
``"refresh"``) removes them when retrieved context is untrusted.
Read-only tools (``list_connections_tool``, ``list_channels_tool``,
``get_job_status_tool``) are preserved under untrusted context.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token

from beever_atlas.capabilities.errors import (
    CapabilityError,
    ChannelAccessDenied,
    ConnectionAccessDenied,
    CooldownActive,
    JobNotFound,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Principal-id contextvar (set per QA turn by the ask runner)
# ---------------------------------------------------------------------------

_current_principal_id: ContextVar[str | None] = ContextVar(
    "orchestration_principal_id", default=None
)


def bind_principal(principal_id: str) -> Token:
    """Bind *principal_id* for the current async task.

    Call this before running the agent turn; reset the returned token
    when the turn finishes::

        token = bind_principal(user_id)
        try:
            ...run agent...
        finally:
            reset_principal(token)
    """
    return _current_principal_id.set(principal_id)


def reset_principal(token: Token) -> None:
    """Reset the contextvar to its previous value."""
    _current_principal_id.reset(token)


def _get_principal() -> str | None:
    """Return the current turn's principal id, or None if unset."""
    return _current_principal_id.get()


# ---------------------------------------------------------------------------
# Error → structured dict translation
# ---------------------------------------------------------------------------

def _capability_error_to_dict(exc: CapabilityError) -> dict:
    """Translate a domain exception into a structured error dict.

    The agent receives this as a tool result instead of a raw traceback
    so it can surface a user-friendly message without crashing the turn.
    """
    if isinstance(exc, ChannelAccessDenied):
        return {"error": "channel_access_denied", "channel_id": exc.channel_id}
    if isinstance(exc, ConnectionAccessDenied):
        return {"error": "connection_access_denied", "connection_id": exc.connection_id}
    if isinstance(exc, CooldownActive):
        return {"error": "cooldown_active", "retry_after_seconds": exc.retry_after_seconds}
    if isinstance(exc, JobNotFound):
        return {"error": "job_not_found", "job_id": exc.job_id}
    return {"error": "capability_error", "detail": str(exc)}


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


async def list_connections_tool() -> dict:
    """List the platform connections owned by the current user.

    **When to call:**
    - The user asks "what connections do I have?" or "which platforms are
      connected?"
    - You need a ``connection_id`` before calling ``list_channels_tool``.
    - This is a cheap read-only call; safe to use under untrusted context.

    **When NOT to call:**
    - You already have a ``connection_id`` from earlier in the conversation.

    Returns a dict with key ``connections`` (list of connection dicts, each
    with ``connection_id``, ``platform``, ``display_name``, ``status``,
    ``last_synced_at``, ``selected_channel_count``, ``source``), or a
    structured error dict if access fails.
    """
    from beever_atlas.capabilities.connections import list_connections

    principal_id = _get_principal()
    if not principal_id:
        logger.warning("list_connections_tool called without a bound principal_id")
        return {"error": "no_principal", "detail": "Principal identity not available in this context"}

    try:
        connections = await list_connections(principal_id)
        return {"connections": connections}
    except CapabilityError as exc:
        logger.warning("list_connections_tool: capability error: %s", exc)
        return _capability_error_to_dict(exc)
    except Exception:
        logger.exception("list_connections_tool: unexpected error for principal=%s", principal_id)
        return {"error": "internal_error", "detail": "Failed to list connections"}


async def list_channels_tool(connection_id: str) -> dict:
    """List the selected channels for a given connection.

    **When to call:**
    - The user asks "what channels do I have in connection X?" or "list
      my channels."
    - You have a ``connection_id`` (from ``list_connections_tool`` or
      from the user's message) and need channel-level details.
    - This is a cheap read-only call; safe to use under untrusted context.

    **When NOT to call:**
    - You already have the ``channel_id`` the user is asking about.

    Args:
        connection_id: The connection whose channels to list.

    Returns a dict with key ``channels`` (list of channel dicts, each
    with ``channel_id``, ``name``, ``platform``, ``last_sync_ts``,
    ``sync_status``, ``message_count_estimate``), or a structured error
    dict on access failure.
    """
    from beever_atlas.capabilities.connections import list_channels

    principal_id = _get_principal()
    if not principal_id:
        logger.warning("list_channels_tool called without a bound principal_id")
        return {"error": "no_principal", "detail": "Principal identity not available in this context"}

    try:
        channels = await list_channels(principal_id, connection_id)
        return {"channels": channels}
    except CapabilityError as exc:
        logger.warning(
            "list_channels_tool: capability error for connection=%s: %s", connection_id, exc
        )
        return _capability_error_to_dict(exc)
    except Exception:
        logger.exception(
            "list_channels_tool: unexpected error for connection=%s principal=%s",
            connection_id,
            principal_id,
        )
        return {"error": "internal_error", "detail": "Failed to list channels"}


async def trigger_sync_tool(
    channel_id: str,
    sync_type: str = "incremental",
) -> dict:
    """Trigger a background sync job for a channel.

    **When to call — be selective:**
    - The user EXPLICITLY asks to sync, refresh, or re-ingest a channel
      (e.g. "please sync #general", "refresh the data for channel X").
    - OR retrieval tools returned empty/stale results AND the channel's
      ``last_sync_ts`` was more than 24 hours ago.

    **When NOT to call:**
    - Every question — call this only when data freshness is the explicit
      concern. Most questions are answered adequately from existing facts.
    - When a sync job is already running (check ``get_job_status_tool``
      first if unsure).

    **Untrusted context:** This tool is automatically removed from the
    tool list when retrieved content is untrusted, as a prompt-injection
    defence.

    Args:
        channel_id: The channel to sync.
        sync_type: ``"incremental"`` (default, only new messages) or
            ``"full"`` (re-ingest from the beginning).

    Returns ``{"job_id": "...", "status_uri": "atlas://job/<id>",
    "status": "queued"}`` on success, or a structured error dict on
    failure (e.g. ``{"error": "cooldown_active",
    "retry_after_seconds": N}``).
    """
    from beever_atlas.capabilities.sync import trigger_sync

    principal_id = _get_principal()
    if not principal_id:
        logger.warning("trigger_sync_tool called without a bound principal_id")
        return {"error": "no_principal", "detail": "Principal identity not available in this context"}

    try:
        result = await trigger_sync(
            principal_id=principal_id,
            channel_id=channel_id,
            sync_type=sync_type,
        )
        logger.info(
            "trigger_sync_tool: queued job_id=%s for channel=%s sync_type=%s",
            result.get("job_id"),
            channel_id,
            sync_type,
        )
        return result
    except CapabilityError as exc:
        logger.warning(
            "trigger_sync_tool: capability error for channel=%s: %s", channel_id, exc
        )
        return _capability_error_to_dict(exc)
    except ValueError as exc:
        # Raised by SyncRunner when a duplicate job exists.
        logger.info(
            "trigger_sync_tool: rejected for channel=%s: %s", channel_id, exc
        )
        return {"error": "sync_rejected", "detail": str(exc)}
    except Exception:
        logger.exception(
            "trigger_sync_tool: unexpected error for channel=%s principal=%s",
            channel_id,
            principal_id,
        )
        return {"error": "internal_error", "detail": "Failed to trigger sync"}


async def refresh_wiki_tool(
    channel_id: str,
    page_types: list[str] | None = None,
) -> dict:
    """Trigger async regeneration of the wiki pages for a channel.

    **When to call — be selective:**
    - The user EXPLICITLY requests a wiki refresh (e.g. "regenerate the
      wiki for #general", "update the FAQ page").
    - OR a recent sync has completed and you know new facts were added,
      making the cached wiki stale.

    **When NOT to call:**
    - Before or instead of answering from existing wiki pages — wiki pages
      are cached and usually fresh. Always try ``get_wiki_page`` first.
    - Without first triggering (or confirming) a completed sync. A wiki
      refresh over un-synced data produces no new content.

    **Untrusted context:** This tool is automatically removed from the
    tool list when retrieved content is untrusted, as a prompt-injection
    defence.

    Args:
        channel_id: The channel whose wiki to refresh.
        page_types: Optional list of page types to regenerate (subset of
            ``overview``, ``faq``, ``decisions``, ``people``, ``glossary``,
            ``activity``, ``topics``). Defaults to all 7 page types.

    Returns ``{"job_id": "...", "status_uri": "atlas://job/<id>",
    "status": "queued"}`` on success, or a structured error dict on
    failure.
    """
    from beever_atlas.capabilities.wiki import refresh_wiki

    principal_id = _get_principal()
    if not principal_id:
        logger.warning("refresh_wiki_tool called without a bound principal_id")
        return {"error": "no_principal", "detail": "Principal identity not available in this context"}

    try:
        result = await refresh_wiki(
            principal_id=principal_id,
            channel_id=channel_id,
            page_types=page_types,
        )
        logger.info(
            "refresh_wiki_tool: queued job_id=%s for channel=%s page_types=%s",
            result.get("job_id"),
            channel_id,
            page_types,
        )
        return result
    except CapabilityError as exc:
        logger.warning(
            "refresh_wiki_tool: capability error for channel=%s: %s", channel_id, exc
        )
        return _capability_error_to_dict(exc)
    except Exception:
        logger.exception(
            "refresh_wiki_tool: unexpected error for channel=%s principal=%s",
            channel_id,
            principal_id,
        )
        return {"error": "internal_error", "detail": "Failed to refresh wiki"}


async def get_job_status_tool(job_id: str) -> dict:
    """Return the current status of a background job (sync or wiki refresh).

    **When to call:**
    - The user asks about a specific job id from an earlier session (e.g.
      "what happened to job abc123?", "is that sync done?").
    - After calling ``trigger_sync_tool`` or ``refresh_wiki_tool``, if the
      user explicitly asks for a progress update.
    - This is a cheap read-only call; safe to use under untrusted context.

    **When NOT to call:**
    - To poll in a tight loop — the agent should mention the ``status_uri``
      and let the user or client poll via the REST endpoint instead.
    - When you don't have a job id.

    Args:
        job_id: The job identifier returned by ``trigger_sync_tool`` or
            ``refresh_wiki_tool``.

    Returns a status dict with ``job_id``, ``kind``, ``status``,
    ``progress``, ``started_at``, ``ended_at``, ``error``, and ``target``,
    or ``{"error": "job_not_found"}`` if the job does not exist or is not
    owned by the current user.
    """
    from beever_atlas.capabilities.jobs import get_job_status

    principal_id = _get_principal()
    if not principal_id:
        logger.warning("get_job_status_tool called without a bound principal_id")
        return {"error": "no_principal", "detail": "Principal identity not available in this context"}

    try:
        return await get_job_status(principal_id, job_id)
    except CapabilityError as exc:
        logger.warning(
            "get_job_status_tool: capability error for job=%s: %s", job_id, exc
        )
        return _capability_error_to_dict(exc)
    except Exception:
        logger.exception(
            "get_job_status_tool: unexpected error for job=%s principal=%s",
            job_id,
            principal_id,
        )
        return {"error": "internal_error", "detail": "Failed to get job status"}


# ---------------------------------------------------------------------------
# Exported list (for adding to tool registries)
# ---------------------------------------------------------------------------

ORCHESTRATION_TOOLS = [
    list_connections_tool,
    list_channels_tool,
    trigger_sync_tool,
    refresh_wiki_tool,
    get_job_status_tool,
]

__all__ = [
    "bind_principal",
    "reset_principal",
    "list_connections_tool",
    "list_channels_tool",
    "trigger_sync_tool",
    "refresh_wiki_tool",
    "get_job_status_tool",
    "ORCHESTRATION_TOOLS",
]
