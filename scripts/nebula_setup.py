"""NebulaGraph setup helper — register storage host and verify connectivity.

Usage:
    python scripts/nebula_setup.py

Requires nebula3-python: pip install nebula3-python

Reads NEBULA_USER / NEBULA_PASSWORD from the environment (same names the
runtime uses); falls back to NebulaGraph's factory defaults so a fresh
local cluster works out of the box. Override both for any non-local
deployment — the factory defaults are widely known.
"""
from __future__ import annotations

import os
import sys
import time


def main() -> None:
    try:
        from nebula3.gclient.net import ConnectionPool
        from nebula3.Config import Config
    except ImportError:
        print("ERROR: nebula3-python not installed. Run: pip install nebula3-python")
        sys.exit(1)

    host = "127.0.0.1"
    port = 9669
    user = os.environ.get("NEBULA_USER", "root")
    password = os.environ.get("NEBULA_PASSWORD", "nebula")
    if password == "nebula":
        print(
            "WARNING: using NebulaGraph factory-default password ('nebula'). "
            "Set NEBULA_PASSWORD for any non-local deployment."
        )

    print(f"Connecting to NebulaGraph at {host}:{port}...")

    config = Config()
    config.max_connection_pool_size = 4
    pool = ConnectionPool()

    # Retry connection (graphd may still be starting)
    for attempt in range(10):
        try:
            ok = pool.init([(host, port)], config)
            if ok:
                break
        except Exception as exc:
            print(f"  Attempt {attempt + 1}/10: {exc}")
            time.sleep(3)
    else:
        print("ERROR: Could not connect to NebulaGraph after 10 attempts.")
        sys.exit(1)

    print("Connected. Registering storage host...")

    session = pool.get_session(user, password)
    try:
        # Register storaged with metad
        result = session.execute("ADD HOSTS \"nebula-storaged\":9779")
        if result.is_succeeded():
            print("  Storage host registered successfully.")
        else:
            msg = result.error_msg()
            if "existed" in msg.lower() or "already" in msg.lower():
                print("  Storage host already registered.")
            else:
                print(f"  ADD HOSTS result: {msg}")

        # Wait for registration to propagate
        print("  Waiting for storage registration to propagate...")
        time.sleep(5)

        # Verify
        result = session.execute("SHOW HOSTS")
        if result.is_succeeded():
            print(f"  SHOW HOSTS: {result.column_values('Host')}")
        else:
            print(f"  SHOW HOSTS failed: {result.error_msg()}")

        # Quick smoke test
        result = session.execute("SHOW SPACES")
        if result.is_succeeded():
            print(f"  SHOW SPACES: OK (existing spaces: {result.row_size()})")
        else:
            print(f"  SHOW SPACES failed: {result.error_msg()}")

        print("\nNebulaGraph is ready!")
        print(f"  Graph endpoint: {host}:{port}")
        print(f"  User: {user}")
        print(f"  Set GRAPH_BACKEND=nebula in .env to use it.")

    finally:
        session.release()
        pool.close()


if __name__ == "__main__":
    main()
