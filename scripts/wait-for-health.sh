#!/usr/bin/env bash
# Poll /api/health until 200 or timeout (90s).
set -euo pipefail

URL="${HEALTH_URL:-http://localhost:8000/api/health}"
TIMEOUT=90
INTERVAL=3
elapsed=0

echo "Waiting for $URL (timeout ${TIMEOUT}s) ..."
until curl -sf "$URL" > /dev/null 2>&1; do
  if [ "$elapsed" -ge "$TIMEOUT" ]; then
    echo "Timed out after ${TIMEOUT}s waiting for $URL"
    docker compose logs --tail=50 || true
    exit 1
  fi
  sleep "$INTERVAL"
  elapsed=$((elapsed + INTERVAL))
  echo "  ${elapsed}s elapsed – retrying ..."
done

echo "Service healthy after ${elapsed}s."
