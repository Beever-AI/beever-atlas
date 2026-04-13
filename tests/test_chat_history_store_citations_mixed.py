"""Tests covering coexistence of legacy-list and envelope citation shapes.

mongomock is not installed (see tests/test_platform_store.py note), so we
exercise the shape-normalization helpers directly rather than spinning up
a full Motor stack. The aggregation pipeline itself is verified as a
structural unit test — we assert the `$switch` branches correctly map
both shapes through a hand-rolled Python evaluator.
"""

from __future__ import annotations

from beever_atlas.stores.chat_history_store import ChatHistoryStore


def test_flatten_in_place_handles_legacy_list_shape():
    msgs = [
        {"role": "assistant", "citations": [{"id": "src_a"}, {"id": "src_b"}]},
    ]
    ChatHistoryStore._flatten_citations_in_place(msgs)
    assert msgs[0]["citations"] == [{"id": "src_a"}, {"id": "src_b"}]


def test_flatten_in_place_handles_envelope_shape():
    msgs = [
        {
            "role": "assistant",
            "citations": {
                "items": [{"id": "src_x"}],
                "sources": [{"id": "src_x", "kind": "pdf"}],
                "refs": [{"marker": 1, "source_id": "src_x"}],
            },
        }
    ]
    ChatHistoryStore._flatten_citations_in_place(msgs)
    assert msgs[0]["citations"] == [{"id": "src_x"}]


def test_flatten_in_place_mixed_messages():
    """Legacy + envelope in the same session are both flattened to list-of-items."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "citations": [{"id": "legacy_1"}]},
        {
            "role": "assistant",
            "citations": {
                "items": [{"id": "env_1"}],
                "sources": [{"id": "env_1"}],
                "refs": [],
            },
        },
    ]
    ChatHistoryStore._flatten_citations_in_place(msgs)
    assert msgs[1]["citations"] == [{"id": "legacy_1"}]
    assert msgs[2]["citations"] == [{"id": "env_1"}]


def test_flatten_in_place_is_documented_as_mutating():
    """Helper docstring must announce its mutation contract (rename M3)."""
    doc = ChatHistoryStore._flatten_citations_in_place.__doc__ or ""
    assert "MUTATES" in doc or "mutat" in doc.lower()


# ---- top_sources_for_channel pipeline: normalize both shapes ----------


def _emulate_normalize(message: dict) -> list[dict]:
    """Emulate the `$addFields._norm_sources` $switch used by the aggregation.

    Mirrors the pipeline stage so we can exercise it shape-by-shape
    without a live Mongo server.
    """
    citations = message.get("citations")
    # Envelope case: dict with `sources` array.
    if isinstance(citations, dict) and isinstance(citations.get("sources"), list):
        return citations["sources"]
    # Legacy case: citations itself is a list of source dicts.
    if isinstance(citations, list):
        return citations
    return []


def test_pipeline_normalizes_legacy_list_shape():
    msg = {"citations": [{"id": "src_legacy"}]}
    assert _emulate_normalize(msg) == [{"id": "src_legacy"}]


def test_pipeline_normalizes_envelope_shape():
    msg = {
        "citations": {
            "items": [{"id": "src_env"}],
            "sources": [{"id": "src_env", "kind": "pdf"}],
            "refs": [],
        }
    }
    assert _emulate_normalize(msg) == [{"id": "src_env", "kind": "pdf"}]


def test_pipeline_both_shapes_contribute_to_counts():
    """Both legacy + envelope rows for the same source should sum correctly."""
    messages = [
        {"citations": [{"id": "shared_src", "title": "legacy"}]},
        {
            "citations": {
                "items": [],
                "sources": [{"id": "shared_src", "title": "envelope"}],
                "refs": [],
            }
        },
        {"citations": None},  # empty / non-array: contributes nothing
    ]
    counts: dict[str, int] = {}
    for m in messages:
        for s in _emulate_normalize(m):
            sid = s.get("id")
            if sid:
                counts[sid] = counts.get(sid, 0) + 1
    assert counts == {"shared_src": 2}


def test_pipeline_has_switch_with_both_branches():
    """Smoke-check that the real pipeline references both shape branches.

    Guards against someone accidentally dropping a branch and reverting
    to envelope-only handling.
    """
    import inspect

    src = inspect.getsource(ChatHistoryStore.top_sources_for_channel)
    assert "$switch" in src
    assert "messages.citations.sources" in src
    assert "messages.citations" in src
    assert "_norm_sources" in src
