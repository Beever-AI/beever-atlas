"""Tests for the ``provenance_drawer`` module.

Covers:
  - catalog entry shape + always-eligible-with-fact predicate
  - message grouping by ``(platform, source_message_id)`` so multiple
    facts from the same source message collapse to one row
  - chronological sort (ts ASC)
  - snippet truncation at 200 chars (word boundary preferred)
  - cap at 25 displayed messages with ``total_count`` reflecting full
    size
  - graceful empty inputs

Pure unit tests — no LLM, network, or DB.
"""

from __future__ import annotations

from beever_atlas.wiki.modules import MODULE_CATALOG
from beever_atlas.wiki.modules.provenance_drawer import (
    build_provenance_drawer_data,
)


# ---------------------------------------------------------------------------
# Catalog entry
# ---------------------------------------------------------------------------


def test_provenance_drawer_in_catalog() -> None:
    assert "provenance_drawer" in MODULE_CATALOG
    spec = MODULE_CATALOG["provenance_drawer"]
    assert spec.id == "provenance_drawer"
    assert spec.label == "Source messages"
    assert spec.renderer_kind == "frontend"


def test_provenance_drawer_eligible_with_one_fact() -> None:
    spec = MODULE_CATALOG["provenance_drawer"]
    assert spec.eligible({"fact_count": 1}) is True
    assert spec.eligible({"fact_count": 50}) is True


def test_provenance_drawer_ineligible_with_zero_facts() -> None:
    spec = MODULE_CATALOG["provenance_drawer"]
    assert spec.eligible({"fact_count": 0}) is False
    assert spec.eligible({}) is False


# ---------------------------------------------------------------------------
# build_provenance_drawer_data — payload shape
# ---------------------------------------------------------------------------


def test_payload_has_expected_top_level_shape() -> None:
    data = build_provenance_drawer_data([])
    assert data["label"] == "Source messages"
    assert data["renderer_kind"] == "frontend"
    assert data["messages"] == []
    assert data["total_count"] == 0


def test_groups_facts_by_source_message_id() -> None:
    facts = [
        {
            "fact_id": "f1",
            "platform": "mattermost",
            "source_message_id": "m1",
            "author_name": "Jacky",
            "message_ts": "2026-04-22T10:32:00Z",
            "memory_text": "Forked beever-atlas as legacy-memory.",
        },
        {
            "fact_id": "f2",
            "platform": "mattermost",
            "source_message_id": "m1",  # same message → same group
            "author_name": "Jacky",
            "message_ts": "2026-04-22T10:32:00Z",
            "memory_text": "Adapted hybrid storage architecture.",
        },
        {
            "fact_id": "f3",
            "platform": "mattermost",
            "source_message_id": "m2",
            "author_name": "Thomas",
            "message_ts": "2026-04-22T11:15:00Z",
            "memory_text": "Different message entirely.",
        },
    ]
    data = build_provenance_drawer_data(facts)
    assert data["total_count"] == 2
    assert len(data["messages"]) == 2
    # Find the m1-grouped message and check fact_ids merged.
    grouped = next(m for m in data["messages"] if m["author"] == "Jacky")
    assert set(grouped["contributed_to_facts"]) == {"f1", "f2"}
    other = next(m for m in data["messages"] if m["author"] == "Thomas")
    assert other["contributed_to_facts"] == ["f3"]


def test_sorts_messages_chronologically_ascending() -> None:
    facts = [
        {
            "fact_id": "f1",
            "platform": "slack",
            "source_message_id": "m1",
            "message_ts": "2026-05-02T09:00:00Z",
            "memory_text": "Late message.",
        },
        {
            "fact_id": "f2",
            "platform": "slack",
            "source_message_id": "m2",
            "message_ts": "2026-04-26T09:00:00Z",
            "memory_text": "Early message.",
        },
        {
            "fact_id": "f3",
            "platform": "slack",
            "source_message_id": "m3",
            "message_ts": "2026-04-30T09:00:00Z",
            "memory_text": "Middle message.",
        },
    ]
    data = build_provenance_drawer_data(facts)
    ts_order = [m["ts"] for m in data["messages"]]
    assert ts_order == sorted(ts_order)
    assert ts_order[0].startswith("2026-04-26")
    assert ts_order[-1].startswith("2026-05-02")


