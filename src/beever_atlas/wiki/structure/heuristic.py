"""Deterministic candidate-folder generation from cheap signals.

Three signals decide whether two topic clusters belong in the same
folder:

  1. **Multi-word common prefix** — "Beever Atlas Documentation" and
     "Beever Atlas GitHub Repository" share the 2-word prefix "Beever
     Atlas". Threshold: ≥2 words. Captures the same case the sidebar's
     historical client-side fold did, but at the data layer.

  2. **Entity overlap (Jaccard)** — clusters whose ``key_entities``
     overlap above 0.4 likely cover the same domain. Threshold: ≥0.4.
     Captures cross-prefix groupings ("JWT auth", "rate limiting",
     "RBAC" all touching the auth-service entity).

  3. **Co-citation count** — number of (fact_a, fact_b) pairs in the
     fact graph where one cites the other across two clusters.
     Threshold: ≥5. Captures topics whose facts genuinely reference
     each other (consolidator-emitted relationships).

Pairs above ANY threshold get joined into the same candidate group via
union-find — so two clusters in the same group share at least one
strong signal with at least one other group member.

The output is the LLM gate's prior: the planner prompt receives the
candidate group ids alongside the cluster index and the LLM mostly
confirms / refines / renames groups rather than inventing them from
scratch. Pure deterministic — same input always yields the same
candidate groups.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# Tunable thresholds. Kept module-level constants (not Settings) so unit
# tests can monkeypatch them; production calls use the defaults below.
PREFIX_MIN_WORDS = 2
ENTITY_JACCARD_THRESHOLD = 0.4
CO_CITATION_THRESHOLD = 5


@dataclass(frozen=True)
class HeuristicGroup:
    """A candidate folder boundary discovered by the heuristic pass.

    ``cluster_ids`` is the unordered set of cluster ids that share at
    least one strong signal with another member. ``signals`` carries
    the per-pair evidence so the LLM gate (and anyone debugging the
    output) can see why these clusters got bundled together.
    """

    cluster_ids: frozenset[str]
    signals: dict[str, list[str]] = field(default_factory=dict)
    """Map ``"prefix" | "entity" | "co_citation"`` → list of human-
    readable signal descriptions (e.g. ``"shared prefix 'Beever Atlas'"``).
    Used for ``rationale`` strings in the LLM prompt and for
    operator-facing debug logs."""


@dataclass
class HeuristicCandidates:
    """Result of the deterministic candidate-discovery pass."""

    groups: list[HeuristicGroup]
    """Candidate folders. Singletons (clusters that share no signal with
    any other) are NOT emitted here — the planner treats absent
    clusters as flat leaves."""

    @classmethod
    def compute(
        cls,
        clusters: list[dict[str, Any]],
        fact_graph: list[tuple[str, str]] | None = None,
    ) -> HeuristicCandidates:
        """Walk all cluster pairs, collect signals, union-find groups.

        ``clusters`` is a list of dicts with at least:
          - ``id`` (str)
          - ``title`` (str)
          - ``key_entities`` (list of str OR list of dicts with ``name``)

        ``fact_graph`` is an optional list of ``(cluster_id_a,
        cluster_id_b)`` tuples representing co-citation edges across
        clusters. The caller (``WikiBuilder.gather`` consumer) builds
        this from the consolidator's relationship output.

        O(N²) in cluster count, which is fine for the typical N≤100
        wiki sizes and avoids needing an index. Returns deterministic
        output: same input always produces the same groups in the same
        order (ordered by lexicographic min cluster_id).
        """
        if not clusters:
            return cls(groups=[])

        # Build a quick id → cluster lookup for the inner loop.
        by_id: dict[str, dict[str, Any]] = {c["id"]: c for c in clusters if "id" in c}
        ids = sorted(by_id.keys())

        # Co-citation count between any two clusters: count of fact_graph
        # edges where one endpoint is in cluster A's facts and the other
        # in cluster B's. Caller passes pre-aggregated (cluster_a,
        # cluster_b) pairs so we don't need to look up fact membership.
        co_count: dict[frozenset[str], int] = {}
        for src, dst in fact_graph or ():
            if src == dst:
                continue
            key = frozenset({src, dst})
            co_count[key] = co_count.get(key, 0) + 1

        # Per-pair signal accumulator, then union-find.
        # Using parallel arrays + a dict-based DSU keeps the implementation
        # readable; performance is fine for cluster counts in the hundreds.
        parent: dict[str, str] = {cid: cid for cid in ids}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        # signals_per_pair maps the union-find root → signal type → descriptions
        # We collect signals at the pair level then merge into groups at the end.
        pair_signals: dict[frozenset[str], dict[str, list[str]]] = {}

        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a_id, b_id = ids[i], ids[j]
                a, b = by_id[a_id], by_id[b_id]
                signals: dict[str, list[str]] = {}

                # Signal 1: multi-word common prefix.
                prefix = _common_word_prefix(a.get("title", ""), b.get("title", ""))
                if _word_count(prefix) >= PREFIX_MIN_WORDS:
                    signals.setdefault("prefix", []).append(f"shared prefix '{prefix}'")

                # Signal 2: entity Jaccard.
                jacc = _entity_jaccard(a.get("key_entities"), b.get("key_entities"))
                if jacc >= ENTITY_JACCARD_THRESHOLD:
                    signals.setdefault("entity", []).append(f"entity overlap {jacc:.2f}")

                # Signal 3: co-citation density.
                cocnt = co_count.get(frozenset({a_id, b_id}), 0)
                if cocnt >= CO_CITATION_THRESHOLD:
                    signals.setdefault("co_citation", []).append(f"{cocnt} cross-cited facts")

                if signals:
                    pair_signals[frozenset({a_id, b_id})] = signals
                    union(a_id, b_id)

        # Materialize groups from the DSU. Skip singletons.
        members_by_root: dict[str, set[str]] = {}
        for cid in ids:
            members_by_root.setdefault(find(cid), set()).add(cid)

        groups: list[HeuristicGroup] = []
        for root in sorted(members_by_root):
            members = members_by_root[root]
            if len(members) < 2:
                continue
            # Aggregate signals across all member pairs into the group.
            agg: dict[str, list[str]] = {}
            for pair, sigs in pair_signals.items():
                if pair.issubset(members):
                    for kind, descs in sigs.items():
                        agg.setdefault(kind, []).extend(descs)
            groups.append(HeuristicGroup(cluster_ids=frozenset(members), signals=agg))

        return cls(groups=groups)


def _word_count(s: str) -> int:
    return sum(1 for w in s.split() if w)


def _common_word_prefix(a: str, b: str) -> str:
    wa = a.split()
    wb = b.split()
    out: list[str] = []
    for x, y in zip(wa, wb):
        if x.lower() == y.lower():
            out.append(x)
        else:
            break
    return " ".join(out)


def _entity_jaccard(
    a: Iterable[Any] | None,
    b: Iterable[Any] | None,
) -> float:
    """Jaccard similarity of two entity collections.

    Accepts either ``list[str]`` (entity names) or ``list[dict]`` with a
    ``name`` key (the consolidator's ``key_entities`` shape). Empty
    sides yield 0.0 (not 1.0 — empty-empty match shouldn't count).
    """
    sa = _entity_names(a)
    sb = _entity_names(b)
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    if not union:
        return 0.0
    return len(inter) / len(union)


def _entity_names(entities: Iterable[Any] | None) -> set[str]:
    if not entities:
        return set()
    out: set[str] = set()
    for e in entities:
        if isinstance(e, str):
            if e.strip():
                out.add(e.strip().lower())
        elif isinstance(e, dict):
            name = e.get("name") or e.get("entity_name") or ""
            if isinstance(name, str) and name.strip():
                out.add(name.strip().lower())
    return out


__all__ = ["HeuristicGroup", "HeuristicCandidates"]
