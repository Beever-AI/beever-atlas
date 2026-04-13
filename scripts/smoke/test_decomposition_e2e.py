"""
End-to-end SSE smoke test for the decomposition event.

Hits the running API server (default http://localhost:8000) with a multi-aspect
question and parses the SSE stream for `event: decomposition`.

Usage:
    python scripts/smoke/test_decomposition_e2e.py
    API_URL=http://localhost:8000 CHANNEL_ID=C025GA5LD python scripts/smoke/test_decomposition_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from urllib.error import URLError

API_URL = os.environ.get("API_URL", "http://localhost:8000")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "C025GA5LD")
QUESTION = os.environ.get(
    "TEST_QUESTION",
    "Compare the architecture, technology stack, and key contributors of the "
    "Beever Atlas project, and explain how recent decisions have shaped its direction.",
)


def main() -> int:
    url = f"{API_URL}/api/channels/{CHANNEL_ID}/ask"
    body = json.dumps({"question": QUESTION, "mode": "deep"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )

    print(f"POST {url}")
    print(f"  question: {QUESTION[:90]}...")
    print()

    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except URLError as e:
        print(f"ERROR: could not reach API server: {e}")
        print("Hint: is uvicorn running on", API_URL, "?")
        return 1

    events: list[tuple[str, str]] = []
    current_event = ""
    decomposition_payload: dict | None = None
    char_budget = 0
    max_chars = 50_000  # cap so we don't stream forever

    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        char_budget += len(line)
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            data_str = line[6:]
            events.append((current_event, data_str))
            if current_event == "decomposition":
                try:
                    decomposition_payload = json.loads(data_str)
                except json.JSONDecodeError:
                    pass
            if current_event == "done":
                break
        if char_budget > max_chars:
            print("(stream capped at max_chars)")
            break

    # Summary
    by_type: dict[str, int] = {}
    for ev, _ in events:
        by_type[ev] = by_type.get(ev, 0) + 1
    print("Event counts:")
    for k in sorted(by_type):
        print(f"  {k}: {by_type[k]}")
    print()

    if decomposition_payload is None:
        print("FAIL: no 'decomposition' SSE event received.")
        print("Hint: backend QueryDecomposer may not be firing. Check server logs for")
        print("      'QueryDecomposer result: is_simple=... internal=...'.")
        return 2

    internal = decomposition_payload.get("internal", [])
    external = decomposition_payload.get("external", [])
    print("Decomposition event payload:")
    print(f"  internal ({len(internal)}):")
    for sq in internal:
        print(f"    [{sq.get('label')}] {sq.get('query')}")
    print(f"  external ({len(external)}):")
    for sq in external:
        print(f"    [{sq.get('label')}] {sq.get('query')}")
    print()

    total = len(internal) + len(external)
    if total >= 2:
        print(f"PASS: decomposition event fired with {total} sub-queries.")
        return 0
    else:
        print(f"WARN: decomposition fired but only {total} sub-query (degraded).")
        return 3


if __name__ == "__main__":
    sys.exit(main())
