"""Tests for the Round-6 LLM-agent retrieval MCP tools.

Covers ``read_wiki_module``, ``find_decisions``, ``get_tensions``,
``find_facts``, and ``read_provenance``. The fastmcp wiring is
exercised via the underlying tool-implementation imports rather than a
real MCP roundtrip — that is the integration test in
``test_mcp_e2e_handshake.py``.
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


def _wiki_page(
    *,
    slug: str = "topic-auth",
    title: str = "Authentication",
    last_facts_seen: list[str] | None = None,
    modules: list[dict[str, Any]] | None = None,
    narrative_sections: list[dict[str, Any]] | None = None,
):
    from beever_atlas.models.persistence import WikiPage, WikiPageSection

    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=f"topic:{slug}",
        title=title,
        slug=slug,
        kind="topic",
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
        last_facts_seen=list(last_facts_seen or []),
        modules=list(modules or []),
        narrative_sections=list(narrative_sections or []),
        version=2,
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


def _atomic_fact(
    *,
    fact_id: str = "f1",
    memory_text: str = "We will adopt CLA before merging external PRs.",
    fact_type: str = "decision",
    importance: str = "high",
    author_name: str = "Alice Chen",
    message_ts: str = "2026-04-15T10:00:00Z",
    rationale: str | None = "Reduces legal risk.",
    alternatives_considered: list[str] | None = None,
    channel_id: str = "C1",
    source_message_id: str = "msg-100",
):
    from beever_atlas.models.domain import AtomicFact

    return AtomicFact(
        id=fact_id,
        memory_text=memory_text,
        fact_type=fact_type,
        importance=importance,
        author_name=author_name,
        message_ts=message_ts,
        channel_id=channel_id,
        source_message_id=source_message_id,
        rationale=rationale,
        alternatives_considered=list(alternatives_considered or []),
        platform="mattermost",
        source_link_urls=["https://chat.example.com/msg/100"],
    )


@pytest.fixture
def registered_tools():
    fake_mcp = _FakeMCP()
    wiki_mcp.register_retrieval_tools(fake_mcp)
    return fake_mcp.tools


def _stores(*, weaviate=None, mongodb=None) -> SimpleNamespace:
    return SimpleNamespace(
        weaviate=weaviate or SimpleNamespace(),
        mongodb=mongodb or SimpleNamespace(db=None),
    )


# ---------------------------------------------------------------------------
# Five new tools must be registered alongside the existing wiki retrieval set
# ---------------------------------------------------------------------------


def test_round6_tools_registered(registered_tools) -> None:
    for name in (
        "read_wiki_module",
        "find_decisions",
        "get_tensions",
        "find_facts",
        "read_provenance",
    ):
        assert name in registered_tools, f"{name} missing from MCP tool registry"


# ---------------------------------------------------------------------------
# read_wiki_module
# ---------------------------------------------------------------------------


async def test_read_wiki_module_returns_data_for_known_anchor(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    page = _wiki_page(
        modules=[{"id": "key_facts", "anchor": "key-facts", "data": {"items": [1, 2, 3]}}]
    )
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=page)
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_module"](
            channel_id="C1",
            page_slug="topic-auth",
            anchor="key-facts",
            ctx=_make_ctx(),
        )
    assert result["module_id"] == "key_facts"
    assert result["data"] == {"items": [1, 2, 3]}
    assert result["page_slug"] == "topic-auth"


async def test_read_wiki_module_returns_module_not_found_for_unknown_anchor(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    page = _wiki_page(modules=[{"id": "key_facts", "anchor": "key-facts", "data": {}}])
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=page)
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_module"](
            channel_id="C1",
            page_slug="topic-auth",
            anchor="ghost",
            ctx=_make_ctx(),
        )
    assert result["error"] == "module_not_found"


async def test_read_wiki_module_returns_page_not_found(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=None)
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_module"](
            channel_id="C1",
            page_slug="missing",
            anchor="key-facts",
            ctx=_make_ctx(),
        )
    assert result == {"error": "wiki_page_not_found", "slug": "missing"}


async def test_read_wiki_module_denies_unauthorized_channel(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    with patch(
        "beever_atlas.infra.channel_access.assert_channel_access",
        new=AsyncMock(side_effect=PermissionError("denied")),
    ):
        result = await registered_tools["read_wiki_module"](
            channel_id="C1",
            page_slug="topic-auth",
            anchor="key-facts",
            ctx=_make_ctx(),
        )
    assert result == {"error": "channel_access_denied", "channel_id": "C1"}


# ---------------------------------------------------------------------------
# find_decisions
# ---------------------------------------------------------------------------


async def test_find_decisions_filters_by_since_and_author_and_sorts_desc(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    decisions = [
        _atomic_fact(
            fact_id="f-old",
            message_ts="2026-03-01T08:00:00Z",
            author_name="Alice Chen",
            memory_text="Old decision A.",
        ),
        _atomic_fact(
            fact_id="f-mid",
            message_ts="2026-04-15T10:00:00Z",
            author_name="Alice Chen",
            memory_text="Mid decision A.",
        ),
        _atomic_fact(
            fact_id="f-new",
            message_ts="2026-05-01T12:00:00Z",
            author_name="Bob Tan",
            memory_text="New decision B.",
        ),
        _atomic_fact(
            fact_id="f-not-decision",
            fact_type="opinion",
            message_ts="2026-04-20T12:00:00Z",
            author_name="Alice Chen",
            memory_text="Just an opinion.",
        ),
    ]

    from beever_atlas.models.api import PaginatedFacts

    fake_weaviate = SimpleNamespace(
        list_facts=AsyncMock(
            return_value=PaginatedFacts(memories=decisions, total=len(decisions), page=1, pages=1)
        )
    )

    fake_page = _wiki_page(slug="topic-cla", last_facts_seen=["f-mid"])
    fake_store = AsyncMock()
    fake_store.list_pages = AsyncMock(return_value=[fake_page])

    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch(
            "beever_atlas.stores.get_stores",
            return_value=_stores(weaviate=fake_weaviate),
        ),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["find_decisions"](
            channel_id="C1",
            ctx=_make_ctx(),
            since="2026-04-01",
            author="Alice Chen",
        )

    assert isinstance(result, list)
    assert [r["fact_id"] for r in result] == ["f-mid"]
    assert result[0]["decided_by"] == "Alice Chen"
    assert result[0]["decided_at"] == "2026-04-15"
    assert result[0]["page_slug"] == "topic-cla"
    assert result[0]["rationale"] == "Reduces legal risk."


async def test_find_decisions_sorts_by_date_desc_no_filters(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    decisions = [
        _atomic_fact(fact_id="f-a", message_ts="2026-01-01T00:00:00Z"),
        _atomic_fact(fact_id="f-b", message_ts="2026-05-01T00:00:00Z"),
        _atomic_fact(fact_id="f-c", message_ts="2026-03-01T00:00:00Z"),
    ]
    from beever_atlas.models.api import PaginatedFacts

    fake_weaviate = SimpleNamespace(
        list_facts=AsyncMock(
            return_value=PaginatedFacts(memories=decisions, total=3, page=1, pages=1)
        )
    )
    fake_store = AsyncMock()
    fake_store.list_pages = AsyncMock(return_value=[])
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch(
            "beever_atlas.stores.get_stores",
            return_value=_stores(weaviate=fake_weaviate),
        ),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["find_decisions"](channel_id="C1", ctx=_make_ctx())
    assert [r["fact_id"] for r in result] == ["f-b", "f-c", "f-a"]


async def test_find_decisions_returns_empty_when_unauthorized(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    with patch(
        "beever_atlas.infra.channel_access.assert_channel_access",
        new=AsyncMock(side_effect=PermissionError("denied")),
    ):
        result = await registered_tools["find_decisions"](channel_id="C1", ctx=_make_ctx())
    assert result == []


# ---------------------------------------------------------------------------
# get_tensions
# ---------------------------------------------------------------------------


async def test_get_tensions_returns_empty_when_no_tensions_present(
    registered_tools, monkeypatch
) -> None:
    """Default case for pre-tension-detection deployments."""
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_store = AsyncMock()
    fake_store.list_pages = AsyncMock(return_value=[_wiki_page(modules=[])])
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["get_tensions"](channel_id="C1", ctx=_make_ctx())
    assert result == []


async def test_get_tensions_surfaces_tension_callout_modules(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    page_a = _wiki_page(
        slug="topic-auth",
        modules=[
            {
                "id": "tension_callout",
                "anchor": "tension-1",
                "data": {
                    "tension_id": "T-1",
                    "title": "OAuth vs. session cookies",
                    "status": "open",
                    "since": "2026-04-10T00:00:00Z",
                    "positions": [
                        {"author": "Alice", "stance": "OAuth", "fact_id": "f1"},
                        {"author": "Bob", "stance": "Cookies", "fact_id": "f2"},
                    ],
                },
            }
        ],
    )
    page_b = _wiki_page(
        slug="topic-deferred",
        modules=[
            {
                "id": "tension_callout",
                "anchor": "tension-2",
                "data": {
                    "tension_id": "T-2",
                    "title": "Deferred",
                    "status": "deferred",
                },
            }
        ],
    )
    fake_store = AsyncMock()
    fake_store.list_pages = AsyncMock(return_value=[page_a, page_b])
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        all_tensions = await registered_tools["get_tensions"](channel_id="C1", ctx=_make_ctx())
        only_open = await registered_tools["get_tensions"](
            channel_id="C1", ctx=_make_ctx(), status="open"
        )

    assert len(all_tensions) == 2
    by_id = {t["tension_id"]: t for t in all_tensions}
    assert by_id["T-1"]["page_slug"] == "topic-auth"
    assert by_id["T-1"]["since"] == "2026-04-10"
    assert len(by_id["T-1"]["positions"]) == 2

    assert [t["tension_id"] for t in only_open] == ["T-1"]


async def test_get_tensions_returns_empty_when_unauthorized(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    with patch(
        "beever_atlas.infra.channel_access.assert_channel_access",
        new=AsyncMock(side_effect=PermissionError("denied")),
    ):
        result = await registered_tools["get_tensions"](channel_id="C1", ctx=_make_ctx())
    assert result == []


# ---------------------------------------------------------------------------
# find_facts
# ---------------------------------------------------------------------------


async def test_find_facts_case_insensitive_substring_with_type_filter(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    facts = [
        _atomic_fact(
            fact_id="f1",
            memory_text="We will adopt CLA before merging.",
            fact_type="decision",
            importance="critical",
            message_ts="2026-04-01T00:00:00Z",
        ),
        _atomic_fact(
            fact_id="f2",
            memory_text="The cla document is hosted in S3.",
            fact_type="observation",
            importance="medium",
            message_ts="2026-04-05T00:00:00Z",
        ),
        _atomic_fact(
            fact_id="f3",
            memory_text="Let's discuss tomorrow.",
            fact_type="decision",
            importance="low",
            message_ts="2026-04-10T00:00:00Z",
        ),
    ]
    from beever_atlas.models.api import PaginatedFacts

    fake_weaviate = SimpleNamespace(
        list_facts=AsyncMock(return_value=PaginatedFacts(memories=facts, total=3, page=1, pages=1))
    )
    fake_store = AsyncMock()
    fake_store.list_pages = AsyncMock(return_value=[])
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch(
            "beever_atlas.stores.get_stores",
            return_value=_stores(weaviate=fake_weaviate),
        ),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        # case-insensitive — 'cla' lowercase still matches 'CLA' uppercase.
        all_cla = await registered_tools["find_facts"](
            channel_id="C1", query="cla", ctx=_make_ctx()
        )
        only_decisions = await registered_tools["find_facts"](
            channel_id="C1",
            query="cla",
            ctx=_make_ctx(),
            fact_type="decision",
        )

    assert {f["fact_id"] for f in all_cla} == {"f1", "f2"}
    # importance DESC: critical (f1) before medium (f2)
    assert all_cla[0]["fact_id"] == "f1"
    assert [f["fact_id"] for f in only_decisions] == ["f1"]


async def test_find_facts_clamps_limit_to_max(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    facts = [
        _atomic_fact(fact_id=f"f{i}", memory_text="match", message_ts=f"2026-04-{i:02d}T00:00:00Z")
        for i in range(1, 30)
    ]
    from beever_atlas.models.api import PaginatedFacts

    fake_weaviate = SimpleNamespace(
        list_facts=AsyncMock(return_value=PaginatedFacts(memories=facts, total=29, page=1, pages=1))
    )
    fake_store = AsyncMock()
    fake_store.list_pages = AsyncMock(return_value=[])
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch(
            "beever_atlas.stores.get_stores",
            return_value=_stores(weaviate=fake_weaviate),
        ),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        # limit=99999 clamps to 100; we only have 29 facts → returns 29.
        bumped = await registered_tools["find_facts"](
            channel_id="C1",
            query="match",
            ctx=_make_ctx(),
            limit=99999,
        )
        # limit=0 clamps to 1.
        floored = await registered_tools["find_facts"](
            channel_id="C1",
            query="match",
            ctx=_make_ctx(),
            limit=0,
        )

    assert len(bumped) == 29
    assert len(floored) == 1


async def test_find_facts_empty_query_returns_empty(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    with patch(
        "beever_atlas.infra.channel_access.assert_channel_access",
        new=AsyncMock(),
    ):
        result = await registered_tools["find_facts"](channel_id="C1", query="", ctx=_make_ctx())
    assert result == []


# ---------------------------------------------------------------------------
# read_provenance
# ---------------------------------------------------------------------------


async def test_read_provenance_returns_source_block_for_known_fact(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    fact = _atomic_fact(
        fact_id="11111111-1111-1111-1111-111111111111",
        source_message_id="msg-100",
    )
    fake_weaviate = SimpleNamespace(get_fact=AsyncMock(return_value=fact))
    fake_mongodb = SimpleNamespace(
        find_channel_message_by_message_id=AsyncMock(
            return_value={"content": "Hi team — going with CLA."}
        ),
        db=None,
    )
    with (
        patch(
            "beever_atlas.stores.get_stores",
            return_value=_stores(weaviate=fake_weaviate, mongodb=fake_mongodb),
        ),
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
    ):
        result = await registered_tools["read_provenance"](
            fact_id="11111111-1111-1111-1111-111111111111",
            ctx=_make_ctx(),
        )

    assert result["fact_id"] == "11111111-1111-1111-1111-111111111111"
    assert result["source"]["platform"] == "mattermost"
    assert result["source"]["author"] == "Alice Chen"
    assert result["source"]["message_id"] == "msg-100"
    assert result["raw_message"] == "Hi team — going with CLA."


async def test_read_provenance_returns_error_for_unknown_fact(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_weaviate = SimpleNamespace(get_fact=AsyncMock(return_value=None))
    with patch(
        "beever_atlas.stores.get_stores",
        return_value=_stores(weaviate=fake_weaviate),
    ):
        result = await registered_tools["read_provenance"](
            fact_id="00000000-0000-0000-0000-000000000000",
            ctx=_make_ctx(),
        )
    assert result == {
        "error": "fact_not_found",
        "fact_id": "00000000-0000-0000-0000-000000000000",
    }


async def test_read_provenance_tolerates_missing_raw_message(registered_tools, monkeypatch) -> None:
    """When the chat store can't resolve the source message, we still
    return a structured citation block — only ``raw_message`` is empty."""
    _patch_principal(monkeypatch, "mcp:agent-1")
    fact = _atomic_fact(
        fact_id="22222222-2222-2222-2222-222222222222",
        source_message_id="missing-msg",
    )
    fake_weaviate = SimpleNamespace(get_fact=AsyncMock(return_value=fact))
    fake_mongodb = SimpleNamespace(
        find_channel_message_by_message_id=AsyncMock(return_value=None),
        db=None,
    )
    with (
        patch(
            "beever_atlas.stores.get_stores",
            return_value=_stores(weaviate=fake_weaviate, mongodb=fake_mongodb),
        ),
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
    ):
        result = await registered_tools["read_provenance"](
            fact_id="22222222-2222-2222-2222-222222222222",
            ctx=_make_ctx(),
        )
    assert result["raw_message"] == ""
    assert result["source"]["author"] == "Alice Chen"


# ---------------------------------------------------------------------------
# Auth gating across all five tools
# ---------------------------------------------------------------------------


async def test_round6_tools_reject_missing_principal(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, None)
    ctx = _make_ctx(principal_id="")

    # read_wiki_module returns a structured error.
    result = await registered_tools["read_wiki_module"](
        channel_id="C1",
        page_slug="x",
        anchor="y",
        ctx=ctx,
    )
    assert result == {"error": "authentication_missing"}

    # read_provenance returns a structured error.
    result = await registered_tools["read_provenance"](fact_id="f1", ctx=ctx)
    assert result == {"error": "authentication_missing"}

    # The list-returning tools degrade to [] without leaking existence.
    for name, args in [
        ("find_decisions", {"channel_id": "C1"}),
        ("get_tensions", {"channel_id": "C1"}),
        ("find_facts", {"channel_id": "C1", "query": "x"}),
    ]:
        out = await registered_tools[name](ctx=ctx, **args)
        assert out == [], f"{name} did not gate on missing principal"


# ---------------------------------------------------------------------------
# read_wiki_section (wiki-narrative-articles)
# ---------------------------------------------------------------------------


def _section(
    *,
    anchor: str = "context",
    heading: str = "Context",
    paragraphs: list[dict] | None = None,
    citations: list[str] | None = None,
    visual: dict | None = None,
) -> dict:
    return {
        "anchor": anchor,
        "heading": heading,
        "paragraphs": list(
            paragraphs
            or [
                {"text": "The team adopted Authlib.", "citations": ["f_1"], "is_inference": False},
            ]
        ),
        "citations": list(citations or ["f_1"]),
        "visual": visual,
        "citation_coverage": 1.0,
    }


async def test_read_wiki_section_returns_section_payload(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    page = _wiki_page(
        narrative_sections=[
            _section(anchor="context", heading="Context"),
            _section(
                anchor="alternatives",
                heading="Alternatives rejected",
                paragraphs=[
                    {
                        "text": "FastAPI was considered.",
                        "citations": ["f_2"],
                        "is_inference": False,
                    },
                ],
                citations=["f_2"],
            ),
        ],
    )
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=page)
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_section"](
            channel_id="C1",
            page_slug="topic-auth",
            anchor="alternatives",
            ctx=_make_ctx(),
        )
    assert result["anchor"] == "alternatives"
    assert result["heading"] == "Alternatives rejected"
    assert result["page_slug"] == "topic-auth"
    assert result["citations"] == ["f_2"]
    # M-5 schema parity — agents can attribute the section without a
    # separate read_wiki_page call.
    assert result["channel_id"] == "C1"
    assert "page_title" in result  # populated from the page's title field


async def test_read_wiki_section_returns_section_not_found_with_available(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    page = _wiki_page(
        narrative_sections=[
            _section(anchor="context"),
            _section(anchor="implications", heading="Implications"),
        ],
    )
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=page)
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_section"](
            channel_id="C1",
            page_slug="topic-auth",
            anchor="ghost-anchor",
            ctx=_make_ctx(),
        )
    assert result["error"] == "section_not_found"
    assert result["page_slug"] == "topic-auth"
    assert sorted(result["available_anchors"]) == ["context", "implications"]


async def test_read_wiki_section_returns_narrative_not_available_when_empty(
    registered_tools, monkeypatch
) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    page = _wiki_page(
        narrative_sections=[],
        modules=[{"id": "key_facts", "anchor": "kf", "data": {}}],
    )
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=page)
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_section"](
            channel_id="C1",
            page_slug="topic-auth",
            anchor="context",
            ctx=_make_ctx(),
        )
    assert result["error"] == "narrative_not_available"
    assert result["page_slug"] == "topic-auth"
    assert result["has_modules"] is True
    assert "read_wiki_page" in result.get("suggestion", "")


async def test_read_wiki_section_returns_page_not_found(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=None)
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_section"](
            channel_id="C1",
            page_slug="missing-page",
            anchor="context",
            ctx=_make_ctx(),
        )
    assert result == {
        "error": "page_not_found",
        "channel_id": "C1",
        "page_slug": "missing-page",
    }


async def test_read_wiki_section_denies_unauthorized_channel(registered_tools, monkeypatch) -> None:
    _patch_principal(monkeypatch, "mcp:agent-1")
    with patch(
        "beever_atlas.infra.channel_access.assert_channel_access",
        new=AsyncMock(side_effect=PermissionError("denied")),
    ):
        result = await registered_tools["read_wiki_section"](
            channel_id="C1",
            page_slug="topic-auth",
            anchor="context",
            ctx=_make_ctx(),
        )
    assert result == {"error": "channel_access_denied", "channel_id": "C1"}


async def test_read_wiki_section_schema_parity_with_persistence(
    registered_tools, monkeypatch
) -> None:
    """Section payload returned by read_wiki_section matches the
    persisted section structure exactly — same field names, same types.

    Verifies the spec requirement "Section data shape consistency with
    page-store" — agents see identical shapes whether they fetch via
    read_wiki_page (extracting from full page) or read_wiki_section.
    """
    _patch_principal(monkeypatch, "mcp:agent-1")
    persisted_section = _section(
        anchor="alternatives",
        heading="Alternatives rejected",
        paragraphs=[
            {
                "text": "FastAPI was considered but rejected.",
                "citations": ["f_2", "f_3"],
                "is_inference": False,
            },
        ],
        citations=["f_2", "f_3"],
        visual={"kind": "table", "content": {"headers": ["A"], "rows": [["1"]]}},
    )
    page = _wiki_page(narrative_sections=[persisted_section])
    fake_store = AsyncMock()
    fake_store.get_page_by_slug = AsyncMock(return_value=page)
    with (
        patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(),
        ),
        patch("beever_atlas.stores.get_stores", return_value=_stores()),
        patch(
            "beever_atlas.wiki.page_store.WikiPageStore",
            return_value=fake_store,
        ),
    ):
        result = await registered_tools["read_wiki_section"](
            channel_id="C1",
            page_slug="topic-auth",
            anchor="alternatives",
            ctx=_make_ctx(),
        )
    # Field equivalence: every persisted-section field surfaces under
    # the same name on the tool response.
    assert result["anchor"] == persisted_section["anchor"]
    assert result["heading"] == persisted_section["heading"]
    assert result["paragraphs"] == persisted_section["paragraphs"]
    assert result["citations"] == persisted_section["citations"]
    assert result["visual"] == persisted_section["visual"]
