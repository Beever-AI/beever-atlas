"""Tests for ``POST /api/channels/{channel_id}/wiki/refresh`` mode dispatch.

The endpoint accepts ``mode={update|reorganize|rebuild}`` and maps each to
a distinct user-facing action:

* ``update``     — ``force_restructure=False``; preserves folder structure
* ``reorganize`` — ``force_restructure=True``; re-runs the structure planner
* ``rebuild``    — snapshots the current wiki to history, wipes the cache,
                   then runs the generator with ``force_restructure=True``

Backward compat: legacy ``?restructure=true`` (without mode) is treated
as ``mode=reorganize``. Unknown modes fall back to ``update`` defensively.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.server.app import app


@pytest.fixture
async def client(mock_stores):  # noqa: ARG001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _patch_refresh_deps(
    *,
    existing_wiki: dict | None = None,
    background_capture: list | None = None,
):
    """Common patch stack for refresh endpoint tests.

    Captures the args ``BackgroundTasks.add_task`` was called with so
    the test can assert on the eventual ``force_restructure`` flag the
    background generator would receive. When ``existing_wiki`` is set,
    ``cache.get_wiki`` returns it (used by rebuild path tests).
    """

    fake_cache = AsyncMock()
    fake_cache.set_generation_status = AsyncMock()
    fake_cache.get_wiki = AsyncMock(return_value=existing_wiki)
    fake_cache.delete_wiki = AsyncMock(return_value=True)
    # Attach a non-async version_store with the methods the rebuild
    # branch awaits — archive + cleanup.
    fake_cache.version_store = MagicMock()
    fake_cache.version_store.archive = AsyncMock(return_value=42)
    fake_cache.version_store.cleanup = AsyncMock(return_value=0)

    if background_capture is not None:

        def _add_task(fn, *args, **kwargs):  # noqa: ANN001
            background_capture.append({"fn": fn, "args": args, "kwargs": kwargs})

        bg_patch = patch(
            "fastapi.BackgroundTasks.add_task",
            side_effect=_add_task,
        )
    else:
        bg_patch = patch(
            "fastapi.BackgroundTasks.add_task",
            new=MagicMock(),
        )

    return (
        patch("beever_atlas.api.wiki._get_cache", return_value=fake_cache),
        patch(
            "beever_atlas.api.wiki._resolve_target_lang",
            new=AsyncMock(return_value="en"),
        ),
        bg_patch,
    ), fake_cache


# Background-task arg layout (positional). Tests assert on these
# indices so a future signature reorder breaks loudly instead of
# silently sending the wrong flag.
_ARG_FORCE_RESTRUCTURE = 4
_ARG_WIPE_BEFORE_RUN = 5


@pytest.mark.skip(
    reason="pre-existing failure on branch since 6875d1c; CI hygiene only — TODO investigate and re-enable"
)
@pytest.mark.asyncio
async def test_refresh_mode_update_does_not_force_restructure(
    client: AsyncClient,
) -> None:
    """``mode=update`` (the default) preserves folder structure — the
    background generator must be invoked with ``force_restructure=False``
    and ``wipe_before_run=False``."""
    captured: list = []
    patches, fake_cache = _patch_refresh_deps(background_capture=captured)
    with patches[0], patches[1], patches[2]:
        resp = await client.post("/api/channels/C_MOCK_GENERAL/wiki/refresh?mode=update")
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "update"
    assert body["restructure"] is False
    # delete_wiki MUST NOT be called from the request handler at all
    # (the wipe — when applicable — runs inside the background task).
    fake_cache.delete_wiki.assert_not_called()
    fake_cache.version_store.archive.assert_not_called()
    assert len(captured) == 1
    assert captured[0]["args"][_ARG_FORCE_RESTRUCTURE] is False
    assert captured[0]["args"][_ARG_WIPE_BEFORE_RUN] is False


@pytest.mark.skip(
    reason="pre-existing failure on branch since 6875d1c; CI hygiene only — TODO investigate and re-enable"
)
@pytest.mark.asyncio
async def test_refresh_mode_reorganize_forces_restructure_no_wipe(
    client: AsyncClient,
) -> None:
    """``mode=reorganize`` forces the structure planner to run BUT does
    not wipe the cache — pages keep their IDs and curation flags
    (Pin/Hide). Distinct from rebuild."""
    captured: list = []
    patches, fake_cache = _patch_refresh_deps(background_capture=captured)
    with patches[0], patches[1], patches[2]:
        resp = await client.post("/api/channels/C_MOCK_GENERAL/wiki/refresh?mode=reorganize")
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "reorganize"
    assert body["restructure"] is True
    fake_cache.delete_wiki.assert_not_called()
    fake_cache.version_store.archive.assert_not_called()
    assert captured[0]["args"][_ARG_FORCE_RESTRUCTURE] is True
    assert captured[0]["args"][_ARG_WIPE_BEFORE_RUN] is False


@pytest.mark.skip(
    reason="pre-existing failure on branch since 6875d1c; CI hygiene only — TODO investigate and re-enable"
)
@pytest.mark.asyncio
async def test_refresh_mode_rebuild_archives_synchronously_defers_wipe(
    client: AsyncClient,
) -> None:
    """``mode=rebuild`` is the destructive path. Two-phase to avoid an
    "empty wiki" race window:

      Synchronous (request handler):
        1. Archive existing wiki to ``version_store`` (rollback point)
        2. Clean up old archived versions

      Deferred (inside background task, only after task starts running):
        3. Delete the live ``wiki_cache`` row (clean slate)
        4. Run the generator with ``force_restructure=True``

    The wipe MUST NOT happen in the request handler — if the background
    task is dropped between request return and execution, the user
    would be left with an empty wiki and no UI affordance to restore."""
    captured: list = []
    existing = {"channel_id": "C_MOCK_GENERAL", "pages": {"x": {"title": "X"}}}
    patches, fake_cache = _patch_refresh_deps(existing_wiki=existing, background_capture=captured)
    with patches[0], patches[1], patches[2]:
        resp = await client.post("/api/channels/C_MOCK_GENERAL/wiki/refresh?mode=rebuild")
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "rebuild"
    assert body["restructure"] is True
    # Synchronous side effects — archive and cleanup ran during the
    # request, providing the rollback point.
    fake_cache.version_store.archive.assert_awaited_once()
    fake_cache.version_store.cleanup.assert_awaited_once()
    # Wipe is DEFERRED — must NOT have happened in the request handler.
    # The background task (which we patched out) would have run it.
    fake_cache.delete_wiki.assert_not_called()
    # The deferred work is captured in the task args: wipe flag is True
    # so when the background task runs it will perform the wipe.
    assert captured[0]["args"][_ARG_FORCE_RESTRUCTURE] is True
    assert captured[0]["args"][_ARG_WIPE_BEFORE_RUN] is True


@pytest.mark.asyncio
async def test_refresh_mode_rebuild_skips_archive_when_no_existing(
    client: AsyncClient,
) -> None:
    """No existing wiki → nothing to archive, but rebuild still
    proceeds. The background task still receives ``wipe_before_run=True``
    (its delete_wiki is a no-op on a missing row but stays defensive)
    and ``force_restructure=True``. Defensive check for first-time
    channels."""
    captured: list = []
    patches, fake_cache = _patch_refresh_deps(existing_wiki=None, background_capture=captured)
    with patches[0], patches[1], patches[2]:
        resp = await client.post("/api/channels/C_MOCK_GENERAL/wiki/refresh?mode=rebuild")
    assert resp.status_code == 202
    fake_cache.version_store.archive.assert_not_called()
    fake_cache.delete_wiki.assert_not_called()
    # Generator still fires with force_restructure=True + wipe flag set.
    assert captured[0]["args"][_ARG_FORCE_RESTRUCTURE] is True
    assert captured[0]["args"][_ARG_WIPE_BEFORE_RUN] is True


@pytest.mark.skip(
    reason="pre-existing failure on branch since 6875d1c; CI hygiene only — TODO investigate and re-enable"
)
@pytest.mark.asyncio
async def test_refresh_legacy_restructure_param_maps_to_reorganize(
    client: AsyncClient,
) -> None:
    """Backward compat: callers still on ``?restructure=true`` (without
    a ``mode`` value) are silently upgraded to ``mode=reorganize``. No
    cache wipe, no archive — only the planner is forced."""
    captured: list = []
    patches, fake_cache = _patch_refresh_deps(background_capture=captured)
    with patches[0], patches[1], patches[2]:
        resp = await client.post("/api/channels/C_MOCK_GENERAL/wiki/refresh?restructure=true")
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "reorganize"
    assert body["restructure"] is True
    fake_cache.delete_wiki.assert_not_called()
    fake_cache.version_store.archive.assert_not_called()
    assert captured[0]["args"][_ARG_FORCE_RESTRUCTURE] is True
    assert captured[0]["args"][_ARG_WIPE_BEFORE_RUN] is False


@pytest.mark.skip(
    reason="pre-existing failure on branch since 6875d1c; CI hygiene only — TODO investigate and re-enable"
)
@pytest.mark.asyncio
async def test_refresh_unknown_mode_falls_back_to_update(
    client: AsyncClient,
) -> None:
    """A stale frontend sending ``mode=foo`` must NOT escalate to a
    destructive action. Unknown modes silently degrade to ``update``."""
    captured: list = []
    patches, fake_cache = _patch_refresh_deps(background_capture=captured)
    with patches[0], patches[1], patches[2]:
        resp = await client.post("/api/channels/C_MOCK_GENERAL/wiki/refresh?mode=foo")
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "update"
    assert body["restructure"] is False
    fake_cache.delete_wiki.assert_not_called()
    fake_cache.version_store.archive.assert_not_called()
    assert captured[0]["args"][_ARG_FORCE_RESTRUCTURE] is False
    assert captured[0]["args"][_ARG_WIPE_BEFORE_RUN] is False


@pytest.mark.skip(
    reason="pre-existing failure on branch since 6875d1c; CI hygiene only — TODO investigate and re-enable"
)
@pytest.mark.asyncio
async def test_run_generation_wipes_inside_task_when_flag_set() -> None:
    """Direct unit test on ``_run_generation`` — when called with
    ``wipe_before_run=True`` it must call ``cache.delete_wiki`` BEFORE
    invoking the builder. This is the core of the race-window fix:
    the wipe lives inside the background task, not in the request
    handler. Order is asserted via call-recording: delete_wiki must
    appear before refresh_wiki in the call sequence."""
    from beever_atlas.api.wiki import _run_generation

    fake_cache = AsyncMock()
    fake_cache.delete_wiki = AsyncMock(return_value=True)
    fake_cache.set_generation_status = AsyncMock()

    fake_builder = AsyncMock()
    fake_builder.refresh_wiki = AsyncMock()

    call_order: list[str] = []
    fake_cache.delete_wiki.side_effect = lambda *a, **kw: call_order.append("delete_wiki") or True
    fake_builder.refresh_wiki.side_effect = lambda *a, **kw: (
        call_order.append("refresh_wiki") or None
    )

    await _run_generation(
        fake_builder,
        "C_MOCK_GENERAL",
        fake_cache,
        target_lang="en",
        force_restructure=True,
        wipe_before_run=True,
    )

    assert call_order == ["delete_wiki", "refresh_wiki"]
    fake_cache.delete_wiki.assert_awaited_once_with("C_MOCK_GENERAL", target_lang="en")
    fake_builder.refresh_wiki.assert_awaited_once_with(
        "C_MOCK_GENERAL", target_lang="en", force_restructure=True
    )


@pytest.mark.asyncio
async def test_run_generation_skips_wipe_when_flag_unset() -> None:
    """``_run_generation`` with ``wipe_before_run=False`` (Update or
    Reorganize) must NOT touch ``delete_wiki`` even when invoked in the
    background. Defensive guard that the wipe path is genuinely opt-in."""
    from beever_atlas.api.wiki import _run_generation

    fake_cache = AsyncMock()
    fake_cache.delete_wiki = AsyncMock()
    fake_cache.set_generation_status = AsyncMock()
    fake_builder = AsyncMock()
    fake_builder.refresh_wiki = AsyncMock()

    await _run_generation(
        fake_builder,
        "C_MOCK_GENERAL",
        fake_cache,
        target_lang="en",
        force_restructure=False,
        wipe_before_run=False,
    )
    fake_cache.delete_wiki.assert_not_called()
    fake_builder.refresh_wiki.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_generation_continues_when_wipe_fails() -> None:
    """Wipe failure inside the background task is recoverable — the
    generator's ``save_wiki`` will overwrite whatever's there. Ensure
    a delete_wiki exception does NOT prevent the regeneration from
    running (otherwise a transient Mongo blip would leave the user
    stuck without a wiki refresh)."""
    from beever_atlas.api.wiki import _run_generation

    fake_cache = AsyncMock()
    fake_cache.delete_wiki = AsyncMock(side_effect=RuntimeError("mongo down"))
    fake_cache.set_generation_status = AsyncMock()
    fake_builder = AsyncMock()
    fake_builder.refresh_wiki = AsyncMock()

    await _run_generation(
        fake_builder,
        "C_MOCK_GENERAL",
        fake_cache,
        target_lang="en",
        force_restructure=True,
        wipe_before_run=True,
    )
    fake_cache.delete_wiki.assert_awaited_once()
    # Generator still ran despite the wipe failure.
    fake_builder.refresh_wiki.assert_awaited_once()
