"""Tests for the wiki-llm-native-redesign MCP read tools (§7.7).

Covers ``read_wiki_page``, ``list_wiki_pages``, and ``get_wiki_graph``
(authorized vs unauthorized channel; default vs ``scope=all``;
hidden-page filtering). The fastmcp wiring is exercised via the
underlying tool-implementation imports rather than a real MCP roundtrip
— that is the integration test in ``test_mcp_e2e_handshake.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from beever_atlas.api.mcp_server import _tools_retrieval as wiki_mcp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMCP:
    """Captures `@mcp.tool(...)` registrations so tests can call the
    wrapped functions directly."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *_, **kwargs):
        name = kwargs.get("name", "")

        def _decorator(fn):
            self.tools[name] = fn
            return fn

        return _decorator


def _make_ctx(*, principal_id: str = "mcp:agent-1", scopes: set[str] | None = None):
    return SimpleNamespace(
        principal_id=principal_id,
        principal_scopes=set(scopes or set()),
        request_context=SimpleNamespace(principal_id=principal_id),
    )


def _patch_principal(monkeypatch: pytest.MonkeyPatch, principal_id: str | None) -> None:
    monkeypatch.setattr(
        "beever_atlas.api.mcp_server._tools_retrieval._get_principal_id",
        lambda ctx: principal_id,
    )


def _wiki_page_doc(
    *,
    slug: str = "topic-auth",
    title: str = "Authentication",
    kind: str = "topic",
    pinned: bool = False,
    hidden: bool = False,
    merged_into: str | None = None,
):
    from beever_atlas.models.persistence import WikiPage, WikiPageSection

    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=f"topic:{slug.split('-', 1)[1]}" if "-" in slug else slug,
        title=title,
        slug=slug,
        kind=kind,
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
        pin_state={
            "pinned": pinned,
            "hidden": hidden,
            "reason": "",
            "set_by": "",
            "set_at": None,
        },
        merged_into=merged_into,
        version=2,
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


@pytest.fixture
def registered_tools(monkeypatch):
    """Register the retrieval tools onto a fake MCP and return a {name: fn} map."""

    fake_mcp = _FakeMCP()
    wiki_mcp.register_retrieval_tools(fake_mcp)
    return fake_mcp.tools


# ---------------------------------------------------------------------------
# §7.6 — three new tools registered
# ---------------------------------------------------------------------------


def test_three_new_tools_registered(registered_tools) -> None:
    assert "read_wiki_page" in registered_tools
    assert "list_wiki_pages" in registered_tools
    assert "get_wiki_graph" in registered_tools


# ---------------------------------------------------------------------------
# §7.7 — read_wiki_page authorized vs unauthorized + hidden-page filter
# ---------------------------------------------------------------------------


async def test_read_wiki_page_returns_payload_for_authorized_channel(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_page = _wiki_page_doc()

    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=fake_page)

    fake_stores = SimpleNamespace(mongodb=SimpleNamespace(db=None))
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_page"](
            channel_id="C1", slug="topic-auth", ctx=_make_ctx(), target_lang="en"
        )
    assert result["slug"] == "topic-auth"
    assert result["title"] == "Authentication"
    assert result["kind"] == "topic"


async def test_read_wiki_page_denies_unauthorized_channel(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    with patch(
        "beever_atlas.infra.channel_access.assert_channel_access",
        new=AsyncMock(side_effect=PermissionError("no access")),
    ):
        result = await registered_tools["read_wiki_page"](
            channel_id="C1", slug="topic-auth", ctx=_make_ctx(), target_lang="en"
        )
    assert result == {"error": "channel_access_denied", "channel_id": "C1"}


async def test_read_wiki_page_filters_hidden_without_scope(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    hidden_page = _wiki_page_doc(slug="topic-old", hidden=True)
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=hidden_page)
    fake_stores = SimpleNamespace(mongodb=SimpleNamespace(db=None))
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_page"](
            channel_id="C1",
            slug="topic-old",
            ctx=_make_ctx(),  # no scopes
            target_lang="en",
        )
    assert result == {"error": "wiki_page_not_found", "slug": "topic-old"}


async def test_read_wiki_page_returns_hidden_when_caller_has_scope(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    hidden_page = _wiki_page_doc(slug="topic-old", hidden=True)
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=hidden_page)
    fake_stores = SimpleNamespace(mongodb=SimpleNamespace(db=None))
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_page"](
            channel_id="C1",
            slug="topic-old",
            ctx=_make_ctx(scopes={"read:hidden_pages"}),
            target_lang="en",
        )
    assert result["slug"] == "topic-old"


async def test_read_wiki_page_returns_not_found_for_missing_slug(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=None)
    fake_stores = SimpleNamespace(mongodb=SimpleNamespace(db=None))
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_page"](
            channel_id="C1", slug="missing", ctx=_make_ctx(), target_lang="en"
        )
    assert result == {"error": "wiki_page_not_found", "slug": "missing"}


# ---------------------------------------------------------------------------
# §7.7 — list_wiki_pages default vs scope=all
# ---------------------------------------------------------------------------


