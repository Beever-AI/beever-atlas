"""FastAPI application entry point."""

import logging
import warnings

warnings.filterwarnings("ignore", category=ResourceWarning, module=r"neo4j\..*")
warnings.filterwarnings("ignore", category=ResourceWarning, module=r"aiohttp\..*")
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load .env into os.environ so all modules (adapters, etc.) can read env vars
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from fastapi.responses import JSONResponse

from beever_atlas.infra.auth import require_bridge, require_user, require_user_loader
from beever_atlas.infra.loader_url_headers import LoaderUrlSecurityHeadersMiddleware

from beever_atlas.adapters import close_adapter
from beever_atlas.infra.rate_limit import limiter
from beever_atlas.api.ask import router as ask_router, public_router as ask_public_router
from beever_atlas.api.channels import router as channels_router
from beever_atlas.api.connections import (
    router as connections_router,
    internal_router as connections_internal_router,
)
from beever_atlas.api.imports import router as imports_router
from beever_atlas.api.sync import shutdown_sync_runner
from beever_atlas.api.sync import router as sync_router
from beever_atlas.api.memories import router as memories_router
from beever_atlas.api.graph import router as graph_router
from beever_atlas.api.search import router as search_router
from beever_atlas.api.stats import router as stats_router
from beever_atlas.api.topics import router as topics_router
from beever_atlas.api.wiki import router as wiki_router
from beever_atlas.api.config import router as config_router
from beever_atlas.api.policies import router as policies_router
from beever_atlas.api.models import router as models_router
from beever_atlas.api.embedding_settings import router as embedding_settings_router
from beever_atlas.api.embedding_migration import router as embedding_migration_router
from beever_atlas.api.endpoints import router as endpoints_router
from beever_atlas.api.assignments import router as assignments_router
from beever_atlas.api.llm_debug import router as llm_debug_router
from beever_atlas.api.dev import router as dev_router
from beever_atlas.api.loader_token import router as loader_token_router
from beever_atlas.api.loaders import router as loader_router
from beever_atlas.api.admin import router as admin_router
from beever_atlas.api.sources import router as sources_router
from beever_atlas.infra.config import get_settings
from beever_atlas.infra.health import health_registry, register_health_checks
from beever_atlas.llm.provider import init_llm_provider
from beever_atlas.models import ComponentHealth, HealthResponse
from beever_atlas.stores import StoreClients, init_stores

# Configure app logger with structured JSON handler so ingestion/pipeline logs
# always appear regardless of uvicorn handler state or level filtering.
from beever_atlas.infra.logging import StructuredFormatter

_app_logger = logging.getLogger("beever_atlas")
_app_logger.setLevel(logging.INFO)
_json_handler = logging.StreamHandler()
_json_handler.setLevel(logging.INFO)
_json_handler.setFormatter(StructuredFormatter())
_app_logger.handlers = [_json_handler]
_app_logger.propagate = False


# Suppress noisy uvicorn access logs for polling endpoints (sync/status, health, OPTIONS).
class _QuietPollFilter(logging.Filter):
    """Drop access log lines for high-frequency polling routes."""

    _QUIET_FRAGMENTS = ("/sync/status ", "/api/health ", "OPTIONS /api/")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(frag in msg for frag in self._QUIET_FRAGMENTS)


logging.getLogger("uvicorn.access").addFilter(_QuietPollFilter())


