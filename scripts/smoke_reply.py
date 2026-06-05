#!/usr/bin/env python
"""Local smoke test for the bot-reply SSE contract.

Drives the REAL ``POST /api/channels/{id}/ask`` endpoint in-process (httpx
ASGITransport) with mocked stores + a stubbed agent runner — no external
services, database, or LLM keys needed — and asserts the v2/v3 SSE contract
fields the chat bot relies on:

  - ``metadata.confidence`` is COMPUTED (not the old hardcoded ``0.85``)
  - ``metadata.is_empty_retrieval`` is present (honest empty-state signal)
  - ``metadata.last_sync_ts`` key is present (freshness signal)
  - the conversation-memory ``session_id`` is accepted (no 500)
  - the stream terminates with ``done``

Run:  uv run python scripts/smoke_reply.py
Exit 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import os

# Must be set before importing the app (auth + dev-mode wiring).
os.environ.setdefault("BEEVER_ENV", "test")
os.environ.setdefault("BEEVER_API_KEYS", "test-key")

import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402


def _make_event(text: str, *, partial: bool = False, turn_complete: bool = False):
    part = MagicMock()
    part.text = text
    content = MagicMock()
    content.parts = [part]
    ev = MagicMock()
    ev.content = content
    ev.partial = partial
    ev.turn_complete = turn_complete
    ev.error_code = None
    ev.error_message = None
    ev.get_function_calls.return_value = []
    ev.get_function_responses.return_value = []
    return ev


async def _fake_runner(**_kwargs):
    """Stand-in ADK runner: stream a short answer, then complete the turn."""
    yield _make_event("GridFS is the OSS default; MinIO is opt-in.", partial=True)
    yield _make_event("", turn_complete=True)


def _install_mock_stores():
    import beever_atlas.stores as stores_mod

    fake = MagicMock(name="MockStoreClients")
    fake.mongodb = MagicMock()
    fake.mongodb.get_channel_sync_state = AsyncMock(return_value=None)
    fake.qa_history = MagicMock()
    fake.qa_history.write_qa_entry = AsyncMock()
    fake.chat_history = MagicMock()
    stores_mod._stores = fake
    stores_mod._stores_ready.set()


def _parse_sse(text: str) -> list[tuple[str, dict | None]]:
    events: list[tuple[str, dict | None]] = []
    for block in text.split("\n\n"):
        etype: str | None = None
        data: dict | None = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                etype = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except Exception:
                    data = None
        if etype:
            events.append((etype, data))
    return events


async def main() -> int:
    _install_mock_stores()

    from beever_atlas.infra.auth import Principal, require_user
    from beever_atlas.server.app import app

    app.dependency_overrides[require_user] = lambda: Principal("user:test", kind="user")

    with (
        patch("beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()),
        patch("beever_atlas.api.ask.create_runner") as mock_cr,
        patch("beever_atlas.api.ask.create_session", new_callable=AsyncMock) as mock_cs,
        patch("beever_atlas.api.ask._build_decomposed_prompt", side_effect=lambda q, _c: (q, None)),
        patch("beever_atlas.api.ask._load_chat_history_parts", side_effect=lambda _s: []),
        patch("beever_atlas.api.ask._assert_session_ownership_or_new", new_callable=AsyncMock),
        # ACL is verified by the dedicated channel-access tests; no-op it here so
        # the smoke can focus on the SSE contract without wiring real connections.
        patch("beever_atlas.api.ask.assert_channel_access", new_callable=AsyncMock),
    ):
        runner = MagicMock()
        runner.run_async = _fake_runner
        mock_cr.return_value = runner
        sess = MagicMock()
        sess.user_id = "user:test"
        sess.id = "smoke-session"
        mock_cs.return_value = sess

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/channels/test-channel/ask",
                json={"question": "why GridFS?", "session_id": "botmem_smoke"},
                headers={"Authorization": "Bearer test-key"},
            )
            body = resp.text

    events = _parse_sse(body)
    types = [e for e, _ in events]
    meta = next((d for e, d in events if e == "metadata"), None)

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    check("HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    check("metadata event present", meta is not None)
    if meta is not None:
        conf = meta.get("confidence")
        check("confidence is numeric", isinstance(conf, (int, float)), f"confidence={conf}")
        check(
            "confidence is computed, not the old 0.85 constant", conf != 0.85, f"confidence={conf}"
        )
        check(
            "is_empty_retrieval present",
            "is_empty_retrieval" in meta,
            f"={meta.get('is_empty_retrieval')}",
        )
        check("last_sync_ts key present", "last_sync_ts" in meta)
    check("stream terminates with done", "done" in types)

    print("Observed SSE event types:", types)
    print("Metadata payload:", json.dumps(meta, indent=2) if meta else None)
    print()
    ok_all = True
    for name, ok, detail in checks:
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] {name}{(' — ' + detail) if detail else ''}")
        ok_all = ok_all and ok
    print()
    print("SMOKE:", "PASS ✅" if ok_all else "FAIL ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
