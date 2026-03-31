"""FastAPI application entry point."""

import logging
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
from beever_atlas.api.sync import shutdown_sync_runner
from beever_atlas.api.sync import router as sync_router
from beever_atlas.api.memories import router as memories_router
from beever_atlas.api.graph import router as graph_router
from beever_atlas.api.stats import router as stats_router
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage store connections and background tasks."""
    settings = get_settings()
    stores = StoreClients.from_settings(settings)
    await stores.startup()
    init_stores(stores)
    init_llm_provider(settings)
    try:
        yield
    finally:
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
app.include_router(sync_router)
app.include_router(memories_router)
app.include_router(graph_router)
app.include_router(stats_router)

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
