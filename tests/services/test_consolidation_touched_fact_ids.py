"""Tests for ``ConsolidationResult.touched_fact_ids`` (close-the-soak-loop §2).

Covers:
  * 2.5 — ``_incremental_cluster`` populates ``touched_fact_ids`` with the
    union of every member fact_id of every touched cluster (created OR
    updated).
  * 2.6 — Dedup: a fact whose cluster is both created and re-summarized
    appears exactly once.
  * 2.7 — Empty consolidation → empty list.
  * 2.8 — Manual-mode regression fix: the WikiMaintainer marks pages dirty
    in response to a non-empty ``touched_fact_ids`` payload (the legacy
    ``mark_all_stale`` behaviour, restored).
  * 2.9 — Auto-mode forwarding: ``apply_update`` is invoked for the
    affected pages.

Convention: ``pyproject.toml`` sets ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock


from beever_atlas.models.domain import AtomicFact, TopicCluster
from beever_atlas.services.consolidation import (
    ConsolidationResult,
    ConsolidationService,
)
from beever_atlas.services import wiki_maintainer as wm_mod
from beever_atlas.services.wiki_maintainer import WikiMaintainer


# ---------------------------------------------------------------------------
# Fixtures — minimal Weaviate stub
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
    entity_tags: list[str] | None = None,
    fact_type: str = "observation",
) -> AtomicFact:
    return AtomicFact(
        id=fid,
        channel_id="C1",
        source_message_id=f"m-{fid}",
        memory_text=f"text {fid}",
        cluster_id=cluster_id,
        text_vector=text_vector or [1.0, 0.0],
        topic_tags=topic_tags or [],
        entity_tags=entity_tags or [],
        fact_type=fact_type,
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
        self._reassignments: list[tuple[str, str]] = []

    async def get_unclustered_facts(self, channel_id: str) -> list[AtomicFact]:
        return self._unclustered

    async def list_clusters(self, channel_id: str) -> list[TopicCluster]:
        return list(self._clusters)

    async def batch_update_fact_clusters(self, pairs):
        self._reassignments.extend(pairs)

    async def upsert_cluster(self, cluster: TopicCluster) -> None:
        self._upserts.append(cluster)

    async def get_cluster(self, cluster_id: str) -> TopicCluster | None:
        for c in self._clusters:
            if c.id == cluster_id:
                return c
        for c in self._upserts:
            if c.id == cluster_id:
                return c
        return None

    async def get_cluster_members(self, cluster_id: str, limit: int = 50):
        for c in (*self._clusters, *self._upserts):
            if c.id == cluster_id:
                return [_fact(mid) for mid in c.member_ids]
        return []


# ---------------------------------------------------------------------------
# 2.5 — _incremental_cluster populates union
# ---------------------------------------------------------------------------


async def test_incremental_cluster_populates_union():
    # Two new clusters created (fa, fb) + one existing cluster updated
    # (fc joining cluster X).
    existing = TopicCluster(
        id="cx",
        channel_id="C1",
        member_ids=["fc-prior"],
        member_count=1,
        centroid_vector=[1.0, 0.0],
    )
    weaviate = _FakeWeaviate(
        unclustered=[
            _fact("fa", text_vector=[1.0, 0.0]),
            _fact("fb", text_vector=[0.0, 1.0]),
            _fact("fc", text_vector=[1.0, 0.0]),
        ],
        clusters=[existing],
    )
    svc = ConsolidationService(weaviate=weaviate, settings=_fake_settings())
    result = ConsolidationResult(channel_id="C1")
    created, updated = await svc._incremental_cluster("C1", result)

    # fa/fc both vector [1,0] → join existing cluster X (sim 1.0).
    # fb [0,1] → orthogonal to X → creates a NEW cluster.
    # So: created = [<new fb cluster>], updated = [cx].
    assert len(created) == 1
    assert updated == ["cx"]
    # touched_fact_ids unions every member of every touched cluster:
    # X gets fc-prior + fa + fc; the new cluster gets fb.
    assert set(result.touched_fact_ids) == {"fc-prior", "fc", "fa", "fb"}


# ---------------------------------------------------------------------------
# 2.6 — Dedup across created + summary
# ---------------------------------------------------------------------------


async def test_touched_fact_ids_deduplicated_after_summary_pass(monkeypatch):
    # _incremental_cluster + _generate_summaries both touch the same
    # fact ids; the final list must contain each fact exactly once.
    weaviate = _FakeWeaviate(
        unclustered=[_fact("fa", text_vector=[1.0, 0.0])],
        clusters=[],
    )
    svc = ConsolidationService(weaviate=weaviate, settings=_fake_settings())

    # Stub the topic LLM so _summarize_one's success branch runs.
    async def _fake_topic_llm(prompt: str) -> dict[str, Any]:
        return {"title": "T", "summary_text": "s"}

    svc._call_topic_llm = _fake_topic_llm  # type: ignore[method-assign]

    result = ConsolidationResult(channel_id="C1")
    created, _ = await svc._incremental_cluster("C1", result)
    await svc._generate_summaries("C1", created, result)

    # 'fa' was the only fact; it appears once in created cluster + once
    # in summary. After both, the list must dedupe to a single entry.
    assert result.touched_fact_ids.count("fa") == 1


# ---------------------------------------------------------------------------
# 2.7 — Empty consolidation returns []
# ---------------------------------------------------------------------------


async def test_empty_consolidation_yields_empty_touched_list():
    weaviate = _FakeWeaviate(unclustered=[], clusters=[])
    svc = ConsolidationService(weaviate=weaviate, settings=_fake_settings())
    result = ConsolidationResult(channel_id="C1")
    created, updated = await svc._incremental_cluster("C1", result)
    assert created == []
    assert updated == []
    assert result.touched_fact_ids == []


# ---------------------------------------------------------------------------
# 2.8 — Manual-mode marks pages dirty (regression fix)
# ---------------------------------------------------------------------------


async def test_manual_mode_marks_pages_dirty_via_consolidation_hook(monkeypatch):
    """memory-then-wiki realignment: consolidation_complete is queue-only in
    both manual and auto mode. Manual-mode operators trigger flushes via the
    Maintain Wiki button (``maintain_now``); the consolidation hook itself
    no longer calls ``page_store.mark_dirty`` inline."""
    page_store = AsyncMock()
    page_store.mark_dirty = AsyncMock(return_value=0)
    maintainer = WikiMaintainer(page_store=page_store)

    async def _load(channel_id, fact_ids):
        return [
            {
                "id": "f10",
                "cluster_id": "auth",
                "entity_tags": ["alice"],
                "fact_type": "observation",
            }
        ]

    maintainer._load_facts = _load  # type: ignore[method-assign]
    monkeypatch.setattr(wm_mod, "_maintainer_instance", maintainer)

    counters = await maintainer.on_consolidation_complete("C1", fact_ids=["f10"], mode="manual")
    # Routing surfaces affected pages so observability counters work.
    assert counters["affected_pages"] >= 2
    # Critical: no inline mark_dirty — flush is deferred to memory_settled
    # (or operator-triggered maintain_now).
    page_store.mark_dirty.assert_not_called()


# ---------------------------------------------------------------------------
# 2.9 — Auto-mode no longer fires apply_update inline (memory-then-wiki
# realignment): the consolidation hook is now queue-only; the terminal
# flush is owned by ``memory_settled``.
# ---------------------------------------------------------------------------


async def test_auto_mode_does_not_fire_apply_update_via_consolidation_hook(monkeypatch):
    page_store = AsyncMock()
    page_store.mark_dirty = AsyncMock(return_value=0)
    maintainer = WikiMaintainer(page_store=page_store)

    apply_calls: list[tuple[str, str, list[str]]] = []

    async def _apply_update(*, channel_id, page_id, new_fact_ids, target_lang="en"):
        apply_calls.append((channel_id, page_id, list(new_fact_ids)))
        return True

    maintainer.apply_update = _apply_update  # type: ignore[method-assign]

    async def _load(channel_id, fact_ids):
        return [
            {
                "id": "f10",
                "cluster_id": "auth",
                "entity_tags": [],
                "fact_type": "decision",
            }
        ]

    maintainer._load_facts = _load  # type: ignore[method-assign]
    monkeypatch.setattr(wm_mod, "_maintainer_instance", maintainer)

    await maintainer.on_consolidation_complete("C1", fact_ids=["f10"], mode="auto")
    # Regression test for the mid-sync wiki rewrite bug: consolidation
    # MUST NOT trigger apply_update — only memory_settled may flush.
    assert apply_calls == []
    page_store.mark_dirty.assert_not_called()
