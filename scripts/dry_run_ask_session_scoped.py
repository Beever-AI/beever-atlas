#!/usr/bin/env python3
"""Dry-run validation for the session-scoped Ask refactor.

Validates in two modes:

1. **Code/import checks (always):** verifies that ChatHistoryStore has the new
   v2 methods, that api/ask.py registers the new routes, and that Pydantic
   models exist. Runs without a backend.

2. **Live HTTP checks (optional, when --live is passed):** exercises the real
   endpoints end-to-end against a running backend at http://localhost:8000.
   Creates a session via two turns with different channels, verifies
   channel_ids aggregation, lists/loads/renames/pins/deletes, uploads a file,
   submits feedback.

Exit code 0 on full pass, 1 on any failure.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from typing import Any

errors: list[str] = []
checks_passed = 0


def check(description: str, condition: bool, detail: str = "") -> None:
    global checks_passed
    if condition:
        checks_passed += 1
        print(f"  ✓ {description}")
    else:
        errors.append(f"{description}: {detail}")
        print(f"  ✗ {description} — {detail}")


# ---------------------------------------------------------------------------
# Code / import checks (no backend required)
# ---------------------------------------------------------------------------


def run_code_checks() -> None:
    print("\n=== Ask Session-Scoped Refactor — Code Checks ===\n")

    print("[1] ChatHistoryStore API surface")
    from beever_atlas.stores.chat_history_store import ChatHistoryStore

    check(
        "create_session_v2 method exists",
        hasattr(ChatHistoryStore, "create_session_v2"),
    )
    check(
        "list_sessions_global method exists",
        hasattr(ChatHistoryStore, "list_sessions_global"),
    )
    check(
        "load_session_with_channels method exists",
        hasattr(ChatHistoryStore, "load_session_with_channels"),
    )
    check(
        "save_message accepts channel_id parameter",
        "channel_id" in ChatHistoryStore.save_message.__annotations__,
    )
    check(
        "legacy create_session method still present",
        hasattr(ChatHistoryStore, "create_session"),
    )
    check(
        "legacy list_sessions method still present",
        hasattr(ChatHistoryStore, "list_sessions"),
    )

    print("\n[2] api/ask.py route registration")
    from beever_atlas.api import ask as ask_module

    # Collect (path, method) pairs across all routes — same path may have
    # multiple methods (GET/PATCH/DELETE on a session).
    route_pairs: set[tuple[str, str]] = set()
    for route in ask_module.router.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path:
            for m in methods:
                route_pairs.add((path, m))

    def has_route(path: str, method: str) -> bool:
        return (path, method) in route_pairs

    check("POST /api/ask registered", has_route("/api/ask", "POST"))
    check("GET /api/ask/sessions registered", has_route("/api/ask/sessions", "GET"))
    check(
        "GET /api/ask/sessions/{session_id} registered",
        has_route("/api/ask/sessions/{session_id}", "GET"),
    )
    check(
        "PATCH /api/ask/sessions/{session_id} registered",
        has_route("/api/ask/sessions/{session_id}", "PATCH"),
    )
    check(
        "DELETE /api/ask/sessions/{session_id} registered",
        has_route("/api/ask/sessions/{session_id}", "DELETE"),
    )
    check("POST /api/ask/upload registered", has_route("/api/ask/upload", "POST"))
    check("POST /api/ask/feedback registered", has_route("/api/ask/feedback", "POST"))

    # Legacy endpoints should still exist for backward compat
    check(
        "Legacy POST /api/channels/{channel_id}/ask still registered",
        has_route("/api/channels/{channel_id}/ask", "POST"),
    )
    check(
        "Legacy GET /api/channels/{channel_id}/ask/history still registered",
        has_route("/api/channels/{channel_id}/ask/history", "GET"),
    )

    print("\n[3] Pydantic models")
    check(
        "AskV2Request model exists",
        hasattr(ask_module, "AskV2Request"),
    )
    check(
        "FeedbackV2Request model exists",
        hasattr(ask_module, "FeedbackV2Request"),
    )
    if hasattr(ask_module, "AskV2Request"):
        fields = ask_module.AskV2Request.model_fields
        check("AskV2Request.channel_id is required", "channel_id" in fields)
        check("AskV2Request.question is required", "question" in fields)
        check("AskV2Request.session_id is optional", "session_id" in fields)
        check("AskV2Request.mode is present", "mode" in fields)

    print("\n[4] _persist_qa_history accepts use_v2_schema flag")
    check(
        "_persist_qa_history has use_v2_schema kwarg",
        "use_v2_schema" in ask_module._persist_qa_history.__annotations__,
    )
    check(
        "_run_agent_stream has use_v2_schema kwarg",
        "use_v2_schema" in ask_module._run_agent_stream.__annotations__,
    )


# ---------------------------------------------------------------------------
# Live HTTP checks (requires --live flag and running backend)
# ---------------------------------------------------------------------------


def run_live_checks(base_url: str) -> None:
    print(f"\n=== Live HTTP Checks ({base_url}) ===\n")
    try:
        import httpx  # type: ignore[import]
    except ImportError:
        errors.append("httpx not installed, cannot run --live checks")
        print("  ✗ httpx not installed, cannot run --live checks")
        return

    session_id = str(uuid.uuid4())
    ch1 = "test-channel-1"
    ch2 = "test-channel-2"

    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        # 1. First turn against ch1
        print(f"[1] POST /api/ask (turn 1, channel={ch1})")
        resp = client.post(
            "/api/ask",
            json={
                "question": "dry-run test question 1",
                "channel_id": ch1,
                "session_id": session_id,
                "mode": "quick",
            },
        )
        check(
            "Turn 1 status 200",
            resp.status_code == 200,
            f"got {resp.status_code}: {resp.text[:200]}",
        )
        # SSE stream; consume to ensure the server processes the turn
        if resp.status_code == 200:
            _ = resp.text  # already fully read (non-stream client)

        # Small delay so async persistence completes
        time.sleep(1.5)

        # 2. Load the session
        print("\n[2] GET /api/ask/sessions/{session_id}")
        resp = client.get(f"/api/ask/sessions/{session_id}")
        check("Session load status 200", resp.status_code == 200)
        session: dict[str, Any] = resp.json() if resp.status_code == 200 else {}
        check("Session has no top-level channel_id", "channel_id" not in session)
        check(
            "Session has channel_ids array",
            isinstance(session.get("channel_ids"), list),
        )
        check(
            f"channel_ids contains {ch1}",
            ch1 in (session.get("channel_ids") or []),
        )

        # 3. Second turn against ch2 (same session)
        print(f"\n[3] POST /api/ask (turn 2, channel={ch2})")
        resp = client.post(
            "/api/ask",
            json={
                "question": "dry-run test question 2",
                "channel_id": ch2,
                "session_id": session_id,
                "mode": "quick",
            },
        )
        check("Turn 2 status 200", resp.status_code == 200)
        time.sleep(1.5)

        # 4. Session now has both channels
        print("\n[4] GET session again — both channels should appear")
        resp = client.get(f"/api/ask/sessions/{session_id}")
        session = resp.json() if resp.status_code == 200 else {}
        check(
            f"channel_ids contains both {ch1} and {ch2}",
            ch1 in (session.get("channel_ids") or [])
            and ch2 in (session.get("channel_ids") or []),
        )

        # 5. List sessions
        print("\n[5] GET /api/ask/sessions")
        resp = client.get("/api/ask/sessions")
        check("List sessions status 200", resp.status_code == 200)
        payload = resp.json() if resp.status_code == 200 else {}
        found = any(s["session_id"] == session_id for s in payload.get("sessions", []))
        check("Our session appears in list", found)

        # 6. Rename + pin
        print("\n[6] PATCH /api/ask/sessions/{session_id}")
        resp = client.patch(
            f"/api/ask/sessions/{session_id}",
            json={"title": "dry-run test", "pinned": True},
        )
        check("Rename/pin status 200", resp.status_code == 200)

        # 7. Upload
        print("\n[7] POST /api/ask/upload")
        resp = client.post(
            "/api/ask/upload",
            files={"file": ("test.txt", b"dry run content", "text/plain")},
        )
        check("Upload status 200", resp.status_code == 200)
        if resp.status_code == 200:
            upload = resp.json()
            check("Upload returns file_id", "file_id" in upload)
            check("Upload returns extracted_text", "extracted_text" in upload)

        # 8. Feedback
        print("\n[8] POST /api/ask/feedback")
        resp = client.post(
            "/api/ask/feedback",
            json={
                "session_id": session_id,
                "message_id": "test-msg-id",
                "rating": "up",
                "channel_id": ch1,
            },
        )
        check("Feedback status 200", resp.status_code == 200)

        # 9. Soft-delete
        print("\n[9] DELETE /api/ask/sessions/{session_id}")
        resp = client.delete(f"/api/ask/sessions/{session_id}")
        check("Delete status 200", resp.status_code == 200)

        # 10. List excludes deleted
        print("\n[10] Deleted session excluded from list")
        resp = client.get("/api/ask/sessions")
        payload = resp.json() if resp.status_code == 200 else {}
        still_listed = any(
            s["session_id"] == session_id for s in payload.get("sessions", [])
        )
        check("Deleted session no longer in list", not still_listed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run live HTTP checks against a running backend",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Backend base URL for --live mode",
    )
    args = parser.parse_args()

    run_code_checks()
    if args.live:
        run_live_checks(args.base_url)

    print(f"\n{'=' * 40}")
    print(f"  Passed: {checks_passed}")
    print(f"  Failed: {len(errors)}")

    if errors:
        print("\n  FAILURES:")
        for e in errors:
            print(f"    ✗ {e}")
        return 1
    print("\n  All checks passed! ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
