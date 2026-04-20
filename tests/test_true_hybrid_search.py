"""Tests for true_hybrid_search and its integration with agent tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# WeaviateStore.true_hybrid_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_true_hybrid_search_calls_weaviate_hybrid():
    """true_hybrid_search must issue a single collection.query.hybrid() call
    with the correct query, vector, and alpha arguments."""
    from beever_atlas.stores.weaviate_store import WeaviateStore

    store = WeaviateStore(url="http://localhost:8080")

    # Minimal AtomicFact stand-in returned by _obj_to_fact
    fake_fact = MagicMock()
    fake_fact.id = "fact-uuid-1"
    fake_fact.memory_text = "Alice deployed the backend on Friday."
    fake_fact.invalid_at = None  # not superseded

    fake_obj = MagicMock()
    fake_obj.properties = {}
    fake_obj.metadata.score = 0.82
    fake_obj.uuid = "fact-uuid-1"

    fake_result = MagicMock()
    fake_result.objects = [fake_obj]

    fake_collection = MagicMock()
    fake_collection.query.hybrid.return_value = fake_result

    with (
        patch.object(store, "_collection", return_value=fake_collection),
        patch.object(store, "_obj_to_fact", return_value=fake_fact),
    ):
        query_vector = [0.1] * 2048
        results = await store.true_hybrid_search(
            query_text="deployment on Friday",
            query_vector=query_vector,
            channel_id="C123",
            tier="atomic",
            limit=10,
            alpha=0.6,
        )

    # Verify hybrid was called with correct kwargs
    fake_collection.query.hybrid.assert_called_once()
    call_kwargs = fake_collection.query.hybrid.call_args.kwargs
    assert call_kwargs["query"] == "deployment on Friday"
    assert call_kwargs["vector"] == query_vector
    assert call_kwargs["alpha"] == 0.6
    assert call_kwargs["limit"] == 10

    # Result shape must match bm25_search / semantic_search consumers
    assert len(results) == 1
    assert "fact" in results[0]
    assert "similarity_score" in results[0]
    assert results[0]["similarity_score"] == round(0.82, 4)


@pytest.mark.asyncio
async def test_true_hybrid_search_uses_settings_alpha_by_default():
    """When alpha is None, true_hybrid_search must read weaviate_hybrid_alpha
    from settings (default 0.6)."""
    from beever_atlas.stores.weaviate_store import WeaviateStore

    store = WeaviateStore(url="http://localhost:8080")

    fake_obj = MagicMock()
    fake_obj.properties = {}
    fake_obj.metadata.score = 0.5
    fake_obj.uuid = "uuid-2"

    fake_result = MagicMock()
    fake_result.objects = [fake_obj]

    fake_collection = MagicMock()
    fake_collection.query.hybrid.return_value = fake_result

    fake_fact = MagicMock()
    fake_fact.id = "uuid-2"
    fake_fact.invalid_at = None  # not superseded

    with (
        patch.object(store, "_collection", return_value=fake_collection),
        patch.object(store, "_obj_to_fact", return_value=fake_fact),
    ):
        await store.true_hybrid_search(
            query_text="test query",
            query_vector=[0.0] * 2048,
            channel_id="C456",
        )

    call_kwargs = fake_collection.query.hybrid.call_args.kwargs
    # Default alpha from settings is 0.6
    assert call_kwargs["alpha"] == pytest.approx(0.6, abs=1e-6)


@pytest.mark.asyncio
async def test_true_hybrid_search_returns_empty_on_error():
    """true_hybrid_search must return [] on Weaviate error, not raise."""
    from beever_atlas.stores.weaviate_store import WeaviateStore

    store = WeaviateStore(url="http://localhost:8080")

    fake_collection = MagicMock()
    fake_collection.query.hybrid.side_effect = RuntimeError("Weaviate down")

    with patch.object(store, "_collection", return_value=fake_collection):
        results = await store.true_hybrid_search(
            query_text="anything",
            query_vector=[0.0] * 10,
            channel_id="C789",
        )

    assert results == []


@pytest.mark.asyncio
async def test_true_hybrid_search_applies_channel_and_tier_filter():
    """Filters for channel_id and tier must both be applied."""
    from beever_atlas.stores.weaviate_store import WeaviateStore

    store = WeaviateStore(url="http://localhost:8080")

    fake_result = MagicMock()
    fake_result.objects = []

    fake_collection = MagicMock()
    fake_collection.query.hybrid.return_value = fake_result

    with patch.object(store, "_collection", return_value=fake_collection):
        await store.true_hybrid_search(
            query_text="query",
            query_vector=[0.0] * 10,
            channel_id="C111",
            tier="atomic",
        )

    call_kwargs = fake_collection.query.hybrid.call_args.kwargs
    # filters must be set (non-None)
    assert call_kwargs.get("filters") is not None


# ---------------------------------------------------------------------------
# search_channel_facts — confirm it calls true_hybrid_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_channel_facts_calls_true_hybrid_search():
    """search_channel_facts must call true_hybrid_search when embedding succeeds."""
    from beever_atlas.agents.tools import memory_tools

    fake_fact = MagicMock()
    fake_fact.memory_text = "Deployment happened on Monday."
    fake_fact.author_name = "Alice"
    fake_fact.author_id = "U1"
    fake_fact.channel_id = "C123"
    fake_fact.platform = "slack"
    fake_fact.message_ts = "1700000000"
    fake_fact.source_message_id = "msg-1"
    fake_fact.importance = 5
    fake_fact.quality_score = 8
    fake_fact.id = "fact-1"
    fake_fact.topic_tags = ["deploy"]
    fake_fact.source_media_urls = []
    fake_fact.source_media_type = None
    fake_fact.source_link_urls = []
    fake_fact.source_link_titles = []

    mock_store = MagicMock()
    mock_store.true_hybrid_search = AsyncMock(
        return_value=[{"fact": fake_fact, "similarity_score": 0.75}]
    )
    mock_store.bm25_search = AsyncMock(return_value=[fake_fact])

    mock_stores = MagicMock()
    mock_stores.weaviate = mock_store

    fake_vector = [0.1] * 2048

    with (
        patch(
            "beever_atlas.agents.tools.memory_tools._embed_query",
            AsyncMock(return_value=fake_vector),
        ),
        patch("beever_atlas.stores.get_stores", return_value=mock_stores),
        patch(
            "beever_atlas.agents.tools.memory_tools.resolve_channel_name",
            AsyncMock(return_value="general"),
        ),
        patch(
            "beever_atlas.agents.tools.memory_tools.cite_tool_output",
            lambda **_: lambda f: f,
        ),
    ):
        results = await memory_tools.search_channel_facts(
            channel_id="C123",
            query="deployment",
            limit=3,
        )

    # true_hybrid_search must have been called, bm25_search must NOT
    mock_store.true_hybrid_search.assert_called_once()
    mock_store.bm25_search.assert_not_called()

    # Result shape preserved
    assert isinstance(results, list)
    if results:
        assert "text" in results[0]
        assert "author" in results[0]


# ---------------------------------------------------------------------------
# QAHistoryStore.true_hybrid_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_history_true_hybrid_search_calls_weaviate_hybrid():
    """QAHistoryStore.true_hybrid_search must call collection.query.hybrid()
    with correct args and return normalized entry dicts."""
    from beever_atlas.stores.qa_history_store import QAHistoryStore

    store = QAHistoryStore(url="http://localhost:8080")

    fake_obj = MagicMock()
    fake_obj.properties = {
        "question": "Who deployed last week?",
        "answer": "Alice deployed on Friday.",
        "citations_json": "[]",
        "timestamp": "2026-04-10T00:00:00+00:00",
        "session_id": "s1",
        "answer_kind": "answered",
    }
    fake_obj.uuid = "qa-uuid-1"

    fake_result = MagicMock()
    fake_result.objects = [fake_obj]

    fake_collection = MagicMock()
    fake_collection.query.hybrid.return_value = fake_result

    with patch.object(store, "_collection", return_value=fake_collection):
        query_vector = [0.2] * 2048
        results = await store.true_hybrid_search(
            channel_id="C123",
            query="last deployment",
            query_vector=query_vector,
            limit=5,
            alpha=0.6,
        )

    fake_collection.query.hybrid.assert_called_once()
    call_kwargs = fake_collection.query.hybrid.call_args.kwargs
    assert call_kwargs["query"] == "last deployment"
    assert call_kwargs["vector"] == query_vector
    assert call_kwargs["alpha"] == pytest.approx(0.6)
    assert call_kwargs["limit"] == 5

    assert len(results) == 1
    assert results[0]["question"] == "Who deployed last week?"
    assert results[0]["answer"] == "Alice deployed on Friday."
    assert results[0]["id"] == "qa-uuid-1"


@pytest.mark.asyncio
async def test_search_qa_history_uses_hybrid_when_vector_provided():
    """search_qa_history must delegate to true_hybrid_search when query_vector
    is given, and fall back to BM25 only when it fails."""
    from beever_atlas.stores.qa_history_store import QAHistoryStore

    store = QAHistoryStore(url="http://localhost:8080")

    expected = [
        {
            "question": "Q?",
            "answer": "A.",
            "citations": [],
            "timestamp": "",
            "session_id": "",
            "id": "qa-1",
            "answer_kind": "answered",
        }
    ]

    with patch.object(store, "true_hybrid_search", AsyncMock(return_value=expected)):
        results = await store.search_qa_history(
            channel_id="C1",
            query="test",
            limit=5,
            query_vector=[0.0] * 10,
        )

    assert results == expected


@pytest.mark.asyncio
async def test_search_qa_history_falls_back_to_bm25_when_no_vector():
    """search_qa_history without a query_vector must use BM25, not hybrid."""
    from beever_atlas.stores.qa_history_store import QAHistoryStore

    store = QAHistoryStore(url="http://localhost:8080")

    fake_obj = MagicMock()
    fake_obj.properties = {
        "question": "Q?",
        "answer": "A.",
        "citations_json": "[]",
        "timestamp": "",
        "session_id": "",
        "answer_kind": "answered",
    }
    fake_obj.uuid = "qa-2"

    fake_result = MagicMock()
    fake_result.objects = [fake_obj]

    fake_collection = MagicMock()
    fake_collection.query.bm25.return_value = fake_result

    with (
        patch.object(store, "_collection", return_value=fake_collection),
        patch.object(store, "true_hybrid_search", AsyncMock()) as mock_hybrid,
    ):
        results = await store.search_qa_history(
            channel_id="C1",
            query="test",
            limit=5,
            # no query_vector — should use BM25 path
        )

    mock_hybrid.assert_not_called()
    fake_collection.query.bm25.assert_called_once()
    assert len(results) == 1
    assert results[0]["id"] == "qa-2"
