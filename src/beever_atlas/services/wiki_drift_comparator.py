"""Wiki page-voice drift A/B comparator.

When ``WIKI_DRIFT_AB=true``, every successful ``WikiMaintainer.apply_update``
ALSO computes the corresponding ``WikiBuilder.generate_wiki`` output for
the SAME page in parallel and emits a structured ``wiki_drift_report``
log line. The report scores the divergence between incremental and full-
regenerate outputs by Levenshtein distance on title + each section's
content_md, plus Jaccard on the section-id sets.

The pass criterion that gates flipping ``WIKI_MAINTENANCE_MODE=auto`` to
default ON is: median Levenshtein < 0.15 AND p95 < 0.30 across all
sections over a 2-week window across at least 3 channels. The comparator
emits the per-comparison metrics; aggregation lives in whatever log
analysis tooling consumes the structured logs.

Spec: ``openspec/changes/oss-redesign-production-wiring/specs/wiki-soak-instrumentation/``
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from beever_atlas.models.persistence import WikiPage

logger = logging.getLogger(__name__)


@dataclass
class DriftReport:
    """Single A/B comparison report.

    Field semantics:
      - Levenshtein distances are NORMALIZED to ``[0.0, 1.0]`` by dividing
        the raw edit count by ``max(len(a), len(b))``. 0.0 = identical;
        1.0 = entirely different.
      - ``levenshtein_section_p50`` / ``_p95`` are computed over the per-
        section distances (one per section that exists on the incremental
        page; sections missing on the regenerate side score 1.0).
      - ``section_id_jaccard`` is ``|A ∩ B| / |A ∪ B|`` — 1.0 if both
        outputs use the same section ids; 0.0 if disjoint.
    """

    channel_id: str
    page_id: str
    levenshtein_title: float
    levenshtein_section_max: float
    levenshtein_section_p50: float
    levenshtein_section_p95: float
    section_id_jaccard: float
    incremental_ms: int
    regenerate_ms: int
    incremental_section_count: int
    regenerate_section_count: int
    sample_section_diffs: list[dict[str, Any]] = field(default_factory=list)
    # ``wiki-llm-native-redesign`` §8.2 — kind facet on every drift report
    # so the soak dashboard can break the median/p95 down per
    # topic / entity / decisions / faq / action_items. Empty string for
    # legacy pages whose kind hasn't been backfilled yet — the
    # aggregation treats "" as a real bucket so it doesn't disappear
    # silently mid-rollout.
    kind: str = ""


def _levenshtein(a: str, b: str) -> int:
    """Standard Wagner-Fischer Levenshtein. Pure Python — fine for the
    page-section sizes we deal with (typically << 4 KB).
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]


def _normalized_distance(a: str, b: str) -> float:
    """Levenshtein distance normalized to ``[0.0, 1.0]``.

    Returns 0.0 when both strings are empty (identical) and 1.0 when
    only one side is empty (maximal divergence).
    """
    if not a and not b:
        return 0.0
    raw = _levenshtein(a, b)
    return raw / max(len(a), len(b))


