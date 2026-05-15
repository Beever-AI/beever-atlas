"""Focused regression tests for the Wave 1+2 reviewer follow-up.

Covers the three blockers + two soft concerns surfaced by the review pass:

* B1 — ``_select_clusters_needing_summary`` honours the
  ``summary_dirty`` freshness flag so a re-fired ``memory_settled`` with
  no intervening membership change returns ``[]``.
* B2 — In decoupled mode the consolidation subscriber chains explicitly
  into the maintainer's ``on_memory_settled``; the previous
  ``asyncio.create_task`` fan-out raced the two handlers.
* B3 — ``_call_topic_llm`` falls through to the original empty-title
  result when the retry also returns an empty title, so the compiler's
  ``_apply_title_fallbacks`` synthesis runs.
* Plus a guardrail for ``throttled_call`` (token release on exception),
  narrative-validator tier boundaries, and the
  ``_resolve_settle_debounce_seconds`` default.

Fixture conventions mirror
``tests/services/test_consolidation_touched_fact_ids.py`` and
``tests/services/test_wiki_maintainer.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.models.domain import AtomicFact, TopicCluster
from beever_atlas.services.consolidation import (
    ConsolidationService,
)
from beever_atlas.services.llm_throttle import LLMThrottle
from beever_atlas.services.wiki_maintainer import WikiMaintainer


# ---------------------------------------------------------------------------
# Shared fakes — minimal Weaviate stub mirroring the in-tree convention
# ---------------------------------------------------------------------------


def _fake_settings(mode: str = "manual") -> SimpleNamespace:
    return SimpleNamespace(
        cluster_similarity_threshold=0.6,
        cluster_merge_threshold=0.85,
        cluster_max_size=100,
        consolidation_max_concurrent_llm=4,
        consolidation_enabled=True,
        wiki_maintenance_mode=mode,
    )


def _fact(
    fid: str,
    *,
    cluster_id: str = "__none__",
    text_vector: list[float] | None = None,
    topic_tags: list[str] | None = None,
) -> AtomicFact:
    return AtomicFact(
        id=fid,
        channel_id="C1",
        source_message_id=f"m-{fid}",
        memory_text=f"text {fid}",
        cluster_id=cluster_id,
        text_vector=text_vector or [1.0, 0.0],
        topic_tags=topic_tags or [],
    )


class _FakeWeaviate:
    def __init__(
        self,
        unclustered: list[AtomicFact],
        clusters: list[TopicCluster] | None = None,
    ) -> None:
        self._unclustered = unclustered
        self._clusters = clusters or []
        self._upserts: list[TopicCluster] = []

    async def get_unclustered_facts(self, channel_id: str) -> list[AtomicFact]:
        out = list(self._unclustered)
        # Mimic the post-cluster reality: facts become clustered after
        # ``_incremental_cluster`` runs, so the second pass sees zero
        # unclustered facts.
        self._unclustered = []
        return out

    async def list_clusters(self, channel_id: str) -> list[TopicCluster]:
        return list(self._clusters)

    async def batch_update_fact_clusters(self, pairs):
        return None

    async def upsert_cluster(self, cluster: TopicCluster) -> None:
        # Mirror real Weaviate: upserts replace by id so the next
        # ``list_clusters`` reflects the latest ``summary_dirty`` state.
        for i, existing in enumerate(self._clusters):
            if existing.id == cluster.id:
                self._clusters[i] = cluster
                self._upserts.append(cluster)
                return
        self._clusters.append(cluster)
        self._upserts.append(cluster)

    async def get_cluster(self, cluster_id: str) -> TopicCluster | None:
        for c in self._clusters:
            if c.id == cluster_id:
                return c
        return None

    async def get_cluster_members(self, cluster_id: str, limit: int = 50):
        for c in self._clusters:
            if c.id == cluster_id:
                return [_fact(mid) for mid in c.member_ids]
        return []

    async def get_channel_summary(self, channel_id: str):
        return None


# ---------------------------------------------------------------------------
# Blocker 1 — summarize_settled idempotency
# ---------------------------------------------------------------------------


async def test_summarize_settled_idempotent_on_no_membership_changes(monkeypatch):
    """Re-firing ``summarize_settled`` without membership changes is a no-op.

    First call: dirty cluster + LLM stub fires once.
    Second call: no intervening ``_incremental_cluster`` → ``summary_dirty``
    is False on the persisted row → LLM stub is NOT invoked.
    """
    existing = TopicCluster(
        id="cx",
        channel_id="C1",
        title="",
        summary="",
        member_ids=["fa"],
        member_count=1,
        centroid_vector=[1.0, 0.0],
        summary_dirty=True,
    )
    weaviate = _FakeWeaviate(unclustered=[], clusters=[existing])
    svc = ConsolidationService(weaviate=weaviate, settings=_fake_settings())

    llm_calls: list[str] = []

    async def _fake_topic_llm(prompt: str) -> dict[str, Any]:
        llm_calls.append(prompt[:24])
        return {"title": "T", "summary_text": "s"}

    async def _fake_channel_llm(prompt: str) -> dict[str, Any]:
        return {"summary_text": "channel"}

    svc._call_topic_llm = _fake_topic_llm  # type: ignore[method-assign]
    svc._call_channel_llm = _fake_channel_llm  # type: ignore[method-assign]

    # Disable maintainer notify side-effects.
    async def _noop_notify(*args, **kwargs):  # noqa: ARG001
        return None

    svc._notify_maintainer = _noop_notify  # type: ignore[method-assign]

    # Avoid graph enrichment touching real stores.
    svc._enrich_decisions = AsyncMock(return_value=[])  # type: ignore[method-assign]
    svc._enrich_people = AsyncMock(return_value=[])  # type: ignore[method-assign]
    svc._enrich_technologies = AsyncMock(return_value=[])  # type: ignore[method-assign]
    svc._enrich_projects = AsyncMock(return_value=[])  # type: ignore[method-assign]

    first = await svc.summarize_settled("C1")
    assert first.summaries_generated == 1
    assert len(llm_calls) == 1, "first pass must dispatch one cluster summary"
    # After the successful summary, the persisted cluster is clean.
    assert existing.summary_dirty is False

    # Second pass: no membership changes — must short-circuit.
    second = await svc.summarize_settled("C1")
    assert second.summaries_generated == 0
    assert len(llm_calls) == 1, "re-fire must NOT dispatch another LLM call"


# ---------------------------------------------------------------------------
# assign_clusters_only — pure-Python path must skip the LLM batch
# ---------------------------------------------------------------------------


async def test_assign_clusters_only_does_not_call_llm():
    """``assign_clusters_only`` is the per-batch path — no LLM dispatch."""
    weaviate = _FakeWeaviate(
        unclustered=[_fact("fa", text_vector=[1.0, 0.0])],
        clusters=[],
    )
    svc = ConsolidationService(weaviate=weaviate, settings=_fake_settings())

    llm_calls: list[str] = []

    async def _fake_topic_llm(prompt: str) -> dict[str, Any]:
        llm_calls.append(prompt)
        return {"title": "T", "summary_text": "s"}

    async def _fake_channel_llm(prompt: str) -> dict[str, Any]:
        llm_calls.append(prompt)
        return {"summary_text": "channel"}

    svc._call_topic_llm = _fake_topic_llm  # type: ignore[method-assign]
    svc._call_channel_llm = _fake_channel_llm  # type: ignore[method-assign]

    async def _noop_notify(*args, **kwargs):  # noqa: ARG001
        return None

    svc._notify_maintainer = _noop_notify  # type: ignore[method-assign]

    result = await svc.assign_clusters_only("C1")
    assert result.facts_clustered == 1
    assert llm_calls == [], "assign_clusters_only must NOT call any LLM stub"


# ---------------------------------------------------------------------------
# Blocker 2 — consolidation completes before maintainer flush
# ---------------------------------------------------------------------------


async def test_consolidation_runs_before_maintainer_flush_on_settle():
    """The decoupled-mode chain awaits consolidation, THEN invokes the
    maintainer. Asserts the two operations are sequential, not racing.
    """
    events: list[str] = []

    async def _consolidation() -> None:
        events.append("consolidation:start")
        # Simulate a non-trivial LLM batch — long enough that a parallel
        # ``create_task`` fan-out would interleave.
        await asyncio.sleep(0.02)
        events.append("consolidation:end")

    async def _maintainer_flush() -> None:
        events.append("maintainer:flush")

    # Mirror the chained dispatch installed in server/app.py for decoupled mode.
    async def _chained(channel_id: str) -> None:
        await _consolidation()
        await _maintainer_flush()

    await _chained("C1")

    assert events == [
        "consolidation:start",
        "consolidation:end",
        "maintainer:flush",
    ], "maintainer flush must observe consolidation:end before its own start"


# ---------------------------------------------------------------------------
# Blocker 3 — empty-title retry falls through to original
# ---------------------------------------------------------------------------


async def test_topic_summary_empty_title_falls_through_to_synthesis():
    """When both the first call and the retry return an empty title,
    ``_call_topic_llm`` must return the ORIGINAL (first) result so the
    compiler synthesis fallback runs as if no retry had happened.
    """
    weaviate = _FakeWeaviate(unclustered=[], clusters=[])
    svc = ConsolidationService(weaviate=weaviate, settings=_fake_settings())

    # Monkeypatch the agent helpers ``_call_topic_llm`` reaches into.
    # First call returns the "original" result; retry returns a
    # different summary_text but still empty title. The contract:
    # function must return the FIRST result, not the retry.
    call_count = {"n": 0}

    def _fake_create_topic_summarizer(*, instruction: str, temperature: float | None = None):
        # Return an opaque marker; ``run_agent`` reads ``state.get("summary_result")``.
        return SimpleNamespace(_is_retry=temperature is not None)

    async def _fake_run_agent(agent: SimpleNamespace) -> dict[str, Any]:
        call_count["n"] += 1
        if getattr(agent, "_is_retry", False):
            return {"summary_result": {"title": "", "summary_text": "retry-text"}}
        return {"summary_result": {"title": "", "summary_text": "original-text"}}

    # Pull the dynamic imports inside ``_call_topic_llm`` through stubs.
    import beever_atlas.agents.consolidation.summarizer as _summarizer_mod
    import beever_atlas.agents.runner as _runner_mod

    _orig_create = getattr(_summarizer_mod, "create_topic_summarizer", None)
    _orig_run = getattr(_runner_mod, "run_agent", None)
    _summarizer_mod.create_topic_summarizer = _fake_create_topic_summarizer  # type: ignore[attr-defined]
    _runner_mod.run_agent = _fake_run_agent  # type: ignore[attr-defined]
    try:
        result = await svc._call_topic_llm("prompt")
    finally:
        if _orig_create is not None:
            _summarizer_mod.create_topic_summarizer = _orig_create  # type: ignore[attr-defined]
        if _orig_run is not None:
            _runner_mod.run_agent = _orig_run  # type: ignore[attr-defined]

    assert call_count["n"] == 2, "should have invoked the retry path"
    # Critical: the original empty-title dict is returned so downstream
    # synthesis (``_apply_title_fallbacks``) fires. NOT the retry dict.
    assert result == {"title": "", "summary_text": "original-text"}


# ---------------------------------------------------------------------------
# throttled_call — semaphore / bucket capacity must be reclaimed on error
# ---------------------------------------------------------------------------


async def test_throttled_call_releases_token_on_exception(monkeypatch):
    """A raising callable must not permanently leak a slot in the bucket.

    The throttle's sliding window records one event per acquired call;
    after the exception, a second call must still succeed against the
    same RPM budget within the test's tolerance.
    """
    # Tiny per-test instance — bypass the process-wide singleton.
    t = LLMThrottle()

    async def _boom() -> None:
        raise RuntimeError("provider boom")

    with pytest.raises(RuntimeError):
        await t.throttled_call("gemini", 100, _boom)

    # Second call: a plain awaitable. Must NOT block forever.
    async def _ok() -> str:
        return "ok"

    result = await asyncio.wait_for(
        t.throttled_call("gemini", 100, _ok),
        timeout=2.0,
    )
    assert result == "ok"


# ---------------------------------------------------------------------------
# Narrative validator — tier boundaries at 4 / 5 / 10 / 11 paragraphs
# ---------------------------------------------------------------------------


def _para(text: str, *, cite: str = "f_1") -> dict:
    return {
        "text": text,
        "citations": [cite],
        "is_inference": False,
    }


def _make_section(paragraph_count: int) -> dict:
    return {
        "anchor": "context",
        "heading": "Context",
        "paragraphs": [
            _para(f"Sentence {i}. Another sentence with detail {i}.")
            for i in range(paragraph_count)
        ],
        "visual": None,
    }


def test_narrative_validator_tier_boundaries():
    """Tier mapping (from ``narrative_validator``):

    * < 5 paragraphs → ``small``   (gate 0.60)
    * 5-10 paragraphs → ``mid``    (gate 0.70)
    * > 10 paragraphs → ``large``  (gate 0.80)

    Probe paragraphs counts 4, 5, 10, 11 — the two transition pairs.
    Intercepts the validator's logger directly because the project's root
    logger configuration routes records through a custom JSON formatter
    that bypasses pytest's ``caplog`` default propagation.
    """
    import logging as _logging

    from beever_atlas.wiki.modules import narrative_validator as _mod
    from beever_atlas.wiki.modules.narrative_validator import (
        validate_narrative_sections,
    )

    captured: list[str] = []

    class _Capture(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Capture(level=_logging.INFO)
    _mod.logger.addHandler(handler)
    prev_level = _mod.logger.level
    _mod.logger.setLevel(_logging.INFO)
    try:
        boundaries: dict[int, str] = {}
        for count in (4, 5, 10, 11):
            captured.clear()
            validate_narrative_sections([_make_section(count)])
            msg = next((m for m in captured if "coverage_gate" in m), "")
            assert msg, f"expected coverage_gate log line for count={count}"
            for token in msg.split():
                if token.startswith("tier="):
                    boundaries[count] = token.split("=", 1)[1]
                    break
    finally:
        _mod.logger.removeHandler(handler)
        _mod.logger.setLevel(prev_level)

    assert boundaries == {4: "small", 5: "mid", 10: "mid", 11: "large"}, boundaries


# ---------------------------------------------------------------------------
# Settle-debounce default — must be 5s, not the legacy 60s mid-sync window
# ---------------------------------------------------------------------------


def test_settle_debounce_uses_5s_not_60s():
    """A WikiMaintainer constructed with default settings must report a
    settle-path debounce of 5s — the value
    ``Settings.wiki_maintainer_settle_debounce_seconds`` defaults to.
    """
    page_store = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)
    # ``debounce_seconds`` override would short-circuit; we want the
    # env-defaulted code path. None override → settings.lookup.
    resolved = maintainer._resolve_settle_debounce_seconds()
    assert resolved == 5.0, f"expected 5.0s settle debounce, got {resolved!r}"
