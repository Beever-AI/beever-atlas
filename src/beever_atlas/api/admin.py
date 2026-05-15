"""Admin-token-gated endpoints that must be available in production.

Distinct from :mod:`beever_atlas.api.dev`, which is mounted only when
``BEEVER_ENV=development``. Routes here run in every environment and are
used by operators (never by end users or the dashboard UI directly).

Auth: ``X-Admin-Token`` header compared against ``BEEVER_ADMIN_TOKEN`` via
:func:`~beever_atlas.infra.auth.require_admin`. User and MCP tokens are NOT
accepted.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from beever_atlas.infra.auth import require_admin
from beever_atlas.models.persistence import ExternalSource
from beever_atlas.stores import get_stores

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)
logger = logging.getLogger(__name__)


@router.get("/mcp-metrics")
async def mcp_metrics() -> dict:
    """Return an aggregated snapshot of MCP tool call metrics (task 7.6).

    Read-only operator view — summarises the in-memory rolling-window counter
    maintained by :mod:`beever_atlas.infra.mcp_metrics`. Shape:

        {
          "window_seconds": 3600,
          "total_calls": int,
          "distinct_principals": int,
          "by_outcome":       {"ok": int, "rate_limited": int, ...},
          "by_principal_tool": [{principal, tool, outcome, count}, ...],
          "by_tool_latency":  {tool: {count, avg_ms, p95_ms}, ...}
        }

    Per-process only — in multi-worker deploys each process reports its own
    slice. An aggregating UI layer can sum them. Principals are the full
    ``mcp:<hash>`` tokens (non-reversible; safe to expose to the admin).
    """
    from beever_atlas.infra import mcp_metrics as metrics_mod

    snapshot = metrics_mod.snapshot_counters()
    return snapshot


@router.post("/mcp-metrics/reset")
async def mcp_metrics_reset() -> dict:
    """Clear the in-memory rolling-window counter. Ops use only."""
    from beever_atlas.infra import mcp_metrics as metrics_mod

    metrics_mod.reset_counters()
    return {"status": "reset"}


# ---------------------------------------------------------------------------
# Push-source registry (admin)
# ---------------------------------------------------------------------------


class CreateSourceRequest(BaseModel):
    """Body for ``POST /api/admin/sources``."""

    source_id: str = Field(min_length=1, max_length=128)
    allowed_channels_pattern: str = Field(default="*", max_length=256)
    description: str = Field(default="", max_length=512)


class SourceListItem(BaseModel):
    """Public shape returned by ``GET /api/admin/sources``.

    Note: the plaintext ``secret`` is NEVER included. Operators see
    ``secret_fingerprint`` (sha256 of the secret) so they can confirm a
    rotation took effect without leaking the key.
    """

    source_id: str
    allowed_channels_pattern: str
    description: str = ""
    secret_fingerprint: str = ""
    created_at: str | None = None
    rotated_at: str | None = None
    idempotency_replay_count_24h: int = 0


class CreateSourceResponse(BaseModel):
    """Body returned ONCE on ``POST`` / ``PATCH /rotate``.

    The ``secret`` field is the plaintext HMAC key — copy it now; it
    cannot be retrieved later.
    """

    source_id: str
    secret: str
    secret_fingerprint: str
    rotated_at: str | None = None


def _generate_secret() -> str:
    """32 bytes of URL-safe entropy (≈ 43 chars). Industry-standard size
    for HMAC-SHA256 keys."""
    return secrets.token_urlsafe(32)


def _to_list_item(source: ExternalSource, replay_count: int) -> SourceListItem:
    return SourceListItem(
        source_id=source.source_id,
        allowed_channels_pattern=source.allowed_channels_pattern,
        description=source.description,
        secret_fingerprint=source.secret_fingerprint,
        created_at=source.created_at.isoformat() if source.created_at else None,
        rotated_at=source.rotated_at.isoformat() if source.rotated_at else None,
        idempotency_replay_count_24h=replay_count,
    )


@router.get("/sources", response_model=list[SourceListItem])
async def list_sources() -> list[SourceListItem]:
    """List all registered push sources for the admin UI."""
    stores = get_stores()
    rows = await stores.mongodb.list_external_sources()
    out: list[SourceListItem] = []
    for src in rows:
        replay_count = await stores.mongodb.count_idempotency_replays_for_source(src.source_id)
        out.append(_to_list_item(src, replay_count))
    return out


@router.post(
    "/sources",
    response_model=CreateSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_source(req: CreateSourceRequest) -> CreateSourceResponse:
    """Register a new push source.

    Generates the HMAC secret server-side and returns the plaintext
    ONCE in the response body. Re-fetching this row via ``GET /sources``
    returns only the fingerprint, never the plaintext.
    """
    stores = get_stores()
    existing = await stores.mongodb.get_external_source(req.source_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"source_id '{req.source_id}' already exists; use PATCH /rotate to rotate the secret",
        )
    plain_secret = _generate_secret()
    source = ExternalSource(
        source_id=req.source_id,
        secret=plain_secret,
        allowed_channels_pattern=req.allowed_channels_pattern,
        description=req.description,
    )
    await stores.mongodb.upsert_external_source(source)
    # Re-fetch so we get the canonical secret_fingerprint that the upsert
    # path computed (defense-in-depth: never echo a hash we computed
    # ourselves before persistence confirmed it).
    persisted = await stores.mongodb.get_external_source(req.source_id)
    fingerprint = persisted.secret_fingerprint if persisted else ""
    return CreateSourceResponse(
        source_id=req.source_id,
        secret=plain_secret,
        secret_fingerprint=fingerprint,
    )


@router.patch(
    "/sources/{source_id}/rotate",
    response_model=CreateSourceResponse,
)
async def rotate_source_secret(source_id: str) -> CreateSourceResponse:
    """Rotate the HMAC secret for an existing source.

    Old signatures stop verifying immediately; the new plaintext is
    returned ONCE in the response body.
    """
    stores = get_stores()
    existing = await stores.mongodb.get_external_source(source_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"source_id '{source_id}' not registered",
        )
    new_secret = _generate_secret()
    rotated = ExternalSource(
        source_id=source_id,
        secret=new_secret,
        allowed_channels_pattern=existing.allowed_channels_pattern,
        description=existing.description,
        created_at=existing.created_at,
    )
    await stores.mongodb.upsert_external_source(rotated)
    persisted = await stores.mongodb.get_external_source(source_id)
    rotated_at: str | None = None
    fingerprint = ""
    if persisted is not None:
        fingerprint = persisted.secret_fingerprint
        rotated_at = persisted.rotated_at.isoformat() if persisted.rotated_at else None
    return CreateSourceResponse(
        source_id=source_id,
        secret=new_secret,
        secret_fingerprint=fingerprint,
        rotated_at=rotated_at,
    )


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: str) -> Response:
    """Delete a push source. Subsequent ingest calls return 404."""
    stores = get_stores()
    deleted = await stores.mongodb.delete_external_source(source_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"source_id '{source_id}' not registered",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Worker observability metrics (production-wiring §20)
# ---------------------------------------------------------------------------


@router.get("/extraction-worker/metrics")
async def extraction_worker_metrics() -> dict:
    """Return a snapshot of the ExtractionWorker's current state.

    Combines:
      - per-channel queue depth (``extraction_status="pending"`` count)
      - rolling claim_rate over 5/15/60min windows
      - rolling success_rate over the last 5min
      - circuit breaker state
      - most recent 10 per-row failures

    Per-process — in multi-replica deploys each worker reports its own
    slice. Snapshot is best-effort: if the worker singleton is not yet
    registered (early lifespan), returns a zeroed-out shape instead of
    erroring.
    """
    try:
        from beever_atlas.services.extraction_worker import get_extraction_worker

        worker = get_extraction_worker()
        if worker is None:
            worker_metrics = {
                "claim_rate_5min": 0.0,
                "claim_rate_15min": 0.0,
                "claim_rate_60min": 0.0,
                "success_rate_5min": 1.0,
                "breaker_state": "unknown",
                "recent_failures": [],
            }
        else:
            worker_metrics = worker.metrics_snapshot()
    except Exception as exc:  # noqa: BLE001 — never crash an observability endpoint
        logger.warning("extraction-worker metrics: worker snapshot failed: %s", exc)
        worker_metrics = {
            "claim_rate_5min": 0.0,
            "claim_rate_15min": 0.0,
            "claim_rate_60min": 0.0,
            "success_rate_5min": 1.0,
            "breaker_state": "unknown",
            "recent_failures": [],
        }

    queue_depth: dict[str, int] = {}
    try:
        stores = get_stores()
        cursor = stores.mongodb._channel_messages.aggregate(  # type: ignore[attr-defined]
            [
                {"$match": {"extraction_status": "pending"}},
                {"$group": {"_id": "$channel_id", "count": {"$sum": 1}}},
            ]
        )
        async for row in cursor:
            cid = row.get("_id") or ""
            if cid:
                queue_depth[str(cid)] = int(row.get("count", 0) or 0)
    except Exception as exc:  # noqa: BLE001 — never crash the endpoint
        logger.warning("extraction-worker metrics: queue depth aggregate failed: %s", exc)

    return {
        "queue_depth_per_channel": queue_depth,
        **worker_metrics,
    }


# ---------------------------------------------------------------------------
# WikiMaintainer observability metrics (close-the-soak-loop §4)
# ---------------------------------------------------------------------------


@router.get("/wiki-maintainer/metrics")
async def wiki_maintainer_metrics() -> dict:
    """Return a snapshot of the WikiMaintainer's current state.

    Mirrors :func:`extraction_worker_metrics` so operators learn one
    pattern: rolling apply_update counts (5/15/60min), recent failures,
    by-page-kind rewrite counts, mark_dirty rate, and per-channel
    pending-dirty page counts. Per-process — multi-replica deploys
    aggregate at the UI layer.

    Snapshot is best-effort. When the maintainer singleton has not been
    registered yet (early lifespan) OR ``metrics_snapshot`` raises, the
    endpoint returns the documented zeroed shape rather than a 500.
    """
    try:
        from beever_atlas.services.wiki_maintainer import (
            get_wiki_maintainer,
            zeroed_maintainer_metrics,
        )

        maintainer = get_wiki_maintainer()
        if maintainer is None:
            return zeroed_maintainer_metrics()
        return await maintainer.metrics_snapshot()
    except Exception as exc:  # noqa: BLE001 — never crash an observability endpoint
        from beever_atlas.services.wiki_maintainer import zeroed_maintainer_metrics

        logger.warning("wiki-maintainer metrics: snapshot failed: %s", exc)
        return zeroed_maintainer_metrics()


# ---------------------------------------------------------------------------
# Wiki drift threshold dashboard endpoint (close-the-soak-loop §5)
# ---------------------------------------------------------------------------


# Hard cap on the ``days`` query parameter. The aggregator runs an
# unbounded $match over the TTL collection; pinning a server-side ceiling
# guarantees a single misbehaving caller cannot trigger a 30-million-row
# scan even if the TTL grows beyond the documented 30-day window.
_WIKI_DRIFT_SUMMARY_MAX_DAYS = 60


def _summary_pass_criterion(p50_median: float, p95_median: float) -> bool:
    """Pass threshold from spec: median Levenshtein < 0.15 AND p95 < 0.30.

    Stays a module-level helper so the criterion is one source of truth
    if a future change tunes it.
    """
    return p50_median < 0.15 and p95_median < 0.30


@router.get("/wiki-drift/summary")
async def wiki_drift_summary(days: int = 14) -> dict:
    """Aggregated drift summary for the soak-pass dashboard.

    Returns ``{channels, pass, data_fresh}``:
      * ``channels``: per-channel ``{channel_id, page_count,
        levenshtein_section_p50_median, levenshtein_section_p95_median,
        last_run_ts, pass_criterion_met}``.
      * ``pass``: True iff every channel meets the threshold.
      * ``data_fresh``: True iff every channel's most recent report is
        within the last 60 minutes.

    Empty window returns ``{channels: [], pass: false, data_fresh: false}``
    with HTTP 200 — empty soak data is a documented state, not an error.
    """
    from datetime import UTC, datetime, timedelta

    capped_days = max(1, min(days, _WIKI_DRIFT_SUMMARY_MAX_DAYS))
    try:
        stores = get_stores()
        rows = await stores.mongodb.aggregate_wiki_drift_summary(capped_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("wiki-drift summary: aggregate failed: %s", exc)
        rows = []

    if not rows:
        return {"channels": [], "pass": False, "data_fresh": False}

    now = datetime.now(tz=UTC)
    fresh_cutoff = now - timedelta(minutes=60)
    channels: list[dict] = []
    pass_overall = True
    data_fresh_overall = True
    for row in rows:
        p50m = float(row.get("levenshtein_section_p50_median", 0.0) or 0.0)
        p95m = float(row.get("levenshtein_section_p95_median", 0.0) or 0.0)
        criterion = _summary_pass_criterion(p50m, p95m)
        last_ts = row.get("last_run_ts")
        is_fresh = isinstance(last_ts, datetime) and last_ts >= fresh_cutoff
        if not criterion:
            pass_overall = False
        if not is_fresh:
            data_fresh_overall = False
        channels.append(
            {
                "channel_id": row.get("channel_id", ""),
                "page_count": int(row.get("page_count", 0) or 0),
                "levenshtein_section_p50_median": p50m,
                "levenshtein_section_p95_median": p95m,
                "last_run_ts": last_ts.isoformat() if isinstance(last_ts, datetime) else last_ts,
                "pass_criterion_met": criterion,
            }
        )
    return {
        "channels": channels,
        "pass": pass_overall,
        "data_fresh": data_fresh_overall,
    }


@router.get("/wiki/narrative-health")
async def wiki_narrative_health(channel_id: str = "") -> dict:
    """Per-channel narrative-article health stats for the operator dashboard.

    Aggregates wiki page documents in a channel and surfaces the soak
    metrics defined in
    ``openspec/changes/wiki-narrative-articles/`` Phase 9:

      - ``narrative_pct``: fraction of pages with non-empty
        ``narrative_sections`` (i.e., the v3 narrative pass succeeded
        AND survived the validator).
      - ``median_citation_coverage``: median per-page citation
        coverage across pages with narrative.
      - ``median_word_count``: median article word count across pages
        with narrative.
      - ``fallback_rate``: 1 - narrative_pct (pages where the v3 pass
        was attempted but the page rendered module-only).
      - ``page_count``: total pages in the channel.
      - ``narrative_page_count``: count of pages with narrative.

    Empty channel returns the documented zeroed shape with HTTP 200 so
    the dashboard renders an empty-state card cleanly.

    Spec: ``wiki-narrative-articles`` Phase 9 task 9.3.
    """
    if not channel_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channel_id query parameter is required",
        )
    try:
        from beever_atlas.wiki.page_store import WikiPageStore

        stores = get_stores()
        page_store = WikiPageStore(db=stores.mongodb.db)
        pages = await page_store.list_pages(channel_id, target_lang="en")
    except Exception as exc:  # noqa: BLE001
        logger.warning("wiki/narrative-health: list_pages failed: %s", exc)
        pages = []

    page_count = len(pages)
    if page_count == 0:
        return {
            "channel_id": channel_id,
            "page_count": 0,
            "narrative_page_count": 0,
            "narrative_pct": 0.0,
            "median_citation_coverage": 0.0,
            "median_word_count": 0,
            "fallback_rate": 0.0,
        }

    coverages: list[float] = []
    word_counts: list[int] = []
    narrative_count = 0
    for page in pages:
        sections = page.narrative_sections or []
        if not sections:
            continue
        narrative_count += 1
        # Per-page coverage = mean of per-section citation_coverage
        # values. Falls back to 0.0 when a section's coverage field
        # is missing (older / hand-edited rows).
        section_covs: list[float] = []
        page_words = 0
        for section in sections:
            if not isinstance(section, dict):
                continue
            cov = section.get("citation_coverage")
            if isinstance(cov, (int, float)):
                section_covs.append(float(cov))
            for paragraph in section.get("paragraphs") or []:
                if isinstance(paragraph, dict):
                    text = paragraph.get("text") or ""
                    if isinstance(text, str):
                        page_words += len(text.split())
        if section_covs:
            coverages.append(sum(section_covs) / len(section_covs))
        word_counts.append(page_words)

    def _median(xs: list[float]) -> float:
        if not xs:
            return 0.0
        sorted_xs = sorted(xs)
        mid = len(sorted_xs) // 2
        if len(sorted_xs) % 2 == 1:
            return float(sorted_xs[mid])
        return float((sorted_xs[mid - 1] + sorted_xs[mid]) / 2)

    narrative_pct = narrative_count / page_count
    median_coverage = _median(coverages)
    median_words = int(_median([float(w) for w in word_counts]))
    fallback_rate = 1.0 - narrative_pct

    return {
        "channel_id": channel_id,
        "page_count": page_count,
        "narrative_page_count": narrative_count,
        "narrative_pct": narrative_pct,
        "median_citation_coverage": median_coverage,
        "median_word_count": median_words,
        "fallback_rate": fallback_rate,
    }


# ---------------------------------------------------------------------------
# LLMThrottle observability metrics (rate-limiting feature B2)
# ---------------------------------------------------------------------------


@router.get("/llm-throttle/metrics")
async def llm_throttle_metrics() -> dict:
    """Return per-provider live state for the LLM token-bucket throttle.

    Snapshot shape::

        {
          "providers": [
            {
              "provider": "gemini",
              "rpm_limit": 10,
              "tpm_limit": 250000,
              "rpm_used_60s": 4,
              "tpm_used_60s": 120000,
              "blocked_calls_60s": 2,
              "recent_429s_60s": 0,
              "in_cooldown": false,
            },
            ...
          ]
        }

    Per-process — in multi-replica deploys each worker reports its own
    slice. Best-effort: a missing throttle returns an empty list rather
    than a 500 so the dashboard renders cleanly on cold startup.
    """
    try:
        from beever_atlas.services.llm_throttle import get_llm_throttle

        throttle = get_llm_throttle()
        return {"providers": throttle.metrics_snapshot()}
    except Exception as exc:  # noqa: BLE001 — never crash an observability endpoint
        logger.warning("llm-throttle metrics: snapshot failed: %s", exc)
        return {"providers": []}


__all__ = ["router"]
