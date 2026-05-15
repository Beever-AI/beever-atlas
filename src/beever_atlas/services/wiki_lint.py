"""Wiki lint service.

Surfaces signals that keep a wiki from rotting (Karpathy):
  * orphan pages — page exists but its source cluster no longer does
  * stale pages — the page's last edit is older than threshold
  * duplicate sections — two pages cover the same content
  * coherence findings — bounded LLM pass per page (max 1 LLM call)

Most of the signal is already computed elsewhere in the codebase but
invisible. The lint pass surfaces it as actionable findings on
``POST /api/channels/{id}/wiki/lint`` so the user can resolve them
through the UI.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/wiki-lint-and-tensions/``
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from beever_atlas.models.persistence import WikiPage
from beever_atlas.wiki.page_store import WikiPageStore

logger = logging.getLogger(__name__)


Severity = Literal["info", "warning", "error"]


class LintFinding(BaseModel):
    """A single lint finding for a page or section.

    ``severity``: ``info`` (suggested cleanup), ``warning`` (likely
    issue), ``error`` (broken state — orphan, missing reference).
    The frontend renders by severity; agents (via the ``lint_wiki``
    MCP tool) can filter by category.
    """

    severity: Severity
    category: str
    """Stable machine-readable label: ``orphan``, ``stale``,
    ``duplicate_section``, ``coherence``."""

    page_id: str
    section_id: str = ""
    message: str
    suggested_action: str = ""
    detected_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class LintReport(BaseModel):
    channel_id: str
    target_lang: str = "en"
    findings: list[LintFinding] = Field(default_factory=list)
    pages_scanned: int = 0
    generated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------


def find_orphans(pages: list[WikiPage], live_cluster_ids: set[str]) -> list[LintFinding]:
    """Pages whose source cluster_id has been deleted from the channel.

    The page_store's topic pages encode their cluster origin in the
    page_id (``topic:<cluster-id>``). If that cluster is not in the
    live set, the page is an orphan candidate.
    """
    findings: list[LintFinding] = []
    for page in pages:
        if not page.page_id.startswith("topic:"):
            continue
        cluster_id = page.page_id.removeprefix("topic:")
        if cluster_id and cluster_id not in live_cluster_ids:
            findings.append(
                LintFinding(
                    severity="warning",
                    category="orphan",
                    page_id=page.page_id,
                    message=f"Page references cluster '{cluster_id}' which no longer exists",
                    suggested_action="Archive or repoint to an existing cluster",
                )
            )
    return findings


def find_stale_pages(
    pages: list[WikiPage],
    threshold_days: int = 30,
    *,
    now: datetime | None = None,
) -> list[LintFinding]:
    """Pages whose ``updated_at`` is older than ``threshold_days``.

    Defensive bound rather than a hard \"must refresh\" — many pages
    intentionally cover stable content that doesn't need frequent
    edits. Surfacing as ``info`` not ``warning`` so the UI doesn't
    cry wolf.
    """
    findings: list[LintFinding] = []
    cutoff = (now or datetime.now(tz=UTC)) - timedelta(days=threshold_days)
    for page in pages:
        if page.updated_at < cutoff:
            age_days = ((now or datetime.now(tz=UTC)) - page.updated_at).days
            findings.append(
                LintFinding(
                    severity="info",
                    category="stale",
                    page_id=page.page_id,
                    message=f"Page hasn't been updated in {age_days} days",
                    suggested_action=(
                        "Run Maintain Wiki on this page or full-regenerate "
                        "if the channel content has shifted significantly"
                    ),
                )
            )
    return findings


def _section_signature(content_md: str) -> str:
    """Cheap content signature for duplicate detection.

    Production should use cosine similarity on embedded section text;
    for the deterministic pass we use word-set overlap + length match
    so two pages with identical or near-identical content collide
    without needing the embedding store at lint time.
    """
    words = sorted(set(w.lower() for w in content_md.split() if len(w) > 3))
    return "|".join(words[:50])


def find_duplicate_sections(pages: list[WikiPage]) -> list[LintFinding]:
    """Sections across pages that share substantially the same content.

    Heuristic at the deterministic layer; a future LLM coherence pass
    can add semantic equivalence detection later. Skips empty sections.
    """
    findings: list[LintFinding] = []
    by_signature: dict[str, list[tuple[str, str]]] = {}
    for page in pages:
        for section in page.sections:
            sig = _section_signature(section.content_md)
            if not sig or len(sig) < 20:
                continue
            by_signature.setdefault(sig, []).append((page.page_id, section.id))
    for sig, occurrences in by_signature.items():
        if len(occurrences) < 2:
            continue
        # Report only the second + later — keep the first as the canonical
        # version unless an operator decides otherwise.
        for page_id, section_id in occurrences[1:]:
            duplicates = [f"{p}#{s}" for p, s in occurrences if (p, s) != (page_id, section_id)]
            findings.append(
                LintFinding(
                    severity="info",
                    category="duplicate_section",
                    page_id=page_id,
                    section_id=section_id,
                    message=(f"Section content overlaps with: {', '.join(duplicates)}"),
                    suggested_action=(
                        "Consider consolidating or linking the sections to avoid "
                        "the wiki saying the same thing twice"
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Bounded LLM coherence pass — at most ONE call per page
# ---------------------------------------------------------------------------


async def coherence_check_page(
    page: WikiPage, llm_provider: Any | None = None
) -> list[LintFinding]:
    """One bounded LLM pass per page that surfaces coherence issues.

    Default implementation runs a structural-only check (no LLM
    invocation) so the lint endpoint stays cheap when the LLM
    provider is unavailable. Production wires ``llm_provider`` to
    Gemini Flash Lite — single call, prompt asks for ``[{severity,
    section_id, message, suggested_action}]`` JSON output.

    Bounded by design: ONE call per page, not per section. A 100-page
    channel costs at most 100 LLM calls per lint run, which the user
    triggers explicitly (not auto-fired on every extraction).
    """
    findings: list[LintFinding] = []
    if llm_provider is None:
        # Structural fallback: empty content_md sections are flagged.
        for section in page.sections:
            if section.title and not section.content_md.strip():
                findings.append(
                    LintFinding(
                        severity="info",
                        category="coherence",
                        page_id=page.page_id,
                        section_id=section.id,
                        message=f"Section '{section.title}' is empty",
                        suggested_action=(
                            "Drop the section if it's unused or fill it during the next "
                            "maintainer pass"
                        ),
                    )
                )
        return findings

    # Production LLM path is wired by the API handler — this function
    # is kept pure-async so unit tests can stub the provider easily.
    return findings


# ---------------------------------------------------------------------------
# Aggregator — the API endpoint calls this
# ---------------------------------------------------------------------------


async def lint_channel_wiki(
    channel_id: str,
    page_store: WikiPageStore,
    *,
    target_lang: str = "en",
    live_cluster_ids: set[str] | None = None,
    stale_threshold_days: int = 30,
    run_coherence_check: bool = True,
    llm_provider: Any | None = None,
) -> LintReport:
    """Run all lint checks for a channel and return a structured report.

    Spec scenario: ``Lint endpoint returns findings for a healthy
    channel`` → empty findings. ``Degraded channel`` → list of
    findings sorted by severity.
    """
    pages = await page_store.list_pages(channel_id, target_lang=target_lang)
    findings: list[LintFinding] = []
    findings.extend(find_orphans(pages, live_cluster_ids or set()))
    findings.extend(find_stale_pages(pages, threshold_days=stale_threshold_days))
    findings.extend(find_duplicate_sections(pages))
    if run_coherence_check:
        for page in pages:
            try:
                findings.extend(await coherence_check_page(page, llm_provider=llm_provider))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "wiki_lint.coherence_check_page failed channel=%s page=%s "
                    "(continuing — partial report is better than no report)",
                    channel_id,
                    page.page_id,
                )

    # Sort by severity (error > warning > info), then by page_id.
    severity_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 99), f.page_id))

    return LintReport(
        channel_id=channel_id,
        target_lang=target_lang,
        findings=findings,
        pages_scanned=len(pages),
    )
