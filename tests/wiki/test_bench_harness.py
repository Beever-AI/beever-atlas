"""Tests for the wiki benchmark harness (Phase 0).

test_bench_harness_produces_baseline_schema: invokes the harness programmatically
on a tiny fixture; asserts baseline.json has all required keys and types.

test_cassette_covers_all_calls: runs compile once with the cassette; asserts zero
cache misses (every LLM call had a pre-recorded entry).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _ROOT / "scripts"
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_SCRIPTS))

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_FULL_FIXTURE = _FIXTURE_DIR / "gathered_bench.json"
_FULL_CASSETTE = _FIXTURE_DIR / "cassette_llm.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tiny_fixture() -> dict:
    """Build a minimal in-memory fixture with 1 cluster and the bare minimum
    ChannelSummary for a compile to succeed."""
    from beever_atlas.models.domain import AtomicFact, ChannelSummary, TopicCluster

    fact = AtomicFact(
        id="tf1",
        memory_text="Team uses Python for backend services",
        quality_score=0.8,
        author_name="Alice",
        message_ts="1704067200",
        source_message_id="tmsg1",
        channel_id="C0TINY",
        importance="high",
        fact_type="observation",
    )
    cluster = TopicCluster(
        id="tiny-cluster",
        channel_id="C0TINY",
        title="Python Backend",
        summary="The team uses Python for all backend services.",
        current_state="Stable and in production.",
        open_questions="",
        impact_note="Core technology choice.",
        topic_tags=["python", "backend"],
        member_ids=["tf1"],
        member_count=1,
        key_facts=[{
            "fact_id": "tf1",
            "memory_text": "Team uses Python for backend services",
            "author_name": "Alice",
            "message_ts": "1704067200",
            "fact_type": "observation",
            "importance": "high",
            "quality_score": 0.8,
            "source_message_id": "tmsg1",
        }],
        faq_candidates=[],
    )
    channel_summary = ChannelSummary(
        id="tiny-summary",
        channel_id="C0TINY",
        channel_name="#tiny-test",
        text="Tiny benchmark channel.",
        themes="Python backend.",
        glossary_terms=[],
    )
    return {
        "channel_id": "C0TINY",
        "channel_name": "#tiny-test",
        "channel_summary": channel_summary,
        "clusters": [cluster],
        "cluster_facts": {"tiny-cluster": [fact]},
        "recent_facts": [fact],
        "media_facts": [],
        "decisions": [],
        "technologies": [],
        "projects": [],
    }


def _make_tiny_cassette_entries() -> dict:
    return {
        "entries": {
            "overview": {"content": "## Overview\n\nA tiny test channel with Python backend content. The team builds services in Python.", "summary": "Python backend channel."},
            "people": {"content": "## Team\n\n### Alice\nBackend developer working on Python services.", "summary": "Alice: backend developer."},
            "activity": {"content": "## Recent Activity\n\nActive development on Python services.", "summary": "Active Python development."},
            "topic": {"content": "## Python Backend\n\n**TL;DR**: The team uses Python for all backend services.\n\nThis is a stable technology choice that has been in production for some time.", "summary": "Python backend services."},
            "translation": "{}",
            "analysis": {"needs_subpages": False, "subpages": []},
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_bench_harness_produces_baseline_schema(tmp_path: Path) -> None:
    """Harness must produce a baseline.json with all required keys and correct types."""
    import wiki_bench

    tiny_fixture = _make_tiny_fixture()
    cassette_data = _make_tiny_cassette_entries()

    # Write tiny cassette to a tmp file.
    cassette_path = tmp_path / "tiny_cassette.json"
    cassette_path.write_text(json.dumps(cassette_data))

    out_path = tmp_path / "baseline.json"

    # Patch _load_fixture to return our in-memory tiny fixture.
    with patch.object(wiki_bench, "_load_fixture", return_value=tiny_fixture):
        cassette = wiki_bench.CassetteLLM(cassette_path)
        with patch.object(wiki_bench, "CassetteLLM", return_value=cassette):
            result = wiki_bench.run_bench(
                fixture_path=Path("dummy_fixture.json"),
                cassette_path=cassette_path,
                n_runs=1,
                out_path=out_path,
            )

    # Verify the file was written.
    assert out_path.exists(), "baseline.json was not written"

    # Verify the returned dict has all required keys with correct types.
    required_keys = {
        "commit_sha": str,
        "recorded_at": str,
        "n_runs": int,
        "page_count": int,
        "duration_ms_p50": int,
        "duration_ms_p95": int,
        "parse_failures_total": int,
        "empty_content_total": int,
        "dash_wall_pages_total": int,
    }

    for key, expected_type in required_keys.items():
        assert key in result, f"Missing key: {key}"
        assert isinstance(result[key], expected_type), (
            f"Key {key!r}: expected {expected_type.__name__}, got {type(result[key]).__name__}"
        )

    # Sanity: n_runs matches what we passed.
    assert result["n_runs"] == 1

    # Verify on-disk JSON matches returned dict.
    on_disk = json.loads(out_path.read_text())
    assert on_disk == result


def test_cassette_covers_all_calls(tmp_path: Path) -> None:
    """Full compile with the cassette fixture must produce zero cache misses."""
    import wiki_bench

    if not _FULL_FIXTURE.exists():
        pytest.skip(f"Full fixture not found: {_FULL_FIXTURE}")
    if not _FULL_CASSETTE.exists():
        pytest.skip(f"Full cassette not found: {_FULL_CASSETTE}")

    out_path = tmp_path / "cassette_check_baseline.json"

    # Run one pass.
    gathered = wiki_bench._load_fixture(_FULL_FIXTURE)
    cassette = wiki_bench.CassetteLLM(_FULL_CASSETTE)

    import asyncio

    wall_ms, records, pages = asyncio.run(
        wiki_bench._run_once(gathered, cassette)
    )

    # Zero misses means every LLM call was covered by the cassette.
    assert cassette.misses == [], (
        f"Cassette misses detected — re-record required for: {cassette.misses}"
    )

    # Some pages must have been produced.
    assert len(pages) > 0, "No pages compiled — fixture or cassette may be broken"
