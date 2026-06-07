"""Persistence models: MongoDB sync state and outbox."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class FactStatus(BaseModel):
    """Tracks the storage status of an individual extracted fact."""

    fact_index: int
    status: str = "pending"  # pending | stored | failed
    weaviate_id: str | None = None
    error: str | None = None
    retry_count: int = 0


class SyncJob(BaseModel):
    """Tracks a channel sync job in MongoDB."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    channel_id: str
    status: str = "running"  # running | completed | failed
    sync_type: str = "full"  # full | incremental
    total_messages: int = 0
    parent_messages: int = 0  # top-level messages only (excludes thread replies)
    processed_messages: int = 0
    current_batch: int = 0
    total_batches: int = 0
    batches_completed: int = 0  # atomic counter; honest with concurrent batches
    current_stage: str | None = None
    batch_size: int = 10
    errors: list[str] = Field(default_factory=list)
    batch_results: list[dict[str, Any]] = Field(default_factory=list)
    stage_timings: dict[str, float] = Field(default_factory=dict)
    stage_details: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime | None = None
    # Batch API fields
    batch_job_state: str | None = None
    batch_job_elapsed_seconds: float | None = None
    version: int = 0
    # Principal id (see `infra/auth.Principal.id`) that created this job.
    # Added by openspec change `atlas-mcp-server` Phase 1 — required by the
    # forthcoming MCP `get_job_status` tool to enforce `job_not_found` for
    # principals that do not own the job. Legacy rows without this field are
    # treated as owned by the ``"legacy:shared"`` sentinel (matching the
    # platform_connections convention at `server/app.py:114`).
    owner_principal_id: str | None = None
    kind: str = "sync"  # "sync" | "wiki_refresh" — used by MCP surface


class ChannelSyncState(BaseModel):
    """Persistent sync state per channel in MongoDB."""

    channel_id: str
    last_sync_ts: str  # ISO-8601 timestamp of last synced message
    total_synced_messages: int = 0
    primary_language: str = "en"
    """BCP-47 language tag for the channel's dominant language. Populated by
    the language detector at ingestion when LANGUAGE_DETECTION_ENABLED=true;
    defaults to "en" for existing channels and when the flag is off.
    """
    primary_language_confidence: float = 0.0
    """Detector confidence [0.0, 1.0] for `primary_language`."""


class WriteIntent(BaseModel):
    """Outbox pattern: a pending write intent in MongoDB."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    channel_id: str | None = None
    """Owning channel for this intent's facts/entities, set at creation by
    ``create_write_intent`` from the persister's session-scoped
    ``channel_id`` (see ``delete-channel-v2`` Wave 1).

    Indexed (``write_intents.channel_id``) so the channel hard-purge can
    delete intents in one pass instead of scanning nested ``facts[]``.

    ``None`` means EITHER a legacy row written before the field existed
    (the backfill script derives it from ``facts[].channel_id`` where all
    facts agree) OR an intent that genuinely batches facts from more than
    one channel. Both cases are handled by the WriteReconciler's per-fact
    channel filter (Wave 0), which never resurrects a purging channel's
    facts regardless of the top-level value."""
    facts: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    weaviate_done: bool = False
    neo4j_done: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class ActivityEvent(BaseModel):
    """An activity event for the dashboard feed."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str  # sync_complete, sync_failed, new_entity
    channel_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


# ---------------------------------------------------------------------------
# Message Store (channel_messages collection)
# ---------------------------------------------------------------------------


