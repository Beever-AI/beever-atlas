"""Shared in-process registry + helpers for the re-embed migration job.

This module owns the *single* process-local migration registry plus the
spawn-the-job and status-snapshot logic. It is imported by BOTH the legacy
deprecation-stamped router (``api/embedding_settings.py`` —
``POST /api/settings/embedding/migrate`` + ``GET .../migrate/status``) AND
the new non-deprecated router (``api/embedding_migration.py`` —
``POST /api/settings/embedding-migration/spawn`` + ``GET .../status``).

Why one registry: a re-embed triggered via *either* surface must dedupe
against the other, and a ``/status`` poll from *either* must reflect the
running job regardless of which surface kicked it off. Keeping the registry
in a third module avoids a circular import (the new router would otherwise
import from the legacy router just for the dict) and makes the ownership
explicit — the re-embed *machinery* is real infra independent of the
deprecated embedding-config surface.

A single in-process registry is intentional: re-embed is operator-triggered
and globally-singleton-by-design. A restart wipes the registry but the
checkpoint in MongoDB collection ``reembed_state`` survives.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)


# Module-level singleton. ``task`` is the asyncio.Task running the re-embed
# (or None); ``job_id`` is the most recent job's uuid; ``started_at`` is the
# ISO timestamp it was kicked off; ``error`` is a class-name-only string for
# the most recent failure (raw exception strings are never stored here).
_active_migration: dict[str, Any] = {
    "task": None,
    "job_id": None,
    "started_at": None,
    "error": None,
}


def spawn_reembed_job() -> tuple[str, str]:
    """Spawn the re-embed job as a fire-and-forget asyncio Task.

    Dedupes: if a task is already running, returns the existing job's id
    with status ``"running_existing"`` instead of starting a second one.

    Returns ``(job_id, status)`` where ``status`` is ``"running"`` (a new
    job was started) or ``"running_existing"`` (an in-flight job was reused).
    """
    existing = _active_migration.get("task")
    if existing is not None and not existing.done():
        return _active_migration["job_id"], "running_existing"

    job_id = str(uuid.uuid4())
    _active_migration["job_id"] = job_id
    _active_migration["started_at"] = datetime.now(tz=UTC).isoformat()
    _active_migration["error"] = None

    async def _run() -> None:
        from scripts.reembed_facts import main as reembed_main

        try:
            # Reuse the server's stores singleton so the migration job does
            # NOT call init_stores/startup/shutdown on a competing
            # StoreClients instance — closing the migration's stores in
            # ``finally`` previously also closed the server's MongoClient
            # (singleton was overwritten), tripping every subsequent request
            # with ``Cannot use MongoClient after close``.
            await reembed_main(stores=get_stores())
        except SystemExit as exc:
            # Defensive: ``main`` no longer parses argv (that lived in the
            # CLI wrapper), so SystemExit should not fire here. If it ever
            # does (e.g. transitively-imported code calling sys.exit), trap
            # it — a bare ``raise`` would propagate through the Task and kill
            # uvicorn.
            _active_migration["error"] = f"SystemExit({exc.code})"
            logger.error("reembed: SystemExit trapped to protect uvicorn: %s", exc)
            await _clear_checkpoint_safely()
        except Exception as exc:  # noqa: BLE001
            # Log the full traceback server-side so the operator can
            # diagnose. Surface ONLY the exception class to the UI — raw
            # exception strings frequently include internal URLs, hostnames,
            # request IDs, and (in some provider SDKs) partial credentials.
            # The class name is enough for the banner to communicate
            # "re-embed failed: <type>", and the server log retains
            # everything else.
            _active_migration["error"] = (
                f"{type(exc).__name__}: migration failed (see server logs for details)"
            )
            logger.error(
                "reembed: migration task failed — %s",
                exc,
                exc_info=True,
            )
            # PR-η.1: clear the checkpoint on failure. The script writes a
            # "stage=weaviate_embed, processed=0" checkpoint at the start of
            # the embed phase and only clears it on success. A mid-flight
            # crash leaves that stale checkpoint in MongoDB, which makes the
            # UI's progress banner show "Re-embedding in progress · 0 / N"
            # forever — even though the task has exited. Without this clear
            # the operator has to surgically delete the doc from MongoDB
            # before retrying. Best-effort: never let cleanup mask the
            # underlying error.
            await _clear_checkpoint_safely()
            raise

    _active_migration["task"] = asyncio.create_task(_run())
    return job_id, "running"


async def _clear_checkpoint_safely() -> None:
    """Delete the ``reembed_state`` checkpoint doc, never raising."""
    try:
        stores = get_stores()
        await stores.mongodb.db["reembed_state"].delete_one({"_id": "reembed_state"})
    except Exception as cleanup_exc:  # noqa: BLE001
        logger.warning(
            "reembed: failed to clear stale checkpoint after job failure: %s",
            cleanup_exc,
        )


async def migration_status_snapshot() -> dict[str, Any]:
    """Return a plain-dict snapshot of the current re-embed migration state.

    Reads the ``reembed_state`` checkpoint doc (the job updates this every
    batch) and the in-process registry. The shape mirrors the legacy
    ``MigrateStatusResponse`` fields so both routers can wrap it in their
    own response models.
    """
    stores = get_stores()
    cp = await stores.mongodb.db["reembed_state"].find_one({"_id": "reembed_state"})
    if cp:
        cp.pop("_id", None)

    task = _active_migration.get("task")
    running = task is not None and not task.done()
    finished_at: str | None = None
    error = _active_migration.get("error")

    if task is not None and task.done():
        finished_at = (cp or {}).get("updated_at")
        if task.exception() is not None:
            error = error or str(task.exception())

    return {
        "running": running,
        "job_id": _active_migration.get("job_id"),
        "stage": (cp or {}).get("stage"),
        "processed": (cp or {}).get("processed"),
        "total": (cp or {}).get("total"),
        "started_at": _active_migration.get("started_at"),
        "finished_at": finished_at,
        "error": error,
    }


__all__ = [
    "_active_migration",
    "spawn_reembed_job",
    "migration_status_snapshot",
]
