"""FastAPI application entry point."""

import logging
import warnings
warnings.filterwarnings("ignore", message="unclosed resource.*TCPTransport", category=ResourceWarning)
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load .env into os.environ so all modules (adapters, etc.) can read env vars
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from beever_atlas.adapters import close_adapter
from beever_atlas.api.ask import router as ask_router
from beever_atlas.api.channels import router as channels_router
from beever_atlas.api.connections import router as connections_router
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
from beever_atlas.api.mcp import mcp as mcp_server
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
        )
        logging.getLogger(__name__).info(
            "Env-to-DB migration: created source='env' platform connection id=%s", conn.id
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Env-to-DB migration failed (non-fatal): %s", exc
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
        yield
    finally:
        try:
            await scheduler.shutdown()
        except Exception:
            pass
        await shutdown_sync_runner()
        await close_adapter()
        await stores.shutdown()


app = FastAPI(
    title="Beever Atlas",
    description="Wiki-first RAG system with dual semantic + graph memory",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS for React dev server and production
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
app.include_router(ask_router)
app.include_router(channels_router)
app.include_router(connections_router)
app.include_router(imports_router)
app.include_router(sync_router)
app.include_router(memories_router)
app.include_router(graph_router)
app.include_router(search_router)
app.include_router(stats_router)
app.include_router(topics_router)
app.include_router(policies_router)
app.include_router(models_router)
app.include_router(dev_router)
app.include_router(wiki_router)
app.include_router(config_router)

# Mount MCP server — auth inherits from FastAPI middleware (Task 8.6/8.7)
app.mount("/mcp", mcp_server.http_app(path="/"))

register_health_checks()


@app.get("/api/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
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
