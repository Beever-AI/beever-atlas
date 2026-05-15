"""Tests for the runtime proxy-host allowlist derived from active
PlatformConnection records.

Goal: prove that connecting a self-hosted Mattermost via the UI causes
its hostname to flow into both proxy allowlists (``infra.http_safe`` for
``/api/files/proxy`` and ``api.media`` for ``/api/media/proxy``)
without any operator-side env var.
"""

from __future__ import annotations

import pytest

from beever_atlas.api.media import (
    clear_runtime_media_hosts,
    effective_allowed_hosts,
    register_runtime_media_hosts,
)
from beever_atlas.infra.http_safe import (
    _active_allowlist,
    clear_runtime_hosts,
    get_runtime_hosts,
    register_runtime_hosts,
)
from beever_atlas.infra.platform_hosts import (
    _hostname_of,
    derive_proxy_hosts_from_connections,
    refresh_runtime_proxy_hosts,
)


@pytest.fixture(autouse=True)
def _isolate_runtime_hosts(monkeypatch):
    """Reset both runtime-host registers AND clear the env-var allowlist
    so the developer's local ``.env`` (which may already have a
    ``FILE_PROXY_HOST_ALLOWLIST_EXTRA`` populated for their own
    Mattermost) doesn't leak hosts into these tests. The whole point
    of these cases is to prove the allowlist updates from connection
    state alone — env var contributions are tested separately."""
    monkeypatch.delenv("FILE_PROXY_HOST_ALLOWLIST", raising=False)
    monkeypatch.delenv("FILE_PROXY_HOST_ALLOWLIST_EXTRA", raising=False)
    clear_runtime_hosts()
    clear_runtime_media_hosts()
    yield
    clear_runtime_hosts()
    clear_runtime_media_hosts()


class TestHostnameOf:
    def test_extracts_hostname_from_https(self) -> None:
        assert _hostname_of("https://team.example.com/api") == "team.example.com"

    def test_lowercases_hostname(self) -> None:
        assert _hostname_of("https://Team.Example.Com") == "team.example.com"

    def test_strips_whitespace(self) -> None:
        assert _hostname_of("  https://x.example.com ") == "x.example.com"

    def test_rejects_non_http_schemes(self) -> None:
        assert _hostname_of("ssh://team.example.com") is None
        assert _hostname_of("file:///etc/passwd") is None

    def test_returns_none_for_non_url_credentials(self) -> None:
        # A Slack bot token (looks like ``xoxb-...``) must not be parsed
        # as a URL.
        assert _hostname_of("xoxb-1234-abcd") is None
        assert _hostname_of("") is None
        assert _hostname_of(None) is None
        assert _hostname_of(12345) is None


class TestRegisterRuntimeHosts:
    def test_registration_unions_into_active_allowlist(self) -> None:
        register_runtime_hosts({"team.votee.com"})
        allowed = _active_allowlist(None)
        assert "team.votee.com" in allowed
        # Defaults still present
        assert "files.slack.com" in allowed

    def test_replacement_semantics_on_re_register(self) -> None:
        # Two connections, then one gets deleted — re-register with the
        # remaining set should drop the dropped host.
        register_runtime_hosts({"a.example.com", "b.example.com"})
        assert "a.example.com" in get_runtime_hosts()
        assert "b.example.com" in get_runtime_hosts()
        register_runtime_hosts({"a.example.com"})  # b deleted
        assert "a.example.com" in get_runtime_hosts()
        assert "b.example.com" not in get_runtime_hosts()

    def test_clear_resets(self) -> None:
        register_runtime_hosts({"x.example.com"})
        clear_runtime_hosts()
        assert get_runtime_hosts() == frozenset()
        assert "x.example.com" not in _active_allowlist(None)

    def test_empty_and_whitespace_filtered(self) -> None:
        register_runtime_hosts({"", "   ", "ok.example.com"})
        assert get_runtime_hosts() == frozenset({"ok.example.com"})

    def test_lowercased(self) -> None:
        register_runtime_hosts({"Team.Example.Com"})
        assert "team.example.com" in get_runtime_hosts()

    def test_full_override_env_still_unions_runtime(self, monkeypatch) -> None:
        # FILE_PROXY_HOST_ALLOWLIST is a "full replacement" of platform
        # defaults, but runtime hosts are still merged so a UI-
        # registered connection isn't accidentally locked out.
        monkeypatch.setenv("FILE_PROXY_HOST_ALLOWLIST", "only.example.com")
        register_runtime_hosts({"team.votee.com"})
        allowed = _active_allowlist(None)
        assert "only.example.com" in allowed
        assert "team.votee.com" in allowed
        # Platform defaults are intentionally dropped under full override
        assert "files.slack.com" not in allowed

    def test_extra_env_unions_with_runtime_and_defaults(self, monkeypatch) -> None:
        monkeypatch.setenv("FILE_PROXY_HOST_ALLOWLIST_EXTRA", "extra.example.com")
        register_runtime_hosts({"team.votee.com"})
        allowed = _active_allowlist(None)
        assert "extra.example.com" in allowed
        assert "team.votee.com" in allowed
        assert "files.slack.com" in allowed  # default preserved


