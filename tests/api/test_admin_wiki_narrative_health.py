"""Tests for ``GET /api/admin/wiki/narrative-health?channel_id=...``.

Spec: ``openspec/changes/wiki-narrative-articles/`` Phase 9 task 9.3 —
operator dashboard endpoint for per-channel narrative-article health
stats.

Convention: ``pyproject.toml`` sets ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api.admin import router as admin_router
from beever_atlas.infra import auth as auth_mod
from beever_atlas.stores import init_stores


_ADMIN_TOKEN = "admin-token-narrative-health"


def _patch_admin(monkeypatch, token: str = _ADMIN_TOKEN) -> None:
    fake = SimpleNamespace(admin_token=token)
    monkeypatch.setattr(auth_mod, "get_settings", lambda: fake)


def _section(*, anchor: str, coverage: float = 1.0, paragraphs: int = 2) -> dict:
    return {
        "anchor": anchor,
        "heading": anchor.title(),
        "paragraphs": [
            {
                "text": "Authlib was adopted for OIDC discovery support.",
                "citations": [f"f_{i}"],
                "is_inference": False,
            }
            for i in range(paragraphs)
        ],
        "citations": [f"f_{i}" for i in range(paragraphs)],
        "visual": None,
        "citation_coverage": coverage,
    }


def _page(
    *,
    slug: str,
    narrative_sections: list[dict] | None = None,
    modules: list[dict] | None = None,
):
    from beever_atlas.models.persistence import WikiPage, WikiPageSection

    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=f"topic:{slug}",
        title=slug.replace("-", " ").title(),
        slug=slug,
        kind="topic",
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
        modules=list(modules or []),
        narrative_sections=list(narrative_sections or []),
        version=1,
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


@pytest.fixture
def fake_stores(monkeypatch):
    """Provide a fake stores container with stub mongodb so the
    endpoint's ``WikiPageStore(db=...)`` import doesn't blow up."""
    container = SimpleNamespace(mongodb=SimpleNamespace(db=None))
    init_stores(container)  # type: ignore[arg-type]
    return container


@pytest.fixture
def app(monkeypatch, fake_stores):  # noqa: ARG001
    _patch_admin(monkeypatch)
    app = FastAPI()
    app.include_router(admin_router)
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": _ADMIN_TOKEN}


# ---------------------------------------------------------------------------
# Empty channel
# ---------------------------------------------------------------------------


def test_empty_channel_returns_zero_shape(client, monkeypatch) -> None:
    """No pages → documented zeroed shape, HTTP 200."""
    fake_store = AsyncMock()
    fake_store.list_pages = AsyncMock(return_value=[])
    with patch(
        "beever_atlas.wiki.page_store.WikiPageStore",
        return_value=fake_store,
    ):
        resp = client.get(
            "/api/admin/wiki/narrative-health?channel_id=C1",
            headers=_admin_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel_id"] == "C1"
    assert body["page_count"] == 0
    assert body["narrative_page_count"] == 0
    assert body["narrative_pct"] == 0.0
    assert body["fallback_rate"] == 0.0


# ---------------------------------------------------------------------------
# All pages have narrative
# ---------------------------------------------------------------------------


def test_all_pages_with_narrative_returns_full_pct(client, monkeypatch) -> None:
    """Every page has narrative_sections → narrative_pct == 1.0."""
    pages = [
        _page(
            slug=f"topic-{i}",
            narrative_sections=[
                _section(anchor="context", coverage=1.0),
                _section(anchor="implications", coverage=1.0),
            ],
        )
        for i in range(3)
    ]
    fake_store = AsyncMock()
    fake_store.list_pages = AsyncMock(return_value=pages)
    with patch(
        "beever_atlas.wiki.page_store.WikiPageStore",
        return_value=fake_store,
    ):
        resp = client.get(
            "/api/admin/wiki/narrative-health?channel_id=C1",
            headers=_admin_headers(),
        )
    body = resp.json()
    assert body["page_count"] == 3
    assert body["narrative_page_count"] == 3
    assert body["narrative_pct"] == 1.0
    assert body["fallback_rate"] == 0.0
    assert body["median_citation_coverage"] == 1.0
    assert body["median_word_count"] > 0


# ---------------------------------------------------------------------------
# Partial narrative coverage
# ---------------------------------------------------------------------------


def test_partial_narrative_returns_correct_pct(client) -> None:
    """Half the pages have narrative → narrative_pct == 0.5."""
    pages = [
        _page(slug="topic-a", narrative_sections=[_section(anchor="context")]),
        _page(slug="topic-b", narrative_sections=[]),
        _page(slug="topic-c", narrative_sections=[_section(anchor="context")]),
        _page(slug="topic-d", narrative_sections=[]),
    ]
    fake_store = AsyncMock()
    fake_store.list_pages = AsyncMock(return_value=pages)
    with patch(
        "beever_atlas.wiki.page_store.WikiPageStore",
        return_value=fake_store,
    ):
        resp = client.get(
            "/api/admin/wiki/narrative-health?channel_id=C1",
            headers=_admin_headers(),
        )
    body = resp.json()
    assert body["page_count"] == 4
    assert body["narrative_page_count"] == 2
    assert body["narrative_pct"] == 0.5
    assert body["fallback_rate"] == 0.5


# ---------------------------------------------------------------------------
# Missing channel_id
# ---------------------------------------------------------------------------


def test_missing_channel_id_returns_400(client) -> None:
    resp = client.get(
        "/api/admin/wiki/narrative-health",
        headers=_admin_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_non_admin_rejected(client) -> None:
    resp = client.get("/api/admin/wiki/narrative-health?channel_id=C1")
    assert resp.status_code == 401
    resp = client.get(
        "/api/admin/wiki/narrative-health?channel_id=C1",
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status_code == 401
