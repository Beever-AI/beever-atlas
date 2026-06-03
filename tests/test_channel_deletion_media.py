"""Durable-channel-media purge stage of the channel-deletion fan-out.

durable-channel-media. Exercises the new media-blob purge stage added to
``services.channel_deletion._purge_fanout`` (step 11): it calls
``stores.media_blob_store.delete_by_channel(channel_id)`` and merges the
returned counts into the purge ``counts`` dict, isolated in its own
try/except like every other stage.

Coverage:
  * present store — ``delete_by_channel`` called once with the channel id and
    its ``blobs_deleted``/``refs_deleted`` merged into ``counts`` under the
    ``channel_media_blobs``/``channel_media_refs`` keys; clean run completes;
  * raising store — the stage records an error, every OTHER stage still runs,
    and the purge is reported "partial" with the lock RETAINED;
  * absent store — ``getattr(stores, "media_blob_store", None) is None`` is
    skipped silently (debug log, NOT an error) and the purge still completes.

Reuses the cross-store fakes + lazy-import patching from
``tests/services/test_channel_deletion.py`` so the media stage is exercised in
the real fan-out ordering rather than against a hand-rolled stub.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import beever_atlas.services.channel_deletion as cd
from tests.services.test_channel_deletion import _CHANNEL_ID, patched  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Rebuild the Settings cache around every test in this module.

    Keeps this module from depending on (or corrupting) the process-wide
    ``get_settings()`` lru_cache — see test_media_blob_store.py for the full
    rationale on the conftest ``_auth_bypass`` / cache-priming interaction.
    """
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _attach_media_store(patched_ns, store) -> None:
    """Wire ``media_blob_store`` onto the patched stores stub.

    The shared ``_make_stores`` builds a ``SimpleNamespace`` WITHOUT a
    ``media_blob_store`` attribute (that absence is exactly the "store absent"
    case), so the present-store tests bolt it on here.
    """
    patched_ns.stores.media_blob_store = store


# ─────────────────────────────────────────────────────────────────────────────
# Present store — counts merged
# ─────────────────────────────────────────────────────────────────────────────


async def test_purge_calls_media_store_and_merges_counts(patched) -> None:  # noqa: F811
    """A present ``media_blob_store`` is called once with the channel id and its
    returned counts are merged into ``counts`` under the media keys."""
    media_store = type("_MediaStore", (), {})()
    media_store.delete_by_channel = AsyncMock(return_value={"blobs_deleted": 5, "refs_deleted": 11})
    _attach_media_store(patched, media_store)

    result = await cd.purge_channel(_CHANNEL_ID, principal_id="user:alice")

    assert result["status"] == "completed"
    assert result["errors"] == {}

    media_store.delete_by_channel.assert_awaited_once_with(_CHANNEL_ID)
    assert result["counts"]["channel_media_blobs"] == 5
    assert result["counts"]["channel_media_refs"] == 11

    # Merged counts also land in the audit payload (audit runs after the
    # fan-out, before the release).
    audit_kwargs = patched.stores.mongodb.log_channel_purge_audit.call_args.kwargs
    assert audit_kwargs["counts"]["channel_media_blobs"] == 5
    assert audit_kwargs["counts"]["channel_media_refs"] == 11

    # Clean run → lock released.
    assert patched.locks.release_calls == 1
    assert not patched.locks.is_held(_CHANNEL_ID)


# ─────────────────────────────────────────────────────────────────────────────
# Raising store — isolated, other stages still run
# ─────────────────────────────────────────────────────────────────────────────


async def test_media_store_raising_records_error_and_runs_other_stages(patched) -> None:  # noqa: F811
    """``delete_by_channel`` raising → the stage records an error, every other
    stage still runs (counts intact), and the purge is reported 'partial' with
    the lock RETAINED for the reaper."""
    media_store = type("_MediaStore", (), {})()
    media_store.delete_by_channel = AsyncMock(side_effect=RuntimeError("gridfs down"))
    _attach_media_store(patched, media_store)

    result = await cd.purge_channel(_CHANNEL_ID, principal_id="user:alice")

    assert result["status"] == "partial"
    assert "media_blob_store_delete_by_channel" in result["errors"]
    assert "gridfs down" in result["errors"]["media_blob_store_delete_by_channel"]
    # No media counts on the failure path.
    assert "channel_media_blobs" not in result["counts"]
    assert "channel_media_refs" not in result["counts"]

    # Other stages still ran — counts from the preceding stages are intact.
    s = patched.stores
    s.graph.delete_channel_data.assert_awaited_once_with(_CHANNEL_ID)
    s.mongodb.purge_channel.assert_awaited_once_with(_CHANNEL_ID)
    assert result["counts"]["entities_deleted"] == 7
    assert result["counts"]["chat_history_deleted"] == 8
    assert result["counts"]["channel_messages"] == 9

    # Audit still written, carrying the promoted error.
    patched.stores.mongodb.log_channel_purge_audit.assert_awaited_once()
    audit_kwargs = patched.stores.mongodb.log_channel_purge_audit.call_args.kwargs
    assert "media_blob_store_delete_by_channel" in audit_kwargs["errors"]

    # Lock RETAINED (not released) so the reaper re-runs.
    assert patched.locks.release_calls == 0
    assert patched.locks.is_held(_CHANNEL_ID)


# ─────────────────────────────────────────────────────────────────────────────
# Absent store — skipped silently
# ─────────────────────────────────────────────────────────────────────────────


async def test_media_store_absent_is_skipped_silently(patched) -> None:  # noqa: F811
    """``getattr(stores, "media_blob_store", None) is None`` → the stage is
    skipped without recording an error and the purge still completes. The
    shared stores stub ships WITHOUT a ``media_blob_store`` attribute, so this
    asserts the default absence path."""
    assert getattr(patched.stores, "media_blob_store", None) is None

    result = await cd.purge_channel(_CHANNEL_ID, principal_id="user:alice")

    assert result["status"] == "completed"
    # The skip is NOT an error.
    assert "media_blob_store_delete_by_channel" not in result["errors"]
    assert result["errors"] == {}
    # No media counts recorded when the store is absent.
    assert "channel_media_blobs" not in result["counts"]
    assert "channel_media_refs" not in result["counts"]

    # Every other stage still ran + clean run released the lock.
    patched.stores.mongodb.purge_channel.assert_awaited_once_with(_CHANNEL_ID)
    assert patched.locks.release_calls == 1
    assert not patched.locks.is_held(_CHANNEL_ID)