async def _migrate_env_connection(stores: StoreClients, settings) -> None:
    """Create a source='env' PlatformConnection if SLACK_BOT_TOKEN is set in env
    and no env-sourced connection already exists in the database."""
    import os

    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not slack_token:
        return

    existing = await stores.platform.get_connections_by_platform_and_source("slack", "env")
    if existing:
        return

    # Credential encryption requires CREDENTIAL_MASTER_KEY — skip silently if unset
    if not settings.credential_master_key:
        logging.getLogger(__name__).warning(
            "SLACK_BOT_TOKEN is set but CREDENTIAL_MASTER_KEY is missing; "
            "skipping env-to-DB migration for platform connection."
        )
        return

    # An app-level token (xapp-...) enables Socket Mode: the bot reaches Slack
    # over an outbound WebSocket, so no public inbound URL/tunnel is required.
    # Prefer it when present; otherwise fall back to Events API (signing secret).
    slack_app_token = os.environ.get("SLACK_APP_TOKEN", "")
    slack_signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    credentials: dict = {"botToken": slack_token}
    if slack_app_token:
        credentials["appToken"] = slack_app_token
    if slack_signing_secret:
        credentials["signingSecret"] = slack_signing_secret

    try:
        conn = await stores.platform.create_connection(
            platform="slack",
            display_name="Slack (env)",
            credentials=credentials,
            status="connected",
            source="env",
            # Env-provisioned rows are shared across users; use the same
            # sentinel the startup backfill assigns to pre-migration rows so
            # `_assert_channel_access` single-tenant fallback applies.
            owner_principal_id="legacy:shared",
        )
        logging.getLogger(__name__).info(
            "Env-to-DB migration: created source='env' platform connection id=%s", conn.id
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("Env-to-DB migration failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Build the MCP ASGI app at module-load time (only if enabled). We must
# construct it BEFORE the FastAPI ``lifespan`` below so the lifespan can
# chain ``_mcp_asgi.lifespan`` — FastMCP's StreamableHTTPSessionManager is
# started inside that chained lifespan, so skipping the chain means every
# request 500s with "Task group is not initialized".
# ---------------------------------------------------------------------------
_boot_settings = get_settings()
_mcp_asgi = None
if _boot_settings.beever_mcp_enabled:
    from starlette.middleware import Middleware

    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.infra.mcp_auth import MCPAuthMiddleware

    _mcp_instance = build_mcp()
    _mcp_asgi = _mcp_instance.http_app(
        path="/",
        middleware=[Middleware(MCPAuthMiddleware)],
        stateless_http=True,
        json_response=True,
        transport="streamable-http",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage store connections and background tasks."""
    settings = get_settings()
    stores = StoreClients.from_settings(settings)
    await stores.startup()
    init_stores(stores)
    init_llm_provider(settings)

    # agent-llm-provider-pluggable PR-G: idempotent migration shim — synth
    # ``endpoints`` + ``llm_assignments`` from legacy data (env vars +
    # ``agent_model_config`` + ``embedding_settings``) when the new
    # collections are empty. Re-running with non-empty endpoints is a no-op.
    # Best-effort — never blocks boot.
    #
    # MUST run BEFORE ``reload_from_db`` so the very first boot of a fresh
    # install propagates the freshly-synthesised Assignments into the live
    # LLMProvider within the same lifespan. Without this ordering, the
    # first ``reload_from_db`` sees an empty ``endpoints`` collection,
    # the provider stays on env defaults, the migration then creates the
    # documents, but nothing re-reads them until the operator saves
    # something in the UI or the server restarts — silently violating the
    # "DB is the source of truth after first boot" design promise.
    try:
        from scripts.migrate_to_endpoint_catalog import migrate_to_endpoint_catalog

        result = await migrate_to_endpoint_catalog(stores)
        if result.get("skipped") is None:
            logging.getLogger(__name__).info(
                "lifespan: hydrated %d endpoints + %d assignments from legacy data",
                result.get("endpoints_created", 0),
                result.get("assignments_created", 0),
            )
    except Exception as exc:  # noqa: BLE001
        # SECURITY: NEVER pass ``exc_info=True`` here. ``migrate_to_endpoint_catalog``
        # holds ``env_value`` (the raw plaintext API key from os.environ) as a
        # local while calling ``endpoint_store.create(plaintext_credential=...)``.
        # An exception during create propagates with that local still on the
        # stack; ``exc_info=True`` would walk back through that frame and
        # serialise the env credential to any structured log sink (Sentry,
        # Datadog, JSON formatter). Log class + message only — same guard
        # pattern as ``provider.py:160-163`` and ``agent_credentials.py:84-86``.
        logging.getLogger(__name__).warning(
            "lifespan: migration_to_endpoint_catalog failed non-fatal (%s: %s)",
            type(exc).__name__,
            exc,
        )

    # PR-ν: hydrate per-agent model overrides from llm_assignments
    # (new) + agent_model_config (legacy). Without this, ``resolve_model``
    # falls back to the static DEFAULT_AGENT_MODELS map until the first
    # ``reload_from_db`` triggered by a UI save — i.e. agent code keeps
    # using the seed model from env even after the operator saved a
    # different one in Settings.
    try:
        from beever_atlas.llm.provider import get_llm_provider

        await get_llm_provider().reload_from_db()
    except Exception as exc:  # noqa: BLE001
        # SECURITY: ``reload_from_db`` reads endpoint documents that carry
        # ``encrypted_key`` envelopes (ciphertext + IV + tag). Locals at
        # exception time may include those raw MongoDB docs. While the blobs
        # are encrypted, they are operational secret material — if
        # ``CREDENTIAL_MASTER_KEY`` ever leaks separately, anything in the
        # log aggregator that captured the ciphertext becomes recoverable
        # plaintext. Defense-in-depth: skip ``exc_info=True``.
        logging.getLogger(__name__).warning(
            "lifespan: LLMProvider.reload_from_db failed non-fatal (%s: %s)",
            type(exc).__name__,
            exc,
        )
    # PR-λ.7: hook LiteLLM's success/failure callbacks so the
    # ``/api/settings/debug/recent-llm-calls`` ring buffer captures ALL
    # litellm activity — including agent calls that bypass our
    # ``dispatch_completion`` / ``dispatch_assignment`` wrappers via
    # Google ADK's ``LiteLlm`` model wrapper.
    try:
        from beever_atlas.services.llm_call_log import register_litellm_observer

        register_litellm_observer()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "lifespan: register_litellm_observer failed (non-fatal)", exc_info=True
        )

    # PR-E: hydrate the DB-stored encrypted API key into the runtime so the
    # embedding shim can use it without round-tripping to MongoDB on every
    # call. Runs BEFORE the dim-guard probe so the probe uses the same key
    # the actual ingestion path will use. Best-effort — a missing master
    # key surfaces only when an operator tries to USE a UI-saved key.
    try:
        from beever_atlas.api.embedding_settings import _decrypt_db_key
        from beever_atlas.llm.embeddings import set_runtime_db_api_key

        db_key = await _decrypt_db_key()
        set_runtime_db_api_key(db_key)
    except Exception as exc:  # noqa: BLE001
        # SECURITY: ``db_key`` is the decrypted plaintext embedding API key.
        # If ``set_runtime_db_api_key`` raises after ``_decrypt_db_key``
        # succeeds, ``db_key`` is a live local in this frame at exception
        # time. ``exc_info=True`` would serialise it to any structured log
        # sink. Log class + message only — same guard as ``F5`` /
        # ``provider.py:160-163`` / ``agent_credentials.py:84-86``.
        logging.getLogger(__name__).warning(
            "lifespan: could not hydrate DB-stored embedding key non-fatal (%s: %s)",
            type(exc).__name__,
            exc,
        )

    # agent-llm-provider-pluggable PR-B: hydrate per-Endpoint credentials
    # from the new ``endpoints`` collection into a process-local cache so
    # ``dispatch_completion`` can read them per-call without round-tripping
    # MongoDB. Runs AFTER the migration shim so freshly-synthesised
    # endpoints get cached too.
    try:
        from beever_atlas.llm.agent_credentials import hydrate_runtime_credentials

        await hydrate_runtime_credentials(stores)
    except Exception as exc:  # noqa: BLE001
        # SECURITY: defense-in-depth. ``hydrate_runtime_credentials`` itself
        # catches per-Endpoint decrypt failures and logs class+message only
        # (see ``agent_credentials.py:84-86``), so credentials shouldn't
        # propagate out of that scope today. But the function COULD raise
        # from ``EndpointStore.list()`` after decrypting a few credentials
        # into the ``_runtime`` cache — and a future refactor might leave
        # plaintext on the stack. Mirror the established no-exc_info=True
        # pattern across all credential-adjacent lifespan wrappers.
        logging.getLogger(__name__).warning(
            "lifespan: could not hydrate per-Endpoint credentials non-fatal (%s: %s)",
            type(exc).__name__,
            exc,
        )

    # PR-C: probe the embedding provider once + refuse to boot when the
    # configured dimension disagrees with what's already stored in
    # Weaviate. Override is `EMBEDDING_DIM_GUARD=false`.
    try:
        from beever_atlas.llm.provider import run_embedding_dim_guard

        await run_embedding_dim_guard(settings)
    except Exception:
        # ``EmbeddingDimensionMismatch`` propagates and aborts startup;
        # other exceptions (Weaviate unavailable, MongoDB not yet seeded)
        # are converted to a WARN inside ``probe_and_validate``. If we got
        # here, the guard decided it's fatal — re-raise to fail the
        # FastAPI startup loud and clear.
        raise
    await _migrate_env_connection(stores, settings)

    # Derive the file-proxy / media-proxy host allowlist from active
    # PlatformConnection records — e.g. a self-hosted Mattermost on
    # ``team.example.com`` is auto-allowed once the operator finishes
    # the connection wizard. Removes the need to also set
    # ``FILE_PROXY_HOST_ALLOWLIST_EXTRA`` for the same hostname.
    try:
        from beever_atlas.infra.platform_hosts import refresh_runtime_proxy_hosts

        await refresh_runtime_proxy_hosts(stores)
    except Exception:
        logging.getLogger(__name__).exception(
            "lifespan: failed to derive proxy hosts from connections (non-fatal)"
        )

    # Start the sync scheduler
    from beever_atlas.services.scheduler import SyncScheduler, init_scheduler

    scheduler = SyncScheduler(settings.mongodb_uri)
    try:
        await scheduler.startup()
        init_scheduler(scheduler)
    except Exception as exc:
        import traceback as _tb

        logging.getLogger(__name__).warning(
            "SyncScheduler startup failed (non-fatal): %s: %s\n%s",
            type(exc).__name__,
            exc,
            _tb.format_exc(),
        )

    # Wire the WikiMaintainer singleton + subscribe it to ExtractionWorker
    # events. Must happen AFTER ``scheduler.startup()`` because the
    # ExtractionWorker singleton is registered by
    # ``SyncScheduler._register_extraction_worker_jobs``. When auto mode is
    # configured, every successful extraction batch fans out to
    # ``WikiMaintainer.on_extraction_done`` so affected wiki pages refresh
    # incrementally without waiting on a full consolidation pass.
    try:
        import asyncio as _asyncio

        from beever_atlas.services.extraction_worker import get_extraction_worker
        from beever_atlas.services.wiki_maintainer import (
            WikiMaintainer,
            init_wiki_maintainer,
        )
        from beever_atlas.wiki.page_store import WikiPageStore

        page_store = WikiPageStore(db=stores.mongodb.db)
        await page_store.ensure_indexes()
        # Pass the Neo4j-backed graph store to the maintainer so the
        # ``wiki-llm-native-redesign`` cross-link upsert path can write
        # ``WikiPage`` nodes + ``REFERENCES`` edges. NullGraphStore /
        # NebulaStore (no parity yet) hit a hasattr-gated no-op inside
        # ``WikiMaintainer._upsert_wiki_graph`` so this is safe regardless
        # of ``GRAPH_BACKEND``.
        maintainer = WikiMaintainer(
            page_store=page_store,
            graph_store=stores.graph,
        )
        init_wiki_maintainer(maintainer)

        # memory-then-wiki-pipeline-realignment — recover any
        # ``wiki_dirty_queue`` rows stuck in ``flushing`` from a prior
        # crashed flush. Runs once at startup; the next debounced flush
        # will pick up the re-pending rows.
        try:
            recovered = await stores.mongodb.recover_stale_flushing()
            if recovered:
                logging.getLogger(__name__).info(
                    "wiki_dirty_queue: recovered %d stale-flushing rows at startup",
                    recovered,
                )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "wiki_dirty_queue: recover_stale_flushing failed at startup: %s",
                exc,
            )

        worker = get_extraction_worker()
        if worker is not None:
            _env_default_mode = settings.wiki_maintenance_mode

            async def _resolve_and_run(channel_id: str, fact_ids: list[str]) -> None:
                # Per-channel mode resolution happens AT FIRE TIME (not at
                # lifespan init) so an operator's UI toggle takes effect on
                # the very next extraction batch without a server restart.
                # The resolution falls through: channel.wiki.maintenance_mode
                # → env (``WIKI_MAINTENANCE_MODE``) → ``"manual"``.
                mode = _env_default_mode
                try:
                    from beever_atlas.services.policy_resolver import (
                        resolve_effective_policy,
                    )

                    effective = await resolve_effective_policy(channel_id)
                    channel_mode = effective.wiki.maintenance_mode
                    if channel_mode in ("auto", "manual"):
                        mode = channel_mode
                except Exception as exc:  # noqa: BLE001 — never block the maintainer
                    logging.getLogger(__name__).debug(
                        "wiki_maintainer mode resolution failed for channel=%s err=%s "
                        "(falling back to env default %s)",
                        channel_id,
                        exc,
                        _env_default_mode,
                    )

                # Auto-initial-build hook: in auto mode, the very first
                # extraction batch on a channel with no wiki kicks off the
                # canonical from-scratch builder once. The maintainer is
                # incremental and cannot produce the initial structure
                # plan, so without this hook a brand-new channel sits at
                # "no wiki yet" until the user clicks Generate. The hook
                # short-circuits the maintainer call for THIS batch when
                # it fires; subsequent events fall through to incremental.
                if mode == "auto":
                    try:
                        from beever_atlas.infra.config import (
                            get_settings as _gs,
                        )
                        from beever_atlas.services.wiki_auto_builder import (
                            maybe_trigger_initial_build,
                        )
                        from beever_atlas.wiki.cache import WikiCache as _WC

                        _cache = _WC(_gs().mongodb_uri)
                        # Pass target_lang=None so the builder's own
                        # per-channel language-resolution logic runs.
                        # ``maybe_trigger_initial_build`` resolves None
                        # to ``settings.default_target_language`` for
                        # the cache lookups internally.
                        kicked_off = await maybe_trigger_initial_build(
                            channel_id,
                            None,
                            cache=_cache,
                        )
                        if kicked_off:
                            return
                    except Exception as exc:  # noqa: BLE001
                        logging.getLogger(__name__).warning(
                            "wiki_auto_initial_build hook failed channel=%s err=%s — "
                            "falling through to incremental maintainer",
                            channel_id,
                            exc,
                        )

                await maintainer.on_extraction_done(channel_id, fact_ids, mode=mode)

            def _on_done_log_exc(task: _asyncio.Task) -> None:
                """Surface any unhandled exception from the fire-and-forget task.

                Without this callback, a top-level error inside
                ``_resolve_and_run`` (e.g. an ``ImportError`` raised
                BEFORE its own try/except) would go to asyncio's default
                exception handler and be easy to miss. Routing it through
                our structured logger keeps it visible.
                """
                if task.cancelled():
                    return
                exc = task.exception()
                if exc is None:
                    return
                logging.getLogger(__name__).warning(
                    "wiki_maintainer fan-out task raised: %s",
                    exc,
                    exc_info=exc,
                )

            # memory-then-wiki-pipeline-realignment — subscribe to the
            # two-event contract so the maintainer accumulates per batch
            # but only flushes after the channel's extraction queue
            # actually drains. Drops the legacy ``subscribe_extraction_done``
            # registration; the maintainer's ``on_extraction_done`` method
            # remains callable for out-of-tree callers during the
            # deprecation window but no longer fires per batch.
            async def _resolve_channel_lang(channel_id: str) -> str:
                """Resolve the channel's wiki target_lang. Falls back to
                ``settings.default_target_language`` then ``"en"``.

                Reads ``channel_sync_state.primary_language`` if present
                — set by the language-detector during the first sync.
                """
                try:
                    from beever_atlas.stores import get_stores as _gs

                    state = await _gs().mongodb.get_channel_sync_state(channel_id)
                    if state is not None:
                        primary = getattr(state, "primary_language", None)
                        if primary:
                            return str(primary)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    return settings.default_target_language or "en"
                except Exception:  # noqa: BLE001
                    return "en"

            async def _resolve_and_run_memory_changed(channel_id: str, fact_ids: list[str]) -> None:
                try:
                    target_lang = await _resolve_channel_lang(channel_id)
                    await maintainer.on_memory_changed(
                        channel_id, fact_ids, target_lang=target_lang
                    )
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).exception(
                        "wiki_maintainer.on_memory_changed crashed channel=%s",
                        channel_id,
                    )

            async def _resolve_and_run_memory_settled(channel_id: str) -> None:
                try:
                    # The auto-overview path (initial build for first sync)
                    # is owned by AutoOverviewSubscriber, which subscribes
                    # to memory_settled separately. Here the maintainer
                    # only schedules the debounced page-flush.
                    target_lang = await _resolve_channel_lang(channel_id)
                    await maintainer.on_memory_settled(channel_id, target_lang=target_lang)
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).exception(
                        "wiki_maintainer.on_memory_settled crashed channel=%s",
                        channel_id,
                    )

            def _on_memory_changed(channel_id: str, fact_ids: list[str]):
                task = _asyncio.create_task(_resolve_and_run_memory_changed(channel_id, fact_ids))
                task.add_done_callback(_on_done_log_exc)

            def _on_memory_settled(channel_id: str):
                task = _asyncio.create_task(_resolve_and_run_memory_settled(channel_id))
                task.add_done_callback(_on_done_log_exc)

            worker.subscribe_memory_changed(_on_memory_changed)
            # memory-then-wiki-pipeline-realignment (Blocker 2 fix) — only
            # subscribe the maintainer's settle-path directly to the worker
            # when consolidation is NOT wired (legacy mode). In decoupled
            # mode the consolidation subscriber registered later in this
            # lifespan invokes ``maintainer.on_memory_settled`` explicitly
            # after ``summarize_settled`` completes, guaranteeing
            # consolidation lands before the maintainer flush. Subscribing
            # both here would race them in parallel via
            # ``ExtractionWorker._emit_memory_settled``'s
            # ``asyncio.create_task`` fan-out — the old 5s settle-debounce
            # hack that this fix removes.
            if not getattr(settings, "decouple_extraction", False):
                worker.subscribe_memory_settled(_on_memory_settled)
    except Exception as exc:
        logging.getLogger(__name__).warning("WikiMaintainer init failed (non-fatal): %s", exc)

    # ``sync-pipeline-feedback-and-auto-wiki`` Phase 2 — auto-build the
    # channel-overview wiki on first sync. Independent of
    # ``WIKI_MAINTENANCE_MODE`` so the "No Wiki Yet" forever-state is
    # gone regardless of whether the operator chose auto/manual
    # maintenance. The fresh-install vs upgrade default is decided here:
    # when ``AUTO_OVERVIEW_WIKI`` is NOT explicitly set in the
    # environment AND any pre-existing overview row exists, the runtime
    # default flips to False so a long-running install does not surprise
    # the operator with an auto-rebuild on the first post-upgrade sync.
    try:
        import os as _os
        import asyncio as _asyncio_aov

        from beever_atlas.services.auto_overview_subscriber import (
            AutoOverviewSubscriber,
            init_auto_overview_subscriber,
        )
        from beever_atlas.services.extraction_worker import (
            get_extraction_worker as _get_extraction_worker_aov,
        )

        # Fresh-install vs upgrade auto-detect. Honour an explicit
        # operator override (env var present, regardless of value).
        if "AUTO_OVERVIEW_WIKI" not in _os.environ:
            try:
                existing = await stores.mongodb.db["wiki_pages"].count_documents(
                    {"page_type": "overview"}
                )
                if existing > 0:
                    settings.auto_overview_wiki = False
                    logging.getLogger(__name__).info(
                        "AutoOverview: detected %d existing overview wiki rows — "
                        "defaulting AUTO_OVERVIEW_WIKI=false (set the env var "
                        "explicitly to override)",
                        existing,
                    )
            except Exception:  # noqa: BLE001 — non-fatal; default stays True
                logging.getLogger(__name__).warning(
                    "AutoOverview: failed to count existing wiki_pages — "
                    "leaving AUTO_OVERVIEW_WIKI default unchanged",
                    exc_info=True,
                )

        _aov_worker = _get_extraction_worker_aov()
        if _aov_worker is not None:
            _auto_overview = AutoOverviewSubscriber()
            init_auto_overview_subscriber(_auto_overview)

            def _on_extraction_done_aov(channel_id: str, fact_ids: list[str]) -> None:
                # Fire-and-forget — never block the worker batch loop on
                # the overview LLM build (can take 30-60s). The
                # subscriber's own ``on_extraction_done`` swallows
                # generation errors, but a top-level failure (import
                # error, etc.) is logged here.
                task = _asyncio_aov.create_task(
                    _auto_overview.on_extraction_done(channel_id, fact_ids)
                )

                def _log_exc(t: _asyncio_aov.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc is not None:
                        logging.getLogger(__name__).warning(
                            "auto_overview_subscriber fan-out task raised channel=%s: %s",
                            channel_id,
                            exc,
                            exc_info=exc,
                        )

                task.add_done_callback(_log_exc)

            # memory-then-wiki-pipeline-realignment — the auto-overview
            # subscriber's 5-gate check used to include ``pending+extracting=0``;
            # the new ``memory_settled`` event already guarantees that
            # invariant. Subscribe to it instead; legacy
            # ``subscribe_extraction_done`` no longer fires the auto-overview.
            def _on_memory_settled_aov(channel_id: str) -> None:
                # The auto-overview subscriber's existing entry point
                # still expects ``(channel_id, fact_ids)``; pass an empty
                # fact-id list since the trigger no longer carries them
                # (it doesn't need them — the gate checks Weaviate counts).
                task = _asyncio_aov.create_task(_auto_overview.on_extraction_done(channel_id, []))

                def _log_exc(t: _asyncio_aov.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc is not None:
                        logging.getLogger(__name__).warning(
                            "auto_overview_subscriber fan-out task raised channel=%s: %s",
                            channel_id,
                            exc,
                            exc_info=exc,
                        )

                task.add_done_callback(_log_exc)

            _aov_worker.subscribe_memory_settled(_on_memory_settled_aov)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "AutoOverviewSubscriber init failed (non-fatal): %s", exc
        )

    # P0-1 (pipeline-cost-latency-reduction-v2) — defer ContradictionDetector
    # to a single post-sync bulk pass driven by ``memory_settled``. Replaces
    # the per-batch ``asyncio.create_task(check_and_supersede(...))`` block
    # that previously fired ~720 LLM calls during a 715-msg sync. Subscriber
    # is independent of WikiMaintainer / AutoOverview so a failure here cannot
    # block wiki rebuilds (and vice versa).
    try:
        import asyncio as _asyncio_contradiction

        from beever_atlas.services.contradiction_detector import (
            check_and_supersede_for_channel,
        )
        from beever_atlas.services.extraction_worker import (
            get_extraction_worker as _get_extraction_worker_contradiction,
        )

        _contradiction_worker = _get_extraction_worker_contradiction()
        if _contradiction_worker is not None:

            async def _run_post_sync_contradiction(channel_id: str) -> None:
                """Wrap the bulk pass in try/except so subscriber errors
                never bubble into the worker tick. The detector itself
                is best-effort; we only log here for top-level surprises
                (e.g. import-time failures).
                """
                try:
                    await check_and_supersede_for_channel(channel_id)
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "post-sync contradiction check raised channel=%s "
                        "(best-effort, will retry on next memory_settled)",
                        channel_id,
                        exc_info=True,
                    )

            def _on_memory_settled_contradiction(channel_id: str) -> None:
                task = _asyncio_contradiction.create_task(_run_post_sync_contradiction(channel_id))

                def _log_exc(t: _asyncio_contradiction.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc is not None:
                        logging.getLogger(__name__).warning(
                            "post-sync contradiction fan-out task raised channel=%s: %s",
                            channel_id,
                            exc,
                            exc_info=exc,
                        )

                task.add_done_callback(_log_exc)

            _contradiction_worker.subscribe_memory_settled(_on_memory_settled_contradiction)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "post-sync ContradictionDetector subscriber init failed (non-fatal): %s",
            exc,
        )

    # Wire consolidation to ExtractionWorker.on_extraction_done so that
    # topic_clusters and channel_summary are built after actual facts land
    # in Weaviate (not at sync-return time when facts=0).
    #
    # Bug A fix: only register in decoupled mode.  In legacy mode
    # (DECOUPLE_EXTRACTION=false) the ExtractionWorker still exists and emits
    # on_extraction_done events for inline batches.  Without this gate the
    # subscriber would fire consolidation on top of the SyncRunner path,
    # running it twice per sync.
    #
    # Bug B fix: two-flag debounce ("running" + "pending") instead of one.
    # The old single-flag pattern dropped every event after the first,
    # so a 7-batch fanout ran consolidation exactly once with only batch-1's
    # facts.  The new pattern guarantees: at most 1 in-flight + at most 1
    # queued follow-up.  After the in-flight finishes it drains the pending
    # flag and runs once more, incorporating all facts from the remaining batches.
    #
    # Bug C fix: the subscriber calls consolidate_only() instead of
    # on_ingestion_complete().  on_ingestion_complete() increments the
    # AFTER_N_SYNCS counter — calling it once per batch would tick the counter
    # N times per logical sync.  consolidate_only() calls _spawn_consolidation
    # directly without touching the counter.  The counter is incremented exactly
    # once per sync in SyncRunner._run_sync (legacy path) which is correct.
    _consolidation_settings = get_settings()
    if _consolidation_settings.decouple_extraction:
        try:
            import asyncio as _asyncio

            from beever_atlas.services.extraction_worker import (
                get_extraction_worker as _get_extraction_worker,
            )

            _consolidation_worker = _get_extraction_worker()
            if _consolidation_worker is not None:
                # Two-flag debounce per channel.
                # _consolidation_running: channel_id -> True while a task is executing.
                # _consolidation_pending: channel_id -> True when >=1 event arrived
                #   while a task was already running (collapsed to one follow-up run).
                _consolidation_running: dict[str, bool] = {}
                _consolidation_pending: dict[str, bool] = {}

                async def _run_consolidation_after_extraction(
                    channel_id: str,
                ) -> None:
                    """Policy-gated consolidation via consolidate_only (no counter tick)."""
                    from beever_atlas.services.pipeline_orchestrator import (
                        consolidate_only as _consolidate_only,
                    )
                    from beever_atlas.services.policy_resolver import (
                        resolve_effective_policy as _rp,
                    )

                    # Policy gate: only fire for consolidation-triggering strategies.
                    try:
                        effective = await _rp(channel_id)
                        strategy = effective.consolidation.strategy
                        # Import here to avoid circular at module load time.
                        from beever_atlas.models.sync_policy import ConsolidationStrategy

                        if strategy not in (
                            ConsolidationStrategy.AFTER_EVERY_SYNC,
                            ConsolidationStrategy.AFTER_N_SYNCS,
                        ):
                            return
                    except Exception as exc:  # noqa: BLE001
                        logging.getLogger(__name__).debug(
                            "consolidation policy resolution failed channel=%s: %s — skipping",
                            channel_id,
                            exc,
                        )
                        return

                    await _consolidate_only(channel_id)

                async def _run_with_debounce(channel_id: str) -> None:
                    """At most 1 in-flight + 1 queued follow-up per channel.

                    If a consolidation is already running when we arrive, we set
                    the pending flag and return immediately.  The in-flight task
                    will see the flag on its next loop iteration and run once more
                    after it finishes, picking up all facts from intervening batches.
                    """
                    if _consolidation_running.get(channel_id):
                        # Collapse all concurrent arrivals into one follow-up.
                        _consolidation_pending[channel_id] = True
                        return
                    _consolidation_running[channel_id] = True
                    try:
                        while True:
                            # Clear pending BEFORE running so any new arrivals
                            # during this run will set it again and be caught.
                            _consolidation_pending.pop(channel_id, None)
                            await _run_consolidation_after_extraction(channel_id)
                            # If another batch arrived while we were running,
                            # loop once more; otherwise we're done.
                            if not _consolidation_pending.pop(channel_id, False):
                                break
                    finally:
                        _consolidation_running.pop(channel_id, None)

                def _on_extraction_done_consolidation(channel_id: str, fact_ids: list[str]) -> None:
                    # Fire-and-forget, same pattern as WikiMaintainer subscriber.
                    # fact_ids is intentionally unused here: consolidate_only calls
                    # _spawn_consolidation which reads directly from Weaviate — the
                    # full accumulated fact set, not just this batch's ids.
                    task = _asyncio.create_task(_run_with_debounce(channel_id))

                    def _log_exc(t: _asyncio.Task) -> None:
                        if t.cancelled():
                            return
                        exc = t.exception()
                        if exc is not None:
                            logging.getLogger(__name__).warning(
                                "consolidation fan-out task raised channel=%s: %s",
                                channel_id,
                                exc,
                                exc_info=exc,
                            )

                    task.add_done_callback(_log_exc)

                _consolidation_worker.subscribe_extraction_done(_on_extraction_done_consolidation)

                # Deferred-summarization subscriber.  When
                # CONSOLIDATION_SUMMARIZE_ON_SETTLE is true (default), the
                # per-batch path above runs only assign_clusters_only — NO
                # LLM. The actual cluster/channel summary LLM batch fires
                # here exactly once per channel per sync, on memory_settled,
                # against the post-drain stable state.
                #
                # Ordering invariant (Blocker 2 fix): consolidation now
                # CHAINS into the maintainer's settle-path explicitly. The
                # maintainer's own ``memory_settled`` subscription is
                # deliberately NOT registered in decoupled mode (see the
                # guard around ``worker.subscribe_memory_settled`` in the
                # maintainer wiring above). This subscriber awaits
                # ``summarize_settled`` to completion, then calls
                # ``maintainer.on_memory_settled`` directly — guaranteeing
                # the maintainer's per-page LLM rewrites read freshly
                # written cluster/channel summaries instead of racing the
                # old 5s settle-debounce window.
                async def _run_summarize_settled(channel_id: str) -> None:
                    from beever_atlas.services.pipeline_orchestrator import (
                        summarize_settled_for_channel,
                    )
                    from beever_atlas.services.wiki_maintainer import (
                        get_wiki_maintainer,
                    )

                    await summarize_settled_for_channel(channel_id)

                    # Explicit chain — fire the maintainer's settle-path
                    # AFTER consolidation has finished writing summaries.
                    # Failures here are isolated so a maintainer error
                    # cannot mask a successful consolidation in logs.
                    _maintainer = get_wiki_maintainer()
                    if _maintainer is None:
                        return
                    # Inline language resolution (mirrors
                    # ``_resolve_channel_lang`` defined in the maintainer
                    # wiring scope above; duplicated here because that
                    # closure is not in scope from this block).
                    _target_lang = "en"
                    try:
                        from beever_atlas.stores import get_stores as _gs

                        _state = await _gs().mongodb.get_channel_sync_state(channel_id)
                        if _state is not None:
                            _primary = getattr(_state, "primary_language", None)
                            if _primary:
                                _target_lang = str(_primary)
                    except Exception:  # noqa: BLE001
                        pass
                    if _target_lang == "en":
                        try:
                            _target_lang = settings.default_target_language or "en"
                        except Exception:  # noqa: BLE001
                            _target_lang = "en"
                    try:
                        await _maintainer.on_memory_settled(channel_id, target_lang=_target_lang)
                    except Exception:  # noqa: BLE001
                        logging.getLogger(__name__).exception(
                            "wiki_maintainer.on_memory_settled crashed channel=%s "
                            "(invoked from consolidation chain)",
                            channel_id,
                        )

                def _on_memory_settled_consolidation(channel_id: str) -> None:
                    task = _asyncio.create_task(_run_summarize_settled(channel_id))

                    def _log_exc(t: _asyncio.Task) -> None:
                        if t.cancelled():
                            return
                        exc = t.exception()
                        if exc is not None:
                            logging.getLogger(__name__).warning(
                                "summarize_settled task raised channel=%s: %s",
                                channel_id,
                                exc,
                                exc_info=exc,
                            )

                    task.add_done_callback(_log_exc)

                _consolidation_worker.subscribe_memory_settled(_on_memory_settled_consolidation)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Consolidation-after-extraction wiring failed (non-fatal): %s", exc
            )

    # Unresolved classifier — second pass after memory settles (PR-A).
    # Lives in the FastAPI lifespan (NOT services/scheduler.py) per the
    # post-critic A-1 fix: subscribe_memory_settled calls only fire when
    # registered on the live ExtractionWorker instance, which is owned
    # by this lifespan via the get_extraction_worker() singleton. The
    # consolidation block above is the model for this wiring.
    try:
        import asyncio as _asyncio_uc

        from beever_atlas.services.extraction_worker import (
            get_extraction_worker as _get_extraction_worker_uc,
        )
        from beever_atlas.services.unresolved_classifier import (
            UnresolvedClassifier,
        )

        _uc_worker = _get_extraction_worker_uc()
        if _uc_worker is not None:
            _uc_classifier = UnresolvedClassifier(stores=stores, settings=settings)

            async def _run_unresolved_classifier(channel_id: str) -> None:
                try:
                    report = await _uc_classifier.classify_channel(channel_id)
                    logging.getLogger(__name__).info(
                        "unresolved_classifier: channel=%s processed=%d "
                        "classified=%d low_confidence=%d new_types=%d "
                        "llm_calls=%d",
                        channel_id,
                        report.processed,
                        report.classified,
                        report.low_confidence,
                        report.new_types_accepted,
                        report.llm_calls,
                    )
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "unresolved_classifier raised channel=%s "
                        "(best-effort, will retry on next memory_settled)",
                        channel_id,
                        exc_info=True,
                    )

            def _on_memory_settled_unresolved(channel_id: str) -> None:
                task = _asyncio_uc.create_task(_run_unresolved_classifier(channel_id))

                def _log_exc_uc(t: _asyncio_uc.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc is not None:
                        logging.getLogger(__name__).warning(
                            "unresolved_classifier fan-out task raised channel=%s: %s",
                            channel_id,
                            exc,
                            exc_info=exc,
                        )

                task.add_done_callback(_log_exc_uc)

            _uc_worker.subscribe_memory_settled(_on_memory_settled_unresolved)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "UnresolvedClassifier subscriber init failed (non-fatal): %s",
            exc,
        )

    # Initialize outbound MCP registry — non-blocking, skips unreachable servers
    from beever_atlas.agents.mcp_registry import init_mcp_registry

    try:
        await init_mcp_registry()
    except Exception as exc:
        logging.getLogger(__name__).warning("MCP registry init failed (non-fatal): %s", exc)

    try:
        # Chain FastMCP's lifespan so its StreamableHTTPSessionManager task
        # group starts before any request hits the mount. Without this chain,
        # every /mcp request returns 500 with "Task group is not initialized".
        if _mcp_asgi is not None:
            async with _mcp_asgi.lifespan(app):
                yield
        else:
            yield
    finally:
        try:
            await scheduler.shutdown()
        except Exception as exc:
            logging.getLogger(__name__).debug("Scheduler shutdown failed: %s", exc, exc_info=False)
        await shutdown_sync_runner()
        await close_adapter()
        await stores.shutdown()


app = FastAPI(
    title="Beever Atlas",
    description="Wiki-first RAG system with dual semantic + graph memory",
    version="0.1.0",
    lifespan=lifespan,
)

# Per-IP rate limit. Limiter instance lives in infra.rate_limit so route
# modules can share it; here we wire it into the FastAPI app.
app.state.limiter = limiter


def _push_source_rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Custom 429 response for ``/api/sources/{source_id}/events``.

    Other rate-limited routes use slowapi's default handler (plain text
    body); the push endpoint surfaces the documented JSON shape so
    OpenClaw / Hermes clients can react programmatically.
    """
    if request.url.path.startswith("/api/sources/") and request.url.path.endswith("/events"):
        # Best-effort retry hint derived from the limit window. ``per`` is
        # in seconds (slowapi's parsed limit). Default to 60 if unknown.
        retry_after = 60
        try:
            limit = getattr(exc, "limit", None)
            per = getattr(getattr(limit, "limit", None), "per", None) or getattr(limit, "per", None)
            if per:
                retry_after = int(per)
        except Exception:  # noqa: BLE001
            pass
        source_id = request.path_params.get("source_id", "unknown")
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limited",
                "source_id": source_id,
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )
    return _rate_limit_exceeded_handler(request, exc)


app.add_exception_handler(RateLimitExceeded, _push_source_rate_limit_handler)

# CORS for React dev server and production
_settings = get_settings()
_cors_origins = [o.strip() for o in _settings.cors_origins.split(",") if o.strip()]
_allow_credentials = True
if _allow_credentials and any(o == "*" for o in _cors_origins):
    raise RuntimeError(
        "CORS misconfigured: cannot use wildcard origin '*' with allow_credentials=True"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "X-Admin-Token"],
)
# Issue #35 — harden responses to URLs carrying ?access_token= so a leaked
# loader URL doesn't leak the embedded API key via the Referer header
# (outbound navigation) or browser disk cache. Endpoints that set their
# own Cache-Control / Referrer-Policy (e.g. the public share GET) are
# preserved via setdefault.
app.add_middleware(LoaderUrlSecurityHeadersMiddleware)

# All routers require Bearer auth except /api/health (declared below) and MCP mount.
_auth = [Depends(require_user)]
# Issue #88 — dedicated auth dep for browser-native loader endpoints
# (<img src>, <a href>) that cannot carry custom Authorization headers.
# `require_user_loader` accepts ?access_token= AND header; `require_user`
# is header-only. Only `loader_router` uses `_loader_auth`.
_loader_auth = [Depends(require_user_loader)]
app.include_router(ask_router, dependencies=_auth)
# Public shared-conversation GET — auth handled inside the endpoint based on
# the share's visibility tier (owner/auth/public). Must NOT inherit `_auth`.
app.include_router(ask_public_router)
app.include_router(channels_router, dependencies=_auth)
app.include_router(connections_router, dependencies=_auth)
# Internal bot→backend routes: bridge key only, never exposed to end users.
app.include_router(connections_internal_router, dependencies=[Depends(require_bridge)])
app.include_router(imports_router, dependencies=_auth)
app.include_router(sync_router, dependencies=_auth)
app.include_router(memories_router, dependencies=_auth)
app.include_router(graph_router, dependencies=_auth)
app.include_router(search_router, dependencies=_auth)
app.include_router(stats_router, dependencies=_auth)
app.include_router(topics_router, dependencies=_auth)
app.include_router(policies_router, dependencies=_auth)
app.include_router(models_router, dependencies=_auth)
app.include_router(embedding_settings_router, dependencies=_auth)
# PR6 (settings-restructure B-i): non-deprecated home for the re-embed
# machinery — reads the ``embedding`` Assignment as the source of truth and
# writes through to the legacy ``embedding_settings`` doc as the job's input.
# The legacy ``embedding_settings_router`` above stays mounted (unchanged)
# until a future Phase-5 cleanup deletes its config read/write/test routes.
app.include_router(embedding_migration_router, dependencies=_auth)
# agent-llm-provider-pluggable PR-E: Endpoint + Assignment catalog APIs.
app.include_router(endpoints_router, dependencies=_auth)
app.include_router(assignments_router, dependencies=_auth)
# PR-λ: debug surface for confirming dispatch state (recent LLM calls).
app.include_router(llm_debug_router, dependencies=_auth)
# Dev router: only mounted in development; its own endpoints require admin token.
if _settings.beever_env == "development":
    app.include_router(dev_router)
# Admin router: always mounted in every env, admin-token gated. Hosts the
# MCP operator view among other ops endpoints.
app.include_router(admin_router)
app.include_router(wiki_router, dependencies=_auth)
app.include_router(config_router, dependencies=_auth)
# Issue #88 — loader_router holds the 2 browser-native proxy endpoints
# (/api/files/proxy, /api/media/proxy). Mounted with `_loader_auth` so
# these are the ONLY non-public endpoints that accept `?access_token=`.
app.include_router(loader_router, dependencies=_loader_auth)
# Issue #89 — loader_token_router exposes POST /api/auth/loader-token for
# browsers to mint short-lived signed tokens. Auth is enforced inline by
# the endpoint's `Depends(require_user)` (header-only — minting a loader
# token via the legacy `?access_token=` query param would be a bootstrap
# loop). Mounted without `_auth` to avoid running require_user twice.
app.include_router(loader_token_router)
# Push-source ingest. Auth is per-source HMAC (verified inside the
# handler), so this router mounts WITHOUT the Bearer-token ``_auth``
# dependency. Unsigned or wrong-signature requests get 401 from the
# HMAC verifier before they touch the store.
app.include_router(sources_router)

# Secure MCP mount (openspec change atlas-mcp-server). The ASGI app was
# built at module-load time above so its lifespan could be chained into
# the FastAPI lifespan; here we just attach it to the route tree.
# Auth is enforced by MCPAuthMiddleware at the ASGI layer BEFORE any
# protocol message reaches FastMCP; the caller's mcp:<hash> principal is
# attached to ASGI scope.state for tool handlers to consume.
if _mcp_asgi is not None:
    app.mount("/mcp", _mcp_asgi)
    logging.getLogger(__name__).info("MCP endpoint mounted at /mcp with auth middleware")

register_health_checks()


@app.get("/health", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """Lightweight liveness probe — no auth, no DB checks.

    Conventional k8s / Docker / monitoring-tool default endpoint. Pairs
    with the deep ``/api/health`` (which dials every store and is
    auth-rate-limited). Returning 200 here silences the relentless 404
    noise that browser extensions and monitoring agents generate when
    they probe ``/health`` against any backend on localhost. Use this
    for "is the process alive" checks; use ``/api/health`` when you
    need component-level status.
    """
    return {"status": "ok"}


@app.get("/api/health", response_model=HealthResponse)
@limiter.limit("60/minute")
async def health_check(request: Request) -> HealthResponse:
    """Check connectivity to all data stores."""
    results = await health_registry.check_all()
    status = health_registry.overall_status(results)

    components = {
        r.name: ComponentHealth(status=r.status, latency_ms=r.latency_ms, error=r.error)
        for r in results
    }

    return HealthResponse(
        status=status,
        components=components,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