class ChannelMessage(BaseModel):
    """Durable message representation in the ``channel_messages`` collection.

    Replaces the prior in-memory ``list[NormalizedMessage]`` flow during sync
    with an idempotent persistent store keyed by ``(source_id, channel_id,
    message_id)``. ``extraction_status`` drives the per-message state machine
    consumed by the background ExtractionWorker.

    See ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/message-store/``.
    """

    # ---- identity (compound unique key) ------------------------------------
    source_id: str
    """Stable source identifier — adapter `source_kind` for pull sources
    (e.g. "slack", "discord", "teams"), or registered push-source id for push
    receivers (e.g. "openclaw-prod"). file imports use ``"file"``."""

    channel_id: str
    message_id: str

    # ---- platform provenance (Discord permalinks) -------------------------
    guild_id: str = ""
    """Discord-only: the guild (server) id that owns this message's channel.
    Threaded from the bridge → sync → fact so the permalink resolver can build
    ``https://discord.com/channels/{guild_id}/{channel_id}/{message_id}``.
    Empty string for Slack/Teams/Mattermost/Telegram (those URL templates do
    not need it)."""

    # ---- channel display (for UI label parity in dual-read path) ----------
    channel_name: str = ""

    # ---- content -----------------------------------------------------------
    timestamp: datetime
    author: str = ""
    author_name: str = ""
    author_image: str = ""
    content: str = ""
    thread_id: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    reactions: list[dict[str, Any]] = Field(default_factory=list)
    reply_count: int = 0
    is_bot: bool = False
    links: list[dict[str, Any]] = Field(default_factory=list)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    # ---- extraction state machine -----------------------------------------
    extraction_status: str = "pending"  # pending | extracting | done | failed
    attempt_count: int = 0
    next_attempt_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    last_error: str | None = None

    # ---- audit -------------------------------------------------------------
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


# Allowed transitions for ChannelMessage.extraction_status. Validation lives in
# the store-layer upsert helpers; tests assert illegal transitions are rejected.
EXTRACTION_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"extracting"},
    "extracting": {"done", "failed"},
    "done": set(),
    # ``failed → pending`` is the worker auto-retry path.
    "failed": {"pending"},
}


# ---------------------------------------------------------------------------
# Push-source ingest (external_sources + idempotency_keys collections)
# ---------------------------------------------------------------------------


class ExternalSource(BaseModel):
    """A registered push-source (e.g. OpenClaw, Hermes Agent runtime).

    The plaintext HMAC secret is stored alongside its sha256 fingerprint
    — verification needs the plaintext (HMAC requires the key) and the
    fingerprint lets the registry detect rotation events. At OSS scale
    this is acceptable; an enterprise tier would source the plaintext
    from a KMS (the verification path stays the same — the KMS is just
    a different lookup).

    Rotation: ``upsert_external_source`` sets ``rotated_at`` whenever
    the row is updated, so old in-flight signatures fail validation
    immediately on the next request.
    """

    source_id: str
    """Stable opaque identifier — the URL path component for ingest
    (``POST /api/sources/{source_id}/events``). Convention: lowercase
    with-dashes-or-underscores. Example: ``openclaw-prod``."""

    secret: str = Field(exclude=True)
    """Plaintext HMAC-SHA256 signing key. Recommended length 32+ bytes
    of random entropy. Stored to support verification.

    ``Field(exclude=True)`` keeps this out of any API response that
    serializes ``ExternalSource`` via ``model_dump()`` / FastAPI's
    automatic Pydantic encoding. The plaintext is intentionally persisted
    (HMAC verification requires the key) but MUST NOT appear in admin /
    list endpoints. Verification reads it directly via
    ``get_external_source`` (see ``api/sources.py``), bypassing the dump
    path."""

    secret_fingerprint: str = ""
    """sha256 hex of ``secret`` — exposed in admin endpoints so an
    operator can confirm a rotation took effect without leaking the
    actual key. Auto-derived in ``upsert_external_source``."""

    allowed_channels_pattern: str = "*"
    """Glob-style channel filter. ``*`` accepts any channel. Used to
    scope a source to a single workspace or owner without coupling auth
    to the URL path."""

    description: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    rotated_at: datetime | None = None


class IdempotencyKeyRecord(BaseModel):
    """One row per ``(source_id, idempotency_key)`` reservation.

    Auto-expires via Mongo TTL after 24h so retries within the
    advertised replay window return the cached 202 response and
    retries beyond it (where re-processing is fine because content-
    hash deterministic IDs prevent duplicates) hit the slow path.
    """

    source_id: str
    idempotency_key: str
    response: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


