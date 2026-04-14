"""DependencyHealth registry — health check functions for each external dependency."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


@dataclass
class HealthCheckResult:
    """Result of a single health check."""

    name: str
    status: str  # "up" or "down"
    latency_ms: float = 0.0
    error: str | None = None
    critical: bool = True


@dataclass
class DependencyHealth:
    """Registry of health check functions for external dependencies."""

    _checks: dict[str, tuple[Callable[[], Coroutine[Any, Any, None]], float, bool]] = field(
        default_factory=dict
    )

    def register(
        self,
        name: str,
        check_fn: Callable[[], Coroutine[Any, Any, None]],
        timeout: float = 5.0,
        critical: bool = True,
    ) -> None:
        """Register a health check function for a dependency.

        Args:
            name: Dependency name (e.g., "weaviate").
            check_fn: Async function that raises on failure.
            timeout: Timeout in seconds.
            critical: Whether this dependency is critical for "healthy" status.
        """
        self._checks[name] = (check_fn, timeout, critical)

    async def check(self, name: str) -> HealthCheckResult:
        """Run a single health check by name."""
        if name not in self._checks:
            return HealthCheckResult(name=name, status="down", error="not registered")

        check_fn, timeout, critical = self._checks[name]
        start = time.monotonic()
        try:
            await asyncio.wait_for(check_fn(), timeout=timeout)
            latency = (time.monotonic() - start) * 1000
            return HealthCheckResult(
                name=name, status="up", latency_ms=round(latency, 2), critical=critical
            )
        except asyncio.TimeoutError:
            latency = (time.monotonic() - start) * 1000
            return HealthCheckResult(
                name=name,
                status="down",
                latency_ms=round(latency, 2),
                error="timeout",
                critical=critical,
            )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return HealthCheckResult(
                name=name,
                status="down",
                latency_ms=round(latency, 2),
                error=str(e),
                critical=critical,
            )

    async def check_all(self) -> list[HealthCheckResult]:
        """Run all registered health checks concurrently."""
        tasks = [self.check(name) for name in self._checks]
        return await asyncio.gather(*tasks)

    def overall_status(self, results: list[HealthCheckResult]) -> str:
        """Determine overall status from individual check results.

        Returns:
            "healthy" if all pass, "degraded" if at least one critical is up,
            "unhealthy" if all critical components are down.
        """
        if all(r.status == "up" for r in results):
            return "healthy"

        critical_results = [r for r in results if r.critical]
        if critical_results and all(r.status == "down" for r in critical_results):
            return "unhealthy"

        return "degraded"


# Module-level registry used by server/app.py
health_registry = DependencyHealth()


def register_health_checks() -> None:
    """Register health check functions for all dependencies."""
    from beever_atlas.infra.config import get_settings

    settings = get_settings()

    async def check_weaviate() -> None:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.weaviate_url}/v1/.well-known/ready")
            r.raise_for_status()

    async def check_neo4j() -> None:
        from neo4j import AsyncGraphDatabase

        driver_kwargs: dict = {"auth": (settings.neo4j_user, settings.neo4j_password)}
        try:
            from neo4j import NotificationDisabledCategory, NotificationMinimumSeverity

            driver_kwargs["notifications_min_severity"] = NotificationMinimumSeverity.WARNING
            driver_kwargs["notifications_disabled_categories"] = [
                NotificationDisabledCategory.UNRECOGNIZED,
            ]
        except ImportError:  # pragma: no cover - defensive (neo4j-driver < 5.7)
            pass
        try:
            driver = AsyncGraphDatabase.driver(settings.neo4j_uri, **driver_kwargs)
        except TypeError:  # pragma: no cover - defensive
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
