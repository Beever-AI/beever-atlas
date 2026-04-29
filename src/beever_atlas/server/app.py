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
from beever_atlas.api.dev import router as dev_router
from beever_atlas.api.loader_token import router as loader_token_router
from beever_atlas.api.loaders import router as loader_router
from beever_atlas.api.admin import router as admin_router
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
    await _migrate_env_connection(stores, settings)

    # Start the sync scheduler
    from beever_atlas.services.scheduler import SyncScheduler, init_scheduler

    scheduler = SyncScheduler(settings.mongodb_uri)
    try:
        await scheduler.startup()
        init_scheduler(scheduler)
    except Exception as exc:
        logging.getLogger(__name__).warning("SyncScheduler startup failed (non-fatal): %s", exc)

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
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
