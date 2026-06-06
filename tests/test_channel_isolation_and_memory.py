"""Channel-isolation ACL + conversation-memory ACL + meta-recall routing.

Covers the bot-reply privacy/memory hardening:
  * the per-turn ``authorized_channel_ids`` contextvar gate,
  * every retrieval/graph/list tool refusing out-of-scope channels,
  * ``list_channels_tool`` gutting the cross-channel / never-synced inventory,
  * ``_load_chat_history_parts`` refusing cross-user / cross-channel memory,
  * ``_is_meta_recall_question`` intent detection.

These are unit-level: the channel guards short-circuit before any store call,
so most need no mocking.
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.tools import graph_tools, memory_tools, orchestration_tools
from beever_atlas.agents.tools.orchestration_tools import (
    bound_authorized_channels,
    get_authorized_channels,
    is_channel_authorized,
)


# --------------------------------------------------------------------------
# Contextvar gate
# --------------------------------------------------------------------------


def test_unbound_gate_is_open():
    # Non-chat callers / tests never bind the set; the gate falls back to the
    # per-capability principal ACL (open here).
    assert is_channel_authorized("C_ANY") is True
    assert get_authorized_channels() == frozenset()


def test_bound_gate_allows_only_listed_channels():
    with bound_authorized_channels({"C1"}):
        assert is_channel_authorized("C1") is True
        assert is_channel_authorized("C2") is False
        assert get_authorized_channels() == frozenset({"C1"})
    # restored after the block
    assert is_channel_authorized("C2") is True


def test_gate_resets_on_exception():
    with pytest.raises(RuntimeError):
        with bound_authorized_channels({"C1"}):
            raise RuntimeError("boom")
    assert is_channel_authorized("C2") is True  # not leaked


# --------------------------------------------------------------------------
# Memory tools refuse out-of-scope channels (short-circuit before any store)
# --------------------------------------------------------------------------


async def test_search_channel_facts_refuses_unauthorized_channel():
    with bound_authorized_channels({"C1"}):
        out = await memory_tools.search_channel_facts("C2", "anything")
    assert out == []


async def test_search_qa_history_refuses_unauthorized_channel():
    with bound_authorized_channels({"C1"}):
        out = await memory_tools.search_qa_history("C2", "anything")
    assert out == []


async def test_get_recent_activity_refuses_unauthorized_channel():
    with bound_authorized_channels({"C1"}):
        out = await memory_tools.get_recent_activity("C2")
    assert out == []


async def test_search_media_references_refuses_unauthorized_channel():
    with bound_authorized_channels({"C1"}):
        out = await memory_tools.search_media_references("C2", "anything")
    assert out == []


async def test_memory_tool_allows_in_scope_channel_short_circuit(monkeypatch):
    # When the channel IS authorized the guard passes through; we stub the
    # store so the test stays offline but proves the guard didn't block.
    called = {}

    class _FakeWeaviate:
        async def bm25_search(self, **kw):
            called["hit"] = True
            return []

        async def true_hybrid_search(self, **kw):
            called["hit"] = True
            return []

    class _Stores:
        weaviate = _FakeWeaviate()

    monkeypatch.setattr(memory_tools, "get_stores", lambda: _Stores(), raising=False)
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: _Stores(), raising=False)
    with bound_authorized_channels({"C1"}):
        await memory_tools.search_channel_facts("C1", "q")
    # the guard let it through (it tried to search); exact result doesn't matter
    assert called.get("hit") is True


# --------------------------------------------------------------------------
# Graph tools refuse out-of-scope channels with the _empty sentinel
# --------------------------------------------------------------------------


async def test_search_relationships_refuses_unauthorized_channel():
    with bound_authorized_channels({"C1"}):
        out = await graph_tools.search_relationships("C2", ["Alice"])
    assert isinstance(out, list) and out and out[0]["_empty"] is True
    assert out[0]["reason"] == "channel_access_denied"


async def test_trace_decision_history_refuses_unauthorized_channel():
    with bound_authorized_channels({"C1"}):
        out = await graph_tools.trace_decision_history("C2", "Topic")
    assert out and out[0]["reason"] == "channel_access_denied"


async def test_find_experts_refuses_unauthorized_channel():
    with bound_authorized_channels({"C1"}):
        out = await graph_tools.find_experts("C2", "Topic")
    assert out and out[0]["reason"] == "channel_access_denied"


# --------------------------------------------------------------------------
# list_channels_tool guts the inventory
# --------------------------------------------------------------------------


async def test_list_channels_tool_filters_to_authorized_and_drops_never_synced(monkeypatch):
    fake_channels = [
        {"channel_id": "C1", "name": "general", "sync_status": "synced"},
        {"channel_id": "C2", "name": "secret", "sync_status": "synced"},
        {"channel_id": "C3", "name": "ghost", "sync_status": "never_synced"},
    ]

    async def _fake_list_channels(principal_id, connection_id):
        return list(fake_channels)

    monkeypatch.setattr("beever_atlas.capabilities.connections.list_channels", _fake_list_channels)
    with orchestration_tools.bound_principal("user:test"):
        with bound_authorized_channels({"C1"}):
            out = await orchestration_tools.list_channels_tool("conn1")
    ids = [c["channel_id"] for c in out["channels"]]
    assert ids == ["C1"]  # C2 out-of-scope, C3 never-synced both dropped


async def test_list_channels_tool_unbound_path_unchanged(monkeypatch):
    # The MCP/web path (no authorized set bound) legitimately enumerates the
    # connection catalog — including never-synced channels. Only the scoped
    # chat path filters.
    fake_channels = [
        {"channel_id": "C1", "name": "general", "sync_status": "synced"},
        {"channel_id": "C3", "name": "ghost", "sync_status": "never_synced"},
    ]

    async def _fake_list_channels(principal_id, connection_id):
        return list(fake_channels)

    monkeypatch.setattr("beever_atlas.capabilities.connections.list_channels", _fake_list_channels)
    with orchestration_tools.bound_principal("user:test"):
        out = await orchestration_tools.list_channels_tool("conn1")
    ids = [c["channel_id"] for c in out["channels"]]
    assert ids == ["C1", "C3"]  # unbound = full catalog, unchanged


async def test_list_connections_tool_refuses_when_scoped(monkeypatch):
    # In a scoped chat turn, enumerating connections would leak org structure.
    called = {"hit": False}

    async def _fake_list_connections(principal_id):
        called["hit"] = True
        return [{"connection_id": "conn1", "platform": "slack"}]

    monkeypatch.setattr(
        "beever_atlas.capabilities.connections.list_connections", _fake_list_connections
    )
    with orchestration_tools.bound_principal("user:test"):
        with bound_authorized_channels({"C1"}):
            out = await orchestration_tools.list_connections_tool()
    assert out.get("error") == "scoped_to_channel"
    assert called["hit"] is False  # never reached the capability


# --------------------------------------------------------------------------
# Wiki tools refuse out-of-scope channels (the data type from the screenshots)
# --------------------------------------------------------------------------


async def test_get_wiki_page_refuses_unauthorized_channel():
    from beever_atlas.agents.tools import wiki_tools

    with bound_authorized_channels({"C1"}):
        out = await wiki_tools.get_wiki_page("C2", "overview")
    assert out is None


async def test_get_topic_overview_refuses_unauthorized_channel():
    from beever_atlas.agents.tools import wiki_tools

    with bound_authorized_channels({"C1"}):
        out = await wiki_tools.get_topic_overview("C2")
    assert out is None


# --------------------------------------------------------------------------
# Meta-recall intent detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "what did I ask you?",
        "What did we discuss earlier",
        "summarize our conversation",
        "remind me what I said",
        "do you remember what I wanted",
    ],
)
def test_meta_recall_detected(q):
    from beever_atlas.api.ask import _is_meta_recall_question

    assert _is_meta_recall_question(q) is True


@pytest.mark.parametrize(
    "q",
    [
        "what is our tech stack?",
        "who decided to use Postgres",
        "summarize the Q3 roadmap page",  # about channel content, not our chat
        "",
    ],
)
def test_meta_recall_not_falsely_triggered(q):
    from beever_atlas.api.ask import _is_meta_recall_question

    assert _is_meta_recall_question(q) is False


# --------------------------------------------------------------------------
# Acting-user-id resolution (bridge-asserted identity, validated)
# --------------------------------------------------------------------------


def test_acting_user_id_uses_valid_asserted():
    from beever_atlas.api.ask import _resolve_acting_user_id

    assert _resolve_acting_user_id("U0B55TPHLHF", "user:hash") == "U0B55TPHLHF"


def test_acting_user_id_falls_back_when_empty():
    from beever_atlas.api.ask import _resolve_acting_user_id

    assert _resolve_acting_user_id(None, "user:hash") == "user:hash"
    assert _resolve_acting_user_id("   ", "user:hash") == "user:hash"


def test_acting_user_id_rejects_malformed():
    from beever_atlas.api.ask import _resolve_acting_user_id

    # injection-ish / overlong values fall back to the principal
    assert _resolve_acting_user_id("a b; drop", "user:hash") == "user:hash"
    assert _resolve_acting_user_id("x" * 200, "user:hash") == "user:hash"
    assert _resolve_acting_user_id("a\nb", "user:hash") == "user:hash"


# --------------------------------------------------------------------------
# Conversation-memory ACL in _load_chat_history_parts
# --------------------------------------------------------------------------


class _FakeChatStore:
    def __init__(self, session_doc, messages):
        self._doc = session_doc
        self._messages = messages

    async def load_session_with_channels(self, session_id):
        return self._doc

    async def get_context_messages(self, session_id):
        return self._messages


def _patch_stores(monkeypatch, store):
    class _Stores:
        chat_history = store

    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: _Stores(), raising=False)


async def test_memory_load_allows_matching_user_and_channel(monkeypatch):
    from beever_atlas.api import ask as ask_mod

    store = _FakeChatStore(
        {"user_id": "U1", "channel_id": "C1", "channel_ids": ["C1"]},
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    )
    _patch_stores(monkeypatch, store)
    parts = await ask_mod._load_chat_history_parts("sess", "U1", "C1")
    assert len(parts) == 2


async def test_memory_load_denies_cross_user(monkeypatch):
    from beever_atlas.api import ask as ask_mod

    store = _FakeChatStore(
        {"user_id": "U1", "channel_id": "C1", "channel_ids": ["C1"]},
        [{"role": "user", "content": "secret"}],
    )
    _patch_stores(monkeypatch, store)
    parts = await ask_mod._load_chat_history_parts("sess", "U2", "C1")
    assert parts == []  # different user → cold start, no leak


async def test_memory_load_denies_cross_channel(monkeypatch):
    from beever_atlas.api import ask as ask_mod

    store = _FakeChatStore(
        {"user_id": "U1", "channel_id": "C1", "channel_ids": ["C1"]},
        [{"role": "user", "content": "secret"}],
    )
    _patch_stores(monkeypatch, store)
    parts = await ask_mod._load_chat_history_parts("sess", "U1", "C2")
    assert parts == []  # different channel → no leak


async def test_memory_load_unknown_session_cold_starts(monkeypatch):
    from beever_atlas.api import ask as ask_mod

    store = _FakeChatStore(None, [])
    _patch_stores(monkeypatch, store)
    parts = await ask_mod._load_chat_history_parts("sess", "U1", "C1")
    assert parts == []


async def test_memory_load_without_acl_context_is_unchanged(monkeypatch):
    # Web-UI path passes no user/channel → no ACL gate, returns turns as before.
    from beever_atlas.api import ask as ask_mod

    store = _FakeChatStore(
        {"user_id": "Ux", "channel_id": "Cx", "channel_ids": ["Cx"]},
        [{"role": "user", "content": "q"}],
    )
    _patch_stores(monkeypatch, store)
    parts = await ask_mod._load_chat_history_parts("sess")
    assert len(parts) == 1
