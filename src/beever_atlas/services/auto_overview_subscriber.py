"""Auto-generate the channel-overview wiki page once a channel finishes
its first extraction batch wave.

Wired into ``ExtractionWorker.on_extraction_done`` at app startup
(see ``server/app.py`` lifespan). Idempotent — never overwrites an
existing overview, never re-fires while a generate job is in flight,
and fires the SAME ``WikiBuilder.refresh_wiki(force_restructure=True)``
code path the manual ``POST /wiki/refresh`` endpoint uses.

Distinct from ``services/wiki_auto_builder.maybe_trigger_initial_build``
which is gated on ``WIKI_MAINTENANCE_MODE=auto`` and lives inside the
maintainer fan-out. This subscriber runs INDEPENDENTLY of maintainer
mode so the "Channel Wiki" tab no longer shows "No Wiki Yet" forever
on a fresh sync, regardless of whether the operator chose auto or
manual maintenance.

Flow
----
1. ExtractionWorker emits ``on_extraction_done(channel_id, fact_ids)``.
2. Subscriber checks 5 gates IN ORDER:
   a. feature flag (``Settings.auto_overview_wiki`` — read fresh on
      every event so operators can flip at runtime),
   b. in-flight set membership (no concurrent generate for same channel),
   c. extraction status — ``pending+extracting`` MUST be zero,
   d. minimum-facts threshold (default 5) — too-few-facts channels wait
      for the manual Generate button,
   e. existing overview — query ``wiki_pages`` for ``page_type=overview``.
3. If all gates pass, reserve the in-flight slot, resolve language, and
   call ``WikiBuilder.refresh_wiki`` with ``force_restructure=True``
   (same as manual Generate). Slot released in finally.

Persistence
-----------
The in-flight set is in-memory only. On worker restart it resets to
empty — a duplicate generation could fire if a restart happens
mid-build, but the underlying ``WikiBuilder`` has its own
``_CHANNEL_LOCKS`` so concurrent builds for the same channel serialize
rather than corrupt data. The cost is at most one extra LLM build per
restart-during-build, which is acceptable.

Spec: ``openspec/changes/sync-pipeline-feedback-and-auto-wiki/specs/auto-channel-overview-wiki/``
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# Default minimum-facts gate. A 1-fact wiki is worse than no wiki —
# wait until enough signal has been extracted to produce a useful
# overview structure plan. Operators with very small channels can
# either lower this via constructor injection (tests) or click the
# manual Generate button to bypass.
_DEFAULT_MIN_FACTS = 5


class AutoOverviewSubscriber:
    """Subscriber that auto-generates the channel-overview wiki on first sync.

    Constructed once at app startup and registered via
    :meth:`ExtractionWorker.subscribe_extraction_done`. Stateless aside
    from the in-flight set used for idempotency.
    """

    # Generous-enough that genuine slow Gemini builds complete on big
    # channels, tight enough that a hung upstream call doesn't pin the
    # UI's loading screen for hours. Class constant for now — future:
    # promote to Settings if operators need per-deployment tuning.
    _GENERATION_TIMEOUT_SECONDS = 600

    def __init__(
        self,
        *,
        min_facts_threshold: int = _DEFAULT_MIN_FACTS,
        feature_flag_resolver: Callable[[], Awaitable[bool]] | None = None,
        language_resolver: Callable[[str], Awaitable[str]] | None = None,
        generator: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        """Construct the subscriber.

        Parameters
        ----------
        min_facts_threshold:
            Minimum ``done`` count before auto-generation fires. Default 5.
        feature_flag_resolver:
            Optional async callable returning ``bool``. When ``None``
            (production), the resolver reads ``Settings.auto_overview_wiki``
            on every event so runtime overrides take effect immediately.
        language_resolver:
            Optional async callable ``(channel_id) -> language_code``. When
            ``None`` the default resolver runs the per-channel policy →
            global default → ``en`` chain.
        generator:
            Optional async callable ``(channel_id, language) -> None`` that
            kicks off the overview build. When ``None`` the default calls
            :meth:`WikiBuilder.refresh_wiki` exactly like the manual
            Generate button.
        """
        self._min_facts = min_facts_threshold
        self._feature_flag_resolver = feature_flag_resolver
        self._language_resolver = language_resolver
        self._generator = generator
        # Channels with an in-flight auto-generate task. Keys are
        # released in ``on_extraction_done``'s finally block so a
        # crashed generator does not permanently lock the channel.
        self._inflight: set[str] = set()
        # Channels that have STARTED an overview generation in this
        # process, keyed by channel_id with the UTC start-time as the
        # value. The value lets the API surface "elapsed since attempt
        # began" so the frontend can render a live timer + retry button
        # if the build hangs. Cleared on terminal failure (so the user
        # can retry through the regenerate endpoint) AND naturally
        # superseded on success by the overview-row existence check
        # (``_safe_overview_state``) which short-circuits this signal.
        self._attempted: dict[str, datetime] = {}
        # Lazy-init so the constructor stays event-loop-free and
        # importable from non-async test fixtures (matches the
        # ``ExtractionWorker._semaphore`` pattern).
        self._inflight_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # Public event handler
    # ------------------------------------------------------------------

    async def on_extraction_done(self, channel_id: str, fact_ids: list[str]) -> None:
        """Idempotent gate-and-trigger. Safe to call concurrently for the
        same channel — only one generation fires."""
        # Gate 1: feature flag — checked FIRST so a disabled deployment
        # never touches MongoDB on the hot path. Read fresh per-event so
        # operators can flip the flag at runtime.
        try:
            if not await self._feature_enabled():
                return
        except Exception:  # noqa: BLE001 — never block the worker on a flag error
            logger.warning(
                "AutoOverviewSubscriber: feature_flag_resolver raised — skipping",
                exc_info=True,
            )
            return

        # Gate 2: in-flight protection — short-circuit before any DB read.
        lock = self._get_lock()
        async with lock:
            if channel_id in self._inflight:
                return

        # Gates 3-5 hit MongoDB. Wrap each in a try/except so a transient
        # store error never propagates into the worker's batch loop.
        try:
            stores = self._get_stores()
            if stores is None:
                return

            # Gate 3: extraction must be complete for this channel.
            if not await self._is_extraction_complete(stores, channel_id):
                logger.debug(
                    "AutoOverviewSubscriber: channel=%s extraction still in flight — skipping",
                    channel_id,
                )
                return

            # Gate 4: minimum-facts threshold.
            done_count = await self._done_count(stores, channel_id)
            if done_count < self._min_facts:
                logger.debug(
                    "AutoOverviewSubscriber: channel=%s done=%d below threshold=%d — skipping",
                    channel_id,
                    done_count,
                    self._min_facts,
                )
                return

            # Gate 5: existing overview check.
            if await self._overview_exists(stores, channel_id):
                logger.debug(
                    "AutoOverviewSubscriber: channel=%s overview already exists — skipping",
                    channel_id,
                )
                return
        except Exception:  # noqa: BLE001
            logger.warning(
                "AutoOverviewSubscriber: gate evaluation failed channel=%s — skipping",
                channel_id,
                exc_info=True,
            )
            return

        # Reserve the in-flight slot under the lock so any concurrent
        # event for the same channel that subsequently re-enters the
        # function sees the channel in ``_inflight`` and returns early.
        async with lock:
            if channel_id in self._inflight:
                return  # raced with another event between gates and reservation
            self._inflight.add(channel_id)
            # Sticky-mark the channel as "ever attempted" so ``is_inflight``
            # keeps reporting True between the moment the generator returns
            # and the moment the wiki_pages overview row appears — closing
            # the API-flicker window observed by test_pipeline_design.
            # Persist a UTC start-time so the API can surface elapsed
            # seconds to the user (retry-after-timeout UX).
            self._attempted[channel_id] = datetime.now(tz=UTC)

        terminal_failure = False
        try:
            language = await self._resolve_language(channel_id)
            logger.info(
                "AutoOverviewSubscriber: triggering overview generation channel=%s language=%s",
                channel_id,
                language,
            )
            await asyncio.wait_for(
                self._generate_overview(channel_id, language),
                timeout=self._GENERATION_TIMEOUT_SECONDS,
            )
            logger.info(
                "AutoOverviewSubscriber: overview generation completed channel=%s language=%s",
                channel_id,
                language,
            )
        except asyncio.TimeoutError:
            terminal_failure = True
            logger.error(
                "AutoOverviewSubscriber: generation timed out after %ds channel=%s — "
                "clearing attempted so the user can retry",
                self._GENERATION_TIMEOUT_SECONDS,
                channel_id,
            )
        except Exception as exc:  # noqa: BLE001 — never propagate; manual retry is the recovery path
            terminal_failure = True
            # Include the exception class + message in the log line so the
            # structured JSON formatter (which strips ``exc_info``) still
            # surfaces enough signal to triage the failure. ``logger.exception``
            # alone produces only the bare "generation failed channel=X" line
            # — the traceback is dropped before it reaches stdout.
            logger.error(
                "AutoOverviewSubscriber: generation failed channel=%s (%s: %s)",
                channel_id,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
        finally:
            async with lock:
                self._inflight.discard(channel_id)
                # _attempted is cleared on terminal failure so the API
                # can return to a pending state and the user can retry.
                # On success, _attempted is cleared elsewhere (the
                # overview-row existence check in ``_safe_overview_state``
                # makes the sticky in_flight redundant once the row
                # lands). Without this, a hung Gemini call would pin the
                # UI on the loading screen until process restart.
                if terminal_failure:
                    self._attempted.pop(channel_id, None)

    # ------------------------------------------------------------------
    # Internals (override-able for tests)
    # ------------------------------------------------------------------

    def _get_lock(self) -> asyncio.Lock:
        if self._inflight_lock is None:
            self._inflight_lock = asyncio.Lock()
        return self._inflight_lock

    def _get_stores(self) -> Any:
        """Return the global stores singleton, or ``None`` when unavailable."""
        try:
            from beever_atlas.stores import get_stores

            return get_stores()
        except Exception:  # noqa: BLE001
            logger.warning(
                "AutoOverviewSubscriber: get_stores() failed — skipping",
                exc_info=True,
            )
            return None

    async def _feature_enabled(self) -> bool:
        if self._feature_flag_resolver is not None:
            return await self._feature_flag_resolver()
        from beever_atlas.infra.config import get_settings

        settings = get_settings()
        return bool(getattr(settings, "auto_overview_wiki", True))

    async def _is_extraction_complete(self, stores: Any, channel_id: str) -> bool:
        """Return True iff the channel has zero ``pending`` and zero
        ``extracting`` rows. Mirrors the aggregation used by
        ``GET /api/channels/{id}/extraction-status``."""
        # When ``count_channel_messages_by_status`` is unavailable (e.g.
        # in unit tests with a synthetic mongo) fall back to an empty
        # counts dict — safer than raising.
        getter = getattr(stores.mongodb, "count_channel_messages_by_status", None)
        if getter is None:
            return True
        counts = await getter(channel_id)
        pending = int(counts.get("pending", 0) or 0)
        extracting = int(counts.get("extracting", 0) or 0)
        return pending == 0 and extracting == 0

    async def _done_count(self, stores: Any, channel_id: str) -> int:
        """Return the count of ``extraction_status=done`` rows."""
        getter = getattr(stores.mongodb, "count_channel_messages_by_status", None)
        if getter is None:
            return 0
        counts = await getter(channel_id)
        return int(counts.get("done", 0) or 0)

    async def _overview_exists(self, stores: Any, channel_id: str) -> bool:
        """Return True when ANY ``wiki_pages`` row with
        ``page_type=overview`` exists for the channel (any language, any
        state — draft, published, regenerating). Acceptance scenario
        "Subsequent extractions do not regenerate"."""
        db = getattr(stores.mongodb, "db", None)
        if db is None:
            return False
        doc = await db["wiki_pages"].find_one(
            {"channel_id": channel_id, "page_type": "overview"},
            projection={"_id": 1},
        )
        return doc is not None

    async def _resolve_language(self, channel_id: str) -> str:
        """Resolve target language: per-channel policy → global default → ``en``."""
        if self._language_resolver is not None:
            return await self._language_resolver(channel_id)
        # Per-channel policy first. ``WikiConfig`` does not currently
        # carry a ``default_language`` field, so this branch is a no-op
        # today; kept for forward compat with the spec's "per-channel
        # default" requirement when the field lands.
        try:
            from beever_atlas.services.policy_resolver import (
                resolve_effective_policy,
            )

            policy = await resolve_effective_policy(channel_id)
            wiki_cfg = getattr(policy, "wiki", None)
            lang = getattr(wiki_cfg, "default_language", None)
            if lang:
                return str(lang)
        except Exception:  # noqa: BLE001 — fall through to settings
            pass
        try:
            from beever_atlas.infra.config import get_settings

            settings = get_settings()
            return getattr(settings, "default_target_language", None) or "en"
        except Exception:  # noqa: BLE001
            return "en"

    async def _generate_overview(self, channel_id: str, language: str) -> None:
        """Reuse the same code path the manual Generate button uses
        (``WikiBuilder.refresh_wiki`` with ``force_restructure=True``).

        Mirror of ``api/wiki.refresh_wiki`` minus the FastAPI plumbing —
        ``BackgroundTasks`` is not available here, so we await the build
        inline. The caller (this subscriber's event handler) already
        runs in a fire-and-forget task spawned from the worker fan-out
        so awaiting here does not block the extraction batch loop.
        """
        if self._generator is not None:
            await self._generator(channel_id, language)
            return

        from beever_atlas.stores import get_stores
        from beever_atlas.wiki.builder import WikiBuilder
        from beever_atlas.wiki.cache import WikiCache
        from beever_atlas.infra.config import get_settings

        settings = get_settings()
        stores = get_stores()
        cache = WikiCache(settings.mongodb_uri)
        builder = WikiBuilder(stores.weaviate, stores.graph, cache)

        # Mirror ``api/wiki.refresh_wiki`` — set status="running" so the
        # frontend's first poll sees it (otherwise the UI shows "idle"
        # for the first poll cycle and only flips to "running" once the
        # builder writes status itself).
        try:
            await cache.set_generation_status(
                channel_id,
                status="running",
                stage="auto-overview",
                stage_detail="Building channel overview wiki…",
                target_lang=language,
            )
        except Exception:  # noqa: BLE001 — not fatal; builder will write status itself
            logger.warning(
                "AutoOverviewSubscriber: set_generation_status(running) failed channel=%s — proceeding",
                channel_id,
                exc_info=True,
            )

        try:
            await builder.refresh_wiki(
                channel_id,
                target_lang=language,
                force_restructure=True,
            )
        except Exception as exc:  # noqa: BLE001
            # Mirror the failure handler in ``api/wiki._run_generation``
            # so the frontend sees ``failed`` instead of stuck-in-running.
            try:
                await cache.set_generation_status(
                    channel_id,
                    status="failed",
                    stage="error",
                    error=str(exc),
                    target_lang=language,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "AutoOverviewSubscriber: failed-status update also failed channel=%s",
                    channel_id,
                )
            raise

        # ``unified-llm-wiki-graph-redesign`` D8 — drain the maintainer's
        # deferred dirty-set now that the Builder has created pages for
        # this channel. Dirty entries the maintainer queued during first
        # sync (gated on first-sync detection) can finally apply. Any
        # subsequent extraction events flow normally.
        # Best-effort: a maintainer flush hiccup must not block the
        # subscriber's success path.
        try:
            from beever_atlas.services.wiki_maintainer import get_wiki_maintainer

            maintainer = get_wiki_maintainer()
            if maintainer is not None and hasattr(maintainer, "_flush_dirty"):
                drained = await maintainer._flush_dirty(target_lang=language)
                if drained:
                    logger.info(
                        "AutoOverviewSubscriber: drained %d deferred maintainer "
                        "rewrites after first-sync Builder run channel=%s",
                        drained,
                        channel_id,
                    )
        except Exception:  # noqa: BLE001 — drain is best-effort
            logger.debug(
                "AutoOverviewSubscriber: post-Builder maintainer drain failed channel=%s",
                channel_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Read-only inspection (used by /sync/status to surface the
    # ``overview_wiki`` phase as ``in_flight``)
    # ------------------------------------------------------------------

    def is_inflight(self, channel_id: str) -> bool:
        """Return True iff an overview build is currently active or pending success.

        Returns True for channels in EITHER the actively-running set
        (``_inflight``) OR the sticky "ever-attempted" set
        (``_attempted``). The latter keeps the API reporting
        ``in_flight`` between attempts when the subscriber transiently
        exits its dispatch path — preventing the ``in_flight → pending``
        flicker observed in scripts/test_pipeline_design.py.

        Once the overview ROW is persisted, ``_safe_overview_state``
        returns ``done`` from its earlier ``wiki_pages`` check, which
        overrides this signal. So the only way to leave the in_flight
        state is forward (→ done), preserving the forward-only state
        machine the UI assumes.
        """
        return channel_id in self._inflight or channel_id in self._attempted

    def attempted_started_at(self, channel_id: str) -> datetime | None:
        """Return the UTC datetime when the current attempt began, or None.

        Used by ``/sync/status`` to surface ``overview_wiki.started_at``
        so the frontend can render a live elapsed-time stamp and decide
        when to expose a Retry button.
        """
        return self._attempted.get(channel_id)

    def force_reset(self, channel_id: str) -> None:
        """Drop the channel from BOTH the in-flight and attempted sets.

        Backs the ``POST /wiki/regenerate-overview`` recovery endpoint.
        After this call the channel is in a clean "pending" state — a
        subsequent ``on_extraction_done`` will pass the in-flight gate
        and re-trigger generation. Synchronous (no await) so the API
        handler can fire it under a regular request lifecycle.
        """
        # The asyncio.Lock would otherwise need to be acquired here, but
        # both ``set.discard`` and ``dict.pop`` are atomic under CPython
        # GIL, and the API handler doesn't run inside the subscriber's
        # event-handler context anyway. Skipping the await keeps the
        # endpoint synchronous-ish (no extra suspension point).
        self._inflight.discard(channel_id)
        self._attempted.pop(channel_id, None)


# ----------------------------------------------------------------------
# Module-level singleton (registered by the lifespan hook in server/app.py)
# ----------------------------------------------------------------------

_subscriber_instance: AutoOverviewSubscriber | None = None


def init_auto_overview_subscriber(subscriber: AutoOverviewSubscriber) -> None:
    """Register the process-wide :class:`AutoOverviewSubscriber` instance.

    Called by ``server/app.py`` lifespan after constructing the subscriber.
    Lets read-side callers (the ``/sync/status`` endpoint, future admin
    tools) inspect in-flight state without threading a reference through
    every constructor.
    """
    global _subscriber_instance
    _subscriber_instance = subscriber


def get_auto_overview_subscriber() -> AutoOverviewSubscriber | None:
    """Return the registered :class:`AutoOverviewSubscriber`, or None
    before startup completes."""
    return _subscriber_instance
