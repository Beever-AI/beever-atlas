"""PR-E.2: legacy LLM-config routes carry Sunset/Deprecation headers."""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api import models as models_api


@pytest.fixture
def models_client(monkeypatch: pytest.MonkeyPatch):
    """Mount the legacy /api/settings/models router with stores mocked."""
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)
    _tmp = tempfile.TemporaryDirectory()
    _prev = os.getcwd()
    os.chdir(_tmp.name)

    # Minimal mongodb stub for get_model_config / get_agent_model_config.
    mongodb = SimpleNamespace(
        get_agent_model_config=AsyncMock(return_value=None),
    )
    stores = SimpleNamespace(mongodb=mongodb)
    monkeypatch.setattr("beever_atlas.api.models.get_stores", lambda: stores)
    # The handler also touches get_llm_provider — patch it to a stub.
    provider_stub = SimpleNamespace(
        get_all_model_strings=lambda: {},
    )
    monkeypatch.setattr("beever_atlas.api.models.get_llm_provider", lambda: provider_stub)

    app = FastAPI()
    app.include_router(models_api.router)
    try:
        yield TestClient(app)
    finally:
        os.chdir(_prev)
        _tmp.cleanup()


def test_legacy_models_route_sets_deprecation_headers(models_client: TestClient) -> None:
    resp = models_client.get("/api/settings/models")
    assert resp.status_code == 200
    assert resp.headers.get("Sunset") == "true"
    assert resp.headers.get("Deprecation") == "true"
    link = resp.headers.get("Link", "")
    assert "/api/settings/assignments" in link
    assert 'rel="successor-version"' in link


def test_deprecation_dep_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The WARN log fires once per route group per process, not per request."""
    from beever_atlas.api import _deprecation

    _deprecation._warned.clear()

    import logging

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger("beever_atlas.api._deprecation")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        dep = _deprecation.deprecated_route("/api/settings/endpoints")

        # Two "requests" through the dependency.
        import asyncio

        from fastapi import Response

        asyncio.run(dep(Response()))  # type: ignore[arg-type]
        asyncio.run(dep(Response()))  # type: ignore[arg-type]
    finally:
        logger.removeHandler(handler)

    warn_records = [r for r in records if r.levelno == logging.WARNING]
    assert len(warn_records) == 1
