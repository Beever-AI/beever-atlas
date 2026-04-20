"""SSRF regression tests for ``media_processor._download_file`` (H3).

Platform message attachments flow through this path — an attacker who
can post a message with an attachment URL must not be able to reach
internal / metadata hosts or arbitrary third-party targets.
"""

from __future__ import annotations

import pytest

from beever_atlas.infra.config import Settings


@pytest.fixture
def processor(monkeypatch):
    """Build a MediaProcessor with a stubbed Settings so no real env is
    required and the bridge URL is predictable."""
    import beever_atlas.services.media_processor as media_mod

    fake_settings = Settings(
        bridge_url="http://bridge.local",
        bridge_api_key="",
    )
    monkeypatch.setattr(media_mod, "get_settings", lambda: fake_settings)

    return media_mod.MediaProcessor()


@pytest.mark.asyncio
async def test_rejects_private_ip_host(processor, caplog):
    """IMDS (not on platform allowlist) returns None and logs a warning."""
    import logging

    caplog.set_level(logging.WARNING, logger="beever_atlas.services.media_processor")
    result = await processor._download_file("http://169.254.169.254/latest/meta-data/")
    assert result is None


@pytest.mark.asyncio
async def test_rejects_off_allowlist_host(processor):
    """Arbitrary third-party host is skipped (returns None, no bytes)."""
    result = await processor._download_file("https://attacker.example.com/file.png")
    assert result is None


@pytest.mark.asyncio
async def test_rejects_localhost(processor):
    result = await processor._download_file("http://localhost/secret")
    assert result is None


@pytest.mark.asyncio
async def test_rejects_file_scheme(processor):
    result = await processor._download_file("file:///etc/passwd")
    assert result is None


@pytest.mark.asyncio
async def test_allowlisted_host_proceeds_to_fetch(processor, monkeypatch):
    """A legitimate cdn.discordapp.com URL passes validation and reaches
    the httpx fetch path (which we stub to return bytes)."""
    import beever_atlas.infra.http_safe as http_safe_mod
    from urllib.parse import quote

    def fake_validate(url, allowlist=None):  # noqa: ARG001
        return quote(url, safe="")

    monkeypatch.setattr(http_safe_mod, "validate_proxy_url", fake_validate)

    class _FakeResp:
        status_code = 200
        content = b"PNG\x89image-bytes-here"
        headers = {"content-type": "image/png"}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        @property
        def is_closed(self):
            return False

        async def get(self, url, headers=None):  # noqa: ARG002
            return _FakeResp()

        async def aclose(self):
            pass

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    result = await processor._download_file("https://cdn.discordapp.com/attachments/1/2/image.png")
    assert result == b"PNG\x89image-bytes-here"
