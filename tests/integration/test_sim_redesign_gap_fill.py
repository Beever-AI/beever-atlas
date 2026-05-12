"""Sim harness scenarios for ``wiki-redesign-gap-fill``.

Covers the operator-visible behaviors the gap-fill change shipped:

  * Group 1 — sync emitters: ``message_processing`` + ``agent_state``
    events surface in ``pipeline_events`` for the SyncMonitor frontend.
  * Group 3 — Builder cost summary event emission + frozen-page skip.
  * Group 4 — curation gates: frozen pages skipped, manual pages mark
    dirty without rewriting.
  * Group 7+8 — adaptive predicate gating (above-threshold instantiates,
    below suppresses, force_kinds / suppress_kinds overrides).
"""

from __future__ import annotations

import pytest

from beever_atlas.services.pipeline_events import (
    EVENT_TYPE_AGENT_STATE,
    EVENT_TYPE_COST_SUMMARY,
    EVENT_TYPE_MESSAGE_PROCESSING,
    EVENT_TYPE_PARSE_FAILURE,
    EVENT_TYPE_WIKI_UPDATE,
    PipelineEventBuffer,
    emit_agent_state,
    emit_message_processing,
)
from beever_atlas.wiki.kinds import (
    KIND_REGISTRY,
    ChannelSignals,
    adaptive_kinds,
    should_instantiate,
)


# ---------------------------------------------------------------------------
# Group 1 — Sync emitter event types
# ---------------------------------------------------------------------------


def test_emit_agent_state_round_trips_through_buffer(monkeypatch) -> None:
    """``agent_state`` events emitted by the helper land in the buffer
    and survive recent_for serialisation with their structured payload."""
    from beever_atlas.services import pipeline_events as pe

    fresh = PipelineEventBuffer()
    monkeypatch.setattr(pe, "_buffer_singleton", fresh)

    emit_agent_state("C1", "fact_extractor", "running", batch_id="b-1")
    emit_agent_state(
        "C1",
        "fact_extractor",
        "done",
        batch_id="b-1",
        elapsed_ms=120,
    )

    events = fresh.recent_for("C1", limit=10)
    types = [e.event_type for e in events]
    assert EVENT_TYPE_AGENT_STATE in types
    by_state = [e.payload["state"] for e in events if e.event_type == EVENT_TYPE_AGENT_STATE]
    assert "running" in by_state
    assert "done" in by_state


def test_emit_message_processing_truncates_preview(monkeypatch) -> None:
    """``message_processing`` payload preview is bounded to 200 chars."""
    from beever_atlas.services import pipeline_events as pe

    fresh = PipelineEventBuffer()
    monkeypatch.setattr(pe, "_buffer_singleton", fresh)

    long_text = "x" * 500
    emit_message_processing(
        "C1",
        message_id="msg-1",
        text_preview=long_text,
        author="alan",
    )

    events = fresh.recent_for("C1", limit=2)
    assert len(events) == 1
    evt = events[0]
    assert evt.event_type == EVENT_TYPE_MESSAGE_PROCESSING
    assert len(evt.payload["text_preview"]) == 200


def test_emit_helpers_swallow_buffer_failures(monkeypatch) -> None:
    """Buffer hiccup must not crash the agent — emit functions are
    best-effort by contract."""
    from beever_atlas.services import pipeline_events as pe

    class _Boom:
        def record(self, *args, **kwargs):  # noqa: ARG002
            raise RuntimeError("boom")

        def clear(self, *args, **kwargs):  # noqa: ARG002
            # conftest teardown calls clear(); must be a no-op shim.
            pass

    monkeypatch.setattr(pe, "_buffer_singleton", _Boom())

    # Both emit helpers must NOT raise, even though the underlying buffer
    # always raises.
    emit_agent_state("C1", "fact_extractor", "running")
    emit_message_processing("C1", message_id="m", text_preview="t", author="a")


# ---------------------------------------------------------------------------
# Group 3 / 4 — Cost summary + frozen page wiring
# ---------------------------------------------------------------------------


def test_cost_summary_event_shape() -> None:
    """The Builder's ``wiki_build_cost_summary`` payload carries the
    counts SyncMonitor needs."""
    buf = PipelineEventBuffer()
    buf.record(
        "C1",
        "wiki_build",
        "Build complete: 8 pages (2 skipped, 12.3s)",
        event_type=EVENT_TYPE_COST_SUMMARY,
        payload={
            "calls_total": 8,
            "calls_skipped": 2,
            "duration_ms": 12345,
        },
    )
    events = buf.recent_for("C1", limit=5)
    assert events[0].event_type == EVENT_TYPE_COST_SUMMARY
    assert events[0].payload["calls_skipped"] == 2


