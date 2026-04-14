"""Tests for ImageExtractor parallel per-image concurrency."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.services.media_extractors import (
    ImageExtractor,
    _get_image_semaphore,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_fake_response(text: str) -> MagicMock:
    r = MagicMock()
    r.text = text
    return r


def _png_bytes() -> bytes:
    # Minimal 1×1 PNG
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
        b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )


# ── config field ─────────────────────────────────────────────────────────────

def test_image_extractor_concurrency_field_defaults():
    from beever_atlas.infra.config import Settings
    s = Settings(
        credential_master_key="a" * 64,
        neo4j_auth="neo4j/safe_password",
        bridge_api_key="k",
        api_keys="k",
        admin_token="t",
    )
    assert s.image_extractor_concurrency == 4


def test_image_extractor_concurrency_field_custom():
    from beever_atlas.infra.config import Settings
    s = Settings(
        image_extractor_concurrency=8,
        credential_master_key="a" * 64,
        neo4j_auth="neo4j/safe_password",
        bridge_api_key="k",
        api_keys="k",
        admin_token="t",
    )
    assert s.image_extractor_concurrency == 8


def test_image_extractor_concurrency_bounds():
    from pydantic import ValidationError
    from beever_atlas.infra.config import Settings
    base = dict(
        credential_master_key="a" * 64,
        neo4j_auth="neo4j/safe_password",
        bridge_api_key="k",
        api_keys="k",
        admin_token="t",
    )
    with pytest.raises(ValidationError):
        Settings(image_extractor_concurrency=0, **base)
    with pytest.raises(ValidationError):
        Settings(image_extractor_concurrency=17, **base)


# ── semaphore wired to settings ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_semaphore_respects_concurrency_setting():
    """_get_image_semaphore() must be bounded by image_extractor_concurrency."""
    import beever_atlas.services.media_extractors as me

    # Reset module-level semaphore so lazy init runs fresh
    original = me._IMAGE_SEMAPHORE
    me._IMAGE_SEMAPHORE = None
    try:
        mock_settings = MagicMock()
        mock_settings.image_extractor_concurrency = 2
        with patch("beever_atlas.services.media_extractors.get_settings", return_value=mock_settings):
            sem = _get_image_semaphore()
        assert sem._value == 2
    finally:
        me._IMAGE_SEMAPHORE = original


# ── parallel execution ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_images_run_in_parallel():
    """4 images dispatched via asyncio.gather must overlap in time."""
    import beever_atlas.services.media_extractors as me

    call_starts: list[float] = []
    call_ends: list[float] = []

    async def slow_generate(*args, **kwargs):
        call_starts.append(asyncio.get_event_loop().time())
        await asyncio.sleep(0.05)
        call_ends.append(asyncio.get_event_loop().time())
        return _make_fake_response("description")

    original_sem = me._IMAGE_SEMAPHORE
    me._IMAGE_SEMAPHORE = asyncio.Semaphore(4)

    mock_client = AsyncMock()
    mock_client.aio.models.generate_content = slow_generate

    mock_settings = MagicMock()
    mock_settings.google_api_key = "key"
    mock_settings.media_vision_model = "gemini-2.5-flash"
    mock_settings.image_extractor_concurrency = 4

    extractor = ImageExtractor()

    try:
        with (
            patch("beever_atlas.services.media_extractors.get_settings", return_value=mock_settings),
            patch("beever_atlas.services.media_extractors._get_gemini_client", AsyncMock(return_value=mock_client)),
            patch("beever_atlas.services.media_extractors.GEMINI_LIMITER", AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())),
        ):
            await asyncio.gather(*[
                extractor._describe_image(_png_bytes(), "", f"img{i}.png")
                for i in range(4)
            ])
    finally:
        me._IMAGE_SEMAPHORE = original_sem

    assert len(call_starts) == 4
    # At least 2 calls must have started before the first one finished — proves overlap
    first_end = min(call_ends)
    overlapping = sum(s < first_end for s in call_starts)
    assert overlapping >= 2, f"Expected parallel overlap, got starts={call_starts} ends={call_ends}"


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency():
    """With concurrency=2, no more than 2 images run simultaneously."""
    import beever_atlas.services.media_extractors as me

    active: list[int] = [0]
    peak: list[int] = [0]

    async def counting_generate(*args, **kwargs):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.04)
        active[0] -= 1
        return _make_fake_response("ok")

    original_sem = me._IMAGE_SEMAPHORE
    me._IMAGE_SEMAPHORE = asyncio.Semaphore(2)

    mock_client = AsyncMock()
    mock_client.aio.models.generate_content = counting_generate

    mock_settings = MagicMock()
    mock_settings.google_api_key = "key"
    mock_settings.media_vision_model = "gemini-2.5-flash"
    mock_settings.image_extractor_concurrency = 2

    extractor = ImageExtractor()

    try:
        with (
            patch("beever_atlas.services.media_extractors.get_settings", return_value=mock_settings),
            patch("beever_atlas.services.media_extractors._get_gemini_client", AsyncMock(return_value=mock_client)),
            patch("beever_atlas.services.media_extractors.GEMINI_LIMITER", AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())),
        ):
            await asyncio.gather(*[
                extractor._describe_image(_png_bytes(), "", f"img{i}.png")
                for i in range(6)
            ])
    finally:
        me._IMAGE_SEMAPHORE = original_sem

    assert peak[0] <= 2, f"Peak concurrency {peak[0]} exceeded semaphore limit of 2"


@pytest.mark.asyncio
async def test_exceptions_do_not_block_other_images():
    """A failing image call must not prevent other images from completing."""
    import beever_atlas.services.media_extractors as me

    call_count = 0

    async def flaky_generate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated Gemini error")
        return _make_fake_response(f"desc-{call_count}")

    original_sem = me._IMAGE_SEMAPHORE
    me._IMAGE_SEMAPHORE = asyncio.Semaphore(4)

    mock_client = AsyncMock()
    mock_client.aio.models.generate_content = flaky_generate

    mock_settings = MagicMock()
    mock_settings.google_api_key = "key"
    mock_settings.media_vision_model = "gemini-2.5-flash"
    mock_settings.image_extractor_concurrency = 4

    extractor = ImageExtractor()

    try:
        with (
            patch("beever_atlas.services.media_extractors.get_settings", return_value=mock_settings),
            patch("beever_atlas.services.media_extractors._get_gemini_client", AsyncMock(return_value=mock_client)),
            patch("beever_atlas.services.media_extractors.GEMINI_LIMITER", AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())),
        ):
            results = await asyncio.gather(*[
                extractor._describe_image(_png_bytes(), "", f"img{i}.png")
                for i in range(4)
            ], return_exceptions=True)
    finally:
        me._IMAGE_SEMAPHORE = original_sem

    # No result is an unhandled exception — ImageExtractor swallows errors and returns ""
    assert all(isinstance(r, str) for r in results)
    # At least 3 out of 4 succeeded
    non_empty = [r for r in results if r]
    assert len(non_empty) >= 3