def _percentile(values: list[float], p: float) -> float:
    """Inclusive percentile — ``p`` in ``[0.0, 1.0]``. Returns 0.0 for an
    empty list (the caller treats "no sections" as zero divergence)."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def compute_drift_report(
    *,
    channel_id: str,
    page_id: str,
    incremental: WikiPage,
    regenerate: WikiPage,
    incremental_ms: int,
    regenerate_ms: int,
) -> DriftReport:
    """Compute the per-page drift report from two ``WikiPage`` outputs."""
    inc_sections = {s.id: s.content_md or "" for s in incremental.sections}
    regen_sections = {s.id: s.content_md or "" for s in regenerate.sections}

    inc_ids = set(inc_sections.keys())
    regen_ids = set(regen_sections.keys())
    union = inc_ids | regen_ids
    intersection = inc_ids & regen_ids
    section_id_jaccard = (len(intersection) / len(union)) if union else 1.0

    section_distances: list[float] = []
    sample_diffs: list[dict[str, Any]] = []
    for sid in sorted(inc_ids):
        a = inc_sections[sid]
        b = regen_sections.get(sid, "")
        d = _normalized_distance(a, b)
        section_distances.append(d)
        if len(sample_diffs) < 3 and (d > 0.10 or sid not in regen_ids):
            sample_diffs.append({"section_id": sid, "distance": round(d, 4)})
    # Sections only on the regenerate side count as 1.0 divergence too —
    # the incremental output omitted them entirely.
    for sid in regen_ids - inc_ids:
        section_distances.append(1.0)

    title_distance = _normalized_distance(incremental.title or "", regenerate.title or "")
    section_max = max(section_distances) if section_distances else 0.0
    section_p50 = _percentile(section_distances, 0.5)
    section_p95 = _percentile(section_distances, 0.95)

    # Prefer the incremental's kind (the apply_update path the maintainer
    # is converging on); fall back to the regenerate's kind, then "".
    page_kind = getattr(incremental, "kind", None) or getattr(regenerate, "kind", None) or ""
    return DriftReport(
        channel_id=channel_id,
        page_id=page_id,
        levenshtein_title=round(title_distance, 4),
        levenshtein_section_max=round(section_max, 4),
        levenshtein_section_p50=round(section_p50, 4),
        levenshtein_section_p95=round(section_p95, 4),
        section_id_jaccard=round(section_id_jaccard, 4),
        incremental_ms=incremental_ms,
        regenerate_ms=regenerate_ms,
        incremental_section_count=len(inc_sections),
        regenerate_section_count=len(regen_sections),
        sample_section_diffs=sample_diffs,
        kind=str(page_kind),
    )


async def compare_apply_update_vs_regenerate(
    *,
    channel_id: str,
    page_id: str,
    incremental_factory,
    regenerate_factory,
) -> DriftReport | None:
    """Run the incremental + full-regenerate paths in parallel, compute the
    drift report, emit a structured log line, and return the report.

    ``incremental_factory`` and ``regenerate_factory`` are async callables
    that return the corresponding ``WikiPage`` for the same page. They're
    factories (not pre-computed pages) so the comparator can time each
    side independently — the timing is part of the report.

    On any failure, returns None and logs a warning. The comparator MUST
    NOT block or destabilise the maintainer's primary path.
    """
    try:

        async def _timed(factory):
            t0 = time.perf_counter()
            page = await factory()
            return page, int((time.perf_counter() - t0) * 1000)

        (inc_page, inc_ms), (regen_page, regen_ms) = await asyncio.gather(
            _timed(incremental_factory),
            _timed(regenerate_factory),
        )
    except Exception as exc:  # noqa: BLE001 — comparator is best-effort
        logger.warning(
            "wiki_drift_comparator failed channel_id=%s page_id=%s err=%s",
            channel_id,
            page_id,
            exc,
        )
        return None

    if inc_page is None or regen_page is None:
        logger.warning(
            "wiki_drift_comparator missing page channel_id=%s page_id=%s inc=%s regen=%s",
            channel_id,
            page_id,
            inc_page is not None,
            regen_page is not None,
        )
        return None

    report = compute_drift_report(
        channel_id=channel_id,
        page_id=page_id,
        incremental=inc_page,
        regenerate=regen_page,
        incremental_ms=inc_ms,
        regenerate_ms=regen_ms,
    )
    logger.info(
        "event=wiki_drift_report " + _format_report(report),
    )
    # Persist alongside the structured log line — defense-in-depth so the
    # admin dashboard endpoint can aggregate without depending on log-
    # shipping. Persistence failures emit a warning but do NOT alter the
    # primary success-log invariant: the structured log line above is the
    # contract every analytic pipeline reads first.
    try:
        from beever_atlas.stores import get_stores

        stores = get_stores()
        mongo = getattr(stores, "mongodb", None)
        if mongo is not None and hasattr(mongo, "insert_wiki_drift_report"):
            await mongo.insert_wiki_drift_report(report)
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning(
            "event=wiki_drift_persist_failed channel_id=%s page_id=%s err=%s",
            channel_id,
            page_id,
            exc,
        )
    return report


def _format_report(report: DriftReport) -> str:
    """Render the report as ``key=value`` pairs for the structured-log
    line. The aggregator that consumes these lines splits on whitespace.
    """
    parts: list[str] = []
    for key, value in asdict(report).items():
        if key == "sample_section_diffs":
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)