async def test_list_wiki_pages_default_scope_excludes_hidden(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_store = AsyncMock()
    captured_scopes: list[str] = []

    async def _list(channel_id, kind=None, target_lang="en", scope="human"):
        captured_scopes.append(scope)
        return [_wiki_page_doc(slug="topic-a")]

    fake_store.list_pages_by_kind = _list
    fake_stores = SimpleNamespace(mongodb=SimpleNamespace(db=None))

    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["list_wiki_pages"](
            channel_id="C1", ctx=_make_ctx(), kind=None, scope="human", target_lang="en"
        )
    assert result["scope"] == "human"
    assert captured_scopes == ["human"]
    assert len(result["pages"]) == 1
    assert result["pages"][0]["slug"] == "topic-a"


async def test_list_wiki_pages_scope_all_downgrades_without_scope_grant(
    registered_tools, monkeypatch
) -> None:
    """A caller without ``read:hidden_pages`` who passes ``scope=all`` is
    silently downgraded to ``human`` so they cannot exfiltrate hidden
    pages by guessing the scope."""
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_store = AsyncMock()
    captured_scopes: list[str] = []

    async def _list(channel_id, kind=None, target_lang="en", scope="human"):
        captured_scopes.append(scope)
        return []

    fake_store.list_pages_by_kind = _list
    fake_stores = SimpleNamespace(mongodb=SimpleNamespace(db=None))

    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["list_wiki_pages"](
            channel_id="C1",
            ctx=_make_ctx(),  # no scope
            kind=None,
            scope="all",
            target_lang="en",
        )
    assert result["scope"] == "human"
    assert captured_scopes == ["human"]


async def test_list_wiki_pages_scope_all_passes_when_caller_has_scope(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_store = AsyncMock()
    captured_scopes: list[str] = []

    async def _list(channel_id, kind=None, target_lang="en", scope="human"):
        captured_scopes.append(scope)
        return []

    fake_store.list_pages_by_kind = _list
    fake_stores = SimpleNamespace(mongodb=SimpleNamespace(db=None))

    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["list_wiki_pages"](
            channel_id="C1",
            ctx=_make_ctx(scopes={"read:hidden_pages"}),
            kind=None,
            scope="all",
            target_lang="en",
        )
    assert result["scope"] == "all"
    assert captured_scopes == ["all"]


async def test_list_wiki_pages_kind_filter_passes_through(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_store = AsyncMock()
    captured_kinds: list[str | None] = []

    async def _list(channel_id, kind=None, target_lang="en", scope="human"):
        captured_kinds.append(kind)
        return []

    fake_store.list_pages_by_kind = _list
    fake_stores = SimpleNamespace(mongodb=SimpleNamespace(db=None))

    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        await registered_tools["list_wiki_pages"](
            channel_id="C1",
            ctx=_make_ctx(),
            kind="entity",
            scope="human",
            target_lang="en",
        )
    assert captured_kinds == ["entity"]


# ---------------------------------------------------------------------------
# §7.7 — get_wiki_graph authorized vs unauthorized
# ---------------------------------------------------------------------------


async def test_get_wiki_graph_returns_payload_for_authorized_channel(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")

    class _FakeGraph:
        async def get_wiki_graph(self, channel_id):
            return {
                "channel_id": channel_id,
                "nodes": [{"data": {"id": "topic-a", "kind": "wiki"}}],
                "edges": [],
            }

    fake_stores = SimpleNamespace(graph=_FakeGraph())
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
    ):
        result = await registered_tools["get_wiki_graph"](channel_id="C1", ctx=_make_ctx())
    assert result["channel_id"] == "C1"
    assert result["nodes"][0]["data"]["id"] == "topic-a"


async def test_get_wiki_graph_denies_unauthorized_channel(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    with patch(
        "beever_atlas.infra.channel_access.assert_channel_access",
        new=AsyncMock(side_effect=PermissionError("denied")),
    ):
        result = await registered_tools["get_wiki_graph"](channel_id="C1", ctx=_make_ctx())
    assert result == {"error": "channel_access_denied", "channel_id": "C1"}


async def test_get_wiki_graph_returns_empty_when_backend_lacks_method(
    registered_tools, monkeypatch
) -> None:
    """NullGraphStore / NebulaStore (no parity yet) → empty payload."""
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_stores = SimpleNamespace(graph=SimpleNamespace())
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
    ):
        result = await registered_tools["get_wiki_graph"](channel_id="C1", ctx=_make_ctx())
    assert result == {"channel_id": "C1", "nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# Authentication / validation
# ---------------------------------------------------------------------------


async def test_all_three_tools_reject_missing_principal(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, None)
    ctx = _make_ctx(principal_id="")
    for name, args in [
        ("read_wiki_page", {"channel_id": "C1", "slug": "x"}),
        ("list_wiki_pages", {"channel_id": "C1"}),
        ("get_wiki_graph", {"channel_id": "C1"}),
    ]:
        result = await registered_tools[name](ctx=ctx, **args)
        assert result == {"error": "authentication_missing"}, f"{name} did not gate"
