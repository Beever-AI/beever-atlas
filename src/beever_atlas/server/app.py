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

    slack_signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    credentials: dict = {"botToken": slack_token}
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
    except Exception:
        logging.getLogger(__name__).warning(
            "lifespan: could not hydrate DB-stored embedding key (non-fatal)",
            exc_info=True,
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

            def _on_extraction_done(channel_id: str, fact_ids: list[str]):
                # Fire-and-forget so the worker's batch loop is never
                # blocked by maintainer LLM calls. Per-page exceptions are
                # already swallowed inside ``on_extraction_done``;
                # ``_on_done_log_exc`` covers the rare top-level failure
                # path (import errors, etc.).
                task = _asyncio.create_task(_resolve_and_run(channel_id, fact_ids))
                task.add_done_callback(_on_done_log_exc)

            worker.subscribe_extraction_done(_on_extraction_done)
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

            def _on_extraction_done_aov(
                channel_id: str, fact_ids: list[str]
            ) -> None:
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
                            "auto_overview_subscriber fan-out task raised "
                            "channel=%s: %s",
                            channel_id,
                            exc,
                            exc_info=exc,
                        )

                task.add_done_callback(_log_exc)

            _aov_worker.subscribe_extraction_done(_on_extraction_done_aov)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "AutoOverviewSubscriber init failed (non-fatal): %s", exc
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
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Consolidation-after-extraction wiring failed (non-fatal): %s", exc
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
