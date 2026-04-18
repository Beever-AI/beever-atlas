"""Job-status capability.

Framework-neutral implementation for openspec change ``atlas-mcp-server``
Phase 1 (task 1.7). Reads from the shared ``sync_jobs`` collection and
enforces ownership: if the job does not exist OR the requesting principal
does not own it, :class:`~capabilities.errors.JobNotFound` is raised so
callers cannot probe for job-id existence without ownership.
"""

from __future__ import annotations

import logging

from beever_atlas.capabilities.errors import JobNotFound

logger = logging.getLogger(__name__)

_LEGACY_SHARED_OWNER = "legacy:shared"


async def get_job_status(principal_id: str, job_id: str) -> dict:
    """Return the status dict for *job_id* if *principal_id* owns it.

    Ownership rules:

    1. ``job.owner_principal_id == principal_id`` → allowed.
    2. ``job.owner_principal_id in {None, "legacy:shared"}`` AND
       ``principal_id`` does **not** start with ``"mcp:"`` → allowed
       (legacy single-tenant fallback for pre-migration rows).
    3. Anything else → raises :class:`~capabilities.errors.JobNotFound`.

    Returned dict keys:
    ``job_id, kind, status, progress, started_at, updated_at, ended_at,
    result, error, target``.
    """
    from beever_atlas.stores import get_stores

    stores = get_stores()
    # Use the internal MongoDB find_one path via the sync_jobs collection.
    doc = await stores.mongodb._sync_jobs.find_one({"id": job_id})  # type: ignore[attr-defined]
    if doc is None:
        raise JobNotFound(job_id)

    doc.pop("_id", None)

    owner = doc.get("owner_principal_id")

    # Explicit ownership match.
    if owner == principal_id:
        return _build_status(doc)

    # Legacy / unowned rows: allow non-MCP principals only.
    if owner in (None, _LEGACY_SHARED_OWNER) and not principal_id.startswith("mcp:"):
        return _build_status(doc)

    # Principal is MCP and the job is owned by someone else (or legacy).
    raise JobNotFound(job_id)


def _build_status(doc: dict) -> dict:
    """Build the public status dict from a raw MongoDB document."""
    started_at = doc.get("started_at")
    completed_at = doc.get("completed_at")
    return {
        "job_id": doc.get("id"),
        "kind": doc.get("kind", "sync"),
        "status": doc.get("status", "unknown"),
        "progress": {
            "processed_messages": doc.get("processed_messages", 0),
            "total_messages": doc.get("total_messages", 0),
            "current_stage": doc.get("current_stage"),
        },
        "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else started_at,
        "updated_at": None,  # not a separate field yet
        "ended_at": completed_at.isoformat() if hasattr(completed_at, "isoformat") else completed_at,
        "result": None,
        "error": doc.get("errors") or None,
        "target": {"channel_id": doc.get("channel_id")},
    }


__all__ = ["get_job_status"]
