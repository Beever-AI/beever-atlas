"""Tests for the per-channel reset endpoint.

Covers ``POST /api/admin/channels/{channel_id}/reset`` — drops a single
channel's derived memory (facts, entities, wiki pages, graph nodes,
sync state) and optionally triggers a follow-up sync. The reset MUST:

  1. Fan calls out to all five derived-data stores in fixed order.
  2. Refuse with 409 when a sync is in flight (concurrent-sync gate).
  3. Refuse with 400 when ``i_understand_data_loss != "yes"``.
  4. Refuse with 401 when the admin token is missing or wrong.
  5. Continue with the remaining stores when one raises — partial
     failure surfaces in ``errors`` but the request returns 200.
  6. Trigger ``SyncRunner.start_sync(sync_type="full")`` when
     ``trigger_resync=true`` and echo the returned ``job_id``.
  7. Be idempotent: a second call returns 200 with zero counts.

The tests build a minimal FastAPI app with the admin router mounted and
patch ``stores`` + ``get_sync_runner`` to in-memory fakes so the test
suite does not require live Neo4j / Weaviate / Mongo / sync runner.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api.admin import router as admin_router
from beever_atlas.infra import auth as auth_mod
from beever_atlas.stores import init_stores


_ADMIN_TOKEN = "admin-token-abc"
_CHANNEL_ID = "C-test-reset"


def _patch_admin(monkeypatch, token: str = _ADMIN_TOKEN) -> None:
    fake = SimpleNamespace(admin_token=token)
    monkeypatch.setattr(auth_mod, "get_settings", lambda: fake)


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": _ADMIN_TOKEN}


class _FakeSyncRunner:
    """Captures ``start_sync`` calls without touching real workers."""

    def __init__(self, job_id: str = "resync-job-1") -> None:
        self.job_id = job_id
        self.calls: list[dict[str, Any]] = []

    async def start_sync(
        self,
        channel_id: str,
        *,
        sync_type: str = "auto",
        use_batch_api: bool = False,
        connection_id: str | None = None,
        owner_principal_id: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "channel_id": channel_id,
                "sync_type": sync_type,
                "use_batch_api": use_batch_api,
                "connection_id": connection_id,
                "owner_principal_id": owner_principal_id,
            }
        )
        return self.job_id


class _FakePageStore:
    """Captures ``delete_all_for_channel`` calls without touching MongoDB."""

    def __init__(self, returns: dict[str, int] | int = 4) -> None:
        # Accept either a per-lang dict or a single int (applied to every
        # language) so tests can dial in fine-grained behaviour.
        self._returns = returns
        self.calls: list[tuple[str, str]] = []
        self.bind_db_called = False

    def __call__(self, db: Any = None) -> "_FakePageStore":
        # ``WikiPageStore(db=stores.mongodb.db)`` constructs a fresh
        # instance — our test fake is also callable so the production
        # call site (``WikiPageStore(db=stores.mongodb.db)``) returns
        # this same captured instance.
        return self

    async def delete_all_for_channel(self, channel_id: str, target_lang: str = "en") -> int:
        self.calls.append((channel_id, target_lang))
        if isinstance(self._returns, dict):
            return int(self._returns.get(target_lang, 0))
        return int(self._returns)


@pytest.fixture
def fake_page_store(monkeypatch) -> _FakePageStore:
    """Patch ``WikiPageStore`` so the endpoint's import-and-instantiate
    pattern returns our capturing fake.
    """
    fake = _FakePageStore(returns=4)
    # The endpoint imports ``WikiPageStore`` from
    # ``beever_atlas.wiki.page_store`` inside the request handler. Patch
    # both module attribute and the symbol that the lazy import path
    # will resolve to.
    import beever_atlas.wiki.page_store as page_store_mod

    monkeypatch.setattr(page_store_mod, "WikiPageStore", fake)
    return fake


@pytest.fixture
def fake_runner(monkeypatch) -> _FakeSyncRunner:
    runner = _FakeSyncRunner()
    # The endpoint imports ``get_sync_runner`` lazily from
    # ``beever_atlas.api.sync``. Patch the module attribute.
    import beever_atlas.api.sync as sync_mod

    monkeypatch.setattr(sync_mod, "get_sync_runner", lambda: runner)
    return runner


@pytest.fixture
def fake_stores(monkeypatch):
    """Wire up in-memory fakes for the five derived-data store entry points."""
    graph = SimpleNamespace(
        delete_channel_data=AsyncMock(
            return_value={
                "events_deleted": 12,
                "media_deleted": 5,
                "entities_deleted": 7,
            }
        ),
    )
    weaviate = SimpleNamespace(
        delete_by_channel=AsyncMock(return_value=42),
    )
    # ``db["channel_messages"].update_many`` is called by the reset to flip
    # ``extraction_status`` back to ``pending`` so the next sync re-extracts.
    channel_messages_coll = SimpleNamespace(
        update_many=AsyncMock(return_value=SimpleNamespace(modified_count=7)),
    )
    # ``db["wiki_cache"].delete_one`` is called per language to drop the
    # cached wiki bundle keyed by ``<channel_id>:<lang>`` so the UI does
    # not keep serving the pre-reset snapshot.
    wiki_cache_coll = SimpleNamespace(
        delete_one=AsyncMock(return_value=SimpleNamespace(deleted_count=1)),
    )
    mongodb = SimpleNamespace(
        db={"channel_messages": channel_messages_coll, "wiki_cache": wiki_cache_coll},
        get_latest_sync_job=AsyncMock(return_value=None),
        clear_channel_sync_state=AsyncMock(return_value=None),
    )
    container = SimpleNamespace(graph=graph, weaviate=weaviate, mongodb=mongodb)
    init_stores(container)  # type: ignore[arg-type]
    return container


@pytest.fixture
def app(monkeypatch, fake_stores, fake_page_store):  # noqa: ARG001
    _patch_admin(monkeypatch)
    app = FastAPI()
    app.include_router(admin_router)
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_reset_happy_path_calls_every_store(client: TestClient, fake_stores) -> None:
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "yes"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset_complete"
    assert body["channel_id"] == _CHANNEL_ID
    assert body["errors"] == []
    assert body["resync_job_id"] is None
    counts = body["counts"]
    assert counts["events_deleted"] == 12
    assert counts["media_deleted"] == 5
    assert counts["entities_deleted"] == 7
    assert counts["weaviate_deleted"] == 42
    assert counts["sync_state_cleared"] == 1
    # Wiki is intentionally left untouched — the wiki subsystem owns its
    # own reset path at ``POST /wiki/refresh?mode=rebuild`` (archives to
    # version history first). No wiki keys appear in the reset response.
    assert "wiki_graph_deleted" not in counts
    assert "wiki_pages_deleted" not in counts
    assert "wiki_bundle_cleared" not in counts
    assert "wiki_generation_status_cleared" not in counts

    fake_stores.graph.delete_channel_data.assert_awaited_once_with(_CHANNEL_ID)
    fake_stores.weaviate.delete_by_channel.assert_awaited_once_with(_CHANNEL_ID)
    fake_stores.mongodb.clear_channel_sync_state.assert_awaited_once_with(_CHANNEL_ID)


def test_reset_does_not_touch_wiki(
    client: TestClient,
    fake_page_store: _FakePageStore,
    fake_stores,
) -> None:
    """The reset endpoint must NOT delete wiki state. Wiki has its own
    versioned rebuild path; doing it here would bypass version archiving.
    """
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params=[
            ("i_understand_data_loss", "yes"),
            ("languages", "en"),
            ("languages", "ja"),
        ],
    )
    assert r.status_code == 200, r.text
    assert fake_page_store.calls == []  # WikiPageStore was not invoked
    # ``delete_channel_wiki_graph`` is not even present on the fake graph
    # store — its absence proves the reset code never reached for it.
    assert not hasattr(fake_stores.graph, "delete_channel_wiki_graph")


# ---------------------------------------------------------------------------
# 2. Missing confirmation token
# ---------------------------------------------------------------------------


def test_reset_rejects_missing_confirmation_token(client: TestClient) -> None:
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
    )
    # ``i_understand_data_loss`` is a required Query param → FastAPI 422.
    assert r.status_code == 422, r.text


def test_reset_rejects_wrong_confirmation_token(client: TestClient) -> None:
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "maybe"},
    )
    assert r.status_code == 400, r.text
    assert "i_understand_data_loss" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 3. Concurrent-sync gate
# ---------------------------------------------------------------------------


def test_reset_returns_409_when_sync_in_progress(
    client: TestClient,
    fake_stores,
) -> None:
    fake_stores.mongodb.get_latest_sync_job = AsyncMock(
        return_value=SimpleNamespace(status="running", id="job-running-1")
    )
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "yes"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "sync in progress, cannot reset"
    # None of the destructive stages ran.
    fake_stores.graph.delete_channel_data.assert_not_called()
    fake_stores.weaviate.delete_by_channel.assert_not_called()
    fake_stores.mongodb.clear_channel_sync_state.assert_not_called()


def test_reset_proceeds_when_latest_job_is_completed(
    client: TestClient,
    fake_stores,
) -> None:
    """A finished sync job does NOT gate a reset."""
    fake_stores.mongodb.get_latest_sync_job = AsyncMock(
        return_value=SimpleNamespace(status="completed", id="job-done-1")
    )
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "yes"},
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 4. Auth — non-admin caller
# ---------------------------------------------------------------------------


def test_reset_rejects_missing_admin_token(client: TestClient) -> None:
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        params={"i_understand_data_loss": "yes"},
    )
    assert r.status_code == 401, r.text


def test_reset_rejects_wrong_admin_token(client: TestClient) -> None:
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers={"X-Admin-Token": "wrong-token"},
        params={"i_understand_data_loss": "yes"},
    )
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# 5. Partial failure resilience
# ---------------------------------------------------------------------------


def test_reset_continues_when_one_store_raises(
    client: TestClient,
    fake_stores,
) -> None:
    """A single-store failure is surfaced in ``errors`` but does NOT
    abort the remaining stages — the documented "no partial rollback"
    contract.
    """
    fake_stores.weaviate.delete_by_channel = AsyncMock(side_effect=RuntimeError("boom"))
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "yes"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Weaviate failure surfaces in errors, no count.
    assert any("weaviate.delete_by_channel" in e for e in body["errors"])
    assert "weaviate_deleted" not in body["counts"]
    # Other stages still ran.
    assert body["counts"]["entities_deleted"] == 7
    assert body["counts"]["sync_state_cleared"] == 1
    fake_stores.mongodb.clear_channel_sync_state.assert_awaited_once_with(_CHANNEL_ID)


# ---------------------------------------------------------------------------
# 6. trigger_resync=true
# ---------------------------------------------------------------------------


def test_reset_with_trigger_resync_calls_sync_runner(
    client: TestClient,
    fake_runner: _FakeSyncRunner,
) -> None:
    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "yes", "trigger_resync": "true"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resync_job_id"] == "resync-job-1"
    assert len(fake_runner.calls) == 1
    call = fake_runner.calls[0]
    assert call["channel_id"] == _CHANNEL_ID
    assert call["sync_type"] == "full"


def test_reset_trigger_resync_failure_is_not_fatal(
    client: TestClient,
    monkeypatch,
) -> None:
    """A failing ``start_sync`` records an error but the request stays 200
    — the deletions are already committed."""

    class _BrokenRunner:
        async def start_sync(self, *args, **kwargs):  # noqa: ARG002, ANN002, ANN003
            raise RuntimeError("runner unavailable")

    import beever_atlas.api.sync as sync_mod

    monkeypatch.setattr(sync_mod, "get_sync_runner", lambda: _BrokenRunner())

    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "yes", "trigger_resync": "true"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resync_job_id"] is None
    assert any("start_sync" in e for e in body["errors"])


# ---------------------------------------------------------------------------
# 7. Idempotency
# ---------------------------------------------------------------------------


def test_reset_is_idempotent_when_second_call_returns_zeros(
    client: TestClient,
    fake_stores,
) -> None:
    """The first call returns the seeded non-zero counts. The second
    call returns 200 with every counter at zero — exactly the shape an
    operator gets when re-running a reset on a freshly-wiped channel.
    """
    # First call uses the default non-zero seeds.
    r1 = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "yes"},
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["counts"]["entities_deleted"] == 7

    # Re-bind the fake stores so the second call sees zeros across the
    # board (the production stores would, by virtue of having just been
    # drained, return the same).
    fake_stores.graph.delete_channel_data = AsyncMock(
        return_value={"events_deleted": 0, "media_deleted": 0, "entities_deleted": 0}
    )
    fake_stores.weaviate.delete_by_channel = AsyncMock(return_value=0)

    r2 = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "yes"},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["counts"]["events_deleted"] == 0
    assert body2["counts"]["media_deleted"] == 0
    assert body2["counts"]["entities_deleted"] == 0
    assert body2["counts"]["weaviate_deleted"] == 0
    assert body2["errors"] == []


# ---------------------------------------------------------------------------
# 8. Zero-count happy path
# ---------------------------------------------------------------------------


def test_reset_returns_200_with_all_zero_counts(
    client: TestClient,
    fake_stores,
) -> None:
    fake_stores.graph.delete_channel_data = AsyncMock(
        return_value={"events_deleted": 0, "media_deleted": 0, "entities_deleted": 0}
    )
    fake_stores.weaviate.delete_by_channel = AsyncMock(return_value=0)

    r = client.post(
        f"/api/admin/channels/{_CHANNEL_ID}/reset",
        headers=_admin_headers(),
        params={"i_understand_data_loss": "yes"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset_complete"
    assert body["errors"] == []
    assert body["counts"]["entities_deleted"] == 0
    assert body["counts"]["weaviate_deleted"] == 0