class TestRegisterRuntimeMediaHosts:
    def test_added_to_effective_allowed_hosts(self) -> None:
        register_runtime_media_hosts({"team.votee.com"})
        assert "team.votee.com" in effective_allowed_hosts()
        # Defaults still present
        assert "files.slack.com" in effective_allowed_hosts()

    def test_replacement_semantics(self) -> None:
        register_runtime_media_hosts({"a.example.com"})
        register_runtime_media_hosts({"b.example.com"})
        eff = effective_allowed_hosts()
        assert "b.example.com" in eff
        assert "a.example.com" not in eff


class _FakeConn:
    def __init__(self, conn_id: str, platform: str, status: str = "connected"):
        self.id = conn_id
        self.platform = platform
        self.status = status


class _FakeStorePlatform:
    """Minimal stub mimicking ``stores.platform`` for the helper test."""

    def __init__(self, items: list[tuple[_FakeConn, dict]]):
        self._items = items

    async def list_connections(self) -> list[_FakeConn]:
        return [c for c, _creds in self._items]

    def decrypt_connection_credentials(self, conn: _FakeConn) -> dict:
        for c, creds in self._items:
            if c.id == conn.id:
                return creds
        return {}


class _FakeStores:
    def __init__(self, items: list[tuple[_FakeConn, dict]]):
        self.items = items  # exposed so tests can mutate connection status
        self.platform = _FakeStorePlatform(items)


class TestDeriveProxyHostsFromConnections:
    @pytest.mark.asyncio
    async def test_extracts_base_url_host(self) -> None:
        stores = _FakeStores(
            [
                (_FakeConn("c1", "mattermost"), {"base_url": "https://team.votee.com"}),
            ]
        )
        hosts = await derive_proxy_hosts_from_connections(stores)
        assert hosts == {"team.votee.com"}

    @pytest.mark.asyncio
    async def test_skips_disconnected(self) -> None:
        stores = _FakeStores(
            [
                (
                    _FakeConn("c1", "mattermost", status="disconnected"),
                    {"base_url": "https://gone.example.com"},
                ),
                (
                    _FakeConn("c2", "mattermost"),
                    {"base_url": "https://active.example.com"},
                ),
            ]
        )
        hosts = await derive_proxy_hosts_from_connections(stores)
        assert hosts == {"active.example.com"}

    @pytest.mark.asyncio
    async def test_recognizes_alternate_url_keys(self) -> None:
        stores = _FakeStores(
            [
                (_FakeConn("c1", "x"), {"server_url": "https://srv1.example.com"}),
                (_FakeConn("c2", "y"), {"instance_url": "https://srv2.example.com"}),
            ]
        )
        hosts = await derive_proxy_hosts_from_connections(stores)
        assert hosts == {"srv1.example.com", "srv2.example.com"}

    @pytest.mark.asyncio
    async def test_ignores_non_url_credentials(self) -> None:
        # A Slack connection has only a bot token — no base_url, so
        # nothing should be registered. The static cloud allowlist
        # already covers ``files.slack.com``.
        stores = _FakeStores(
            [
                (_FakeConn("c1", "slack"), {"bot_token": "xoxb-1-2-3"}),
            ]
        )
        hosts = await derive_proxy_hosts_from_connections(stores)
        assert hosts == set()

    @pytest.mark.asyncio
    async def test_refresh_writes_into_both_allowlists(self) -> None:
        stores = _FakeStores(
            [
                (
                    _FakeConn("c1", "mattermost"),
                    {"base_url": "https://team.votee.com"},
                ),
            ]
        )
        await refresh_runtime_proxy_hosts(stores)
        assert "team.votee.com" in _active_allowlist(None)
        assert "team.votee.com" in effective_allowed_hosts()

    @pytest.mark.asyncio
    async def test_refresh_drops_host_when_connection_disconnected(self) -> None:
        # First refresh: connection is connected → host registered.
        stores = _FakeStores(
            [
                (
                    _FakeConn("c1", "mattermost"),
                    {"base_url": "https://team.votee.com"},
                ),
            ]
        )
        await refresh_runtime_proxy_hosts(stores)
        assert "team.votee.com" in _active_allowlist(None)

        # Second refresh after disconnecting → host should drop.
        stores.items[0][0].status = "disconnected"
        await refresh_runtime_proxy_hosts(stores)
        assert "team.votee.com" not in _active_allowlist(None)
