"""Unit tests for the blob-key contract (``blob_backend``).

Covers the S4 hardening: :func:`blob_key` and :func:`blob_prefix` validate
their ``channel_id`` (and ``blob_key`` its ``sha256``) against a strict
charset so a crafted id containing ``/`` or ``..`` cannot escape the
``channels/{id}/`` prefix on S3/MinIO (path traversal into another channel's
objects).

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import hashlib

import pytest

from beever_atlas.stores.blob_backend import blob_key, blob_prefix


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Rebuild the Settings cache around every test (see sibling media tests)."""
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_SHA = hashlib.sha256(b"x").hexdigest()


class TestBlobKeyValidation:
    def test_accepts_valid_components(self):
        # Real platform channel-id shapes all match ^[A-Za-z0-9_.:-]+$.
        for channel_id in (
            "C0123ABCD",  # Slack
            "1234567890123456789",  # Discord snowflake
            "19:abc-def-ghi",  # Teams composite id with ':' and '-'
            "abc123def456ghi789jkl012mn",  # Mattermost 26-char id
            "slack:T1",
        ):
            key = blob_key(channel_id, _SHA)
            assert key == f"channels/{channel_id}/{_SHA}"

    @pytest.mark.parametrize(
        "bad",
        [
            "../escape",
            "a/b",
            "..",
            "with space",
            "with\ttab",
            "with\nnewline",
            "",
            "weird*char",
            "semi;colon",
        ],
    )
    def test_rejects_unsafe_channel_id(self, bad):
        with pytest.raises(ValueError, match="invalid channel_id"):
            blob_key(bad, _SHA)

    @pytest.mark.parametrize("bad", ["../sha", "a/b", "..", "has space", "", "star*"])
    def test_rejects_unsafe_sha256(self, bad):
        with pytest.raises(ValueError, match="invalid sha256"):
            blob_key("C1", bad)


class TestBlobPrefixValidation:
    def test_accepts_valid_channel_id(self):
        assert blob_prefix("C0123ABCD") == "channels/C0123ABCD/"

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "with space", ""])
    def test_rejects_unsafe_channel_id(self, bad):
        with pytest.raises(ValueError, match="invalid channel_id"):
            blob_prefix(bad)
