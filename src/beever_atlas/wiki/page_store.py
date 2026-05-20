"""Per-page wiki document store.

Replaces the flat ``pages`` subdoc on the legacy ``wiki_cache`` row
with one MongoDB document per ``(channel_id, target_lang, page_id)``.
Per-page documents enable incremental maintenance, per-page versioning,
and dirty tracking — none of which were possible with the monolithic schema.

The legacy ``WikiCache`` is retained during the dual-write window;
the ``PER_PAGE_WIKI`` flag dispatches reads. Writes go to the new
collection unconditionally so a soak rollback (flag → OFF) reads the
legacy doc but doesn't lose any page edits made under the new path.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/wiki-page-store/``
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ReturnDocument

from beever_atlas.models.persistence import WikiPage, WikiPageSection, WikiTension

logger = logging.getLogger(__name__)


# Fields whose byte-equal match between the prior persisted doc and
# the new save indicate "no content change" so the version bump can
# be suppressed. Kept narrow on purpose — these are the user-visible
# rendering surfaces. ``updated_at`` / ``version`` are excluded
# (always different); ``last_facts_seen`` is excluded (set under
# concurrent maintainer events even when content is unchanged);
# ``pin_state`` is excluded (curation API has its own no-bump path).
_CONTENT_DIFF_FIELDS: tuple[str, ...] = (
    "title",
    "slug",
    "page_type",
    "kind",
    "kind_schema",
    "parent_id",
    "is_synthetic",
    "is_dirty",
    "merged_into",
    "children",
    "children_fingerprint",
    "modules",
    "narrative_sections",
    "sections",
    "tensions",
    "cross_links",
    "cross_links_broken",
)


def _narrative_content_equal(prior: dict[str, Any], new_doc: dict[str, Any]) -> bool:
    """Return True when the prior + new docs represent identical
    user-visible content.

    Used by ``save_page`` to suppress the version bump when a regen
    produced byte-identical pages — the ``narrative_sections`` field
    in particular re-runs deterministically on the same fact set, so
    drifting version on every regen makes "version" useless as a
    change-detection signal.

    Comparison is done in canonical-JSON form so dict-key ordering and
    Pydantic ``UUID`` / ``datetime`` re-serialization don't trip the
    diff. Defensive: any exception (e.g. unexpected non-JSON-able
    values that slipped past ``model_dump``) returns False so the
    standard $inc path takes over.
    """
    try:
        import json as _json

        for field in _CONTENT_DIFF_FIELDS:
            prior_val = prior.get(field)
            new_val = new_doc.get(field)
            if _json.dumps(prior_val, sort_keys=True, default=str) != _json.dumps(
                new_val, sort_keys=True, default=str
            ):
                return False
        return True
    except Exception:  # noqa: BLE001 — fall back to bumping on any compare error
        return False


def _canonical_page_path(doc: dict[str, Any]) -> str:
    """Return the canonical ``/wiki/...`` path for a stored page doc.

    For root-level pages the path is ``/wiki/<slug>``. For nested pages
    we encode the immediate parent in the path (``/wiki/<parent_id>/<slug>``)
    so a leaf moving from ``parent=null`` to ``parent=folder-X`` writes
    a redirect row keyed off the old parent-less path. The frontend
    resolves redirects via slug lookup anyway — this canonical path is
    just a deterministic key for the redirect index.
    """
    slug = doc.get("slug") or doc.get("page_id") or ""
    if not slug:
        return ""
    parent = doc.get("parent_id")
    if parent:
        return f"/wiki/{parent}/{slug}"
    return f"/wiki/{slug}"


class WikiPageStore:
    """Per-page accessor over the ``wiki_pages`` collection.

    Compound unique index on ``(channel_id, target_lang, page_id)``
    gives idempotent upsert. The ``version`` field is bumped on every
    write so the WikiMaintainer can detect staleness and archive prior
    versions in a future enhancement.
    """

    def __init__(self, db: AsyncIOMotorDatabase | None = None) -> None:
        # ``db`` may be injected by the application's MongoDBStore so
        # this object shares the existing connection pool. When None,
        # callers must call ``bind_db`` before any read/write.
        self._db = db
        self._collection: Any = None
        # Sibling collection for path-redirect entries written when a
        # leaf moves between folders during a structure-planner pass.
        # See ``llm-wiki-folder-structure`` change spec.
        self._redirects: Any = None
        if db is not None:
            self._collection = db["wiki_pages"]
            self._redirects = db["wiki_redirects"]

    @classmethod
    def from_client(
        cls, client: AsyncIOMotorClient, db_name: str = "beever_atlas"
    ) -> "WikiPageStore":
        return cls(db=client[db_name])

    def bind_db(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db
        self._collection = db["wiki_pages"]
        self._redirects = db["wiki_redirects"]

    async def ensure_indexes(self) -> None:
        if self._collection is None:
            return
        await self._collection.create_index(
            [("channel_id", 1), ("target_lang", 1), ("page_id", 1)],
            unique=True,
            name="wiki_pages_compound_unique",
        )
        # Secondary index for ``list_pages`` performance — newest first.
        await self._collection.create_index(
            [("channel_id", 1), ("target_lang", 1), ("updated_at", -1)],
            name="wiki_pages_channel_updated",
        )
        # ``wiki-llm-native-redesign`` indexes (Phase 1 §2.5).
        # Slug uniqueness is enforced per (channel_id, target_lang). Empty
        # slugs are excluded from the unique constraint via a partial
        # filter expression so legacy rows (which all share ``slug=""``
        # before the migration script runs) do NOT collide. Once the
        # migration backfills slugs the partial filter is still safe —
        # the maintainer never produces an empty slug for new pages.
        await self._collection.create_index(
            [("channel_id", 1), ("target_lang", 1), ("slug", 1)],
            unique=True,
            name="wiki_pages_channel_lang_slug_unique",
            partialFilterExpression={"slug": {"$gt": ""}},
        )
        # Sparse index on ``merged_into`` so the merge-redirect lookup
        # (``find one where merged_into == <target>``) is cheap. Sparse
        # because only merged pages carry the field; the vast majority
        # of pages are excluded from the index.
        await self._collection.create_index(
            [("merged_into", 1)],
            sparse=True,
            name="wiki_pages_merged_into_sparse",
        )
        # ``list_pages_by_kind`` uses (channel_id, kind, updated_at DESC)
        # — supports MCP ``list_wiki_pages(kind=...)`` plus the per-kind
        # facets on the drift dashboard.
        await self._collection.create_index(
            [("channel_id", 1), ("kind", 1), ("updated_at", -1)],
            name="wiki_pages_channel_kind_updated",
        )
        # ``llm-wiki-folder-structure`` — wiki_redirects collection.
        # Compound unique key on (channel_id, target_lang, old_path)
        # so chained moves of the same path overwrite (latest wins);
        # ``resolve_redirect`` then chases ``new_path`` forward up to
        # a small depth bound. The collection may be empty during the
        # rollout window before any folder moves occur.
        redirects = getattr(self, "_redirects", None)
        if redirects is not None:
            await redirects.create_index(
                [("channel_id", 1), ("target_lang", 1), ("old_path", 1)],
                unique=True,
                name="wiki_redirects_compound_unique",
            )
        # ``unified-llm-wiki-graph-redesign`` indexes:
        # ``parent_id`` for multi-level Topic hierarchy traversal — the
        # WikiTab sidebar resolves children of a folder via this index.
        # Sparse because root-level pages have parent_id=null.
        await self._collection.create_index(
            [("channel_id", 1), ("target_lang", 1), ("parent_id", 1)],
            sparse=True,
            name="wiki_pages_parent_id",
        )
        # ``archived`` for cleanup-phase queries. Sparse because almost
        # every page has archived=false; only legacy entity-page rows
        # set archived=true after the migration tags them.
        await self._collection.create_index(
            [("channel_id", 1), ("archived", 1)],
            sparse=True,
            name="wiki_pages_archived",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_page(
        self, channel_id: str, page_id: str, target_lang: str = "en"
    ) -> WikiPage | None:
        """Read one page by its compound key.

        Returns None on miss — callers (the wiki UI tool callers) do
        NOT need to know the difference between \"page never existed\"
        and \"page was deleted\". Both are no-content responses.
        """
        if self._collection is None:
            return None
        doc = await self._collection.find_one(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": page_id,
            }
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return WikiPage.model_validate(doc)

    async def list_pages(self, channel_id: str, target_lang: str = "en") -> list[WikiPage]:
        """List all pages for a channel + language, newest-edited first."""
        if self._collection is None:
            return []
        cursor = self._collection.find({"channel_id": channel_id, "target_lang": target_lang}).sort(
            "updated_at", -1
        )
        pages: list[WikiPage] = []
        async for doc in cursor:
            doc.pop("_id", None)
            pages.append(WikiPage.model_validate(doc))
        return pages

    async def get_section(
        self,
        channel_id: str,
        page_slug: str,
        anchor: str,
        target_lang: str = "en",
    ) -> dict | None:
        """Return one narrative section's payload by ``(page_slug, anchor)``.

        Backs the MCP ``read_wiki_section`` tool. Reads only the
        ``narrative_sections`` array from the page document and scans
        for the matching ``anchor``. Returns the raw section dict
        (``{anchor, heading, paragraphs, citations, visual,
        citation_coverage}``) on hit, ``None`` on miss (page missing,
        anchor missing, or no narrative_sections persisted yet).

        Spec: ``openspec/changes/wiki-narrative-articles/specs/wiki-page-store/``
        — "Page document supports targeted section retrieval".

        Implementation note: a Mongo projection would shave a few
        bytes per call, but the per-page document is bounded (~10s of
        KB for typical pages) and the cost of fetching the full doc
        is dwarfed by the channel ACL check the MCP wrapper already
        runs. Keeping the read simple avoids a divergence risk where
        the projection drifts from the schema.
        """
        if self._collection is None or not page_slug or not anchor:
            return None
        doc = await self._collection.find_one(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "slug": page_slug,
            }
        )
        if doc is None:
            return None
        sections = doc.get("narrative_sections") or []
        if not isinstance(sections, list):
            return None
        for section in sections:
            if not isinstance(section, dict):
                continue
            if str(section.get("anchor") or "") == anchor:
                return section
        return None

    async def save_page(self, page: WikiPage) -> None:
        """Idempotent upsert — bumps ``version`` and ``updated_at`` atomically.

        Uses ``$inc`` for the version bump rather than a read-then-write
        ``existing.version + 1`` pattern, so two concurrent saves on the
        same ``(channel_id, target_lang, page_id)`` cannot both write the
        same version. The maintainer runs single-threaded per channel today,
        but this keeps the invariant intact when concurrent edits land.

        ``created_at`` is ALWAYS moved to ``$setOnInsert`` so the original
        creation timestamp is preserved across rewrites. The maintainer
        typically re-builds the page from a fresh ``WikiPage`` on each
        refresh, and Pydantic's ``default_factory`` would set a new
        ``datetime.now()`` every time — putting it in ``$set`` would
        overwrite the genuine first-creation timestamp on every save.

        ``llm-wiki-folder-structure`` Phase A add-on: when the saved
        page's canonical path differs from its previously-stored path
        (typically because a leaf has moved between folders during a
        structure-planner pass), a row is written to the
        ``wiki_redirects`` collection so existing wikilinks continue
        to resolve via ``WikiPageStore.resolve_redirect``. Self-
        redirects are filtered at write time. The fingerprint-aware
        body-preservation optimization for folder pages lands in
        Phase C alongside ``_compile_folder_page``.

        ``wiki-narrative-articles`` Phase 1 add-on: when the
        ``narrative_sections`` content matches the prior persisted
        document byte-for-byte (same anchors, same paragraph text,
        same citation lists), the version counter is NOT incremented.
        Identical narrative content on re-compile is a no-content-
        change save — version stays put, ``updated_at`` is still
        refreshed so list-by-recency ordering reflects the touch. A
        content diff (any field) reverts to the standard ``$inc``
        bump. See H-3 / H-4 in the wiki-narrative-articles code review.
        """
        if self._collection is None:
            raise RuntimeError("WikiPageStore not bound to a database")

        # Read the prior row once so we can detect path changes for
        # the redirect bookkeeping below. This is a small extra read,
        # but it's not on the hot path (save_page runs once per page
        # per regenerate / maintain cycle, not per request).
        prior: dict[str, Any] | None = await self._collection.find_one(
            {
                "channel_id": page.channel_id,
                "target_lang": page.target_lang,
                "page_id": page.page_id,
            }
        )

        # Build the $set document WITHOUT version OR created_at.
        # $inc handles version; $setOnInsert handles created_at.
        doc = page.model_dump(mode="json")
        doc.pop("version", None)
        created_at = doc.pop("created_at", None) or datetime.now(tz=UTC).isoformat()
        doc["updated_at"] = datetime.now(tz=UTC).isoformat()

        # Decide whether this save is a true content change or a
        # no-op re-write (e.g., a regen produced byte-identical
        # narrative on the same fact set). When the prior doc exists
        # AND the relevant content fields are deep-equal, suppress
        # the ``$inc`` so version doesn't drift on harmless saves.
        # ``updated_at`` is still refreshed via $set so the recency
        # ordering reflects the touch.
        update: dict[str, Any]
        if prior is not None and _narrative_content_equal(prior, doc):
            update = {
                "$set": doc,
                "$setOnInsert": {"created_at": created_at},
            }
        else:
            update = {
                "$set": doc,
                "$inc": {"version": 1},
                "$setOnInsert": {"created_at": created_at},
            }
        await self._collection.update_one(
            {
                "channel_id": page.channel_id,
                "target_lang": page.target_lang,
                "page_id": page.page_id,
            },
            update,
            upsert=True,
        )

        # Path-change → redirect row. The old path uses the prior row's
        # parent_id + slug; the new path uses the freshly-written doc.
        # Self-redirects (no path change) are silently dropped so the
        # collection stays small.
        redirects = getattr(self, "_redirects", None)
        if prior is not None and redirects is not None:
            old_path = _canonical_page_path(prior)
            new_path = _canonical_page_path(doc)
            if old_path and new_path and old_path != new_path:
                await redirects.update_one(
                    {
                        "channel_id": page.channel_id,
                        "target_lang": page.target_lang,
                        "old_path": old_path,
                    },
                    {
                        "$set": {
                            "new_path": new_path,
                            "created_at": datetime.now(tz=UTC).isoformat(),
                        },
                        "$setOnInsert": {
                            "channel_id": page.channel_id,
                            "target_lang": page.target_lang,
                            "old_path": old_path,
                        },
                    },
                    upsert=True,
                )

    async def resolve_redirect(self, channel_id: str, target_lang: str, path: str) -> str | None:
        """Resolve a redirect chain to its latest target.

        Returns the latest known ``new_path`` for the given ``path``, or
        ``None`` when no redirect is registered. Chases up to 16 hops
        before giving up — a safety bound to prevent unbounded loops if
        a write produces a cycle (which shouldn't be possible given the
        unique index on ``(channel_id, target_lang, old_path)`` but the
        bound is cheap to enforce).

        Self-redirects are filtered at write time, so a ``None`` return
        always means "no redirect for this path", never "redirects to
        itself".
        """
        redirects = getattr(self, "_redirects", None)
        if redirects is None or not path:
            return None
        seen: set[str] = set()
        current = path
        for _ in range(16):
            if current in seen:
                # Cycle detected — bail and return last known target so
                # callers don't loop forever.
                return current
            seen.add(current)
            doc = await redirects.find_one(
                {
                    "channel_id": channel_id,
                    "target_lang": target_lang,
                    "old_path": current,
                }
            )
            if doc is None:
                # First miss is the answer: ``current`` is the latest
                # target if we've followed at least one redirect, else
                # we found nothing for the original ``path``.
                return current if current != path else None
            next_path = doc.get("new_path")
            if not next_path or next_path == current:
                return current if current != path else None
            current = next_path
        return current

    async def mark_dirty(
        self, channel_id: str, page_ids: list[str], target_lang: str = "en"
    ) -> int:
        """Set ``is_dirty=True`` on the named pages.

        Used by the WikiMaintainer's ``manual`` mode — the Maintain Wiki
        button reads pages where ``is_dirty=True`` and processes them on
        demand.
        """
        if self._collection is None or not page_ids:
            return 0
        result = await self._collection.update_many(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": {"$in": page_ids},
            },
            {
                "$set": {
                    "is_dirty": True,
                    "updated_at": datetime.now(tz=UTC),
                }
            },
        )
        return int(result.modified_count)

    async def clear_dirty(
        self, channel_id: str, page_ids: list[str], target_lang: str = "en"
    ) -> int:
        """Clear ``is_dirty=False`` after the maintainer processes a page."""
        if self._collection is None or not page_ids:
            return 0
        result = await self._collection.update_many(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": {"$in": page_ids},
            },
            {
                "$set": {
                    "is_dirty": False,
                    "updated_at": datetime.now(tz=UTC),
                }
            },
        )
        return int(result.modified_count)

    async def append_tensions(
        self,
        channel_id: str,
        page_id: str,
        new_tensions: list[WikiTension],
        target_lang: str = "en",
    ) -> bool:
        """Append contradictions to a page's ``tensions`` list.

        Used by the wiki lint pass and the contradiction detector wire-up.
        Idempotent — duplicate (fact_id, contradicts_fact_id) tuples are
        deduped on read by the renderer.
        """
        if self._collection is None or not new_tensions:
            return False
        result = await self._collection.update_one(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": page_id,
            },
            {
                "$push": {"tensions": {"$each": [t.model_dump(mode="json") for t in new_tensions]}},
                "$set": {"updated_at": datetime.now(tz=UTC)},
            },
        )
        return bool(result.modified_count)

    async def delete_page(self, channel_id: str, page_id: str, target_lang: str = "en") -> bool:
        if self._collection is None:
            return False
        result = await self._collection.delete_one(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "page_id": page_id,
            }
        )
        return bool(result.deleted_count)

    async def delete_all_for_channel(self, channel_id: str, target_lang: str = "en") -> int:
        """Bulk-delete every page row for ``(channel_id, target_lang)``.

        Used by the ``mode=rebuild`` path on POST /wiki/refresh to wipe
        the per-page store alongside the legacy monolith cache so the
        regeneration starts from a true clean slate. Without this,
        rebuild silently leaves stale per-page docs (curation flags,
        old slugs, dangling parent_id references) under the new
        regeneration.

        Also clears the ``wiki_redirects`` rows for the same scope —
        redirects pointing at slugs that no longer exist would 404.
        Returns the count of page rows deleted.
        """
        deleted = 0
        if self._collection is not None:
            result = await self._collection.delete_many(
                {"channel_id": channel_id, "target_lang": target_lang}
            )
            deleted = int(result.deleted_count or 0)
        if self._redirects is not None:
            try:
                await self._redirects.delete_many(
                    {"channel_id": channel_id, "target_lang": target_lang}
                )
            except Exception:
                # Best-effort — redirect rows are advisory; a stale
                # redirect just resolves to "not found" on the next hit.
                pass
        return deleted

    async def delete_all_for_channel_all_langs(self, channel_id: str) -> int:
        """Bulk-delete every page row for ``channel_id`` across ALL languages.

        delete-channel-v2 Wave 1. Like :meth:`delete_all_for_channel` but
        WITHOUT the ``target_lang`` filter, so a channel hard-purge wipes
        every translation (``en``, ``es``, ...) in one pass. The single-lang
        method is intentionally kept — the wiki ``mode=rebuild`` / reset path
        still scopes to one language.

        Also clears the ``wiki_redirects`` rows for the channel across all
        languages (best-effort — stale redirects are advisory and would just
        resolve to "not found"). Returns the count of page rows deleted.
        """
        deleted = 0
        if self._collection is not None:
            result = await self._collection.delete_many({"channel_id": channel_id})
            deleted = int(result.deleted_count or 0)
        if self._redirects is not None:
            try:
                await self._redirects.delete_many({"channel_id": channel_id})
            except Exception:
                # Best-effort — redirect rows are advisory; a stale redirect
                # just resolves to "not found" on the next hit.
                pass
        return deleted

    # ------------------------------------------------------------------
    # wiki-llm-native-redesign — curation API support (§5.5)
    # ------------------------------------------------------------------

    async def get_page_by_slug(
        self, channel_id: str, slug: str, target_lang: str = "en"
    ) -> WikiPage | None:
        """Look up one page by slug.

        Slugs are immutable after first-touch (per the redesign spec)
        so this is the canonical handle the curation HTTP endpoints
        accept in the URL. Returns None on miss.
        """
        if self._collection is None or not slug:
            return None
        doc = await self._collection.find_one(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "slug": slug,
            }
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return WikiPage.model_validate(doc)

    async def list_pages_by_kind(
        self,
        channel_id: str,
        kind: str | None = None,
        target_lang: str = "en",
        scope: str = "human",
        include_archived: bool = False,
    ) -> list[WikiPage]:
        """List pages with optional ``kind`` filter and visibility ``scope``.

        ``scope="human"`` (default) excludes pages whose ``pin_state.hidden``
        is True — these are still indexed for agent retrieval but should
        not appear in human nav. ``scope="all"`` includes everything;
        callers MUST verify the caller has the appropriate permission
        before requesting it.

        ``include_archived`` defaults to False so the wiki-redesign-gap-fill
        archive script's ``archived=true`` rows are filtered out by default.
        Pass ``True`` to inspect cleanup state from the debug surface.
        """
        if self._collection is None:
            return []
        query: dict[str, Any] = {"channel_id": channel_id, "target_lang": target_lang}
        if kind:
            query["kind"] = kind
        if scope == "human":
            # Excludes both hidden pages AND merged pages (which are
            # functionally hidden from human nav — the redirect target
            # carries the canonical content).
            query["pin_state.hidden"] = {"$ne": True}
            query["merged_into"] = None
        if not include_archived:
            query["archived"] = {"$ne": True}
        cursor = self._collection.find(query).sort("updated_at", -1)
        pages: list[WikiPage] = []
        async for doc in cursor:
            doc.pop("_id", None)
            pages.append(WikiPage.model_validate(doc))
        return pages

    async def update_curation_mode(
        self,
        channel_id: str,
        slug: str,
        curation_mode: str,
        target_lang: str = "en",
    ) -> WikiPage | None:
        """Set ``wiki_pages.curation_mode`` for one page by slug.

        ``curation_mode`` is validated by the caller (``Literal["auto",
        "manual", "frozen"]``). Curation is metadata, not content, so
        ``version`` is NOT bumped; ``updated_at`` is touched so any
        list-by-recency view reflects the operator action.
        """
        if self._collection is None:
            return None
        if curation_mode not in {"auto", "manual", "frozen"}:
            return None
        result = await self._collection.find_one_and_update(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "slug": slug,
            },
            {
                "$set": {
                    "curation_mode": curation_mode,
                    "updated_at": datetime.now(tz=UTC).isoformat(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            return None
        result.pop("_id", None)
        return WikiPage.model_validate(result)

    async def list_manual_dirty_pages(
        self,
        channel_id: str,
        target_lang: str = "en",
    ) -> list[WikiPage]:
        """Return every page where ``curation_mode="manual" AND is_dirty``.

        Used by the WikiTab "Apply Pending Updates" button to count how
        many pages have pending operator-approved rewrites and to drive
        the manual-flush endpoint.
        """
        if self._collection is None:
            return []
        cursor = self._collection.find(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "curation_mode": "manual",
                "is_dirty": True,
            }
        )
        pages: list[WikiPage] = []
        async for doc in cursor:
            doc.pop("_id", None)
            pages.append(WikiPage.model_validate(doc))
        return pages

    async def update_pin_state(
        self,
        channel_id: str,
        slug: str,
        pin_state: dict[str, Any],
        target_lang: str = "en",
    ) -> WikiPage | None:
        """Update a page's curation flags WITHOUT bumping version.

        ``pin_state`` carries the operator's intent (pinned / hidden /
        reason / set_by / set_at). Curation writes are editorial — the
        page's content has not changed — so ``version`` stays put. The
        ``updated_at`` timestamp is touched so the maintainer's
        list_pages-by-recency ordering reflects the curation event.
        """
        if self._collection is None:
            return None
        result = await self._collection.find_one_and_update(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "slug": slug,
            },
            {
                "$set": {
                    "pin_state": pin_state,
                    "updated_at": datetime.now(tz=UTC).isoformat(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            return None
        result.pop("_id", None)
        return WikiPage.model_validate(result)

    async def record_merged_into(
        self,
        channel_id: str,
        source_slug: str,
        target_slug: str,
        target_lang: str = "en",
    ) -> WikiPage | None:
        """Set ``source.merged_into = target_slug`` and hide the source.

        Idempotent — calling twice with the same target leaves the
        source unchanged. Like ``update_pin_state``, this does NOT bump
        ``version`` (operator action, not content edit).
        """
        if self._collection is None:
            return None
        # Read current pin_state so the merge does not clobber an
        # existing reason / set_by.
        existing = await self._collection.find_one(
            {"channel_id": channel_id, "target_lang": target_lang, "slug": source_slug}
        )
        if existing is None:
            return None
        pin_state = dict(existing.get("pin_state") or {})
        pin_state["hidden"] = True
        pin_state.setdefault("reason", f"merged into {target_slug}")
        pin_state["set_at"] = datetime.now(tz=UTC).isoformat()
        result = await self._collection.find_one_and_update(
            {"channel_id": channel_id, "target_lang": target_lang, "slug": source_slug},
            {
                "$set": {
                    "merged_into": target_slug,
                    "pin_state": pin_state,
                    "updated_at": datetime.now(tz=UTC).isoformat(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            return None
        result.pop("_id", None)
        return WikiPage.model_validate(result)

    async def find_merge_candidates(
        self,
        channel_id: str,
        threshold: float = 0.70,
        target_lang: str = "en",
    ) -> list[tuple[str, str, float]]:
        """Surface page pairs whose ``last_facts_seen`` Jaccard overlap
        exceeds ``threshold``.

        Returns a list of ``(slug_a, slug_b, jaccard)`` tuples sorted by
        descending Jaccard. The caller (``on_extraction_done`` subscriber)
        writes these to the ``wiki_merge_proposals`` collection and the
        operator UI surfaces them for one-click approval — the
        maintainer NEVER auto-merges.

        Pairs where either page is already hidden / merged are skipped
        — they are functionally retired and should not show up in the
        proposal stream.
        """
        if self._collection is None:
            return []
        cursor = self._collection.find(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "merged_into": None,
                "pin_state.hidden": {"$ne": True},
            }
        )
        pages: list[dict[str, Any]] = []
        async for doc in cursor:
            facts = doc.get("last_facts_seen") or []
            if not facts:
                continue
            slug = doc.get("slug") or ""
            if not slug:
                continue
            pages.append({"slug": slug, "facts": set(facts)})
        out: list[tuple[str, str, float]] = []
        for i in range(len(pages)):
            for j in range(i + 1, len(pages)):
                a, b = pages[i], pages[j]
                inter = a["facts"] & b["facts"]
                if not inter:
                    continue
                union = a["facts"] | b["facts"]
                jaccard = len(inter) / len(union) if union else 0.0
                if jaccard >= threshold:
                    out.append((a["slug"], b["slug"], jaccard))
        out.sort(key=lambda t: t[2], reverse=True)
        return out

    async def remove_facts_from_page(
        self,
        channel_id: str,
        slug: str,
        fact_ids: list[str],
        target_lang: str = "en",
    ) -> bool:
        """Drop the given fact ids from ``last_facts_seen`` on a page.

        Used by the split endpoint after the operator extracts a subset
        of a page's facts to a new page — the source's
        ``last_facts_seen`` must shrink so the maintainer doesn't keep
        treating those facts as "already integrated here".
        """
        if self._collection is None or not fact_ids:
            return False
        result = await self._collection.update_one(
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "slug": slug,
            },
            {
                "$pull": {"last_facts_seen": {"$in": list(fact_ids)}},
                "$set": {"updated_at": datetime.now(tz=UTC).isoformat()},
            },
        )
        return bool(result.modified_count)


__all__ = ["WikiPageStore", "WikiPage", "WikiPageSection", "WikiTension"]
