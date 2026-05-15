"""Tests for the ``PullSource`` / ``PushSource`` protocols.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/message-source-protocol/``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from beever_atlas.adapters.source_protocol import (
    MessageSource,
    PullSource,
    PushSource,
)


# ─────────────────────────────────────────────────────────────────────────────
# Stub implementations exercise structural typing.
# ─────────────────────────────────────────────────────────────────────────────


class _FakePullSource:
    source_id = "stub-pull"

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime | None, int]] = []

    async def fetch_and_persist(
        self,
        channel_id: str,
        since: datetime | None = None,
        max_messages: int = 1000,
    ) -> int:
        self.calls.append((channel_id, since, max_messages))
        return 42


class _FakePushSource:
    source_id = "stub-push"

    def __init__(self) -> None:
        self.received: list[tuple[str, str, dict]] = []

    async def on_message_received(
        self,
        channel_id: str,
        message_id: str,
        payload: dict,
    ) -> None:
        self.received.append((channel_id, message_id, payload))


# ─────────────────────────────────────────────────────────────────────────────
# isinstance() against runtime_checkable Protocols
# ─────────────────────────────────────────────────────────────────────────────


def test_fake_pull_source_satisfies_pull_source_protocol() -> None:
    """Spec scenario: ``A platform adapter implements PullSource``."""
    src = _FakePullSource()
    assert isinstance(src, PullSource)


def test_fake_push_source_satisfies_push_source_protocol() -> None:
    """Spec scenario: ``A future webhook receiver implements PushSource``."""
    src = _FakePushSource()
    assert isinstance(src, PushSource)


def test_pull_source_is_not_a_push_source() -> None:
    """Interface segregation: a pull source does not satisfy the push contract."""
    src = _FakePullSource()
    assert not isinstance(src, PushSource)


def test_push_source_is_not_a_pull_source() -> None:
    """Interface segregation: a push source does not satisfy the pull contract."""
    src = _FakePushSource()
    assert not isinstance(src, PullSource)


def test_message_source_alias_accepts_either() -> None:
    """Spec scenario: ``Generic code accepts either source``."""
    sources: list[MessageSource] = [_FakePullSource(), _FakePushSource()]
    assert len(sources) == 2
    # Each element satisfies at least one of the union's members.
    for s in sources:
        assert isinstance(s, (PullSource, PushSource))


# ─────────────────────────────────────────────────────────────────────────────
# Behavioural smoke
# ─────────────────────────────────────────────────────────────────────────────


async def test_pull_source_fetch_and_persist_round_trip() -> None:
    src = _FakePullSource()
    n = await src.fetch_and_persist("C123", since=None, max_messages=100)
    assert n == 42
    assert src.calls == [("C123", None, 100)]


async def test_push_source_on_message_received_round_trip() -> None:
    """Spec scenario: ``Same push event delivered twice`` (per-call shape)."""
    src = _FakePushSource()
    payload = {"content": "hi", "author": "alice"}
    await src.on_message_received("C123", "m1", payload)
    await src.on_message_received("C123", "m1", payload)
    # The protocol contract permits the second call as a no-op at the
    # persistence layer; the stub records both arrivals to verify shape.
    assert len(src.received) == 2
    assert src.received[0] == ("C123", "m1", payload)


# ─────────────────────────────────────────────────────────────────────────────
# Static check — no NotImplementedError stubs
# ─────────────────────────────────────────────────────────────────────────────


def test_protocol_module_has_no_notimplementederror_stubs() -> None:
    """Spec requirement: no ``NotImplementedError`` stubs in the protocol module.

    A single protocol with both methods raising ``NotImplementedError`` for
    inapplicable callers would violate interface segregation. The split design
    avoids this — verify by source inspection that the marker doesn't appear.
    """
    src = Path(__file__).parents[2] / "src" / "beever_atlas" / "adapters" / "source_protocol.py"
    text = src.read_text(encoding="utf-8")
    assert "NotImplementedError" not in text, (
        "source_protocol.py must not use NotImplementedError stubs — split the protocols instead"
    )
