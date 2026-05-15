"""Tests for the deterministic candidate-folder heuristic.

Covers the spec's Heuristic stage: prefix similarity, entity Jaccard,
co-citation density. Each signal alone should produce a candidate
group; combinations should still produce a single transitively-closed
group via union-find.
"""

from __future__ import annotations

from beever_atlas.wiki.structure.heuristic import (
    HeuristicCandidates,
    PREFIX_MIN_WORDS,
    ENTITY_JACCARD_THRESHOLD,
    CO_CITATION_THRESHOLD,
)


def _cluster(cid: str, title: str, *, entities: list[str] | None = None) -> dict:
    return {
        "id": cid,
        "title": title,
        "key_entities": entities or [],
        "summary": "",
        "member_count": 0,
    }


def test_no_signals_emits_no_groups() -> None:
    """Two completely unrelated clusters → no candidate group."""
    clusters = [
        _cluster("a", "Authentication"),
        _cluster("b", "Marketing Funnel"),
    ]
    out = HeuristicCandidates.compute(clusters)
    assert out.groups == []


def test_prefix_signal_groups_two_clusters() -> None:
    """Two adjacent topics sharing a 2-word prefix get bundled."""
    clusters = [
        _cluster("a", "Beever Atlas Documentation"),
        _cluster("b", "Beever Atlas GitHub Repository"),
        _cluster("c", "Marketing Funnel"),  # unrelated
    ]
    out = HeuristicCandidates.compute(clusters)
    assert len(out.groups) == 1
    assert out.groups[0].cluster_ids == frozenset({"a", "b"})
    # Signal kind recorded so the LLM gate can rationalize.
    assert "prefix" in out.groups[0].signals


def test_one_word_prefix_does_not_trigger() -> None:
    """A 1-word prefix is below threshold and must NOT group."""
    assert PREFIX_MIN_WORDS == 2
    clusters = [
        _cluster("a", "Beever Project Alpha"),
        _cluster("b", "Beever Roadmap"),
    ]
    out = HeuristicCandidates.compute(clusters)
    assert out.groups == []


def test_entity_jaccard_signal() -> None:
    """Two clusters sharing >40% entities get bundled even without prefix."""
    assert ENTITY_JACCARD_THRESHOLD == 0.4
    clusters = [
        _cluster("a", "JWT Login", entities=["auth-service", "alice", "bob"]),
        _cluster("b", "Token Refresh Flow", entities=["auth-service", "alice", "carol"]),
    ]
    # |inter| = 2 (auth-service, alice), |union| = 4 → jaccard = 0.5 ≥ 0.4
    out = HeuristicCandidates.compute(clusters)
    assert len(out.groups) == 1
    assert out.groups[0].cluster_ids == frozenset({"a", "b"})
    assert "entity" in out.groups[0].signals


def test_entity_jaccard_below_threshold() -> None:
    """Below 40% entity overlap → no group."""
    clusters = [
        _cluster("a", "Topic A", entities=["x", "y", "z"]),
        _cluster("b", "Topic B", entities=["y", "p", "q"]),
    ]
    # |inter| = 1, |union| = 5 → jaccard = 0.2 < 0.4
    out = HeuristicCandidates.compute(clusters)
    assert out.groups == []


def test_co_citation_signal() -> None:
    """Cross-cluster fact references above threshold trigger grouping."""
    assert CO_CITATION_THRESHOLD == 5
    clusters = [
        _cluster("a", "Topic A"),
        _cluster("b", "Topic B"),
    ]
    fact_graph = [("a", "b")] * 6  # 6 edges → above threshold
    out = HeuristicCandidates.compute(clusters, fact_graph=fact_graph)
    assert len(out.groups) == 1
    assert "co_citation" in out.groups[0].signals


def test_transitive_closure_via_union_find() -> None:
    """A→B (prefix) and B→C (entity) bundles all three even though A and C share nothing directly."""
    clusters = [
        _cluster("a", "Beever Atlas Auth", entities=["x"]),
        _cluster("b", "Beever Atlas Sync", entities=["alice", "bob"]),
        _cluster("c", "Topic C", entities=["alice", "bob"]),  # entity overlap with b
    ]
    out = HeuristicCandidates.compute(clusters)
    assert len(out.groups) == 1
    assert out.groups[0].cluster_ids == frozenset({"a", "b", "c"})


def test_singleton_clusters_are_not_emitted_as_groups() -> None:
    """A single cluster with no neighbours is NOT a candidate."""
    clusters = [_cluster("solo", "Solo Topic")]
    out = HeuristicCandidates.compute(clusters)
    assert out.groups == []


def test_dict_entities_supported() -> None:
    """key_entities can also be a list of {name: str} dicts."""
    clusters = [
        _cluster(
            "a",
            "Topic A",
            entities=[{"name": "Alice"}, {"name": "Bob"}],
        ),
        _cluster(
            "b",
            "Topic B",
            entities=[{"name": "Alice"}, {"name": "Bob"}],
        ),
    ]
    out = HeuristicCandidates.compute(clusters)  # type: ignore[arg-type]
    assert len(out.groups) == 1


def test_empty_input_returns_empty_candidates() -> None:
    out = HeuristicCandidates.compute([])
    assert out.groups == []
