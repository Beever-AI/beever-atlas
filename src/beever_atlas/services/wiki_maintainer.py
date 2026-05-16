"""WikiMaintainer service.

Karpathy-style LLM Wiki bookkeeping. Replaces the
``cache.mark_all_stale(channel_id)`` invocation at
``services/consolidation.py:130-139`` — that was a single boolean
\"refresh everything\" hammer; the maintainer routes new facts to the
specific pages they affect and rewrites only those pages' affected
sections.

Flow when WIKI_MAINTENANCE_MODE=auto:
  1. ExtractionWorker emits on_extraction_done(channel_id, fact_ids).
  2. Maintainer's plan_updates() routes fact_ids → affected page_ids
     deterministically (cluster_id → topic page, entity_tags → entity
     pages, fact_type → role pages). NO LLM call here.
  3. The (channel_id, page_id) pairs are added to an in-memory dirty-set
     and ONE debounced flush task is scheduled (default 60s window). A
     burst of N events touching the same page within the window collapses
     into a single rewrite carrying all N events' fact_ids — the maintainer
     does NOT issue an LLM call per event. See decision D3 in
     ``openspec/changes/sync-pipeline-feedback-and-auto-wiki/design.md``.
  4. When the flush fires, for each affected page, apply_update() invokes
     ONE per-page LLM call that rewrites only the affected sections. Title,
     slug, and unaffected sections are preserved byte-identical so page
     voice does not drift.
  5. Page version bumps; last_facts_seen records the new fact_ids.

When WIKI_MAINTENANCE_MODE=manual, step 1 marks the affected pages
``is_dirty=True`` but does NOT call apply_update() — the user clicks
``Maintain Wiki`` to drain the dirty queue on demand. Manual mode bypasses
the debounce path entirely.

Persistence: the dirty-set is in-memory only. If the maintainer process
crashes mid-debounce window, pending updates are lost. Worst-case loss is
one debounce window (default 60s) of pending rewrites; the next extraction
event for the affected pages re-routes them to a fresh dirty-set. The
``on_extraction_done`` event itself is not durable (out of scope).

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/wiki-maintainer/``
      ``openspec/changes/sync-pipeline-feedback-and-auto-wiki/specs/wiki-maintainer/``
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.wiki.page_store import WikiPageStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# wiki-llm-native-redesign — per-kind prompt + schema dispatch
# ---------------------------------------------------------------------------
# Kinds the redesign knows how to dispatch. Anything else falls through to
# the legacy single-prompt path so unknown future kinds (or operator-edited
# `kind` values) never crash the maintainer.
_KNOWN_KINDS: frozenset[str] = frozenset({"topic", "entity", "decisions", "faq", "action_items"})

# Resolve at import time so tests cannot trip on cwd changes.
_WIKI_RESOURCE_ROOT: Path = Path(__file__).resolve().parent.parent / "wiki"
_PROMPT_DIR: Path = _WIKI_RESOURCE_ROOT / "prompts"
_SCHEMA_DIR: Path = _WIKI_RESOURCE_ROOT / "schemas"


@lru_cache(maxsize=None)
def _load_kind_prompt(kind: str) -> str:
    """Read the per-kind synthesis prompt template from disk.

    Cached because the prompt files are static — re-reading on every
    apply_update call is wasted I/O. Tests that modify prompts at runtime
    must call ``_load_kind_prompt.cache_clear()``.
    """
    if kind not in _KNOWN_KINDS:
        raise KeyError(f"unknown wiki kind: {kind!r}")
    path = _PROMPT_DIR / f"{kind}.txt"
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _load_kind_schema(kind: str) -> dict[str, Any]:
    if kind not in _KNOWN_KINDS:
        raise KeyError(f"unknown wiki kind: {kind!r}")
    path = _SCHEMA_DIR / f"{kind}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_kind_schema(kind: str, payload: Any) -> str | None:
    """Validate ``payload`` against the kind's JSON Schema.

    Returns None on success, a one-line error suitable for the retry
    prompt on failure. ``jsonschema`` is a transitive dep so we import
    it lazily — module import time should not pay for this branch.
    """
    import jsonschema

    if not isinstance(payload, dict):
        return f"kind_schema must be a JSON object, got {type(payload).__name__}"
    schema = _load_kind_schema(kind)
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        path_repr = "/".join(str(p) for p in exc.absolute_path) or "root"
        return f"{exc.message} (at {path_repr})"
    return None


def derive_kind_from_page_id(page_id: str) -> str:
    """Map a structural ``page_id`` to its synthesis kind.

    Bridges the migration window: legacy pages have ``kind`` defaulted
    to ``"topic"`` regardless of their actual structure. The dispatcher
    consults this helper for pages whose stored ``kind`` is the default
    so an entity / decisions / faq / action-items page is dispatched
    to its correct prompt before the migration script runs.
    """
    if not page_id:
        return "topic"
    if page_id.startswith("entity:"):
        return "entity"
    if page_id == "decisions":
        return "decisions"
    if page_id == "faq":
        return "faq"
    if page_id == "action-items":
        return "action_items"
    # wiki-redesign-gap-fill / Group 7+8 — adaptive kinds. Their page_ids
    # are the kind name itself (e.g., ``projects``, ``architecture``) so
    # the suppression gate at ``_apply_update_inner`` can correctly
    # identify them as adaptive (not-required) and drop placeholder
    # creation for pages the Builder hasn't instantiated yet.
    if page_id in (
        "projects",
        "architecture",
        "open-questions",
        "timeline",
        "stakeholders",
    ):
        return page_id
    return "topic"


def _resolve_dispatch_kind(page: "WikiPage") -> str:
    """Pick the dispatch kind for a page.

    Explicitly-set kinds (operator split / merge / first-touch on a new
    redesigned page) win. Pages whose ``kind`` is the model default
    (``"topic"``) fall back to a structural derivation from ``page_id``,
    so legacy pages are dispatched correctly without a migration.
    """
    if page.kind and page.kind != "topic":
        return page.kind
    return derive_kind_from_page_id(page.page_id)


def _parse_affected_sections_from_obj(
    parsed: dict[str, Any],
) -> list["WikiPageSection"]:
    """Extract ``affected_sections`` from a parsed response object.

    Shared helper used by both the legacy parser and the per-kind parser
    so the section-merge contract stays identical across paths.
    """
    affected_raw = parsed.get("affected_sections")
    if not isinstance(affected_raw, list):
        return []
    out: list[WikiPageSection] = []
    for entry in affected_raw:
        if not isinstance(entry, dict):
            continue
        section_id = str(entry.get("id", "")).strip()
        content_md = str(entry.get("content_md", "")).strip()
        if not section_id or not content_md:
            continue
        title = str(entry.get("title", "")).strip() or section_id.title()
        out.append(
            WikiPageSection(
                id=section_id,
                title=title,
                content_md=content_md,
            )
        )
    return out


def _parse_kind_response(
    raw: str,
) -> tuple[list["WikiPageSection"], dict[str, Any] | None]:
    """Parse the per-kind LLM response into (sections, kind_schema).

    ``kind_schema`` is None when the response was unparseable, lacked
    a ``kind_schema`` key, or carried a non-object value there. The
    caller decides whether to retry, fall through, or save the page
    without the structured payload.
    """
    if not raw:
        return [], None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("event=wiki_kind_response_parse_failed raw_len=%d", len(raw))
        return [], None
    if not isinstance(parsed, dict):
        return [], None
    sections = _parse_affected_sections_from_obj(parsed)
    kind_schema_raw = parsed.get("kind_schema")
    kind_schema: dict[str, Any] | None = (
        kind_schema_raw if isinstance(kind_schema_raw, dict) else None
    )
    return sections, kind_schema


def _render_kind_prompt(
    kind: str,
    page: "WikiPage",
    new_facts: list[dict[str, Any]],
    *,
    target_lang: str = "en",
    retry_validation_error: str | None = None,
) -> str:
    """Build the per-kind apply_update prompt.

    Mirrors ``_render_apply_update_prompt``'s payload shape so the LLM
    sees a familiar structure; the system prompt switches per kind.
    Includes the prior ``kind_schema`` so the LLM can update it
    incrementally rather than rebuild from scratch on each touch.
    """
    system = _load_kind_prompt(kind)
    payload: dict[str, Any] = {
        "page": {
            "page_id": page.page_id,
            "title": page.title,
            "slug": page.slug,
            "voice_seed": page.page_voice_seed or "",
            "page_voice_seed": page.page_voice_seed or "",
            "target_lang": target_lang,
            "last_facts_seen": list(page.last_facts_seen),
            "sections": [
                {"id": s.id, "title": s.title, "content_md": s.content_md} for s in page.sections
            ],
            "prior_kind_schema": page.kind_schema,
        },
        "new_facts": [
            {
                "id": f.get("id", ""),
                "memory_text": f.get("memory_text", ""),
                "cluster_id": f.get("cluster_id"),
                "entity_tags": list(f.get("entity_tags") or []),
                "fact_type": f.get("fact_type", ""),
                "source_message_id": f.get("source_message_id", ""),
            }
            for f in new_facts
        ],
    }
    out = system + "\n\n--- INPUT ---\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    if retry_validation_error:
        out += (
            "\n\n--- RETRY ---\n"
            "Your previous response failed kind_schema validation:\n"
            f"  {retry_validation_error}\n"
            "Re-emit the entire JSON object with kind_schema fixed.\n"
        )
    if _is_page_pinned(page):
        out += _PINNED_PAGE_ADDENDUM
    out += "\n\n--- OUTPUT (JSON only) ---\n"
    return out


# Prompt addendum the maintainer appends when a page is pinned. The
# operator's pin signals "this layout is intentional — do not
# restructure" — so the LLM still updates content but cannot rename
# sections, drop the title, or reorder the affected_sections array.
_PINNED_PAGE_ADDENDUM = (
    "\n\n--- CURATION CONSTRAINTS ---\n"
    "This page is PINNED by the operator. You MUST:\n"
    "  - keep every existing section_id stable;\n"
    "  - not rename the page title;\n"
    "  - not drop or reorder existing sections;\n"
    "  - integrate new facts into existing sections rather than "
    "creating new ones, unless absolutely required.\n"
    "Pinned pages are load-bearing — the operator pinned this exact "
    "layout deliberately.\n"
)


def _is_page_pinned(page: "WikiPage") -> bool:
    """True when the operator has flipped ``pin_state.pinned``.

    Defensive against legacy rows where ``pin_state`` is missing or
    not a dict (the model defaults to a populated dict so this is the
    deserialization-edge case).
    """
    state = getattr(page, "pin_state", None)
    if not isinstance(state, dict):
        return False
    return bool(state.get("pinned"))


def _resolve_curation_mode(page: "WikiPage") -> str:
    """Return the page's effective curation mode.

    Introduced by ``unified-llm-wiki-graph-redesign``:
      * ``auto`` — maintainer marks dirty AND applies LLM patches.
      * ``manual`` — maintainer marks dirty but skips patch; operator
        triggers via "Apply Pending Updates".
      * ``frozen`` — maintainer skips entirely.

    Defensive: legacy rows without ``curation_mode`` default to
    ``auto``. Legacy ``pin_state.pinned=True`` is treated as ``manual``
    so existing operator pins still skip auto-rewrites.
    """
    mode = getattr(page, "curation_mode", None)
    if mode in {"auto", "manual", "frozen"}:
        return str(mode)
    if _is_page_pinned(page):
        return "manual"
    return "auto"


# ---------------------------------------------------------------------------
# wiki-llm-native-redesign — `[[wikilink]]` parser + resolver
# ---------------------------------------------------------------------------
# The redesign instructs LLM prompts to emit `[[Page Title]]` references
# inline in markdown. After ``apply_update`` saves the page, a post-processor:
#   1. Parses titles from the rewritten content (this regex);
#   2. Resolves each title to a slug via exact / case-insensitive /
#      plural-aware / fuzzy match (≤0.15 Levenshtein, expressed as
#      difflib ratio ≥0.85);
#   3. Persists ``cross_links`` / ``cross_links_broken`` on the page
#      document and (best-effort) writes a ``REFERENCES`` edge in Neo4j.

# Bracketed-title regex — matches ``[[Title]]`` where Title contains no
# embedded brackets or newlines. ``[[[bad]]]`` and ``[[a [b] c]]`` fall
# through cleanly because the inner content rejects ``[`` and ``]``.
_WIKILINK_PATTERN = re.compile(r"\[\[([^\[\]\n]+?)\]\]")

# Difflib ratio threshold for fuzzy title matching. The change spec
# specifies "≤0.15 Levenshtein"; difflib's ``SequenceMatcher.ratio()``
# is approximately ``1 - (edit_distance / total_chars)`` for sequences
# of similar length, so the equivalent threshold is ≥0.85.
_WIKILINK_FUZZY_THRESHOLD = 0.85


def _parse_wikilinks(content_md: str) -> list[str]:
    """Extract bracketed titles from a markdown body.

    Returns titles in document order. Whitespace is stripped from each
    match; empty matches are dropped. Duplicates are NOT deduped here —
    callers running across multiple sections may want to dedupe globally.
    """
    if not content_md:
        return []
    out: list[str] = []
    for match in _WIKILINK_PATTERN.finditer(content_md):
        title = match.group(1).strip()
        if title:
            out.append(title)
    return out


def _normalize_title_for_match(title: str) -> str:
    """Lowercase + trim + drop a trailing ``s``.

    The trailing-``s`` rule is a deliberately small plural-stemmer so
    ``[[Decision]]`` resolves to a ``Decisions`` page without dragging
    in the full nltk surface area. False positives ('boss' → 'bos') are
    accepted because they are shorter than the fuzzy-match cutoff and
    will fall through to the next rule.
    """
    n = title.strip().lower()
    if len(n) > 1 and n.endswith("s"):
        n = n[:-1]
    return n


def _build_page_index(
    pages: list["WikiPage"],
    *,
    exclude_self_page_id: str | None = None,
) -> dict[str, str]:
    """Build a {key: slug} resolver index from a list of WikiPage.

    Pages are sorted by ``updated_at`` DESC so ties on a normalized key
    resolve to the most-recently-edited target — matches the spec's
    fuzzy-match tie-break rule.
    """

    def _slug_of(page: "WikiPage") -> str:
        return page.slug or page.page_id.replace(":", "-")

    sorted_pages = sorted(
        pages,
        key=lambda p: getattr(p, "updated_at", None) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    index: dict[str, str] = {}
    for page in sorted_pages:
        if exclude_self_page_id is not None and page.page_id == exclude_self_page_id:
            continue
        slug = _slug_of(page)
        if not slug:
            continue
        candidates = {
            slug,
            slug.lower(),
            page.title,
            page.title.lower() if page.title else "",
            _normalize_title_for_match(page.title) if page.title else "",
        }
        for key in candidates:
            if key and key not in index:
                index[key] = slug
    return index


def _resolve_wikilink_against_index(title: str, page_index: dict[str, str]) -> str | None:
    """Resolve a wikilink title against a pre-built index.

    Match precedence: exact → lowercased → plural-stripped → fuzzy
    (difflib ratio ≥ 0.85). Returns None when no candidate clears the
    fuzzy threshold; the caller surfaces it in ``cross_links_broken``.
    """
    raw = title.strip()
    if not raw or not page_index:
        return None
    if raw in page_index:
        return page_index[raw]
    lowered = raw.lower()
    if lowered in page_index:
        return page_index[lowered]
    norm = _normalize_title_for_match(raw)
    if norm in page_index:
        return page_index[norm]
    matches = difflib.get_close_matches(
        norm,
        list(page_index.keys()),
        n=1,
        cutoff=_WIKILINK_FUZZY_THRESHOLD,
    )
    if matches:
        return page_index[matches[0]]
    return None


def _slug_for_topic(cluster_id: str) -> str:
    """Convert a cluster id into a stable topic page id.

    The cluster_id is opaque to consumers but uses ``/`` as a hierarchy
    separator. We sanitize to ASCII-safe slugs and prefix with
    ``topic:`` so the page namespace is unambiguous from entity /
    decisions / faq pages.
    """
    safe = (cluster_id or "unspecified").replace("/", "-")
    return f"topic:{safe}"


def _slug_for_entity(entity_name: str) -> str:
    safe = (entity_name or "").strip().lower().replace(" ", "-")
    return f"entity:{safe}" if safe else ""


def _slug_for_fact_type(fact_type: str) -> str | None:
    """Map fact_type → page slug for role-based pages.

    Returns None for fact_types that don't have a dedicated page
    (``observation``, ``opinion`` are not surfaced as their own pages
    — they belong on topic / entity pages alongside their cluster).
    """
    role_map = {
        "decision": "decisions",
        "question": "faq",
        "action_item": "action-items",
    }
    return role_map.get(fact_type)


# Upper bound on the per-channel fact scan in ``_load_facts(channel_id, None)``.
# Hitting it produces a structured warning so we can revisit during soak. The
# main path is the explicit-id branch; the channel-wide path only runs from
# ``maintain_now`` (the manual-mode UI button), where bounded latency matters
# more than completeness on a 50k-fact channel.
_CHANNEL_FACT_LOAD_CAP = 5000


def _atomic_fact_to_routing_dict(fact: Any) -> dict[str, Any]:
    """Convert an ``AtomicFact`` Pydantic record into the dict shape
    ``plan_updates`` consumes. Defensive against missing attributes so
    monkeypatched tests can hand in plain dicts too.
    """
    if isinstance(fact, dict):
        return {
            "id": str(fact.get("id") or fact.get("fact_id") or ""),
            "cluster_id": fact.get("cluster_id"),
            "entity_tags": list(fact.get("entity_tags") or []),
            "fact_type": fact.get("fact_type") or "",
            "memory_text": fact.get("memory_text") or "",
            "source_message_id": fact.get("source_message_id") or "",
        }
    return {
        "id": str(getattr(fact, "id", "") or ""),
        "cluster_id": getattr(fact, "cluster_id", None),
        "entity_tags": list(getattr(fact, "entity_tags", []) or []),
        "fact_type": getattr(fact, "fact_type", "") or "",
        "memory_text": getattr(fact, "memory_text", "") or "",
        "source_message_id": getattr(fact, "source_message_id", "") or "",
    }


_APPLY_UPDATE_SYSTEM_PROMPT = (
    "You are the wiki maintainer for an in-app personal-intelligence wiki. "
    "Your job is to integrate one or more new facts into ONE existing wiki "
    "page. You MUST:\n"
    " 1. Return ONLY the sections that need to change — never the whole page.\n"
    " 2. Preserve the page title, slug, and overall voice / tone / person.\n"
    " 3. Leave unaffected sections untouched (caller will keep them "
    "byte-identical).\n"
    " 4. Use the same markdown style + heading depth as the existing "
    "section content.\n"
    " 5. Cite each new fact inline as [fact_id] so the QA agent can resolve "
    "the source message later.\n"
    " 6. If a section truly does not exist yet but the new fact warrants "
    "one, return a NEW section (id, title, content_md). Otherwise keep the "
    "existing section ids stable.\n"
    "Output a single JSON object: "
    '{"affected_sections": [{"id": str, "title": str, "content_md": str}], '
    '"reason": str}.'
)


def _render_apply_update_prompt(
    page: "WikiPage",
    new_facts: list[dict[str, Any]],
    *,
    target_lang: str = "en",
) -> str:
    """Build the apply_update prompt mirroring WikiCompiler's structure.

    The prompt is a single string (system + JSON user payload). Gemini's
    ``response_mime_type="application/json"`` nudge is set on the call site;
    here we just make the input deterministic + parseable.
    """
    import json

    payload: dict[str, Any] = {
        "page": {
            "page_id": page.page_id,
            "title": page.title,
            "slug": page.slug,
            "page_voice_seed": page.page_voice_seed or "",
            "target_lang": target_lang,
            "last_facts_seen": list(page.last_facts_seen),
            "sections": [
                {
                    "id": s.id,
                    "title": s.title,
                    "content_md": s.content_md,
                }
                for s in page.sections
            ],
        },
        "new_facts": [
            {
                "id": f.get("id", ""),
                "memory_text": f.get("memory_text", ""),
                "cluster_id": f.get("cluster_id"),
                "entity_tags": list(f.get("entity_tags") or []),
                "fact_type": f.get("fact_type", ""),
                "source_message_id": f.get("source_message_id", ""),
            }
            for f in new_facts
        ],
    }
    # Legacy single-prompt path — flag-OFF byte-identical guarantee.
    # The pinned-page addendum lives ONLY on the kind-dispatch path
    # (``_render_kind_prompt``) so this prompt stays identical to its
    # pre-redesign form. Operators who pin pages while the flag is OFF
    # still see ``pin_state.pinned=True`` persisted (curation API runs
    # regardless of the flag) — the addendum starts steering the LLM
    # the moment ``WIKI_LLM_NATIVE_REDESIGN`` flips ON.
    return (
        _APPLY_UPDATE_SYSTEM_PROMPT
        + "\n\n--- INPUT ---\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n--- OUTPUT (JSON only) ---\n"
    )


def _parse_apply_update_response(raw: str) -> list["WikiPageSection"]:
    """Parse the LLM response into a list of ``WikiPageSection``.

    Returns an empty list on any parse error so the caller treats the
    response as "do nothing" rather than corrupting the page.
    """
    import json

    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "event=wiki_maintainer_response_parse_failed raw_len=%d",
            len(raw),
        )
        return []

    if not isinstance(parsed, dict):
        return []
    affected_raw = parsed.get("affected_sections")
    if not isinstance(affected_raw, list):
        return []

    out: list[WikiPageSection] = []
    for entry in affected_raw:
        if not isinstance(entry, dict):
            continue
        section_id = str(entry.get("id", "")).strip()
        content_md = str(entry.get("content_md", "")).strip()
        if not section_id or not content_md:
            continue
        title = str(entry.get("title", "")).strip() or section_id.title()
        out.append(
            WikiPageSection(
                id=section_id,
                title=title,
                content_md=content_md,
            )
        )
    return out


def _slug_to_title_fallback(page_id: str) -> str:
    """Convert a page_id slug into a human-friendly title.

    Used as the universal fallback when the cluster / entity registry
    lookup wired in §4 doesn't yield a better answer.
    """
    if not page_id:
        return "Untitled"
    bare = page_id.split(":", 1)[-1]
    parts = [p for p in bare.replace("_", "-").split("-") if p]
    if not parts:
        return page_id
    return " ".join(p.capitalize() for p in parts)


# Role pages have fixed human-readable titles. Role page_ids are NOT
# prefixed with ``topic:`` or ``entity:`` — they are flat slugs that
# match the literal `_slug_for_fact_type` returns.
_ROLE_PAGE_TITLES: dict[str, str] = {
    "decisions": "Decisions",
    "faq": "Frequently Asked Questions",
    "action-items": "Action Items",
}


def _split_page_id(page_id: str) -> list[tuple[str, str]]:
    """Classify a ``page_id`` into ``(kind, identifier)`` tuples.

    Returns a list because callers iterate it (the iteration is a single
    classification pass; structuring as a list keeps the call site
    branchless). ``kind`` is one of ``"topic"``, ``"entity"``, ``"role"``,
    or ``"unknown"``.
    """
    if not page_id:
        return [("unknown", "")]
    if page_id.startswith("topic:"):
        return [("topic", page_id.split(":", 1)[1])]
    if page_id.startswith("entity:"):
        return [("entity", page_id.split(":", 1)[1])]
    if page_id in _ROLE_PAGE_TITLES:
        return [("role", page_id)]
    return [("unknown", page_id)]


class WikiMaintainer:
    """Subscribes to ExtractionWorker events and incrementally maintains
    the per-page wiki documents.

    Stateless — every call recomputes the routing from the freshly
    extracted facts. The only state is in ``WikiPageStore`` (per-page
    docs) and ``WikiCache`` (legacy, soon to be deprecated).
    """

    def __init__(
        self,
        page_store: WikiPageStore,
        llm_provider: Any | None = None,
        graph_store: Any | None = None,
        *,
        debounce_seconds: float | None = None,
        mode: str | None = None,
    ) -> None:
        self._page_store = page_store
        # ``llm_provider`` is only required for ``apply_update`` —
        # routing (``plan_updates``) MUST NOT call any LLM. Tests
        # leave it None to lock in that invariant.
        self._llm_provider = llm_provider
        # ``graph_store`` is the optional cross-link target. The
        # ``wiki-llm-native-redesign`` change uses this to persist
        # ``WikiPage`` nodes + ``REFERENCES`` edges. None / non-Neo4j
        # backends are tolerated (cross-links resolve and persist to
        # Mongo regardless; the graph upsert no-ops via a hasattr check).
        self._graph_store = graph_store
        # Per-(channel, page) timestamps of the most recent drift comparator
        # invocation. Trimmed of entries older than 5 min on each insert so
        # this never grows unbounded — the rate limiter only needs the most
        # recent timestamp per key, the trim is just memory-bounding.
        self._drift_compare_last_run: dict[tuple[str, str], float] = {}
        # Rolling-window observability counters. ``apply_update_records`` is
        # ``[(monotonic_ts, page_kind), ...]`` trimmed to the last 60 min;
        # ``mark_dirty_records`` is ``[monotonic_ts, ...]`` (one entry per
        # page that flipped to dirty); ``apply_update_failures`` is capped
        # at 10 entries (oldest first) per the spec for the metrics endpoint.
        self._apply_update_records: list[tuple[float, str]] = []
        self._mark_dirty_records: list[float] = []
        self._apply_update_failures: list[dict[str, Any]] = []
        # ── Debounced auto-mode dispatch (sync-pipeline-feedback §B3) ────
        # Per-page in-memory dirty-set keyed on ``(channel_id, page_id)``.
        # Each entry collects every fact_id seen across multiple
        # ``on_extraction_done`` events touching that page within the
        # debounce window. One scheduled flush task drains the entire set
        # in one pass, issuing one rewrite per page (carrying every
        # accumulated fact_id). See decision D3.
        self._dirty: dict[tuple[str, str], set[str]] = {}
        # Lazy-init: ``asyncio.Lock`` requires a running loop in older
        # Python releases and the WikiMaintainer is constructed from sync
        # contexts (test fixtures, app startup before the loop runs). The
        # actual lock is created on first awaited touch.
        self._dirty_lock: asyncio.Lock | None = None
        self._flush_task: asyncio.Task[None] | None = None
        # ``debounce_seconds`` overrides the env-driven default. Settings
        # is consulted lazily (not in __init__) because importing
        # ``infra.config`` here can pull pydantic into module-load time
        # for tests that monkeypatch it.
        self._debounce_seconds_override: float | None = debounce_seconds
        # Per-instance mode override; when None, ``on_extraction_done``
        # falls back to the per-call ``mode`` keyword and ultimately the
        # ``WIKI_MAINTENANCE_MODE`` setting wired by the app.
        self._mode_override: str | None = mode

    # ------------------------------------------------------------------
    # Deterministic routing — no LLM call
    # ------------------------------------------------------------------

    def plan_updates(self, facts: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Group fact ids by the page_id they affect.

        Routing rules (deterministic, no LLM call):
          * ``fact.cluster_id`` → ``topic:<safe-cluster-id>``
          * if any ``fact.entity_tags`` are present → BOTH ``people``
            AND ``glossary`` (single canonical pages absorb the
            per-entity intent; the per-kind prompts decide which
            entries — people vs non-people — surface in each page)
          * ``fact.fact_type=="decision"`` → ``decisions``
          * ``fact.fact_type=="question"`` → ``faq``
          * ``fact.fact_type=="action_item"`` → ``action-items``

        ``unified-llm-wiki-graph-redesign``: this is the redesign
        routing. The legacy ``entity:<slug>`` page kind is dead — the
        maintainer NEVER emits per-entity page targets, regardless of
        configuration. People & Glossary pages absorb the per-entity
        intent.

        Same input always yields the same routing — invariant under
        retry. Empty entity_tags / cluster_id are tolerated; the fact
        contributes only to the role page (if any).

        Returns ``{page_id: [fact_id, ...]}``. Order within each list
        matches the input order so subsequent rewrites are stable.
        """
        plan: dict[str, list[str]] = {}

        def _add(page_id: str, fact_id: str) -> None:
            if not page_id or not fact_id:
                return
            existing = plan.setdefault(page_id, [])
            # Avoid duplicate fact ids when the same fact lands on the
            # same page via multiple routing rules (e.g., cluster +
            # role) — an entity-tagged fact contributing to both
            # people + glossary should NOT inflate either list with the
            # same fact id twice.
            if fact_id not in existing:
                existing.append(fact_id)

        for fact in facts:
            fact_id = str(fact.get("id") or fact.get("fact_id") or "")
            if not fact_id:
                continue
            cluster_id = fact.get("cluster_id")
            if cluster_id:
                _add(_slug_for_topic(str(cluster_id)), fact_id)
            entity_tags = fact.get("entity_tags", []) or []
            if entity_tags:
                # Two pages instead of N entity pages — same fact
                # event, bounded LLM fan-out. The per-kind prompts in
                # ``wiki/prompts.py`` filter what each page surfaces.
                _add("people", fact_id)
                _add("glossary", fact_id)
            fact_type = str(fact.get("fact_type") or "")
            role_slug = _slug_for_fact_type(fact_type)
            if role_slug:
                _add(role_slug, fact_id)
        return plan

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _resolve_debounce_seconds(self) -> float:
        """Pick the debounce window for the next flush.

        Order of precedence: per-instance constructor override → env var
        via ``Settings.wiki_maintainer_debounce_seconds`` → 60s default.
        """
        if self._debounce_seconds_override is not None:
            return float(self._debounce_seconds_override)
        try:
            from beever_atlas.infra.config import get_settings

            return float(get_settings().wiki_maintainer_debounce_seconds)
        except Exception:  # noqa: BLE001 — fall back to spec default
            return 60.0

    def _resolve_settle_debounce_seconds(self) -> float:
        """Pick the debounce window for the settle-path flush.

        Used exclusively by :meth:`on_memory_settled`. The settle event is
        terminal (fires once per channel-drain), so only a tiny grace window
        is needed — unlike the 60s mid-sync coalescing debounce.

        Order of precedence: per-instance constructor override (same knob as
        the main debounce, so tests that set debounce=0 get immediate flush
        on both paths) → env var via
        ``Settings.wiki_maintainer_settle_debounce_seconds`` → 5s default.
        """
        if self._debounce_seconds_override is not None:
            return float(self._debounce_seconds_override)
        try:
            from beever_atlas.infra.config import get_settings

            return float(get_settings().wiki_maintainer_settle_debounce_seconds)
        except Exception:  # noqa: BLE001 — fall back to spec default
            return 5.0

    def _get_dirty_lock(self) -> asyncio.Lock:
        """Lazily create the dirty-set lock on first awaited touch.

        The maintainer must be importable from sync contexts (test
        fixtures, app startup before the loop is running). The lock is
        bound to the active loop on first use.
        """
        if self._dirty_lock is None:
            self._dirty_lock = asyncio.Lock()
        return self._dirty_lock

    async def on_memory_changed(
        self,
        channel_id: str,
        fact_ids: list[str],
        *,
        target_lang: str = "en",
    ) -> dict[str, int]:
        """``memory_changed`` subscriber — accumulator path only.

        Routes ``fact_ids`` to affected pages via the existing
        ``plan_updates`` pipeline, then enqueues each ``(channel_id,
        page_id, fact_ids)`` tuple into ``wiki_dirty_queue`` so the
        state survives backend restarts. NEVER schedules a debounced
        flush — that happens in :meth:`on_memory_settled` once the
        channel's extraction queue drains.

        Also writes through to the legacy in-memory ``_dirty`` dict
        for the deprecation window so existing flush paths keep
        working. The in-memory cache is removed in a follow-up commit.
        """
        counters: dict[str, int] = {"affected_pages": 0}
        if not fact_ids:
            return counters

        affected_pages = await self._route_facts_to_pages(
            channel_id, fact_ids, target_lang=target_lang
        )
        counters["affected_pages"] = len(affected_pages)

        # Write to the durable queue first so a crash before the
        # in-memory write still preserves the work.
        try:
            from beever_atlas.stores import get_stores

            stores = get_stores()
            for page_id, page_fact_ids in affected_pages.items():
                await stores.mongodb.enqueue_dirty(
                    channel_id=channel_id,
                    page_id=page_id,
                    fact_ids=list(page_fact_ids),
                )
        except Exception:  # noqa: BLE001 — never destabilise the worker hot path
            logger.exception(
                "wiki_maintainer.on_memory_changed: enqueue_dirty failed channel=%s",
                channel_id,
            )

        # Mirror into legacy in-memory dirty set so the existing flush
        # path (which reads ``self._dirty``) continues to work during
        # the deprecation window. Removed in the cleanup commit.
        async with self._get_dirty_lock():
            for page_id, page_fact_ids in affected_pages.items():
                key = (channel_id, page_id)
                self._dirty.setdefault(key, set()).update(page_fact_ids)

        logger.info(
            "wiki_maintainer.on_memory_changed channel=%s affected=%d (queue-only, no flush)",
            channel_id,
            counters["affected_pages"],
        )
        return counters

    async def on_memory_settled(
        self,
        channel_id: str,
        *,
        target_lang: str = "en",
    ) -> dict[str, int]:
        """``memory_settled`` subscriber — terminal trigger.

        Schedules one debounced flush task for the channel. Multiple
        ``memory_settled`` events for the same channel within the
        debounce window collapse to a single flush (idempotent
        scheduling via ``_ensure_flush_scheduled``).

        Uses a short settle-path debounce (default 5s, configured via
        ``WIKI_MAINTAINER_SETTLE_DEBOUNCE_SECONDS``) rather than the
        60s mid-sync debounce — the queue has already drained so there
        is nothing left to coalesce; the tiny window only covers a race
        where extraction barely missed the queue-drain check.

        Manual mode (``WIKI_MAINTENANCE_MODE=manual``) skips the
        scheduled flush — the operator's "Maintain Wiki" button is
        the trigger via :meth:`maintain_now`.
        """
        # Determine the effective mode the same way on_extraction_done does.
        effective_mode = self._mode_override or self._resolve_default_mode()
        if effective_mode == "manual":
            logger.info(
                "wiki_maintainer.on_memory_settled channel=%s mode=manual "
                "(flush deferred to operator)",
                channel_id,
            )
            return {"scheduled": 0}

        debounce = self._resolve_settle_debounce_seconds()
        if debounce <= 0:
            # Immediate flush — preserves the legacy synchronous path
            # used by some unit tests.
            rewritten = await self._flush_dirty(target_lang=target_lang)
            logger.info(
                "wiki_maintainer.on_memory_settled channel=%s rewritten=%d "
                "(debounce=0, inline flush)",
                channel_id,
                rewritten,
            )
            return {"scheduled": 0, "rewritten": rewritten}

        self._ensure_flush_scheduled(debounce, target_lang=target_lang)
        logger.info(
            "wiki_maintainer.on_memory_settled channel=%s debounce_seconds=%.1f (flush scheduled)",
            channel_id,
            debounce,
        )
        return {"scheduled": 1}

    def _resolve_default_mode(self) -> str:
        """Read the global ``WIKI_MAINTENANCE_MODE`` env setting."""
        from beever_atlas.infra.config import get_settings

        try:
            return get_settings().wiki_maintenance_mode or "auto"
        except Exception:  # noqa: BLE001
            return "auto"

    async def on_extraction_done(
        self,
        channel_id: str,
        fact_ids: list[str],
        *,
        target_lang: str = "en",
        mode: str = "manual",
    ) -> dict[str, Any]:
        """Hook invoked from ExtractionWorker after a successful batch.

        ``mode`` toggles between ``auto`` (route facts to affected pages
        and schedule a debounced flush; the actual per-page LLM rewrites
        run later inside ``_debounced_flush``) and ``manual`` (mark pages
        dirty synchronously; user processes them later via the Maintain
        Wiki button — manual mode bypasses the debounce path entirely).

        ``fact_ids`` are the newly extracted facts. The maintainer loads
        their full records from Weaviate so routing can read
        ``cluster_id`` + ``entity_tags`` + ``fact_type``.

        Auto mode is debounced (default 60s window): N events touching the
        same page within the window collapse to ONE rewrite carrying every
        event's facts. The returned ``rewritten`` counter therefore counts
        only rewrites flushed inline (when ``debounce_seconds`` resolves to
        0); a positive ``debounce_seconds`` records ``rewritten=0`` and the
        flush task tallies its own log line.

        Returns a counters dict for observability:
            {
                "affected_pages": int,
                "marked_dirty": int,
                "rewritten": int,
            }
        """
        counters: dict[str, int] = {
            "affected_pages": 0,
            "marked_dirty": 0,
            "rewritten": 0,
        }
        if not fact_ids:
            return counters

        # Per-instance ``mode`` override wins when set (tests construct
        # ``WikiMaintainer(..., mode="auto")`` to force auto without a
        # process-wide setting flip).
        effective_mode = self._mode_override or mode

        affected_pages = await self._route_facts_to_pages(
            channel_id, fact_ids, target_lang=target_lang
        )
        counters["affected_pages"] = len(affected_pages)

        # Surface high-overlap merge candidates as proposals (§5.8).
        # Best-effort — a Mongo write hiccup must not stall the
        # extraction event handler.
        try:
            await self._record_merge_proposals(channel_id=channel_id, target_lang=target_lang)
        except Exception:  # noqa: BLE001 — best-effort
            logger.exception(
                "event=wiki_merge_proposal_record_failed channel_id=%s",
                channel_id,
            )

        if effective_mode == "manual":
            modified = await self._page_store.mark_dirty(
                channel_id, list(affected_pages.keys()), target_lang=target_lang
            )
            counters["marked_dirty"] = modified
            self._record_mark_dirty(modified)
            logger.info(
                "wiki_maintainer.on_extraction_done channel=%s mode=manual "
                "affected=%d marked_dirty=%d",
                channel_id,
                counters["affected_pages"],
                counters["marked_dirty"],
            )
            return counters

        # auto mode — accumulate into dirty-set, schedule one debounced
        # flush. A burst of N events touching the same page within the
        # window collapses to a single rewrite at flush time.
        debounce = self._resolve_debounce_seconds()
        async with self._get_dirty_lock():
            for page_id, page_fact_ids in affected_pages.items():
                key = (channel_id, page_id)
                self._dirty.setdefault(key, set()).update(page_fact_ids)

        if debounce <= 0:
            # Immediate flush — used by unit tests that need synchronous
            # ``rewritten`` counters and by operators who want the legacy
            # synchronous-rewrite behaviour.
            counters["rewritten"] = await self._flush_dirty(target_lang=target_lang)
            logger.info(
                "wiki_maintainer.on_extraction_done channel=%s mode=auto "
                "affected=%d rewritten=%d (debounce=0, inline flush)",
                channel_id,
                counters["affected_pages"],
                counters["rewritten"],
            )
            return counters

        self._ensure_flush_scheduled(debounce, target_lang=target_lang)
        logger.info(
            "wiki_maintainer.on_extraction_done channel=%s mode=auto "
            "affected=%d debounce_seconds=%.1f (flush scheduled)",
            channel_id,
            counters["affected_pages"],
            debounce,
        )
        return counters

    # ------------------------------------------------------------------
    # Debounced flush (sync-pipeline-feedback-and-auto-wiki §B3)
    # ------------------------------------------------------------------

    async def _route_facts_to_pages(
        self,
        channel_id: str,
        fact_ids: list[str],
        *,
        target_lang: str = "en",
    ) -> dict[str, list[str]]:
        """Load full fact records and route them to affected page_ids.

        Wraps the deterministic ``plan_updates`` + ``_apply_merge_redirects``
        pipeline so the dirty-set accumulator and the legacy synchronous
        path share one routing implementation. Tests can monkeypatch this
        method to exercise the debounce mechanics without seeding
        Weaviate.
        """
        facts = await self._load_facts(channel_id, fact_ids)
        plan = self.plan_updates(facts)
        # Curation-aware re-routing (§5.6): pages with `merged_into` set
        # forward their fact list to the merge target so future plan
        # outputs converge on the canonical page.
        plan = await self._apply_merge_redirects(
            plan, channel_id=channel_id, target_lang=target_lang
        )
        return plan

    def _ensure_flush_scheduled(self, debounce_seconds: float, *, target_lang: str = "en") -> None:
        """Schedule one debounced flush task if none is already in flight.

        Idempotent — subsequent calls while the existing task is still
        running (sleep + drain) are no-ops; the events accumulated since
        the last flush will be picked up by the in-flight task's drain.
        """
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — caller is sync-only; cannot schedule.
            # Fall back to leaving the dirty-set accumulating and rely on
            # the next ``on_extraction_done`` to schedule.
            return
        self._flush_task = loop.create_task(
            self._debounced_flush(debounce_seconds, target_lang=target_lang)
        )

    async def _debounced_flush(self, debounce_seconds: float, *, target_lang: str = "en") -> None:
        """Sleep the debounce window, then drain the dirty-set atomically."""
        try:
            if debounce_seconds > 0:
                await asyncio.sleep(debounce_seconds)
            await self._flush_dirty(target_lang=target_lang)
        except Exception:  # noqa: BLE001 — never let a flush kill the maintainer
            logger.exception("wiki_maintainer._debounced_flush crashed")

    async def _flush_dirty(self, *, target_lang: str = "en") -> int:
        """Drain the dirty queue and rewrite each page once.

        memory-then-wiki-pipeline-realignment P0 fix: the durable
        ``wiki_dirty_queue`` is now the authoritative source for dirty
        pages. ``claim_dirty(channel_id)`` atomically transitions
        pending rows to ``flushing`` so a crash mid-flush leaves rows
        that ``recover_stale_flushing`` can revive on the next startup.
        After successful per-page rewrite, ``mark_dirty_done`` flips
        each row to ``status="done"``.

        The legacy in-memory ``_dirty`` snapshot path is retained as a
        fallback for tests that manually populate ``self._dirty``
        without the queue. Production callers always go through
        ``on_memory_changed`` which writes to both stores; the queue
        path wins when both are available.

        ``unified-llm-wiki-graph-redesign`` D8 — first-sync gate: when a
        channel's wiki has not been built yet (no ``wiki_pages`` rows
        exist for it), the maintainer's flush MUST defer that channel's
        dirty entries instead of creating pages on-the-fly. The Builder
        owns first-sync page creation; the maintainer only patches
        existing pages.
        """
        # ── Primary path: drain the durable queue ────────────────────
        # Identify channels with pending dirty work. Without a global
        # ``list_dirty_channels`` helper we walk the in-memory set as a
        # proxy (callers always co-write both). If the queue has rows
        # the in-memory set doesn't know about (post-crash recovery),
        # they get picked up on the NEXT flush triggered by either a
        # ``memory_settled`` event or the operator's ``maintain_now``.
        async with self._get_dirty_lock():
            in_memory_channels: set[str] = {ch for (ch, _pid) in self._dirty}

        snapshot: dict[tuple[str, str], set[str]] = {}
        claimed_doc_ids: dict[tuple[str, str], Any] = {}

        try:
            from beever_atlas.stores import get_stores

            stores = get_stores()
            for channel_id in in_memory_channels:
                try:
                    claimed = await stores.mongodb.claim_dirty(channel_id)
                except Exception:  # noqa: BLE001 — best-effort; fall back to in-memory
                    claimed = []
                for row in claimed:
                    page_id = str(row.get("page_id", ""))
                    fact_ids = list(row.get("fact_ids", []) or [])
                    if not page_id:
                        continue
                    snapshot[(channel_id, page_id)] = set(fact_ids)
                    claimed_doc_ids[(channel_id, page_id)] = row.get("_id")
        except Exception:  # noqa: BLE001 — store may not be initialised in test paths
            pass

        # ── Fallback path: in-memory snapshot for tests/legacy ───────
        # Merge any in-memory entries the queue didn't return (this
        # primarily catches tests that write to ``self._dirty`` directly).
        async with self._get_dirty_lock():
            for key, facts in self._dirty.items():
                if key not in snapshot:
                    snapshot[key] = set(facts)
                else:
                    snapshot[key].update(facts)
            self._dirty.clear()

        if not snapshot:
            return 0

        # First-sync gate: group by channel_id and check whether the
        # Builder has run yet (any wiki_pages row exists). Channels with
        # no pages get their dirty entries re-deferred so the Builder
        # owns first-sync page creation. Channels with pages flow
        # normally.
        #
        # Conservative defer: only defer when we're CERTAIN the page
        # list is an empty real list. Mocks, exceptions, or non-list
        # return values (test harness stubs) fall through to legacy
        # behaviour so we don't block tests that don't seed pages.
        channels_seen: set[str] = {ch for (ch, _pid) in snapshot}
        deferred_channels: set[str] = set()
        for channel_id in channels_seen:
            try:
                pages = await self._page_store.list_pages(channel_id, target_lang)
                if not isinstance(pages, list):
                    # Tests with AsyncMock often return a Mock rather
                    # than a real list. Skip the gate — the legacy
                    # path's create-on-the-fly behaviour applies.
                    continue
                # Filter archived rows so legacy kind=entity stragglers
                # don't accidentally satisfy the "Builder has run" check.
                active_pages = [p for p in pages if not getattr(p, "archived", False)]
                if not active_pages:
                    deferred_channels.add(channel_id)
            except Exception:  # noqa: BLE001 — best-effort gate
                # If the page-store check fails, fall through to the
                # legacy behaviour (let the maintainer create pages).
                # Failing closed here would silently stall channels
                # whose wiki actually exists but the lookup hiccupped.
                pass
        if deferred_channels:
            async with self._get_dirty_lock():
                for (ch, page_id), fact_ids in snapshot.items():
                    if ch not in deferred_channels:
                        continue
                    self._dirty.setdefault((ch, page_id), set()).update(fact_ids)
            logger.info(
                "wiki_maintainer._flush_dirty deferred channels=%s pages=%d "
                "(Builder hasn't run yet — first-sync gate)",
                sorted(deferred_channels),
                sum(1 for (ch, _pid) in snapshot if ch in deferred_channels),
            )

        rewritten = 0
        # Collect doc_ids that completed successfully so we can mark
        # them done in a single bulk update at the end of the flush.
        done_ids: list[Any] = []
        for (channel_id, page_id), fact_ids in snapshot.items():
            if channel_id in deferred_channels:
                continue
            try:
                applied = await self._rewrite_page(
                    channel_id, page_id, sorted(fact_ids), target_lang=target_lang
                )
                if applied:
                    rewritten += 1
                    # Mark the queue row done — incrementally finalised
                    # rather than all-or-nothing so a crash mid-flush
                    # leaves only the failed pages in ``flushing`` state
                    # for ``recover_stale_flushing`` to revive.
                    doc_id = claimed_doc_ids.get((channel_id, page_id))
                    if doc_id is not None:
                        done_ids.append(doc_id)
            except Exception:  # noqa: BLE001 — one bad page must not stall others
                logger.exception(
                    "wiki_maintainer._flush_dirty: rewrite failed channel=%s page=%s fact_count=%d",
                    channel_id,
                    page_id,
                    len(fact_ids),
                )
        # Bulk-mark completed rows as done. Done as a tail operation so
        # one bad-mongo-write doesn't lose the work already completed.
        if done_ids:
            try:
                from beever_atlas.stores import get_stores

                await get_stores().mongodb.mark_dirty_done(done_ids)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "wiki_maintainer._flush_dirty: mark_dirty_done failed for %d rows",
                    len(done_ids),
                )
        logger.info(
            "wiki_maintainer._flush_dirty pages=%d rewritten=%d deferred=%d",
            len(snapshot),
            rewritten,
            sum(1 for (ch, _pid) in snapshot if ch in deferred_channels),
        )
        return rewritten

    async def _rewrite_page(
        self,
        channel_id: str,
        page_id: str,
        fact_ids: list[str],
        *,
        target_lang: str = "en",
    ) -> bool:
        """Per-page rewrite seam called by ``_flush_dirty``.

        Wraps ``apply_update`` so tests can override one method without
        replacing the whole flush pipeline. Returns True iff
        ``apply_update`` actually rewrote the page (vs. skipped because
        every fact_id was already in ``last_facts_seen``).
        """
        return await self.apply_update(
            channel_id=channel_id,
            page_id=page_id,
            new_fact_ids=fact_ids,
            target_lang=target_lang,
        )

    async def on_consolidation_complete(
        self,
        channel_id: str,
        fact_ids: list[str],
        *,
        target_lang: str = "en",
        mode: str = "manual",
    ) -> dict[str, Any]:
        """Hook invoked after consolidation finishes for a channel.

        Routes the consolidation's touched fact ids into the dirty queue via
        the accumulator path (:meth:`on_memory_changed`) so they are picked
        up by the next terminal flush. Does NOT schedule a debounced flush —
        ``memory_settled`` owns that, ensuring the wiki only projects from a
        stable memory state. Empty ``fact_ids`` is a no-op.

        The ``mode`` argument is accepted for backwards-compatibility with
        callers that pre-date the memory-then-wiki realignment but is unused
        here: routing is always non-flushing. Manual-mode operators trigger
        page rewrites via the ``Maintain Wiki`` button which calls
        :meth:`maintain_now`.
        """
        del mode  # accepted for backwards-compat; flushes are owned by memory_settled
        return await self.on_memory_changed(channel_id, fact_ids, target_lang=target_lang)

    async def maintain_now(self, channel_id: str, target_lang: str = "en") -> dict[str, int]:
        """Drain the dirty page queue for one channel — used by the
        manual-mode ``Maintain Wiki`` button.

        Returns ``{rewritten, errors}`` counters.
        """
        counters: dict[str, int] = {"rewritten": 0, "errors": 0}
        pages = await self._page_store.list_pages(channel_id, target_lang)
        dirty = [p for p in pages if p.is_dirty]
        for page in dirty:
            try:
                # The maintainer doesn't know which facts triggered
                # the dirty flag — it processes whatever the page's
                # last_facts_seen has missed. Production wires
                # ``_load_facts`` to fetch the channel's full fact
                # set; tests stub it to a fixed list.
                channel_facts = await self._load_facts(channel_id, None)
                already_seen = set(page.last_facts_seen)
                new_fact_ids = [
                    str(f.get("id") or "")
                    for f in channel_facts
                    if str(f.get("id") or "") not in already_seen
                ]
                applied = await self.apply_update(
                    channel_id=channel_id,
                    page_id=page.page_id,
                    new_fact_ids=new_fact_ids,
                    target_lang=target_lang,
                )
                if applied:
                    counters["rewritten"] += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "wiki_maintainer.maintain_now failed channel=%s page=%s",
                    channel_id,
                    page.page_id,
                )
                counters["errors"] += 1
        return counters

    # ------------------------------------------------------------------
    # Per-page LLM rewrite (the actual maintainer)
    # ------------------------------------------------------------------

    async def apply_update(
        self,
        channel_id: str,
        page_id: str,
        new_fact_ids: list[str],
        *,
        target_lang: str = "en",
    ) -> bool:
        """Invoke ONE per-page LLM call to integrate ``new_fact_ids``
        into the affected sections of one wiki page.

        Preserves: title, slug, page_voice_seed, and unaffected
        sections (byte-identical). Bumps version. Clears is_dirty.

        Returns True if the page was rewritten; False if there was
        nothing to do (e.g. all ``new_fact_ids`` were already in
        ``last_facts_seen``) or the LLM call failed (in which case the
        page is left unchanged and a structured error is logged).
        """
        # wiki-redesign-gap-fill / Group 1 — wrap apply_update in try/finally
        # so every return path emits agent_state(done) for the maintainer.
        # Without this, a frozen / manual / no-new-facts early-return would
        # leave SyncMonitor's LED stuck on "running".
        from beever_atlas.services.pipeline_events import (
            emit_agent_state as _emit_agent_state_local,
        )

        _maint_started_at = time.monotonic()
        _emit_agent_state_local(channel_id, "wiki_maintainer", "running", batch_id=page_id)
        try:
            return await self._apply_update_inner(
                channel_id, page_id, new_fact_ids, target_lang=target_lang
            )
        finally:
            _emit_agent_state_local(
                channel_id,
                "wiki_maintainer",
                "done",
                batch_id=page_id,
                elapsed_ms=int((time.monotonic() - _maint_started_at) * 1000),
            )

    async def _apply_update_inner(
        self,
        channel_id: str,
        page_id: str,
        new_fact_ids: list[str],
        *,
        target_lang: str = "en",
    ) -> bool:
        """Inner body of :meth:`apply_update`.

        Split out so the surrounding ``apply_update`` wrapper can emit
        ``agent_state(running/done)`` events around every return path.
        """
        page = await self._page_store.get_page(channel_id, page_id, target_lang=target_lang)
        already_seen = set(page.last_facts_seen) if page else set()
        truly_new = [fid for fid in new_fact_ids if fid not in already_seen]
        if not truly_new:
            return False

        # ``unified-llm-wiki-graph-redesign`` — honor per-page curation
        # mode before invoking the LLM. ``manual`` pages stay dirty
        # until an operator explicitly triggers the rewrite; ``frozen``
        # pages skip even mark-dirty. Legacy ``pin_state.pinned=True``
        # rows are treated as ``manual`` so existing operator pins
        # don't suddenly start auto-rewriting.
        if page is not None:
            mode = _resolve_curation_mode(page)
            if mode == "frozen":
                logger.info(
                    "event=wiki_maintainer_apply_update_skipped_frozen channel_id=%s page_id=%s",
                    channel_id,
                    page_id,
                )
                return False
            if mode == "manual":
                # Mark dirty so the operator's "Apply Pending Updates"
                # button has work to do, but do NOT invoke the LLM
                # automatically. Best-effort mark-dirty.
                try:
                    await self._page_store.mark_dirty(
                        channel_id, [page_id], target_lang=target_lang
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "event=wiki_maintainer_manual_mark_dirty_failed channel=%s page=%s",
                        channel_id,
                        page_id,
                    )
                logger.info(
                    "event=wiki_maintainer_apply_update_deferred_manual channel_id=%s page_id=%s",
                    channel_id,
                    page_id,
                )
                return False

        # Load full fact records for the prompt. ``fetch_by_ids`` is the
        # cheap path (one Weaviate object lookup per id); even when
        # ``_load_facts`` is monkeypatched in tests, calling it here keeps
        # the production wiring honest.
        new_facts = await self._load_facts(channel_id, truly_new)
        if not new_facts:
            # No fact records resolved — likely a test that didn't seed
            # the loader, or a Weaviate hiccup. Don't write a placeholder
            # page; let the caller retry on the next event.
            logger.warning(
                "event=wiki_maintainer_apply_update_no_facts channel_id=%s page_id=%s requested=%d",
                channel_id,
                page_id,
                len(truly_new),
            )
            return False

        if page is None:
            # wiki-redesign-gap-fill / Group 8 — silent drop for routes to
            # adaptive page kinds that haven't been instantiated yet. The
            # Builder owns initial creation of adaptive pages (predicate-gated);
            # the Maintainer's job is to keep existing pages fresh, not to
            # spawn new pages for kinds the channel doesn't qualify for.
            try:
                from beever_atlas.wiki.kinds import KIND_REGISTRY

                _candidate_kind = derive_kind_from_page_id(page_id)
                _spec = KIND_REGISTRY.get(_candidate_kind)
                if _spec is not None and not _spec.is_required:
                    logger.info(
                        "event=wiki_maintainer_skip_adaptive_route "
                        "channel_id=%s page_id=%s kind=%s",
                        channel_id,
                        page_id,
                        _candidate_kind,
                    )
                    return False
            except Exception:  # noqa: BLE001 — never destabilise apply_update
                pass
            page = WikiPage(
                channel_id=channel_id,
                target_lang=target_lang,
                page_id=page_id,
                title=await self._resolve_first_touch_title(page_id, channel_id),
                slug=page_id.replace(":", "-"),
                kind=derive_kind_from_page_id(page_id),
                sections=[
                    WikiPageSection(
                        id="overview",
                        title="Overview",
                        content_md="",
                    )
                ],
            )

        # ---- per-kind dispatch (wiki-llm-native-redesign §3.8) ----
        # When the redesign flag is OFF, OR the resolved kind isn't one of
        # the known kinds, fall through to the legacy single-prompt path.
        # Behaviour on the legacy branch is byte-identical to pre-redesign.
        from beever_atlas.infra.config import get_settings

        settings = get_settings()
        dispatch_kind = _resolve_dispatch_kind(page)
        use_kind_dispatch = settings.wiki_llm_native_redesign and dispatch_kind in _KNOWN_KINDS

        new_kind_schema: dict[str, Any] | None = None
        affected_sections: list[WikiPageSection]
        if use_kind_dispatch:
            try:
                affected_sections, new_kind_schema = await self._invoke_kind_dispatch_with_retry(
                    channel_id=channel_id,
                    page=page,
                    new_facts=new_facts,
                    kind=dispatch_kind,
                    target_lang=target_lang,
                )
            except Exception as exc:  # noqa: BLE001 — leave page unchanged on any LLM error
                logger.exception(
                    "event=wiki_maintainer_apply_update_llm_failed channel_id=%s page_id=%s err=%s",
                    channel_id,
                    page_id,
                    exc,
                )
                self._record_apply_update_failure(channel_id, page_id, exc)
                return False
        else:
            prompt = _render_apply_update_prompt(page, new_facts, target_lang=target_lang)
            try:
                raw = await self._invoke_apply_update_llm(prompt)
            except Exception as exc:  # noqa: BLE001 — leave page unchanged on any LLM error
                logger.exception(
                    "event=wiki_maintainer_apply_update_llm_failed channel_id=%s page_id=%s err=%s",
                    channel_id,
                    page_id,
                    exc,
                )
                self._record_apply_update_failure(channel_id, page_id, exc)
                return False
            affected_sections = _parse_apply_update_response(raw)

        if not affected_sections:
            logger.warning(
                "event=wiki_maintainer_apply_update_no_affected_sections channel_id=%s page_id=%s",
                channel_id,
                page_id,
            )
            self._record_apply_update_failure(
                channel_id, page_id, ValueError("no_affected_sections")
            )
            # ``unified-llm-wiki-graph-redesign`` — emit a structured
            # parse_failure event so the WikiTab banner counter sees
            # this as a recoverable failure mode. Best-effort.
            try:
                from beever_atlas.services.pipeline_events import (
                    EVENT_TYPE_PARSE_FAILURE,
                    get_pipeline_events,
                )

                get_pipeline_events().record(
                    channel_id=channel_id,
                    stage="wiki_maintenance",
                    label=f"Parse failure on {page_id}",
                    event_type=EVENT_TYPE_PARSE_FAILURE,
                    payload={
                        "page_id": page_id,
                        "raw_len": len(raw) if raw else 0,
                    },
                )
            except Exception:  # noqa: BLE001
                pass
            return False

        # Merge in place so each updated section keeps its original
        # position; only genuinely new sections (ids not already on the
        # page) are appended at the end. This preserves layout across
        # repeated rewrites — without this, an LLM update on the
        # ``"overview"`` section would shift it from the top of the page
        # to the bottom, and the order would drift unpredictably as
        # different sections get touched on different batches.
        affected_map: dict[str, WikiPageSection] = {s.id: s for s in affected_sections}
        merged: list[WikiPageSection] = [affected_map.pop(s.id, s) for s in page.sections]
        # Anything left in ``affected_map`` is a genuinely new section the
        # LLM added (id not already on the page). Append in the order the
        # LLM emitted them (dict insertion order is preserved in 3.7+).
        merged.extend(affected_map.values())

        page.sections = merged
        page.last_facts_seen = sorted(set(page.last_facts_seen) | set(truly_new))
        page.is_dirty = False
        page.updated_at = datetime.now(tz=UTC)
        # On the kind-dispatch path, persist the structured payload too.
        # The flag-OFF / unknown-kind branch leaves ``kind`` and
        # ``kind_schema`` untouched so legacy behaviour stays byte-identical.
        if use_kind_dispatch:
            page.kind = dispatch_kind
            page.kind_schema = new_kind_schema  # may be None on 2x validation failure
            # wiki-redesign-gap-fill / Group 3 — persist the canonical
            # schema hash so the Builder's recompile-skip can detect
            # unchanged inputs on the next regenerate. Best-effort.
            try:
                from beever_atlas.wiki.hashing import compute_kind_schema_hash

                page.kind_schema_hash = compute_kind_schema_hash(dispatch_kind, new_kind_schema)
            except Exception:  # noqa: BLE001
                page.kind_schema_hash = None
        # title, slug, page_voice_seed are intentionally NOT touched here —
        # the LLM contract returns ONLY affected sections, and the merge
        # path only rewrites sections by id. Voice preservation is a
        # structural invariant.

        # Cross-link resolution runs ONLY on the redesign path. The
        # legacy single-prompt does not instruct ``[[wikilink]]`` syntax,
        # so resolving on the legacy path would write empty arrays and
        # be a no-op anyway — but byte-identical behaviour for flag-OFF
        # installs is a hard guarantee, so the call is gated explicitly.
        resolved_slugs: list[str] = []
        if use_kind_dispatch:
            try:
                resolved_map, _broken = await self._persist_cross_links(
                    page, target_lang=target_lang
                )
                # Deduplicate slugs for the Neo4j edge upsert below — two
                # titles can resolve to the same slug (synonym + canonical).
                seen_slug: set[str] = set()
                for slug in resolved_map.values():
                    if slug not in seen_slug:
                        seen_slug.add(slug)
                        resolved_slugs.append(slug)
            except Exception:  # noqa: BLE001 — never destabilise apply_update
                logger.exception(
                    "event=wiki_persist_cross_links_failed channel_id=%s page_id=%s",
                    channel_id,
                    page_id,
                )

        await self._page_store.save_page(page)
        self._record_apply_update_success(page_id)
        # ``unified-llm-wiki-graph-redesign`` — emit a structured
        # wiki_update event so the SyncMonitor's right pane (Wiki
        # Updates) renders this page rewrite live. Best-effort.
        try:
            from beever_atlas.services.pipeline_events import (
                EVENT_TYPE_WIKI_UPDATE,
                get_pipeline_events,
            )

            get_pipeline_events().record(
                channel_id=channel_id,
                stage="wiki_maintenance",
                label=f"Page '{page.title or page_id}' updated",
                event_type=EVENT_TYPE_WIKI_UPDATE,
                payload={
                    "page_id": page_id,
                    "page_title": page.title or page_id,
                    "action": "patched",
                    "facts_integrated": len(truly_new),
                    "version": page.version,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        # Persist the cross-link graph (best-effort). Runs synchronously
        # so the next ``GET /api/channels/{id}/wiki/graph`` reflects the
        # rewrite immediately, but wrapped in try/except inside
        # ``_upsert_wiki_graph`` itself so a Neo4j hiccup never crashes
        # the maintainer's primary path.
        if use_kind_dispatch:
            await self._upsert_wiki_graph(page, resolved_slugs, target_lang=target_lang)
        # Drift A/B comparator (gated by ``Settings.wiki_drift_ab``). MUST run
        # AFTER ``save_page`` succeeds so the comparator sees the canonical
        # incremental output the user will read. The schedule helper is
        # fire-and-forget — it never blocks ``apply_update`` and never
        # propagates exceptions back to the maintainer's primary path.
        try:
            self._schedule_drift_compare(channel_id, page_id, page, target_lang)
        except Exception:  # noqa: BLE001 — never destabilise apply_update
            logger.exception(
                "event=wiki_drift_schedule_failed channel_id=%s page_id=%s",
                channel_id,
                page_id,
            )
        return True

    # ------------------------------------------------------------------
    # wiki-llm-native-redesign §4 — cross-link resolver + Neo4j upsert
    # ------------------------------------------------------------------

    async def _resolve_wikilink(
        self,
        channel_id: str,
        target_lang: str,
        title: str,
    ) -> str | None:
        """Resolve a single ``[[Title]]`` reference to a slug.

        Convenience wrapper for ad-hoc lookups (e.g. from tests or the
        broken-link create-page flow). Loads every page in the channel —
        for batch resolution inside ``apply_update`` use
        ``_persist_cross_links`` which builds the index once.
        """
        pages = await self._page_store.list_pages(channel_id, target_lang=target_lang)
        index = _build_page_index(pages)
        return _resolve_wikilink_against_index(title, index)

    async def _persist_cross_links(
        self,
        page: "WikiPage",
        target_lang: str,
    ) -> tuple[dict[str, str], list[str]]:
        """Parse and resolve every wikilink in ``page.sections``.

        Mutates ``page.cross_links`` (a ``{title: slug}`` mapping) and
        ``page.cross_links_broken`` (titles only) in place; the caller is
        responsible for the subsequent ``save_page`` so resolution +
        persistence land in a single Mongo write. Returns
        ``(resolved_map, broken_titles)`` so the Neo4j upsert call site
        can iterate the resolved slugs without re-extracting them from
        the dict. Self-references are excluded from the index so a page
        never cross-links to itself.
        """
        seen: set[str] = set()
        ordered_titles: list[str] = []
        for section in page.sections:
            for title in _parse_wikilinks(section.content_md):
                if title not in seen:
                    seen.add(title)
                    ordered_titles.append(title)
        if not ordered_titles:
            page.cross_links = {}
            page.cross_links_broken = []
            return {}, []

        all_pages = await self._page_store.list_pages(page.channel_id, target_lang=target_lang)
        index = _build_page_index(all_pages, exclude_self_page_id=page.page_id)

        resolved: dict[str, str] = {}
        broken: list[str] = []
        seen_broken: set[str] = set()
        for title in ordered_titles:
            slug = _resolve_wikilink_against_index(title, index)
            if slug is None:
                if title not in seen_broken:
                    seen_broken.add(title)
                    broken.append(title)
            else:
                # First occurrence wins for the title→slug mapping; if
                # an LLM emits the same title twice (one with diacritics,
                # one without) only the first one's resolution survives.
                resolved.setdefault(title, slug)

        page.cross_links = resolved
        page.cross_links_broken = broken
        return resolved, broken

    async def _upsert_wiki_graph(
        self,
        page: "WikiPage",
        resolved_slugs: list[str],
        *,
        target_lang: str = "en",
    ) -> None:
        """Best-effort Neo4j upsert for the page node + REFERENCES edges.

        Tolerates a missing graph store, a graph backend that doesn't
        expose the wiki helpers (NullGraphStore, NebulaStore until they
        gain parity), and any runtime Neo4j failure. The maintainer's
        primary path stays unaffected — page content is already saved
        to Mongo before this call.

        Also emits ``:REFERENCES_ENTITY`` edges for every wikilink that
        resolves against the entity registry. This is the writer side
        of the WikiPage→Entity bridge — without it the
        ``get_wiki_graph`` reader has nothing to surface.
        """
        store = self._graph_store
        if store is None:
            return
        if not hasattr(store, "upsert_wiki_page_node"):
            return
        if not hasattr(store, "upsert_wiki_reference_edge"):
            return
        try:
            self_slug = page.slug or page.page_id.replace(":", "-")
            await store.upsert_wiki_page_node(
                channel_id=page.channel_id,
                slug=self_slug,
                kind=page.kind,
                title=page.title,
                version=page.version,
                last_updated=page.updated_at,
                target_lang=target_lang,
            )
            for dst_slug in resolved_slugs:
                if not dst_slug or dst_slug == self_slug:
                    continue
                await store.upsert_wiki_reference_edge(
                    channel_id=page.channel_id,
                    src_slug=self_slug,
                    dst_slug=dst_slug,
                    target_lang=target_lang,
                )

            # WikiPage → Entity bridge edges. For each wikilink title in
            # the page body, try to resolve it against the entity
            # registry; when it hits a real ``:Entity`` row, emit a
            # ``:REFERENCES_ENTITY`` edge. A wikilink that resolves
            # to a sibling wiki page (handled above) and ALSO matches a
            # typed entity will produce both edges — that's intentional;
            # the reader filters by edge kind.
            if not hasattr(store, "upsert_wiki_reference_entity_edge"):
                return
            if not hasattr(store, "find_entity_by_name_or_alias"):
                return
            seen_titles: set[str] = set()
            ordered_titles: list[str] = []
            for section in page.sections:
                for title in _parse_wikilinks(section.content_md):
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)
                    ordered_titles.append(title)
            for title in ordered_titles:
                try:
                    canonical = await store.find_entity_by_name_or_alias(title)
                except Exception:  # noqa: BLE001 — never destabilise apply_update
                    canonical = None
                if not canonical:
                    continue
                await store.upsert_wiki_reference_entity_edge(
                    channel_id=page.channel_id,
                    target_lang=target_lang,
                    src_slug=self_slug,
                    entity_name=canonical,
                )
        except Exception:  # noqa: BLE001 — best-effort
            logger.exception(
                "event=wiki_graph_upsert_failed channel_id=%s page_id=%s",
                page.channel_id,
                page.page_id,
            )

    # ------------------------------------------------------------------
    # wiki-llm-native-redesign §5.6 / §5.8 — curation-aware routing
    # ------------------------------------------------------------------

    async def _apply_merge_redirects(
        self,
        plan: dict[str, list[str]],
        *,
        channel_id: str,
        target_lang: str,
    ) -> dict[str, list[str]]:
        """Re-route plan entries whose page has ``merged_into`` set.

        Builds a ``{source_slug: target_slug}`` redirect map by scanning
        every page in the channel; then for each plan entry whose page
        is a merge source, the fact ids are merged into the target's
        entry. Empty plan or no merged pages → original plan returned
        unchanged so the common case stays cheap.
        """
        if not plan:
            return plan
        try:
            pages = await self._page_store.list_pages(channel_id, target_lang=target_lang)
        except Exception:  # noqa: BLE001 — best-effort
            logger.exception("event=wiki_merge_redirect_load_failed channel_id=%s", channel_id)
            return plan
        # ``page_id → target_slug`` redirect, keyed by either page_id or
        # slug since plan entries can be either depending on the routing
        # rule that produced them.
        redirect: dict[str, str] = {}
        slug_to_page_id: dict[str, str] = {}
        for page in pages:
            slug = page.slug or page.page_id.replace(":", "-")
            slug_to_page_id[slug] = page.page_id
            if page.merged_into:
                redirect[page.page_id] = page.merged_into
                redirect[slug] = page.merged_into
        if not redirect:
            return plan
        out: dict[str, list[str]] = {}
        for source_key, fact_ids in plan.items():
            target_slug = redirect.get(source_key)
            if not target_slug:
                out.setdefault(source_key, []).extend(fact_ids)
                continue
            # Translate target_slug back into a page_id when one exists,
            # so apply_update's existing page_id-keyed lookups still
            # resolve. If the target page is missing entirely (rare —
            # operator merged into a target that was deleted out-of-band),
            # fall back to the slug as-is.
            target_page_id = slug_to_page_id.get(target_slug, target_slug)
            out.setdefault(target_page_id, []).extend(fact_ids)
        # Dedupe fact_ids per target so a fact that hit both the source
        # and the target naturally is counted once.
        return {page_id: list(dict.fromkeys(fact_ids)) for page_id, fact_ids in out.items()}

    async def _record_merge_proposals(
        self,
        *,
        channel_id: str,
        target_lang: str,
    ) -> None:
        """Surface high-Jaccard page pairs as ``wiki_merge_proposals`` rows.

        Threshold lives in ``Settings.wiki_page_merge_threshold`` (default
        0.70). Proposals are idempotent on
        ``(channel_id, target_lang, source_slug, target_slug)`` so the
        same pair surfacing on every event handler tick does not
        compound. The collection handle comes from the configured
        Mongo store; the helper is a no-op when the store does not
        expose one (test fakes typically don't).
        """
        from beever_atlas.infra.config import get_settings

        try:
            from beever_atlas.stores import get_stores
        except Exception:  # noqa: BLE001 — testing without app stores
            return
        try:
            stores = get_stores()
        except Exception:  # noqa: BLE001 — singleton not initialised in tests
            return
        proposals_collection = getattr(stores.mongodb, "wiki_merge_proposals", None)
        if proposals_collection is None:
            return
        threshold = float(getattr(get_settings(), "wiki_page_merge_threshold", 0.70))
        candidates = await self._page_store.find_merge_candidates(
            channel_id, threshold=threshold, target_lang=target_lang
        )
        now = datetime.now(tz=UTC).isoformat()
        for source_slug, target_slug, jaccard in candidates:
            doc = {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "source_slug": source_slug,
                "target_slug": target_slug,
                "jaccard": jaccard,
                "status": "open",
                "surfaced_at": now,
            }
            try:
                await proposals_collection.update_one(
                    {
                        "channel_id": channel_id,
                        "target_lang": target_lang,
                        "source_slug": source_slug,
                        "target_slug": target_slug,
                    },
                    {
                        "$setOnInsert": {**doc, "created_at": now},
                        "$set": {"jaccard": jaccard, "surfaced_at": now},
                    },
                    upsert=True,
                )
            except Exception:  # noqa: BLE001 — single-pair failure logged + skipped
                logger.exception(
                    "event=wiki_merge_proposal_upsert_failed channel_id=%s source=%s target=%s",
                    channel_id,
                    source_slug,
                    target_slug,
                )

    async def _invoke_kind_dispatch_with_retry(
        self,
        *,
        channel_id: str,
        page: "WikiPage",
        new_facts: list[dict[str, Any]],
        kind: str,
        target_lang: str,
    ) -> tuple[list["WikiPageSection"], dict[str, Any] | None]:
        """Invoke the per-kind apply_update LLM call with one schema-retry.

        Returns ``(affected_sections, kind_schema)``. ``kind_schema`` is
        None when both attempts failed JSON Schema validation — the caller
        keeps the markdown sections (so the page is still updated) and a
        ``wiki_kind_schema_validation_failed`` warning is emitted. The LLM
        invocation reuses ``_invoke_apply_update_llm`` so tests that
        monkeypatch the legacy hook also exercise this path.
        """
        last_validation_error: str | None = None
        affected_sections: list[WikiPageSection] = []

        for attempt in (0, 1):
            prompt = _render_kind_prompt(
                kind,
                page,
                new_facts,
                target_lang=target_lang,
                retry_validation_error=(last_validation_error if attempt == 1 else None),
            )
            raw = await self._invoke_apply_update_llm(prompt)
            attempt_sections, attempt_schema = _parse_kind_response(raw)

            # Always honor the most recent affected_sections — even if both
            # attempts fail schema validation, the markdown body should
            # still land so the page is not silently stuck.
            affected_sections = attempt_sections

            if attempt_schema is None:
                last_validation_error = "response missing or non-object kind_schema"
                continue
            error = _validate_kind_schema(kind, attempt_schema)
            if error is None:
                return affected_sections, attempt_schema
            last_validation_error = error

        logger.warning(
            "event=wiki_kind_schema_validation_failed channel_id=%s page_id=%s kind=%s err=%s",
            channel_id,
            page.page_id,
            kind,
            last_validation_error,
        )
        # Both attempts failed validation — return markdown so the page
        # body still updates; kind_schema stays None so the agent surface
        # exposes the degraded state honestly.
        return affected_sections, None

    async def _invoke_apply_update_llm(self, prompt: str) -> str:
        """Single LLM call for ``apply_update``. Override in tests.

        Production path: resolve the ``wiki_maintainer`` model via
        ``LLMProvider``, then issue a JSON-mode completion via
        ``dispatch_completion`` (which gates on the per-provider throttle).
        Returns the raw JSON text (parsed by the caller).
        """
        from beever_atlas.llm.provider import get_llm_provider
        from beever_atlas.services.llm_dispatch import (
            dispatch_completion,
            normalize_litellm_model,
            sniff_provider,
        )

        provider = self._llm_provider or get_llm_provider()
        model_name = provider.get_model_string("wiki_maintainer")

        response = await dispatch_completion(
            provider=sniff_provider(model_name),
            model=normalize_litellm_model(model_name),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=4096,
            temperature=0.2,
        )
        return response.choices[0].message.content or "{}"  # type: ignore[index, union-attr]

    async def _resolve_first_touch_title(self, page_id: str, channel_id: str) -> str:
        """Look up the human-friendly title for a brand-new page.

        Resolution order:
        1. ``topic:<cluster_id>`` → ``WeaviateStore.get_cluster(cluster_id).title``
        2. ``entity:<slug>`` → entity registry canonical name (capitalized)
        3. Role page (``decisions``, ``faq``, ``action-items``) → fixed constant
        4. Fallback → title-cased slug

        Any lookup failure quietly falls through to the next strategy so a
        Weaviate hiccup never blocks page creation.
        """
        for kind, ident in _split_page_id(page_id):
            if kind == "topic":
                title = await self._lookup_cluster_title(channel_id, ident)
                if title:
                    return title
                return _slug_to_title_fallback(ident)
            if kind == "entity":
                title = await self._lookup_entity_display_name(ident)
                if title:
                    return title
                return _slug_to_title_fallback(ident)
            if kind == "role":
                return _ROLE_PAGE_TITLES.get(ident, _slug_to_title_fallback(ident))
        return _slug_to_title_fallback(page_id)

    async def _lookup_cluster_title(self, channel_id: str, cluster_id: str) -> str | None:
        try:
            from beever_atlas.stores import get_stores

            stores = get_stores()
            weaviate = getattr(stores, "weaviate", None)
            if weaviate is None:
                return None
            cluster = await weaviate.get_cluster(cluster_id)
            title = getattr(cluster, "title", None) if cluster else None
            return title or None
        except Exception:  # noqa: BLE001 — title is best-effort, never blocks page creation
            logger.debug("cluster title lookup failed for %s", cluster_id, exc_info=True)
            return None

    async def _lookup_entity_display_name(self, entity_slug: str) -> str | None:
        try:
            from beever_atlas.stores import get_stores

            stores = get_stores()
            registry = getattr(stores, "entity_registry", None)
            if registry is None:
                return None
            # ``entity_slug`` already lowercased + dashed; entity registry
            # keys are canonical names (mixed case + spaces). Try the
            # un-slugified form first, then the slug verbatim as fallback.
            unslug = entity_slug.replace("-", " ")
            canonical = await registry.get_canonical(unslug)
            if canonical:
                return canonical
            canonical = await registry.get_canonical(entity_slug)
            if canonical:
                return canonical
            return None
        except Exception:  # noqa: BLE001
            logger.debug("entity display lookup failed for %s", entity_slug, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Drift A/B comparator wiring
    # ------------------------------------------------------------------

    def _should_compare_drift(self, channel_id: str, page_id: str) -> bool:
        """Per-(channel, page) rate-limit gate for the drift comparator.

        Returns False when the same key was last compared inside the rate
        limit window. The window length is read from
        ``Settings.wiki_drift_ab_rate_limit_seconds`` (default 60s) on each
        call so an operator can tune it via env without restart-on-import.
        Trims entries older than 5 minutes on every check so the in-memory
        dict cannot grow unbounded for a churning channel set.
        """
        from beever_atlas.infra.config import get_settings

        window = float(get_settings().wiki_drift_ab_rate_limit_seconds)
        now = time.monotonic()
        # Trim entries older than max(5 min, window) — bounds memory while
        # guaranteeing we never evict a timestamp before its rate-limit
        # window has actually elapsed. The 5-min floor keeps memory tight
        # for the typical 60s default; the max() shields against an
        # operator setting WIKI_DRIFT_AB_RATE_LIMIT_SECONDS > 300 (e.g.
        # raised to 10 min during a high-cost soak window).
        if self._drift_compare_last_run:
            cutoff = now - max(300.0, window)
            self._drift_compare_last_run = {
                k: v for k, v in self._drift_compare_last_run.items() if v >= cutoff
            }
        key = (channel_id, page_id)
        last = self._drift_compare_last_run.get(key)
        if last is None:
            return True
        elapsed = now - last
        if elapsed >= window:
            return True
        logger.info(
            "event=wiki_drift_rate_limited channel_id=%s page_id=%s "
            "since_last_seconds=%.1f window=%.1f",
            channel_id,
            page_id,
            elapsed,
            window,
        )
        return False

    def _make_regenerate_factory(self, channel_id: str, page_id: str, target_lang: str):
        """Build an async factory that returns the from-scratch ``WikiPage``
        for the same ``(channel_id, page_id, target_lang)``.

        The factory invokes ``WikiBuilder.generate_wiki`` (the legacy "build
        the whole channel's wiki, then extract this page" path) so the
        comparator can score the incremental output's drift versus a fresh
        regeneration. The closure is the small contract change that
        quarantines WikiBuilder coupling to the maintainer module.
        """

        async def _factory() -> WikiPage | None:
            try:
                from beever_atlas.infra.config import get_settings
                from beever_atlas.stores import get_stores
                from beever_atlas.wiki.builder import WikiBuilder
                from beever_atlas.wiki.cache import WikiCache

                stores = get_stores()
                weaviate = getattr(stores, "weaviate", None)
                graph = getattr(stores, "graph", None)
                # ``WikiCache`` takes a Mongo URI string (not the store).
                # Construct it the same way ``api/wiki.py:_get_cache`` does
                # so a soak run sees the same backing collection production
                # uses. The cache is cheap to instantiate (no startup
                # handshake) — no need to hold a singleton here.
                cache = WikiCache(get_settings().mongodb_uri)
                builder = WikiBuilder(weaviate, graph, cache)
                response = await builder.generate_wiki(channel_id, target_lang=target_lang)
            except Exception as exc:  # noqa: BLE001 — comparator must not destabilise
                logger.warning(
                    "event=wiki_drift_regenerate_factory_failed channel_id=%s page_id=%s err=%s",
                    channel_id,
                    page_id,
                    exc,
                )
                return None
            # ``WikiResponse`` shape varies across builder revisions; the
            # comparator only needs a ``WikiPage`` shape with title +
            # sections, which we can synthesise from whatever per-page
            # representation the builder returned.
            page_payload = _extract_regenerate_page(response, page_id)
            if page_payload is None:
                return None
            return WikiPage(
                channel_id=channel_id,
                target_lang=target_lang,
                page_id=page_id,
                title=page_payload.get("title", "") or "",
                slug=page_payload.get("slug", page_id.replace(":", "-")),
                sections=[
                    WikiPageSection(
                        id=str(s.get("id", "")) or "section",
                        title=str(s.get("title", "")),
                        content_md=str(s.get("content_md", "")),
                    )
                    for s in (page_payload.get("sections") or [])
                ],
            )

        return _factory

    def _schedule_drift_compare(
        self,
        channel_id: str,
        page_id: str,
        saved_page: WikiPage,
        target_lang: str,
    ) -> None:
        """Fire the drift comparator as a fire-and-forget asyncio task.

        Gated on ``Settings.wiki_drift_ab`` and the per-(channel, page) rate
        limit. Captures the just-saved ``WikiPage`` as the incremental
        factory so the comparator times only the regenerate side
        meaningfully (the incremental side already finished). Records the
        post-schedule timestamp so the rate limiter ticks even if the task
        itself is still in-flight (otherwise a slow comparator could be
        re-scheduled before it finishes, defeating the rate limit).

        ``done_callback`` surfaces unhandled exceptions to the structured
        log — an unhandled task exception in asyncio would otherwise be
        silently logged to ``sys.stderr`` only on event-loop shutdown.
        """
        from beever_atlas.infra.config import get_settings

        if not get_settings().wiki_drift_ab:
            return
        if not self._should_compare_drift(channel_id, page_id):
            return
        regenerate_factory = self._make_regenerate_factory(channel_id, page_id, target_lang)

        async def _incremental_factory() -> WikiPage:
            return saved_page

        from beever_atlas.services.wiki_drift_comparator import (
            compare_apply_update_vs_regenerate,
        )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g. a sync test path) — nothing to schedule.
            logger.warning(
                "event=wiki_drift_schedule_no_loop channel_id=%s page_id=%s",
                channel_id,
                page_id,
            )
            return

        task = loop.create_task(
            compare_apply_update_vs_regenerate(
                channel_id=channel_id,
                page_id=page_id,
                incremental_factory=_incremental_factory,
                regenerate_factory=regenerate_factory,
            )
        )
        # Stamp the rate-limit timestamp now (post-schedule) so a quick
        # second apply_update for the same page within the window is
        # rejected even if the original task is still running.
        self._drift_compare_last_run[(channel_id, page_id)] = time.monotonic()

        def _on_done(t: asyncio.Task) -> None:
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.warning(
                    "event=wiki_drift_task_failed channel_id=%s page_id=%s err=%s",
                    channel_id,
                    page_id,
                    exc,
                )

        task.add_done_callback(_on_done)

    # ------------------------------------------------------------------
    # Observability counters
    # ------------------------------------------------------------------

    def _record_apply_update_success(self, page_id: str) -> None:
        """Record a successful ``apply_update`` rewrite. Trims rolling
        window to the last 60 minutes on each insert so the list stays
        bounded under sustained traffic."""
        now = time.monotonic()
        self._trim_rolling(self._apply_update_records, now)
        self._apply_update_records.append((now, _page_kind_from_id(page_id)))

    def _record_apply_update_failure(
        self, channel_id: str, page_id: str, exc: BaseException
    ) -> None:
        """Append a failure record (capped at 10 entries — oldest first
        dropped when the cap is reached)."""
        entry = {
            "channel_id": channel_id,
            "page_id": page_id,
            "error_class": type(exc).__name__,
            "ts": datetime.now(tz=UTC).isoformat(),
        }
        self._apply_update_failures.append(entry)
        if len(self._apply_update_failures) > 10:
            # Drop oldest first.
            del self._apply_update_failures[0 : len(self._apply_update_failures) - 10]

    def _record_mark_dirty(self, count: int) -> None:
        """Record ``count`` mark-dirty events — one timestamp per page that
        flipped to dirty. Trims rolling window like the apply-update one."""
        if count <= 0:
            return
        now = time.monotonic()
        self._trim_rolling_floats(self._mark_dirty_records, now)
        self._mark_dirty_records.extend([now] * count)

    @staticmethod
    def _trim_rolling(records: list[tuple[float, str]], now: float) -> None:
        cutoff = now - 3600.0
        # Records are appended in chronological order so the oldest sit at
        # the front; drop the prefix older than the cutoff in O(N) once.
        keep_from = len(records)
        for i, entry in enumerate(records):
            if entry[0] >= cutoff:
                keep_from = i
                break
        if keep_from > 0:
            del records[0:keep_from]

    @staticmethod
    def _trim_rolling_floats(records: list[float], now: float) -> None:
        cutoff = now - 3600.0
        keep_from = len(records)
        for i, ts in enumerate(records):
            if ts >= cutoff:
                keep_from = i
                break
        if keep_from > 0:
            del records[0:keep_from]

    def _in_memory_metrics_snapshot(self) -> dict[str, Any]:
        """Synchronous slice of metrics — no Mongo. Used both by tests
        (cheap) and by the async ``metrics_snapshot`` (which adds the
        Mongo-backed ``pending_dirty_pages_per_channel`` count)."""
        now = time.monotonic()
        self._trim_rolling(self._apply_update_records, now)
        self._trim_rolling_floats(self._mark_dirty_records, now)

        def _count_within(records: list[tuple[float, str]], window: float) -> int:
            cutoff = now - window
            return sum(1 for ts, _ in records if ts >= cutoff)

        def _count_within_floats(records: list[float], window: float) -> int:
            cutoff = now - window
            return sum(1 for ts in records if ts >= cutoff)

        rewrite_by_kind = {
            "topic": 0,
            "entity": 0,
            "decisions": 0,
            "faq": 0,
            "action_items": 0,
        }
        for _ts, kind in self._apply_update_records:
            if kind in rewrite_by_kind:
                rewrite_by_kind[kind] += 1
        return {
            "apply_update_count_5min": _count_within(self._apply_update_records, 300.0),
            "apply_update_count_15min": _count_within(self._apply_update_records, 900.0),
            "apply_update_count_60min": _count_within(self._apply_update_records, 3600.0),
            "mark_dirty_count_5min": _count_within_floats(self._mark_dirty_records, 300.0),
            "apply_update_failures": list(self._apply_update_failures),
            "rewrite_count_by_page_kind": rewrite_by_kind,
        }

    async def metrics_snapshot(self) -> dict[str, Any]:
        """Return the documented metrics shape, including the Mongo-backed
        ``pending_dirty_pages_per_channel``. On Mongo failure the rest of
        the metrics are returned with ``pending_dirty_pages_per_channel={}``
        and a warning log line — the endpoint must never crash on a
        transient observability dependency."""
        snapshot = self._in_memory_metrics_snapshot()
        pending: dict[str, int] = {}
        try:
            from beever_atlas.stores import get_stores

            stores = get_stores()
            mongo = getattr(stores, "mongodb", None)
            if mongo is not None:
                pending = await _aggregate_pending_dirty(mongo)
        except Exception as exc:  # noqa: BLE001 — observability is best-effort
            logger.warning("event=wiki_maintainer_pending_dirty_failed err=%s", exc)
            pending = {}
        snapshot["pending_dirty_pages_per_channel"] = pending
        return snapshot

    # ------------------------------------------------------------------
    # Internal — fact loader (overridden in tests)
    # ------------------------------------------------------------------

    async def _load_facts(
        self, channel_id: str, fact_ids: list[str] | None
    ) -> list[dict[str, Any]]:
        """Fetch fact records by id or by channel from Weaviate.

        When ``fact_ids`` is provided, batch-loads exactly those facts via
        ``WeaviateStore.fetch_by_ids`` (one cheap object lookup per id, no
        full scan). When ``fact_ids is None`` (the ``maintain_now``
        channel-wide path), pages through ``list_facts`` 500 at a time and
        caps the total at ``_CHANNEL_FACT_LOAD_CAP`` (5000) to avoid an
        unbounded scan on a high-traffic channel; when the cap is hit, an
        explicit ``wiki_maintainer_fact_load_truncated`` warning is emitted
        so we know to revisit during soak.

        Returns dicts in the shape ``plan_updates`` expects:
        ``{"id", "cluster_id", "entity_tags", "fact_type"}``. Tests may
        still subclass / monkeypatch this method to inject a synthetic
        fact set without touching Weaviate.
        """
        from beever_atlas.models.api import MemoryFilters
        from beever_atlas.stores import get_stores

        stores = get_stores()
        weaviate = getattr(stores, "weaviate", None)
        if weaviate is None:
            return []

        if fact_ids:
            facts = await weaviate.fetch_by_ids(list(fact_ids))
            return [_atomic_fact_to_routing_dict(f) for f in facts]

        out: list[dict[str, Any]] = []
        empty_filters = MemoryFilters()
        page_size = 500
        page = 1
        while len(out) < _CHANNEL_FACT_LOAD_CAP:
            paginated = await weaviate.list_facts(
                channel_id, empty_filters, page=page, limit=page_size
            )
            if not paginated.memories:
                break
            for f in paginated.memories:
                out.append(_atomic_fact_to_routing_dict(f))
                if len(out) >= _CHANNEL_FACT_LOAD_CAP:
                    break
            if page >= paginated.pages:
                break
            page += 1

        if len(out) >= _CHANNEL_FACT_LOAD_CAP:
            logger.warning(
                "event=wiki_maintainer_fact_load_truncated channel_id=%s total_returned=%d cap=%d",
                channel_id,
                _CHANNEL_FACT_LOAD_CAP,
                _CHANNEL_FACT_LOAD_CAP,
            )
        return out


def _hash_fact_ids(fact_ids: list[str]) -> str:
    import hashlib

    joined = "\x00".join(sorted(fact_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _page_kind_from_id(page_id: str) -> str:
    """Derive the metrics-bucket kind from a ``page_id``.

    Returns one of: ``topic``, ``entity``, ``decisions``, ``faq``,
    ``action_items``, or ``other``. The role pages (``decisions``, ``faq``,
    ``action-items``) are flat slugs; the prefixed kinds split on ``:``.
    """
    if not page_id:
        return "other"
    if page_id.startswith("topic:"):
        return "topic"
    if page_id.startswith("entity:"):
        return "entity"
    if page_id == "decisions":
        return "decisions"
    if page_id == "faq":
        return "faq"
    if page_id == "action-items":
        return "action_items"
    return "other"


def _extract_regenerate_page(response: Any, page_id: str) -> dict[str, Any] | None:
    """Best-effort extraction of one page from a ``WikiBuilder.generate_wiki``
    response. The legacy response has a flat ``pages`` subdoc whose entries
    each carry a ``page_id`` (or a ``slug`` derivable into one). Defensive
    against shape drift — returns None when nothing matches.
    """
    if response is None:
        return None
    pages = None
    if isinstance(response, dict):
        pages = response.get("pages")
    else:
        pages = getattr(response, "pages", None)
    if pages is None:
        return None
    if hasattr(pages, "items"):
        # Flat dict keyed by page_id.
        for pid, page in pages.items():
            if str(pid) != page_id:
                continue
            return _normalise_legacy_page(page)
        return None
    # Iterable of pages.
    try:
        for page in pages:
            pid = page.get("page_id") if isinstance(page, dict) else getattr(page, "page_id", None)
            if pid == page_id:
                return _normalise_legacy_page(page)
    except TypeError:
        return None
    return None


def _normalise_legacy_page(page: Any) -> dict[str, Any]:
    """Coerce a builder-shape page (Pydantic model OR plain dict) into the
    title/slug/sections dict shape ``WikiPage`` expects."""
    if isinstance(page, dict):
        return {
            "title": page.get("title", ""),
            "slug": page.get("slug", ""),
            "sections": page.get("sections", []),
        }
    return {
        "title": getattr(page, "title", "") or "",
        "slug": getattr(page, "slug", "") or "",
        "sections": [
            {
                "id": getattr(s, "id", "") or "section",
                "title": getattr(s, "title", "") or "",
                "content_md": getattr(s, "content_md", "") or "",
            }
            for s in (getattr(page, "sections", []) or [])
        ],
    }


async def _aggregate_pending_dirty(mongo: Any) -> dict[str, int]:
    """Aggregate ``wiki_pages`` documents where ``is_dirty=true`` grouped by
    ``channel_id``. Returns ``{channel_id: count}``. Reaches into the Mongo
    store's database accessor — the existing ``MongoDBStore`` exposes
    ``.db``."""
    out: dict[str, int] = {}
    db = getattr(mongo, "db", None)
    if db is None:
        return out
    pipeline: list[dict[str, Any]] = [
        {"$match": {"is_dirty": True}},
        {"$group": {"_id": "$channel_id", "count": {"$sum": 1}}},
    ]
    cursor = db["wiki_pages"].aggregate(pipeline)
    async for row in cursor:
        cid = row.get("_id") or ""
        if cid:
            out[str(cid)] = int(row.get("count", 0) or 0)
    return out


def zeroed_maintainer_metrics() -> dict[str, Any]:
    """Default response shape used by the admin endpoint when the
    maintainer singleton is not registered or the snapshot raises. Kept
    in sync with :meth:`WikiMaintainer.metrics_snapshot`'s real shape."""
    return {
        "apply_update_count_5min": 0,
        "apply_update_count_15min": 0,
        "apply_update_count_60min": 0,
        "mark_dirty_count_5min": 0,
        "apply_update_failures": [],
        "rewrite_count_by_page_kind": {
            "topic": 0,
            "entity": 0,
            "decisions": 0,
            "faq": 0,
            "action_items": 0,
        },
        "pending_dirty_pages_per_channel": {},
    }


# ----------------------------------------------------------------------
# Singleton wiring (init by the FastAPI lifespan; subscribers wire to it)
# ----------------------------------------------------------------------

_maintainer_instance: WikiMaintainer | None = None


def init_wiki_maintainer(maintainer: WikiMaintainer) -> None:
    global _maintainer_instance
    _maintainer_instance = maintainer


def get_wiki_maintainer() -> WikiMaintainer | None:
    return _maintainer_instance