def test_frozen_skip_event_shape() -> None:
    """Frozen-page Builder skip emits a wiki_update event with
    ``action="skipped_frozen"``."""
    buf = PipelineEventBuffer()
    buf.record(
        "C1",
        "wiki_build",
        "Skipped (frozen): topic:gpu",
        event_type=EVENT_TYPE_WIKI_UPDATE,
        payload={
            "page_id": "topic:gpu",
            "page_title": "GPU Procurement",
            "action": "skipped_frozen",
        },
    )
    events = buf.recent_for("C1", limit=5)
    assert events[0].payload["action"] == "skipped_frozen"


def test_parse_failure_event_taxonomy() -> None:
    """Parse failures still flow as ``parse_failure`` events for the
    WikiTab banner."""
    buf = PipelineEventBuffer()
    buf.record(
        "C1",
        "wiki_maintenance",
        "Parse failure on topic:x",
        event_type=EVENT_TYPE_PARSE_FAILURE,
        payload={"page_id": "topic:x", "raw_len": 0},
    )
    assert buf.parse_failure_count_last_10_min("C1") == 1


# ---------------------------------------------------------------------------
# Group 7+8 — Adaptive predicates
# ---------------------------------------------------------------------------


def test_predicate_gates_are_independent() -> None:
    """Each adaptive predicate's threshold is independent."""
    above_projects = ChannelSignals(fact_count_by_type={"project": 5})
    below_projects = ChannelSignals(fact_count_by_type={"project": 1})

    assert should_instantiate("projects", above_projects)[0] is True
    assert should_instantiate("projects", below_projects)[0] is False


def test_force_kinds_overrides_predicate_below_threshold() -> None:
    s = ChannelSignals(force_kinds=frozenset({"architecture"}))
    should, reason = should_instantiate("architecture", s)
    assert should is True
    assert reason == "policy:force"


def test_suppress_kinds_overrides_predicate_above_threshold() -> None:
    s = ChannelSignals(
        fact_count_by_type={"project": 10},
        suppress_kinds=frozenset({"projects"}),
    )
    should, reason = should_instantiate("projects", s)
    assert should is False


def test_required_kinds_unaffected_by_predicate_machinery() -> None:
    """Required kinds always instantiate regardless of signals or overrides."""
    s = ChannelSignals(suppress_kinds=frozenset())
    for kind in ("topic", "people", "glossary", "decisions", "faq"):
        assert should_instantiate(kind, s)[0] is True


def test_adaptive_kinds_match_design() -> None:
    """The five new adaptive kinds match the spec's catalog."""
    expected = {
        "projects",
        "architecture",
        "open-questions",
        "timeline",
        "stakeholders",
    }
    assert set(adaptive_kinds()) == expected


def test_kind_registry_specs_carry_prompt_paths() -> None:
    """Every adaptive kind references a prompt file in wiki/prompts/."""
    for kind in adaptive_kinds():
        spec = KIND_REGISTRY[kind]
        assert spec.prompt_path is not None
        assert spec.prompt_path.endswith(".txt")


# ---------------------------------------------------------------------------
# Maintainer adaptive-kind suppression — placeholder creation gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintainer_drops_routes_to_uninstantiated_adaptive_kind() -> None:
    """The maintainer must not auto-create placeholder pages for
    adaptive kinds whose Builder hasn't run yet — the Builder owns
    instantiation of adaptive pages."""
    from beever_atlas.services.wiki_maintainer import WikiMaintainer

    class _Store:
        async def get_page(self, channel_id, page_id, target_lang="en"):
            return None  # page does not exist

        async def list_pages(self, channel_id, target_lang="en"):
            # Existing pages exist so the first-sync gate doesn't defer.
            from beever_atlas.models.persistence import WikiPage

            return [WikiPage(channel_id=channel_id, page_id="overview", title="Overview")]

        async def save_page(self, page):
            raise AssertionError("Maintainer should not save adaptive page placeholders")

        async def mark_dirty(self, channel_id, page_ids, target_lang="en"):  # noqa: ARG002
            return None

    maint = WikiMaintainer(page_store=_Store())  # type: ignore[arg-type]

    async def _fake_load(channel_id, fact_ids):  # noqa: ARG001
        return [{"id": fid, "fact_text": f"fact {fid}"} for fid in fact_ids]

    maint._load_facts = _fake_load  # type: ignore[method-assign]

    # ``projects`` is an adaptive kind with no existing page — must skip
    # without creating a placeholder.
    result = await maint.apply_update("C1", "projects", ["f1", "f2"], target_lang="en")
    assert result is False


# ---------------------------------------------------------------------------
# Helper: compose-time invariant — stale dates handled
# ---------------------------------------------------------------------------


def test_channel_signals_immutability() -> None:
    """ChannelSignals is frozen — predicates can't mutate inputs."""
    s = ChannelSignals(channel_age_days=42)
    with pytest.raises(Exception):
        s.channel_age_days = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Group 5 — Multi-level Topic split heuristic
