"""Tests for wiki versioning: WikiVersionStore, archive-on-save, and API endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.wiki.version_store import WikiVersionStore


@pytest.fixture(autouse=True)
def _install_mock_stores(mock_stores):
    """RES-177: wiki endpoints now call `assert_channel_access`, which
    needs a usable `get_stores()`. Wire the conftest-provided mock
    stores uniformly across this file."""
    yield mock_stores


# ── Helpers ──────────────────────────────────────────────────────────────


class AsyncCursorMock:
    """Mock for Motor async cursors that supports async iteration and chaining."""

    def __init__(self, items: list):
        self._items = items

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


def _make_wiki_doc(channel_id: str = "C001", channel_name: str = "general") -> dict:
    """Create a minimal wiki document for testing."""
    return {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "platform": "slack",
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "is_stale": False,
        "structure": {"channel_id": channel_id, "pages": []},
        "overview": {"id": "overview", "slug": "overview", "title": "Overview", "content": "Hello"},
        "pages": {
            "overview": {
                "id": "overview",
                "slug": "overview",
                "title": "Overview",
                "content": "Hello",
            },
            "people": {"id": "people", "slug": "people", "title": "People", "content": "Team"},
        },
        "metadata": {"page_count": 2, "model": "gemini-2.5-flash"},
    }


# ── WikiVersionStore unit tests ──────────────────────────────────────────


class TestWikiVersionStoreArchive:
    """Tests for WikiVersionStore.archive()."""

    @pytest.fixture
    def store(self):
        with patch("beever_atlas.wiki.version_store.AsyncIOMotorClient") as mock_client:
            mock_db = MagicMock()
            mock_collection = AsyncMock()
            mock_db.__getitem__ = MagicMock(return_value=mock_collection)
            mock_client.return_value.__getitem__ = MagicMock(return_value=mock_db)
            s = WikiVersionStore("mongodb://localhost:27017/test")
            s._collection = mock_collection
            yield s, mock_collection

    @pytest.mark.anyio
    async def test_first_version_gets_number_1(self, store):
        s, col = store
        col.find_one = AsyncMock(return_value=None)
        col.find_one_and_update = AsyncMock(return_value={"_id": "C001", "seq": 1})
        col.insert_one = AsyncMock()

        wiki = _make_wiki_doc()
        version_num = await s.archive("C001", wiki)

        assert version_num == 1
        col.insert_one.assert_called_once()
        inserted = col.insert_one.call_args[0][0]
        assert inserted["version_number"] == 1
        assert inserted["channel_id"] == "C001"
        assert "archived_at" in inserted

    @pytest.mark.anyio
    async def test_subsequent_version_increments(self, store):
        s, col = store
        col.find_one = AsyncMock(return_value={"_id": "C001", "seq": 3})
        col.find_one_and_update = AsyncMock(return_value={"_id": "C001", "seq": 4})
        col.insert_one = AsyncMock()

        wiki = _make_wiki_doc()
        version_num = await s.archive("C001", wiki)

        assert version_num == 4

    @pytest.mark.anyio
    async def test_archive_preserves_page_count(self, store):
        s, col = store
        col.find_one = AsyncMock(return_value=None)
        col.find_one_and_update = AsyncMock(return_value={"_id": "C001", "seq": 1})
        col.insert_one = AsyncMock()

        wiki = _make_wiki_doc()
        await s.archive("C001", wiki)

        inserted = col.insert_one.call_args[0][0]
        assert inserted["page_count"] == 2


class TestWikiVersionStoreCleanup:
    """Tests for WikiVersionStore.cleanup()."""

    @pytest.fixture
    def store(self):
        with patch("beever_atlas.wiki.version_store.AsyncIOMotorClient") as mock_client:
            mock_db = MagicMock()
            mock_collection = AsyncMock()
            mock_db.__getitem__ = MagicMock(return_value=mock_collection)
            mock_client.return_value.__getitem__ = MagicMock(return_value=mock_db)
            s = WikiVersionStore("mongodb://localhost:27017/test")
            s._collection = mock_collection
            yield s, mock_collection

    @pytest.mark.anyio
    async def test_no_cleanup_under_limit(self, store):
        s, col = store
        col.count_documents = AsyncMock(return_value=5)

        deleted = await s.cleanup("C001", max_versions=10)
        assert deleted == 0

    @pytest.mark.anyio
    async def test_cleanup_deletes_oldest(self, store):
        s, col = store
        col.count_documents = AsyncMock(return_value=12)

        col.find = MagicMock(
            return_value=AsyncCursorMock([{"version_number": 1}, {"version_number": 2}])
        )

        mock_delete_result = MagicMock()
        mock_delete_result.deleted_count = 2
        col.delete_many = AsyncMock(return_value=mock_delete_result)

        deleted = await s.cleanup("C001", max_versions=10)
        assert deleted == 2
        col.delete_many.assert_called_once()


class TestWikiVersionStoreQueries:
    """Tests for list_versions, get_version, get_version_page, count_versions."""

    @pytest.fixture
    def store(self):
        with patch("beever_atlas.wiki.version_store.AsyncIOMotorClient") as mock_client:
            mock_db = MagicMock()
            mock_collection = AsyncMock()
            mock_db.__getitem__ = MagicMock(return_value=mock_collection)
            mock_client.return_value.__getitem__ = MagicMock(return_value=mock_db)
            s = WikiVersionStore("mongodb://localhost:27017/test")
            s._collection = mock_collection
            yield s, mock_collection

    @pytest.mark.anyio
    async def test_list_versions(self, store):
        s, col = store
        versions = [
            {
                "version_number": 2,
                "channel_id": "C001",
                "generated_at": "2026-01-02",
                "archived_at": "2026-01-03",
                "page_count": 5,
                "model": "flash",
            },
            {
                "version_number": 1,
                "channel_id": "C001",
                "generated_at": "2026-01-01",
                "archived_at": "2026-01-02",
                "page_count": 3,
                "model": "flash",
            },
        ]
        col.find = MagicMock(return_value=AsyncCursorMock(list(versions)))

        result = await s.list_versions("C001")
        assert len(result) == 2
        assert result[0]["version_number"] == 2

    @pytest.mark.anyio
    async def test_get_version_found(self, store):
        s, col = store
        col.find_one = AsyncMock(return_value={"version_number": 1, "channel_id": "C001"})

        result = await s.get_version("C001", 1)
        assert result is not None
        assert result["version_number"] == 1

    @pytest.mark.anyio
    async def test_get_version_not_found(self, store):
        s, col = store
        col.find_one = AsyncMock(return_value=None)

        result = await s.get_version("C001", 99)
        assert result is None

    @pytest.mark.anyio
    async def test_get_version_page_found(self, store):
        s, col = store
        col.find_one = AsyncMock(
            return_value={"pages": {"overview": {"id": "overview", "title": "Overview"}}}
        )

        result = await s.get_version_page("C001", 1, "overview")
        assert result is not None
        assert result["id"] == "overview"

    @pytest.mark.anyio
    async def test_get_version_page_not_found(self, store):
        s, col = store
        col.find_one = AsyncMock(return_value={"pages": {}})

        result = await s.get_version_page("C001", 1, "nonexistent")
        assert result is None

    @pytest.mark.anyio
    async def test_count_versions(self, store):
        s, col = store
        col.count_documents = AsyncMock(return_value=7)

        result = await s.count_versions("C001")
        assert result == 7


# ── WikiCache archive-on-save integration ────────────────────────────────


class TestWikiCacheArchiveOnSave:
    """Tests for the archive step in WikiCache.save_wiki()."""

    @pytest.mark.anyio
    async def test_save_archives_existing_wiki(self):
        """When an existing wiki is present, save_wiki should archive it first."""
        with (
            patch("beever_atlas.wiki.cache.AsyncIOMotorClient") as mock_client,
            patch("beever_atlas.wiki.cache.WikiVersionStore") as mock_vs_cls,
        ):
            mock_db = MagicMock()
            mock_collection = AsyncMock()
            mock_status_collection = AsyncMock()

            def get_collection(name):
                if name == "wiki_cache":
                    return mock_collection
                return mock_status_collection

            mock_db.__getitem__ = MagicMock(side_effect=get_collection)
            mock_client.return_value.__getitem__ = MagicMock(return_value=mock_db)

            mock_vs = AsyncMock()
            mock_vs.archive = AsyncMock(return_value=1)
            mock_vs.cleanup = AsyncMock()
            mock_vs_cls.return_value = mock_vs

            from beever_atlas.wiki.cache import WikiCache

            cache = WikiCache("mongodb://localhost:27017/test")

            existing_wiki = _make_wiki_doc()
            mock_collection.find_one = AsyncMock(return_value=existing_wiki)
            mock_collection.update_one = AsyncMock()

            new_wiki = _make_wiki_doc()
            await cache.save_wiki("C001", new_wiki)

            mock_vs.archive.assert_called_once_with(
                "C001",
                existing_wiki,
                target_lang="en",
            )
            mock_vs.cleanup.assert_called_once_with("C001")
            mock_collection.update_one.assert_called_once()

    @pytest.mark.anyio
    async def test_save_skips_archive_when_no_existing(self):
        """When no existing wiki, save_wiki should not call archive."""
        with (
            patch("beever_atlas.wiki.cache.AsyncIOMotorClient") as mock_client,
            patch("beever_atlas.wiki.cache.WikiVersionStore") as mock_vs_cls,
        ):
            mock_db = MagicMock()
            mock_collection = AsyncMock()
            mock_status_collection = AsyncMock()

            def get_collection(name):
                if name == "wiki_cache":
                    return mock_collection
                return mock_status_collection

            mock_db.__getitem__ = MagicMock(side_effect=get_collection)
            mock_client.return_value.__getitem__ = MagicMock(return_value=mock_db)

            mock_vs = AsyncMock()
            mock_vs_cls.return_value = mock_vs

            from beever_atlas.wiki.cache import WikiCache

            cache = WikiCache("mongodb://localhost:27017/test")

            mock_collection.find_one = AsyncMock(return_value=None)
            mock_collection.update_one = AsyncMock()

            new_wiki = _make_wiki_doc()
            await cache.save_wiki("C001", new_wiki)

            mock_vs.archive.assert_not_called()
            mock_collection.update_one.assert_called_once()

    @pytest.mark.anyio
    async def test_save_continues_on_archive_failure(self):
        """If archive throws, save_wiki should still save the new wiki."""
        with (
            patch("beever_atlas.wiki.cache.AsyncIOMotorClient") as mock_client,
            patch("beever_atlas.wiki.cache.WikiVersionStore") as mock_vs_cls,
        ):
            mock_db = MagicMock()
            mock_collection = AsyncMock()
            mock_status_collection = AsyncMock()

            def get_collection(name):
                if name == "wiki_cache":
                    return mock_collection
                return mock_status_collection

            mock_db.__getitem__ = MagicMock(side_effect=get_collection)
            mock_client.return_value.__getitem__ = MagicMock(return_value=mock_db)

            mock_vs = AsyncMock()
            mock_vs.archive = AsyncMock(side_effect=Exception("DB write error"))
            mock_vs_cls.return_value = mock_vs

            from beever_atlas.wiki.cache import WikiCache

            cache = WikiCache("mongodb://localhost:27017/test")

            existing_wiki = _make_wiki_doc()
            mock_collection.find_one = AsyncMock(return_value=existing_wiki)
            mock_collection.update_one = AsyncMock()

            new_wiki = _make_wiki_doc()
            await cache.save_wiki("C001", new_wiki)

            # Save should still proceed
            mock_collection.update_one.assert_called_once()


# ── API endpoint tests ───────────────────────────────────────────────────


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    from beever_atlas.server.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestWikiVersionEndpoints:
    """Tests for the wiki version API endpoints."""

    @pytest.mark.anyio
    async def test_list_versions_empty(self, client):
        mock_cache = AsyncMock()
        mock_vs = AsyncMock()
        mock_vs.list_versions = AsyncMock(return_value=[])
        mock_cache.version_store = mock_vs

        with patch("beever_atlas.api.wiki._get_cache", return_value=mock_cache):
            resp = await client.get("/api/channels/C001/wiki/versions")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.anyio
    async def test_list_versions_with_data(self, client):
        versions = [
            {
                "version_number": 2,
                "channel_id": "C001",
                "generated_at": "2026-01-02",
                "archived_at": "2026-01-03",
                "page_count": 5,
                "model": "flash",
            },
            {
                "version_number": 1,
                "channel_id": "C001",
                "generated_at": "2026-01-01",
                "archived_at": "2026-01-02",
                "page_count": 3,
                "model": "flash",
            },
        ]
        mock_cache = AsyncMock()
        mock_vs = AsyncMock()
        mock_vs.list_versions = AsyncMock(return_value=versions)
        mock_cache.version_store = mock_vs

        with patch("beever_atlas.api.wiki._get_cache", return_value=mock_cache):
            resp = await client.get("/api/channels/C001/wiki/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["version_number"] == 2

    @pytest.mark.anyio
    async def test_get_version_found(self, client):
        version = {"version_number": 1, "channel_id": "C001", "structure": {}, "pages": {}}
        mock_cache = AsyncMock()
        mock_vs = AsyncMock()
        mock_vs.get_version = AsyncMock(return_value=version)
        mock_cache.version_store = mock_vs

        with patch("beever_atlas.api.wiki._get_cache", return_value=mock_cache):
            resp = await client.get("/api/channels/C001/wiki/versions/1")
        assert resp.status_code == 200
        assert resp.json()["version_number"] == 1

    @pytest.mark.anyio
    async def test_get_version_not_found(self, client):
        mock_cache = AsyncMock()
        mock_vs = AsyncMock()
        mock_vs.get_version = AsyncMock(return_value=None)
        mock_cache.version_store = mock_vs

        with patch("beever_atlas.api.wiki._get_cache", return_value=mock_cache):
            resp = await client.get("/api/channels/C001/wiki/versions/99")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_get_version_page_found(self, client):
        page = {"id": "overview", "title": "Overview", "content": "Hello"}
        mock_cache = AsyncMock()
        mock_vs = AsyncMock()
        mock_vs.get_version_page = AsyncMock(return_value=page)
        mock_cache.version_store = mock_vs

        with patch("beever_atlas.api.wiki._get_cache", return_value=mock_cache):
            resp = await client.get("/api/channels/C001/wiki/versions/1/pages/overview")
        assert resp.status_code == 200
        assert resp.json()["id"] == "overview"

    @pytest.mark.anyio
    async def test_get_version_page_not_found(self, client):
        mock_cache = AsyncMock()
        mock_vs = AsyncMock()
        mock_vs.get_version_page = AsyncMock(return_value=None)
        mock_cache.version_store = mock_vs

        with patch("beever_atlas.api.wiki._get_cache", return_value=mock_cache):
            resp = await client.get("/api/channels/C001/wiki/versions/1/pages/nope")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_wiki_response_includes_version_count(self, client):
        wiki_doc = _make_wiki_doc()
        wiki_doc["version_count"] = 3
        mock_cache = AsyncMock()
        mock_cache.get_wiki = AsyncMock(return_value=wiki_doc)

        with patch("beever_atlas.api.wiki._get_cache", return_value=mock_cache):
            resp = await client.get("/api/channels/C001/wiki")
        assert resp.status_code == 200
        assert resp.json()["version_count"] == 3