# ---------------------------------------------------------------------------
# Wiki page-store (wiki_pages collection)
# ---------------------------------------------------------------------------


class WikiPageSection(BaseModel):
    """One section within a wiki page.

    The WikiMaintainer updates these one at a time when new facts arrive,
    preserving unchanged sections byte-identical so page voice doesn't
    drift across incremental rewrites. ``last_facts_hash`` records which
    facts were considered when the section was last written — the
    maintainer compares against the current fact set to decide whether
    the section needs a refresh.
    """

    id: str
    title: str = ""
    content_md: str = ""
    last_facts_hash: str = ""


class WikiTension(BaseModel):
    """A surfaced contradiction between two facts.

    Populated by ``services/contradiction_detector.check_and_supersede``
    (running at ``services/batch_processor.py``) and rendered as a
    Tensions section on the affected pages by the wiki lint pass.
    """

    fact_id: str
    contradicts_fact_id: str
    summary: str = ""
    detected_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class WikiPage(BaseModel):
    """Per-page wiki document keyed by ``(channel_id, target_lang, page_id)``.

    Replaces the flat ``pages`` subdoc on the legacy ``wiki_cache`` row.
    Per-page documents enable incremental update, per-page versioning,
    and per-page dirty tracking. ``last_facts_seen`` lets the
    WikiMaintainer route new facts to affected pages deterministically (by
    membership) and skip pages whose fact set hasn't changed.
    """

    channel_id: str
    target_lang: str = "en"
    page_id: str
    """Stable slug for the page within the channel + language. Examples:
    ``topic:auth-redesign``, ``person:alice``, ``decisions``, ``faq``."""

    title: str = ""
    slug: str = ""
    """Stable, human-readable identity for the page within
    ``(channel_id, target_lang)``. Derived at first-touch from the
    page's title (kebab-case + dedupe-suffix); immutable thereafter
    unless the operator explicitly merges or splits. The
    ``wiki-llm-native-redesign`` change promotes this from a
    presentation-only URL helper to a primary identity field — see
    the redesign's ``wiki-page-identity`` capability spec.

    Legacy rows that predate the redesign retain ``slug=""``; the
    Phase-3 migration script backfills them from ``title``. The
    ``wiki_pages_channel_lang_slug_unique`` index uses a partial
    filter so empty slugs do NOT collide during the migration window.
    """

    sections: list[WikiPageSection] = Field(default_factory=list)
    version: int = 1
    is_dirty: bool = False
    """Set by the WikiMaintainer in ``manual`` mode when new facts have
    arrived but the user hasn't clicked ``Maintain Wiki`` yet. The
    Maintain button reads pages where ``is_dirty=True`` and processes
    them on demand."""

    last_facts_seen: list[str] = Field(default_factory=list)
    """Fact IDs that were considered when the page was last written.
    Maintainer diffs the current channel fact set against this list to
    decide which pages have new facts to integrate."""

    tensions: list[WikiTension] = Field(default_factory=list)
    """Inline contradictions surfaced from the contradiction detector."""

    page_voice_seed: str = ""
    """Optional reference excerpt the WikiMaintainer uses to keep the
    rewrite tone consistent. Populated on first generation from the
    original page content; subsequent rewrites preserve it."""

    # ---- wiki-llm-native-redesign fields ----------------------------------
    # The redesign promotes per-page-kind synthesis: each page declares a
    # ``kind`` (topic / entity / decisions / faq / action_items) which
    # selects a distinct synthesis prompt + JSON schema. Cross-links and
    # curation flags also live on the page document so the maintainer can
    # read them without an extra collection lookup. All defaults match
    # the pre-redesign behavior so legacy rows keep working under the
    # ``WIKI_LLM_NATIVE_REDESIGN=False`` flag (the maintainer falls
    # through to the legacy single-prompt path).
    kind: str = "topic"
    """Page kind — drives prompt + schema selection in
    ``WikiMaintainer.apply_update`` when ``WIKI_LLM_NATIVE_REDESIGN`` is
    ON. Allowed values: ``topic`` (default), ``entity``, ``decisions``,
    ``faq``, ``action_items``. Unknown kinds fall through to the legacy
    prompt — additive evolution rather than a closed enum keeps
    forward-compat with future kinds."""

    kind_schema: dict[str, Any] | None = None
    """Structured payload validated against the page's
    ``wiki/schemas/{kind}.json`` schema. Consumed by the MCP
    ``read_wiki_page`` tool so agents can iterate the page
    structurally without re-parsing markdown. ``None`` means the
    maintainer's response failed schema validation twice — the page's
    ``content_md`` is still trustworthy; only the structured surface
    is missing."""

    cross_links: dict[str, str] = Field(default_factory=dict)
    """Title → slug mapping for ``[[wikilink]]`` references this page
    contains. Resolved at maintainer time so the renderer doesn't run
    N fuzzy matches per page render — the frontend just looks up the
    bracketed title and emits a clickable anchor to the slug.
    ``dict`` rather than ``list[str]`` so the renderer carries the
    same title the LLM emitted; no extra API call is needed to
    materialize the link text."""

    cross_links_broken: list[str] = Field(default_factory=list)
    """Titles inside ``[[...]]`` that did NOT resolve to an existing
    page. Surfaced in the UI as broken-link indicators with a
    ``Create page?`` affordance. Kept distinct from ``cross_links``
    so resolved + broken state can both be rendered without
    re-running the resolver client-side."""

    pin_state: dict[str, Any] = Field(
        default_factory=lambda: {
            "pinned": False,
            "hidden": False,
            "reason": "",
            "set_by": "",
            "set_at": None,
        }
    )
    """Operator curation flags. Maintainer's ``apply_update`` adds a
    ``do not restructure`` addendum when ``pinned=True``; the
    ``list_pages`` (human scope) excludes pages where ``hidden=True``.
    Curation writes go through ``WikiPageStore.update_pin_state``
    which deliberately does NOT bump ``version`` — flag toggles
    aren't content edits."""

    merged_into: str | None = None
    """When non-null, this page has been merged into the named target
    slug. The wiki HTTP layer issues a 301 to the target on direct
    GETs, the maintainer routes future facts to the target, and the
    page is hidden from human nav. ``None`` is the normal case."""

    # ---- llm-wiki-folder-structure fields --------------------------------
    # Folder pages are first-class wiki pages whose ``page_type`` (carried
    # on the domain WikiPage; persistence model derives via ``kind``) is
    # ``folder``. They synthesize a 200-400 word index page AND maintain
    # a ``children`` list of immediate descendants. ``children_fingerprint``
    # is a SHA-256 of sorted child slugs used by the maintainer + compiler
    # to skip redundant folder-index re-synthesis when membership hasn't
    # changed. ``is_synthetic`` distinguishes planner-produced folders
    # from hand-curated ones (future).
    page_type: str = "topic"
    """Page type — ``"fixed"`` | ``"topic"`` | ``"sub-topic"`` | ``"folder"``.
    Persistence layer holds the same value the domain WikiPage model uses
    so the cache → API round-trip preserves it. Defaults to ``topic``
    because that's the most common kind for legacy rows; the legacy
    ``kind`` field is the LLM-prompt selector and is independent."""

    parent_id: str | None = None
    """Immediate parent page id (folder id, topic id, or fixed-root id).
    Null for root-level pages. Generalizes the old "topic owns its
    sub-topics" rule to "any page can be a child of any folder"."""

    children: list[dict[str, Any]] = Field(default_factory=list)
    """Ordered immediate-child references for folder pages. Each entry
    is ``{slug, title, page_type, section_number}``. Empty for
    non-folder pages. Persisted as raw dicts (not WikiPageRef) so the
    persistence layer can evolve the ref shape without breaking on-disk
    data — the domain layer's WikiPageRef validates the shape on read."""

    children_fingerprint: str | None = None
    """SHA-256 hex of the sorted child slugs of a folder page. The
    compiler skips folder-index re-synthesis when the new fingerprint
    matches the stored value AND the row already has non-empty content.
    Null on non-folder pages."""

    is_synthetic: bool = False
    """``True`` when this page (typically a folder) was produced by the
    structure planner; ``False`` for hand-curated or legacy pages.
    Surfaced in the UI as a hint that the agent owns the structure
    decision."""

    modules: list[dict[str, Any]] = Field(default_factory=list)
    """Adaptive page module plan. Each entry is at minimum
    ``{"id": str, "anchor": str}`` and MAY include ``"data": dict``
    (per-module structured payload used by the maintainer for
    surgical patching). Empty list means the page predates the
    adaptive-modules system and the renderer falls back to the
    legacy single-template flow over ``content``. See spec
    ``adaptive-page-modules`` for the catalog."""

    narrative_sections: list[dict[str, Any]] = Field(default_factory=list)
    """Multi-section narrative article body produced by the v3
    ``MODULE_COMPILE_PROMPT_V3``. Each entry is ``{anchor, heading,
    paragraphs: [{text, citations[], is_inference}], citations[],
    visual: dict | None, citation_coverage: float}``. Empty list means
    the page predates narrative generation OR the LLM response failed
    citation/parse gates and the page falls back to module-only
    rendering. See ``openspec/changes/wiki-narrative-articles/`` for
    the schema + citation discipline rules."""

    # ---- unified-llm-wiki-graph-redesign fields --------------------------
    # The redesign collapses the parallel entity-page pipeline into a
    # single graph-shaped Channel Wiki. The fields below give pages the
    # metadata the new builder + maintainer + agent retrieval layers need
    # without a schema migration. Defaults preserve legacy behavior so
    # existing rows keep working under the transition feature flags.
    archetype: str | None = None
    """Optional adaptive-rendering hint. Examples: ``project``, ``system``,
    ``decision-log-entry``, ``sub-topic``, ``folder``. Used by the
    compiler to pick rendering templates and by the structure planner
    to inform sub-topic splits. ``None`` falls through to ``kind``-driven
    rendering."""

    page_embedding: list[float] | None = None
    """Cosine-comparable embedding vector of the prose body. Used by
    tier-1 wiki-first agent retrieval. Refreshed when ``content_hash``
    changes; ``None`` means embedding generation has not run for this
    page yet (legacy rows pre-redesign)."""

    content_hash: str = ""
    """SHA-256 of the page's prose body. Used to detect content
    staleness on tier-1 retrieval (mismatch = downgrade to tier-3) and
    to gate ``page_embedding`` refresh (skip when hash matches prior).
    Empty string for legacy rows; first patch / Builder run populates."""

    kind_schema_hash: str | None = None
    """SHA-256 of the canonical ``kind_schema`` payload (the structured
    input data that produced this page) used by the Builder's
    recompile-skip optimisation. When the new build's hash matches the
    stored hash, the LLM compile call is skipped and the prior prose
    is reused. ``None`` means recompile-skip is disabled for this page
    (forces a real compile next run)."""

    curation_mode: Literal["auto", "manual", "frozen"] = "auto"
    """Per-page maintenance contract. ``auto``: maintainer marks dirty
    AND applies LLM patches. ``manual``: maintainer marks dirty but
    operator must explicitly trigger the rewrite. ``frozen``: maintainer
    skips the page entirely. Authoritative for new pages; legacy
    ``pin_state.pinned`` and ``pin_state.hidden`` remain readable for
    backward compatibility but are not consulted on new pages once the
    redesign rolls out."""

    quality_metrics: dict[str, Any] | None = None
    """Snapshot of per-page graph quality counters: orphan-edge count,
    broken-link count, last-rewrite-cost. Surfaced in the Wiki Health
    admin view. ``None`` means metrics have not been computed for this
    page yet."""

    archived: bool = False
    """Set to ``True`` for legacy ``kind=entity`` rows during the cleanup
    phase of the redesign. Archived rows are excluded from human-facing
    listings and MCP tool results; retained 30 days then dropped."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