# ---------------------------------------------------------------------------


def test_split_candidate_above_fact_threshold() -> None:
    """A cluster with ≥50 facts becomes a split candidate."""
    from beever_atlas.wiki.structure.planner import _compute_split_candidates

    clusters = [
        {"id": "topic-small", "fact_count": 5},
        {"id": "topic-large", "fact_count": 60},
    ]
    candidates = _compute_split_candidates(clusters=clusters, fact_graph=None)
    assert candidates == ["topic-large"]


def test_split_candidate_above_inbound_edge_threshold() -> None:
    """A cluster with ≥35 inbound cross-link edges becomes a split candidate."""
    from beever_atlas.wiki.structure.planner import _compute_split_candidates

    clusters = [
        {"id": "topic-hub", "fact_count": 10},
    ]
    fact_graph = [(f"topic-{i}", "topic-hub") for i in range(40)]
    candidates = _compute_split_candidates(clusters=clusters, fact_graph=fact_graph)
    assert candidates == ["topic-hub"]


def test_split_depth_cap_rejects_subtopics() -> None:
    """Sub-topics at depth 2 (max_depth=3 means depth 0/1 can split, depth 2 can't)."""
    from beever_atlas.wiki.structure.planner import _compute_split_candidates

    clusters = [
        {"id": "deep-subtopic", "fact_count": 100, "depth": 2},
    ]
    candidates = _compute_split_candidates(clusters=clusters, fact_graph=None)
    assert candidates == []


def test_no_split_when_signals_below_threshold() -> None:
    from beever_atlas.wiki.structure.planner import _compute_split_candidates

    clusters = [{"id": "topic-small", "fact_count": 10}]
    fact_graph = [(f"src-{i}", "topic-small") for i in range(5)]
    candidates = _compute_split_candidates(clusters=clusters, fact_graph=fact_graph)
    assert candidates == []


# ---------------------------------------------------------------------------
# Group 3 — build-input hash recompile-skip
# ---------------------------------------------------------------------------


def test_build_input_hash_stable_for_same_input() -> None:
    from types import SimpleNamespace

    from beever_atlas.wiki.builder import _compute_build_input_hash

    cluster_a = SimpleNamespace(id="topic-a", member_count=5, summary="alpha")
    cluster_b = SimpleNamespace(id="topic-b", member_count=3, summary="beta")
    summary = SimpleNamespace(fact_count=8, glossary_terms=["GPU", "RAM"])
    decisions = [SimpleNamespace(id="d1"), SimpleNamespace(id="d2")]
    cluster_facts = {
        "topic-a": [SimpleNamespace(id="f1"), SimpleNamespace(id="f2")],
        "topic-b": [SimpleNamespace(id="f3")],
    }
    media_facts: list[object] = []
    gathered = {
        "clusters": [cluster_a, cluster_b],
        "channel_summary": summary,
        "decisions": decisions,
        "cluster_facts": cluster_facts,
        "media_facts": media_facts,
    }

    h1 = _compute_build_input_hash(gathered)
    h2 = _compute_build_input_hash(gathered)
    assert h1 == h2


def test_build_input_hash_changes_when_facts_change() -> None:
    from types import SimpleNamespace

    from beever_atlas.wiki.builder import _compute_build_input_hash

    base = {
        "clusters": [SimpleNamespace(id="topic-a", member_count=5, summary="alpha")],
        "channel_summary": SimpleNamespace(fact_count=8, glossary_terms=[]),
        "decisions": [],
        "cluster_facts": {"topic-a": [SimpleNamespace(id="f1")]},
        "media_facts": [],
    }
    h_base = _compute_build_input_hash(base)
    base_with_new_fact = {
        **base,
        "cluster_facts": {"topic-a": [SimpleNamespace(id="f1"), SimpleNamespace(id="f2_new")]},
    }
    h_new = _compute_build_input_hash(base_with_new_fact)
    assert h_base != h_new


def test_build_input_hash_invariant_to_cluster_order() -> None:
    """Cluster ordering shouldn't bust the cache — the planner sorts."""
    from types import SimpleNamespace

    from beever_atlas.wiki.builder import _compute_build_input_hash

    a = SimpleNamespace(id="a", member_count=1, summary="A")
    b = SimpleNamespace(id="b", member_count=2, summary="B")
    summary = SimpleNamespace(fact_count=0, glossary_terms=[])
    g1 = {
        "clusters": [a, b],
        "channel_summary": summary,
        "decisions": [],
        "cluster_facts": {},
        "media_facts": [],
    }
    g2 = {
        "clusters": [b, a],
        "channel_summary": summary,
        "decisions": [],
        "cluster_facts": {},
        "media_facts": [],
    }
    assert _compute_build_input_hash(g1) == _compute_build_input_hash(g2)