def test_snippet_truncates_long_text_at_word_boundary() -> None:
    long_text = "alpha " * 80  # ~480 chars, well over 200 budget
    facts = [
        {
            "fact_id": "f1",
            "platform": "slack",
            "source_message_id": "m1",
            "memory_text": long_text,
        }
    ]
    data = build_provenance_drawer_data(facts)
    snippet = data["messages"][0]["snippet"]
    # Truncated to <= 201 chars (200 + ellipsis) and ends with the
    # truncation indicator.
    assert len(snippet) <= 201
    assert snippet.endswith("…")


def test_snippet_keeps_short_text_intact() -> None:
    facts = [
        {
            "fact_id": "f1",
            "platform": "slack",
            "source_message_id": "m1",
            "memory_text": "Short note.",
        }
    ]
    data = build_provenance_drawer_data(facts)
    assert data["messages"][0]["snippet"] == "Short note."


def test_caps_displayed_messages_at_25_with_total_count() -> None:
    facts = [
        {
            "fact_id": f"f{i}",
            "platform": "slack",
            "source_message_id": f"m{i}",
            "message_ts": f"2026-04-{(i % 28) + 1:02d}T09:00:00Z",
            "memory_text": f"Fact #{i}",
        }
        for i in range(40)
    ]
    data = build_provenance_drawer_data(facts)
    assert len(data["messages"]) == 25
    assert data["total_count"] == 40  # full size for "+N more" affordance


def test_promotes_richer_metadata_when_first_entry_lacks_it() -> None:
    """When the first fact in a group has missing author/url/channel
    but a later fact in the same group has them, the merged record
    should pick up the richer metadata."""
    facts = [
        {
            "fact_id": "f1",
            "platform": "mattermost",
            "source_message_id": "m1",
            "author_name": "",  # missing
            "permalink": "",
            "memory_text": "First fact (missing metadata).",
        },
        {
            "fact_id": "f2",
            "platform": "mattermost",
            "source_message_id": "m1",
            "author_name": "Jacky Chan",
            "permalink": "https://team.votee.com/post/abc",
            "channel_name": "tech-beever-atlas",
            "memory_text": "Second fact (with metadata).",
        },
    ]
    data = build_provenance_drawer_data(facts)
    msg = data["messages"][0]
    assert msg["author"] == "Jacky Chan"
    assert msg["url"] == "https://team.votee.com/post/abc"
    assert msg["channel"] == "tech-beever-atlas"


def test_handles_facts_without_source_message_id() -> None:
    """Old facts may not have a source_message_id — fall back to the
    fact_id so each becomes its own row (no spurious collapse)."""
    facts = [
        {"fact_id": "f1", "platform": "slack", "memory_text": "Fact 1"},
        {"fact_id": "f2", "platform": "slack", "memory_text": "Fact 2"},
    ]
    data = build_provenance_drawer_data(facts)
    assert data["total_count"] == 2


def test_handles_non_list_input_gracefully() -> None:
    data = build_provenance_drawer_data(None)  # type: ignore[arg-type]
    assert data["messages"] == []
    assert data["total_count"] == 0


def test_skips_facts_with_no_id_or_message_id() -> None:
    facts = [
        {"platform": "slack", "memory_text": "ghost"},  # no fact_id, no msg_id
        {"fact_id": "f1", "platform": "slack", "memory_text": "real fact"},
    ]
    data = build_provenance_drawer_data(facts)
    assert data["total_count"] == 1
    assert data["messages"][0]["contributed_to_facts"] == ["f1"]


def test_uses_permalink_or_source_url_for_deep_link() -> None:
    facts = [
        {
            "fact_id": "f1",
            "platform": "slack",
            "source_message_id": "m1",
            "source_url": "https://example.slack.com/archives/C/p123",
            "memory_text": "Fact",
        }
    ]
    data = build_provenance_drawer_data(facts)
    assert data["messages"][0]["url"] == "https://example.slack.com/archives/C/p123"
