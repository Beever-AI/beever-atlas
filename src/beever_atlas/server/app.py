"""FastAPI application entry point."""

from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load .env into os.environ so all modules (adapters, etc.) can read env vars
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from beever_atlas.api.ask import router as ask_router
from beever_atlas.api.channels import router as channels_router
from beever_atlas.infra.config import get_settings
from beever_atlas.infra.health import DependencyHealth
from beever_atlas.server.models import ComponentHealth, HealthResponse

app = FastAPI(
    title="Beever Atlas",
    description="Wiki-first RAG system with dual semantic + graph memory",
    version="0.1.0",
)

# CORS for React dev server and production
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
app.include_router(ask_router)
app.include_router(channels_router)

# Health check registry
health_registry = DependencyHealth()


def _register_health_checks() -> None:
    """Register health check functions for all dependencies."""
    settings = get_settings()

    async def check_weaviate() -> None:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.weaviate_url}/v1/.well-known/ready")
            r.raise_for_status()

    async def check_neo4j() -> None:
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        try:
            await driver.verify_connectivity()
        finally:
            await driver.close()

    async def check_mongodb() -> None:
        from pymongo import AsyncMongoClient

        client = AsyncMongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000)
        try:
            await client.admin.command("ping")
        finally:
            await client.close()

    async def check_redis() -> None:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, socket_timeout=5.0)
        try:
            await client.ping()
        finally:
            await client.aclose()

    health_registry.register("weaviate", check_weaviate, timeout=5.0, critical=True)
    health_registry.register("neo4j", check_neo4j, timeout=5.0, critical=False)
    health_registry.register("mongodb", check_mongodb, timeout=5.0, critical=True)
    health_registry.register("redis", check_redis, timeout=2.0, critical=False)


_register_health_checks()


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
