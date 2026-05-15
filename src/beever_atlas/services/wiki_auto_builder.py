"""Auto-trigger the initial wiki build on first sync.

Wired into ``server/app.py:_resolve_and_run`` so the very first time the
ExtractionWorker reports facts for a channel that has no wiki yet, we
fire the canonical ``WikiBuilder.refresh_wiki`` once. Subsequent
extraction events fall through to the incremental
``WikiMaintainer.on_extraction_done`` path.

Why this lives outside the maintainer
-------------------------------------
``WikiMaintainer.on_extraction_done`` is incremental — it routes facts to
existing pages via ``plan_updates`` and rewrites them. It cannot produce
the initial channel structure (folder tree, Overview, page plan); that
is the structure-planner's job inside ``WikiBuilder.refresh_wiki``. So a
channel with zero pages stays empty until the planner runs once. This
module is the single place that decides "first build" should fire and
calls the canonical builder path used by ``POST /wiki/refresh``.

Decisions encoded
-----------------
* **Threshold gate**: skip when fact count is below
  ``WIKI_AUTO_INITIAL_BUILD_THRESHOLD`` (default 10) — a single-fact
  wiki is worse than no wiki.
* **Idempotency via cache.get_generation_status**: ``running`` /
  ``completed`` / ``failed`` all short-circuit. No retries on failure —
  user retries via the Generate button so a persistent prompt/quota
  error cannot loop.
* **Race-safe slot reservation**: ``set_generation_status("running")``
  before the builder fires. Concurrent ``on_extraction_done`` events
  for the same channel re-enter this function and see ``running``, so
  at most one build fires.
* **Caller short-circuits maintainer**: when this returns ``True``, the
  caller MUST skip ``maintainer.on_extraction_done`` for the same
  fact_ids — otherwise an incremental rewrite races the from-scratch
  build for the same pages.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from beever_atlas.infra.config import get_settings

if TYPE_CHECKING:
    from beever_atlas.wiki.cache import WikiCache

logger = logging.getLogger(__name__)


# Canonical statuses written by the wiki generation pipeline (see
# WikiBuilder._generate_wiki_locked which writes ``"done"`` on success).
# ``"completed"`` is NOT a value the pipeline ever writes — using it
# here would silently bypass the idempotency gate after a successful
# build. The frontend type union is "idle" | "running" | "done" | "failed".
_TERMINAL_STATUSES = frozenset({"running", "done", "failed"})


# Per-channel asyncio lock to close the TOCTOU window between
# ``get_generation_status`` (returns None) and ``set_generation_status``
# (writes "running"). Without it, two concurrent ``on_extraction_done``
# events for the same channel can both pass the guard and both fire
# WikiBuilder runs. The builder has its own ``_CHANNEL_LOCKS`` so the
# second invocation would not corrupt data, but it doubles the LLM cost.
# In-process lock is sufficient for single-process deployments; multi-
# replica deployments would need a Mongo-side compare-and-swap.
_BUILD_LOCKS: dict[str, asyncio.Lock] = {}


def _channel_lock(channel_id: str) -> asyncio.Lock:
    lock = _BUILD_LOCKS.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _BUILD_LOCKS[channel_id] = lock
    return lock


async def maybe_trigger_initial_build(
    channel_id: str,
    target_lang: str | None,
    *,
    cache: "WikiCache",
) -> bool:
    """Decide whether to fire the initial build, and fire it if so.

    Returns ``True`` iff this call started (or attempted to start) an
    initial build — caller MUST skip the incremental maintainer for the
    same batch when ``True`` is returned.

    Returns ``False`` when:
        * the feature is disabled,
        * a generation is already in flight / completed / failed,
        * a wiki already exists for the channel,
        * fact count is below the threshold,
        * a Mongo / Weaviate read failed (fail-open: never block worker).
    """
    settings = get_settings()
    if not settings.wiki_auto_initial_build:
        return False

    # Resolve target_lang to the configured default when the caller
    # passes None. The cache APIs require a string; the builder accepts
    # None and re-resolves per-channel, but our cache lookups (status,
    # existing wiki) need a deterministic key. ``default_target_language``
    # is the same fallback the builder uses internally.
    resolved_lang = target_lang or settings.default_target_language

    # Per-channel lock guards the TOCTOU window between the status check
    # and the ``running`` write. Without it, two concurrent extraction
    # events can both pass the idempotency gate and both fire builds.
    async with _channel_lock(channel_id):
        # 1. Idempotency: any prior status decision blocks auto-trigger.
        #    The user-driven Generate button OWNS the recovery path on
        #    "failed"; we never auto-retry to avoid loops on persistent
        #    LLM errors.
        try:
            status = await cache.get_generation_status(channel_id, target_lang=resolved_lang)
        except Exception as exc:  # noqa: BLE001 — never block the worker
            logger.warning(
                "wiki_auto_initial_build: get_generation_status failed channel=%s err=%s",
                channel_id,
                exc,
            )
            return False
        if status is not None and status.get("status") in _TERMINAL_STATUSES:
            return False

        # 2. A wiki already exists → not a first-build situation. Hand off
        #    to the incremental maintainer.
        try:
            existing = await cache.get_wiki(channel_id, target_lang=resolved_lang)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wiki_auto_initial_build: get_wiki failed channel=%s err=%s",
                channel_id,
                exc,
            )
            return False
        if existing and existing.get("pages"):
            return False

        # 3. Threshold gate. Below threshold → defer to a later event.
        threshold = settings.wiki_auto_initial_build_threshold
        try:
            from beever_atlas.stores import get_stores

            weaviate = get_stores().weaviate
            fact_count = await weaviate.count_facts(channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wiki_auto_initial_build: count_facts failed channel=%s err=%s",
                channel_id,
                exc,
            )
            return False
        if fact_count < threshold:
            logger.info(
                "wiki_auto_initial_build: deferred channel=%s facts=%d threshold=%d",
                channel_id,
                fact_count,
                threshold,
            )
            return False

        # 4. Reserve the slot under the lock so any concurrent caller
        #    that subsequently acquires the lock sees ``running`` and
        #    no-ops at step 1.
        try:
            await cache.set_generation_status(
                channel_id,
                status="running",
                stage="auto-initial",
                stage_detail="Building wiki for the first time…",
                target_lang=resolved_lang,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "wiki_auto_initial_build: set_generation_status(running) failed "
                "channel=%s err=%s — skipping auto-build",
                channel_id,
                exc,
            )
            return False

    # 5. Fire the canonical builder OUTSIDE the lock — refresh_wiki can
    #    take 30-60s and we don't want to serialize the whole event
    #    pipeline on a single channel's build. The status="running"
    #    write above is what subsequent callers observe to no-op.
    #    ``force_restructure=True`` is required for a brand-new channel:
    #    the structure planner MUST run before any page can be compiled.
    #    Pass the original ``target_lang`` (may be None) so the builder's
    #    own per-channel language-resolution logic runs.
    try:
        from beever_atlas.stores import get_stores
        from beever_atlas.wiki.builder import WikiBuilder

        stores = get_stores()
        builder = WikiBuilder(stores.weaviate, stores.graph, cache)
        logger.info(
            "wiki_auto_initial_build: starting channel=%s lang=%s facts=%d threshold=%d",
            channel_id,
            resolved_lang,
            fact_count,
            threshold,
        )
        await builder.refresh_wiki(
            channel_id,
            target_lang=target_lang,
            force_restructure=True,
        )
        logger.info(
            "wiki_auto_initial_build: completed channel=%s lang=%s",
            channel_id,
            resolved_lang,
        )
    except Exception as exc:  # noqa: BLE001 — recover by setting failed status
        logger.exception(
            "wiki_auto_initial_build_failed channel=%s lang=%s err=%s",
            channel_id,
            resolved_lang,
            exc,
        )
        try:
            await cache.set_generation_status(
                channel_id,
                status="failed",
                stage="error",
                error=str(exc),
                target_lang=resolved_lang,
            )
        except Exception:  # noqa: BLE001 — diagnostic only
            logger.exception(
                "wiki_auto_initial_build: failed-status update also failed channel=%s",
                channel_id,
            )
    finally:
        # Evict the per-channel lock now that the (one-shot) initial build
        # has completed. Without this, ``_BUILD_LOCKS`` grows unboundedly
        # in a long-running server with many channels — small per entry
        # (~120B) but objectionable for a process that runs for weeks.
        # The lock is only ever needed during the very first build per
        # channel; subsequent ``maybe_trigger_initial_build`` calls
        # short-circuit at the status / wiki-existence gates without
        # taking the lock.
        _BUILD_LOCKS.pop(channel_id, None)
    return True
