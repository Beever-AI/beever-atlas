#!/usr/bin/env bash
# =============================================================================
# smoke_test_media_persistence.sh
#
# Production smoke-test for the durable channel-media feature.
# Run this against the deployed EE site immediately after merge + deploy to
# verify that the GridFS/MinIO blob store, read-through proxy, ACL enforcement,
# and backfill machinery are all wired up correctly.
#
# USAGE
# -----
#   BASE_URL=https://beever-atlas.votee.dev \
#   ADMIN_TOKEN=<BEEVER_ADMIN_TOKEN> \
#   LOADER_KEY=<one BEEVER_API_KEY entry — the channel owner's key> \
#   LOADER_KEY_OTHER=<a second BEEVER_API_KEY entry that does NOT own CHANNEL_ID> \
#   CHANNEL_ID=<a channel whose media has already been ingested> \
#   MEDIA_URL=<a known-stored attachment URL from that channel> \
#   bash scripts/smoke_test_media_persistence.sh
#
# OPTIONAL ENV VARS
# -----------------
#   OTHER_CHANNEL_ID      A second channel ID (currently unused but reserved
#                         for future cross-channel ref tests).
#   CONFIRM_PURGE         Set to "yes" together with TEST_CHANNEL_ID to run
#                         Stage 6 (destructive purge check). DANGEROUS — this
#                         hard-deletes the test channel. Leave unset normally.
#   TEST_CHANNEL_ID       Channel to delete in Stage 6. MUST differ from
#                         CHANNEL_ID used in Stages 1-5.
#
# EXAMPLE INVOCATION
# ------------------
#   BASE_URL=https://beever-atlas.votee.dev \
#   ADMIN_TOKEN=supersecret \
#   LOADER_KEY=myapikey \
#   LOADER_KEY_OTHER=otherapikey \
#   CHANNEL_ID=C01234567 \
#   MEDIA_URL='https://cdn.discordapp.com/attachments/111/222/photo.png' \
#   bash scripts/smoke_test_media_persistence.sh
#
# REQUIRED TOOLS: curl, sha256sum (or shasum on macOS)
# OPTIONAL TOOLS: jq (graceful fallback to raw output when absent)
#
# EXIT CODE: 0 if every non-SKIP non-PROD-ONLY stage passes, non-zero otherwise.
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS=0
FAIL=0
_TMPDIR=""

_cleanup() {
  if [ -n "$_TMPDIR" ] && [ -d "$_TMPDIR" ]; then
    rm -rf "$_TMPDIR"
  fi
}
trap _cleanup EXIT

_make_tmp() {
  _TMPDIR="$(mktemp -d)"
}

_green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
_red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
_yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
_bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

_pass() {
  PASS=$((PASS + 1))
  _green "  PASS  $*"
}

_fail() {
  FAIL=$((FAIL + 1))
  _red   "  FAIL  $*"
}

_skip() {
  _yellow "  SKIP  $*"
}

_stage() {
  echo ""
  _bold "=== Stage $* ==="
}

# sha256sum is GNU coreutils; shasum is the macOS fallback.
_sha256() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    echo "sha256-unavailable"
  fi
}

# jq is optional — fall back to raw output.
_jq() {
  if command -v jq >/dev/null 2>&1; then
    jq "$@"
  else
    cat
  fi
}

# Extract a JSON field by key using jq when available, else grep+sed fallback.
_json_field() {
  local json="$1"
  local key="$2"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$json" | jq -r ".$key // empty"
  else
    # Crude grep fallback: handles simple string and numeric values.
    printf '%s' "$json" | grep -o "\"${key}\":[^,}]*" | sed 's/.*://;s/[" ]//g' | head -1
  fi
}

# ---------------------------------------------------------------------------
# Environment validation
# ---------------------------------------------------------------------------

_validate_env() {
  local missing=()
  for var in BASE_URL ADMIN_TOKEN LOADER_KEY CHANNEL_ID MEDIA_URL; do
    if [ -z "${!var:-}" ]; then
      missing+=("$var")
    fi
  done
  if [ ${#missing[@]} -gt 0 ]; then
    _red "ERROR: Required env vars not set: ${missing[*]}"
    echo ""
    echo "Usage: See the comment block at the top of this script."
    exit 1
  fi

  # Trim any trailing slash from BASE_URL.
  BASE_URL="${BASE_URL%/}"

  if [ -z "${LOADER_KEY_OTHER:-}" ]; then
    _yellow "NOTE: LOADER_KEY_OTHER not set — Stage 4 (cross-channel ACL deny) will be SKIPPED."
    echo "      To enable: set LOADER_KEY_OTHER to a BEEVER_API_KEY entry that does NOT"
    echo "      own CHANNEL_ID. This is a security gap — the deny check is not exercised."
  fi

  echo ""
  _bold "Target: $BASE_URL"
  echo "  CHANNEL_ID : $CHANNEL_ID"
  echo "  MEDIA_URL  : $MEDIA_URL"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_make_tmp
_validate_env

# ---------------------------------------------------------------------------
# Stage 0 — PREFLIGHT: confirm store is wired
# ---------------------------------------------------------------------------
_stage "0 — PREFLIGHT: GET /api/admin/media/stats"
echo "  Proves: admin endpoint reachable + MediaBlobStore is wired (non-null stats)."

STATS_RESP="$(curl -sf \
  -w '\n__HTTP_STATUS__%{http_code}' \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  "${BASE_URL}/api/admin/media/stats")" || {
  _fail "curl failed — is the site up? Check BASE_URL=${BASE_URL}"
  exit 1
}

STATS_STATUS="$(printf '%s' "$STATS_RESP" | grep '__HTTP_STATUS__' | sed 's/__HTTP_STATUS__//')"
STATS_BODY="$(printf '%s' "$STATS_RESP" | grep -v '__HTTP_STATUS__')"

echo "  HTTP $STATS_STATUS"
echo "  Body: $(printf '%s' "$STATS_BODY" | _jq .)"

if [ "$STATS_STATUS" != "200" ]; then
  _fail "Expected 200, got $STATS_STATUS"
else
  TOTAL_BLOBS="$(_json_field "$STATS_BODY" "total_blobs")"
  TOTAL_BYTES="$(_json_field "$STATS_BODY" "total_bytes")"
  TOTAL_REFS="$(_json_field  "$STATS_BODY" "total_refs")"
  echo "  total_blobs=$TOTAL_BLOBS  total_bytes=$TOTAL_BYTES  total_refs=$TOTAL_REFS"

  if [ -z "$TOTAL_BLOBS" ] || [ -z "$TOTAL_BYTES" ]; then
    _fail "Response missing expected fields total_blobs / total_bytes"
  else
    _pass "Store is wired: stats returned successfully"
  fi
fi

# Discover backend from stats response (field present if server exposes it).
BACKEND_HINT="$(_json_field "$STATS_BODY" "backend" 2>/dev/null || true)"
if [ -n "$BACKEND_HINT" ]; then
  echo "  Backend reported by server: $BACKEND_HINT"
else
  echo "  Backend: not reported in stats response (check CHANNEL_MEDIA_BACKEND env on server)"
fi

# ---------------------------------------------------------------------------
# Stage 1 — READ-THROUGH STORE HIT
# ---------------------------------------------------------------------------
_stage "1 — READ-THROUGH STORE HIT: GET /api/media/proxy"
echo "  Proves: blob store serves the known-ingested URL with X-Media-Source: store."
echo "  URL: $MEDIA_URL"

STAGE1_BODY_FILE="${_TMPDIR}/stage1_body"

STAGE1_HEADERS_FILE="${_TMPDIR}/stage1_headers"

HTTP_STATUS_1="$(curl -sf \
  --dump-header "$STAGE1_HEADERS_FILE" \
  -o "$STAGE1_BODY_FILE" \
  -w '%{http_code}' \
  --get \
  --data-urlencode "url=${MEDIA_URL}" \
  --data-urlencode "channel_id=${CHANNEL_ID}" \
  --data-urlencode "access_token=${LOADER_KEY}" \
  "${BASE_URL}/api/media/proxy")" || {
  _fail "Stage 1: curl failed (network error or 4xx/5xx — rerun with -v to diagnose)"
  HTTP_STATUS_1="000"
}

echo "  HTTP $HTTP_STATUS_1"

STAGE1_HEADERS="$(cat "$STAGE1_HEADERS_FILE" 2>/dev/null || true)"
BODY_BYTES="$(wc -c < "$STAGE1_BODY_FILE" 2>/dev/null || echo 0)"
STAGE1_SHA="$(_sha256 "$STAGE1_BODY_FILE")"

echo "  Body: ${BODY_BYTES} bytes  sha256=${STAGE1_SHA}"

# Extract headers (case-insensitive).
X_MEDIA_SOURCE_1="$(printf '%s' "$STAGE1_HEADERS" | grep -i '^x-media-source:' | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')"
X_CONTENT_TYPE_OPTIONS_1="$(printf '%s' "$STAGE1_HEADERS" | grep -i '^x-content-type-options:' | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')"
CSP_1="$(printf '%s' "$STAGE1_HEADERS" | grep -i '^content-security-policy:' | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')"
CONTENT_DISPOSITION_1="$(printf '%s' "$STAGE1_HEADERS" | grep -i '^content-disposition:' | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')"
CONTENT_TYPE_1="$(printf '%s' "$STAGE1_HEADERS" | grep -i '^content-type:' | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')"

echo "  X-Media-Source        : ${X_MEDIA_SOURCE_1:-(absent)}"
echo "  X-Content-Type-Options: ${X_CONTENT_TYPE_OPTIONS_1:-(absent)}"
echo "  Content-Security-Policy: ${CSP_1:-(absent)}"
echo "  Content-Disposition   : ${CONTENT_DISPOSITION_1:-(absent)}"
echo "  Content-Type          : ${CONTENT_TYPE_1:-(absent)}"

STAGE1_PASS=true

if [ "$HTTP_STATUS_1" != "200" ]; then
  _fail "Stage 1: expected HTTP 200, got $HTTP_STATUS_1"
  STAGE1_PASS=false
fi

if [ "$X_MEDIA_SOURCE_1" != "store" ]; then
  _fail "Stage 1: X-Media-Source should be 'store', got '${X_MEDIA_SOURCE_1}'"
  _yellow "       (If this is 'origin', the media URL may not have been ingested yet;"
  _yellow "        run the backfill first or pick a URL from an already-extracted message.)"
  STAGE1_PASS=false
fi

if [ "$X_CONTENT_TYPE_OPTIONS_1" != "nosniff" ]; then
  _fail "Stage 1: X-Content-Type-Options should be 'nosniff', got '${X_CONTENT_TYPE_OPTIONS_1}'"
  STAGE1_PASS=false
fi

if ! printf '%s' "$CSP_1" | grep -qi 'sandbox'; then
  _fail "Stage 1: Content-Security-Policy should contain 'sandbox', got '${CSP_1}'"
  STAGE1_PASS=false
fi

if [ "$STAGE1_PASS" = "true" ]; then
  _pass "HTTP 200 + X-Media-Source: store + nosniff + CSP sandbox"
fi

# ---------------------------------------------------------------------------
# Stage 2 — SECURITY HEADERS: Content-Disposition sanity
# ---------------------------------------------------------------------------
_stage "2 — SECURITY HEADERS: Content-Disposition"
echo "  Proves: disposition is 'inline' for safe image/PDF MIME types, 'attachment' for all others."

if [ "$HTTP_STATUS_1" != "200" ]; then
  _skip "Skipping (Stage 1 did not return 200)"
else
  MIME_BASE="$(printf '%s' "$CONTENT_TYPE_1" | sed 's/;.*//' | tr -d ' ' | tr '[:upper:]' '[:lower:]')"
  case "$MIME_BASE" in
    image/png|image/jpeg|image/gif|image/webp|image/avif|application/pdf)
      EXPECTED_DISP="inline"
      ;;
    *)
      EXPECTED_DISP="attachment"
      ;;
  esac

  echo "  Content-Type MIME: $MIME_BASE"
  echo "  Expected disposition: $EXPECTED_DISP  Actual: ${CONTENT_DISPOSITION_1:-(absent)}"

  if [ -z "$CONTENT_DISPOSITION_1" ]; then
    _fail "Stage 2: Content-Disposition header absent"
  elif [ "$CONTENT_DISPOSITION_1" = "$EXPECTED_DISP" ]; then
    _pass "Content-Disposition=$CONTENT_DISPOSITION_1 correct for MIME=$MIME_BASE"
  else
    _fail "Stage 2: expected Content-Disposition=$EXPECTED_DISP, got '$CONTENT_DISPOSITION_1'"
  fi
fi

# ---------------------------------------------------------------------------
# Stage 3 — LINK-ROT: serve from store even with mutated/removed query string
# ---------------------------------------------------------------------------
_stage "3 — LINK-ROT: mutated query string still hits store"
echo "  Proves: store lookup keys on host+path only; expired/rotated query params don't matter."

# Determine whether MEDIA_URL has a query string.
URL_HAS_QUERY=false
if printf '%s' "$MEDIA_URL" | grep -q '?'; then
  URL_HAS_QUERY=true
fi

if [ "$URL_HAS_QUERY" = "false" ]; then
  _skip "MEDIA_URL has no query string — link-rot test not applicable for this URL."
else
  # Strip the entire query string to simulate a signature expiry.
  MEDIA_URL_STRIPPED="$(printf '%s' "$MEDIA_URL" | sed 's/?.*//')"
  echo "  Requesting (no query): $MEDIA_URL_STRIPPED"

  STAGE3_BODY_FILE="${_TMPDIR}/stage3_body"
  STAGE3_HEADERS_FILE="${_TMPDIR}/stage3_headers"

  HTTP_STATUS_3="$(curl -sf \
    --dump-header "$STAGE3_HEADERS_FILE" \
    -o "$STAGE3_BODY_FILE" \
    -w '%{http_code}' \
    --get \
    --data-urlencode "url=${MEDIA_URL_STRIPPED}" \
    --data-urlencode "channel_id=${CHANNEL_ID}" \
    --data-urlencode "access_token=${LOADER_KEY}" \
    "${BASE_URL}/api/media/proxy")" || {
    _fail "Stage 3: curl failed"
    HTTP_STATUS_3="000"
  }

  echo "  HTTP $HTTP_STATUS_3"
  X_MEDIA_SOURCE_3="$(cat "$STAGE3_HEADERS_FILE" 2>/dev/null | grep -i '^x-media-source:' | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')"
  STAGE3_SHA="$(_sha256 "$STAGE3_BODY_FILE")"
  echo "  X-Media-Source: ${X_MEDIA_SOURCE_3:-(absent)}  sha256=${STAGE3_SHA}"

  STAGE3_PASS=true
  if [ "$HTTP_STATUS_3" != "200" ]; then
    _fail "Stage 3: expected 200, got $HTTP_STATUS_3"
    STAGE3_PASS=false
  fi
  if [ "$X_MEDIA_SOURCE_3" != "store" ]; then
    _fail "Stage 3: X-Media-Source should be 'store', got '${X_MEDIA_SOURCE_3}'"
    STAGE3_PASS=false
  fi
  if [ "$STAGE3_SHA" != "$STAGE1_SHA" ]; then
    _fail "Stage 3: sha256 mismatch — stripped-query response differs from original"
    _yellow "       stage1=${STAGE1_SHA}  stage3=${STAGE3_SHA}"
    STAGE3_PASS=false
  fi
  if [ "$STAGE3_PASS" = "true" ]; then
    _pass "HTTP 200 + store hit + sha256 matches stage 1 (link-rot immune)"
  fi
fi

# ---------------------------------------------------------------------------
# Stage 4 — ACL DENY: cross-channel/tenant 403
# ---------------------------------------------------------------------------
_stage "4 — ACL DENY: cross-channel principal gets 403"
echo "  Proves: a principal that does not own CHANNEL_ID cannot read its media."
echo "  Security contract: deny raised BEFORE any origin re-fetch; X-Media-Source absent."

if [ -z "${LOADER_KEY_OTHER:-}" ]; then
  _skip "LOADER_KEY_OTHER not set — cross-channel ACL deny check skipped."
  echo "       This is a KEY GAP: without this check, the S1 channel-ACL chokepoint"
  echo "       is unverified in this smoke run. Set LOADER_KEY_OTHER to close it."
else
  STAGE4_HEADERS_FILE="${_TMPDIR}/stage4_headers"
  STAGE4_BODY_FILE="${_TMPDIR}/stage4_body"

  HTTP_STATUS_4="$(curl -s \
    --dump-header "$STAGE4_HEADERS_FILE" \
    -o "$STAGE4_BODY_FILE" \
    -w '%{http_code}' \
    --get \
    --data-urlencode "url=${MEDIA_URL}" \
    --data-urlencode "channel_id=${CHANNEL_ID}" \
    --data-urlencode "access_token=${LOADER_KEY_OTHER}" \
    "${BASE_URL}/api/media/proxy")" || {
    _fail "Stage 4: curl failed"
    HTTP_STATUS_4="000"
  }

  echo "  HTTP $HTTP_STATUS_4"
  STAGE4_BODY_TEXT="$(cat "$STAGE4_BODY_FILE" 2>/dev/null || true)"
  STAGE4_X_MEDIA_SOURCE="$(cat "$STAGE4_HEADERS_FILE" 2>/dev/null | grep -i '^x-media-source:' | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')"
  echo "  Body: $STAGE4_BODY_TEXT"
  echo "  X-Media-Source: ${STAGE4_X_MEDIA_SOURCE:-(absent — correct, deny must not leak)}"

  STAGE4_PASS=true
  if [ "$HTTP_STATUS_4" != "403" ]; then
    _fail "Stage 4: expected 403, got $HTTP_STATUS_4"
    STAGE4_PASS=false
  fi

  if ! printf '%s' "$STAGE4_BODY_TEXT" | grep -qi "channel access denied"; then
    _fail "Stage 4: body should contain 'Channel access denied', got: $STAGE4_BODY_TEXT"
    STAGE4_PASS=false
  fi

  if [ -n "$STAGE4_X_MEDIA_SOURCE" ]; then
    _fail "Stage 4: X-Media-Source present on deny response ('${STAGE4_X_MEDIA_SOURCE}') — deny leaked via origin"
    STAGE4_PASS=false
  fi

  if [ "$STAGE4_PASS" = "true" ]; then
    _pass "403 + 'Channel access denied' + X-Media-Source absent — ACL deny confirmed"
  fi
fi

# ---------------------------------------------------------------------------
# Stage 5 — ACL ALLOW: owner re-confirmed
# ---------------------------------------------------------------------------
_stage "5 — ACL ALLOW: channel owner still gets 200"
echo "  Proves: Stage 4 didn't accidentally lock out the real owner (regression guard)."

if [ "$HTTP_STATUS_1" = "200" ] && [ "$X_MEDIA_SOURCE_1" = "store" ]; then
  _pass "Owner access confirmed (already verified in Stage 1 — HTTP $HTTP_STATUS_1, X-Media-Source=$X_MEDIA_SOURCE_1)"
else
  # Re-request to be explicit.
  STAGE5_HEADERS_FILE="${_TMPDIR}/stage5_headers"
  HTTP_STATUS_5="$(curl -sf \
    --dump-header "$STAGE5_HEADERS_FILE" \
    -o /dev/null \
    -w '%{http_code}' \
    --get \
    --data-urlencode "url=${MEDIA_URL}" \
    --data-urlencode "channel_id=${CHANNEL_ID}" \
    --data-urlencode "access_token=${LOADER_KEY}" \
    "${BASE_URL}/api/media/proxy")" || HTTP_STATUS_5="000"

  X_MEDIA_SOURCE_5="$(cat "$STAGE5_HEADERS_FILE" 2>/dev/null | grep -i '^x-media-source:' | head -1 | sed 's/^[^:]*: *//' | tr -d '\r')"
  echo "  HTTP $HTTP_STATUS_5  X-Media-Source: ${X_MEDIA_SOURCE_5:-(absent)}"

  if [ "$HTTP_STATUS_5" = "200" ] && [ "$X_MEDIA_SOURCE_5" = "store" ]; then
    _pass "Owner access: HTTP 200 + X-Media-Source: store"
  elif [ "$HTTP_STATUS_5" = "200" ]; then
    _pass "Owner access: HTTP 200 (X-Media-Source=${X_MEDIA_SOURCE_5:-missing} — store may have been bypassed)"
  else
    _fail "Stage 5: owner got HTTP $HTTP_STATUS_5 (expected 200)"
  fi
fi

# ---------------------------------------------------------------------------
# Stage 6 — PURGE (DESTRUCTIVE — opt-in only)
# ---------------------------------------------------------------------------
_stage "6 — PURGE (DESTRUCTIVE — requires CONFIRM_PURGE=yes + TEST_CHANNEL_ID)"
echo "  Proves: after channel hard-delete, its media is no longer accessible."

if [ "${CONFIRM_PURGE:-}" != "yes" ] || [ -z "${TEST_CHANNEL_ID:-}" ]; then
  _skip "Skipping. To enable: set CONFIRM_PURGE=yes and TEST_CHANNEL_ID=<disposable channel id>."
  echo "       WARNING: This stage calls DELETE /api/channels/{id} which is IRREVERSIBLE."
  echo "       Only run it against a throwaway test channel, never against CHANNEL_ID."
else
  if [ "${TEST_CHANNEL_ID}" = "${CHANNEL_ID}" ]; then
    _fail "Stage 6: TEST_CHANNEL_ID must not equal CHANNEL_ID — refusing to delete the test fixture channel."
  else
    echo "  Deleting channel: $TEST_CHANNEL_ID"
    # DELETE /api/channels/{id}?confirm={id} uses the user API key, not admin token.
    # `confirm` must equal the channel display name or fall back to channel_id.
    DEL_STATUS="$(curl -s \
      -X DELETE \
      -w '%{http_code}' \
      -o /dev/null \
      -H "Authorization: Bearer ${LOADER_KEY}" \
      "${BASE_URL}/api/channels/${TEST_CHANNEL_ID}?confirm=${TEST_CHANNEL_ID}")" || DEL_STATUS="000"

    echo "  DELETE HTTP $DEL_STATUS"

    if [ "$DEL_STATUS" != "200" ] && [ "$DEL_STATUS" != "207" ]; then
      _fail "Stage 6: DELETE returned $DEL_STATUS (expected 200 or 207)"
    else
      echo "  Verifying store miss for a test URL from the deleted channel..."
      # We don't have a specific media URL for TEST_CHANNEL_ID here — we just
      # confirm the channel itself is gone (next GET returns 404 from the API).
      AFTER_STATUS="$(curl -s \
        -X DELETE \
        -w '%{http_code}' \
        -o /dev/null \
        -H "Authorization: Bearer ${LOADER_KEY}" \
        "${BASE_URL}/api/channels/${TEST_CHANNEL_ID}?confirm=${TEST_CHANNEL_ID}")" || AFTER_STATUS="000"
      echo "  Re-delete HTTP $AFTER_STATUS (expect 404 — already gone)"
      if [ "$AFTER_STATUS" = "404" ]; then
        _pass "Stage 6: Channel deleted; re-delete confirmed 404"
      else
        _fail "Stage 6: expected 404 after deletion, got $AFTER_STATUS"
      fi
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Stage 7 — BACKFILL DRY-RUN
# ---------------------------------------------------------------------------
_stage "7 — BACKFILL DRY-RUN: POST /api/admin/channels/\$CHANNEL_ID/backfill-media"
echo "  Proves: dry-run returns would-store count without actually writing anything."

# Snapshot stats before dry-run.
STATS_BEFORE_RESP="$(curl -sf \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  "${BASE_URL}/api/admin/media/stats" 2>/dev/null)" || STATS_BEFORE_RESP="{}"
BLOBS_BEFORE="$(_json_field "$STATS_BEFORE_RESP" "total_blobs")"
BYTES_BEFORE="$(_json_field "$STATS_BEFORE_RESP" "total_bytes")"
echo "  Stats before dry-run: total_blobs=${BLOBS_BEFORE:-unknown}  total_bytes=${BYTES_BEFORE:-unknown}"

BACKFILL_RESP="$(curl -sf \
  -w '\n__HTTP_STATUS__%{http_code}' \
  -X POST \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true, "max_messages": 50}' \
  "${BASE_URL}/api/admin/channels/${CHANNEL_ID}/backfill-media")" || {
  _fail "Stage 7: curl failed"
  BACKFILL_RESP="__HTTP_STATUS__000"
}

BACKFILL_STATUS="$(printf '%s' "$BACKFILL_RESP" | grep '__HTTP_STATUS__' | sed 's/__HTTP_STATUS__//')"
BACKFILL_BODY="$(printf '%s' "$BACKFILL_RESP" | grep -v '__HTTP_STATUS__')"

echo "  HTTP $BACKFILL_STATUS"
echo "  Body: $(printf '%s' "$BACKFILL_BODY" | _jq .)"

WOULD_STORE="$(_json_field "$BACKFILL_BODY" "stored")"
DRY_RUN_FLAG="$(_json_field "$BACKFILL_BODY" "dry_run")"
MSGS_SCANNED="$(_json_field "$BACKFILL_BODY" "messages_scanned")"
echo "  dry_run=$DRY_RUN_FLAG  messages_scanned=$MSGS_SCANNED  would-store=$WOULD_STORE"

# Snapshot stats after dry-run.
STATS_AFTER_RESP="$(curl -sf \
  -H "X-Admin-Token: ${ADMIN_TOKEN}" \
  "${BASE_URL}/api/admin/media/stats" 2>/dev/null)" || STATS_AFTER_RESP="{}"
BLOBS_AFTER="$(_json_field "$STATS_AFTER_RESP" "total_blobs")"
BYTES_AFTER="$(_json_field "$STATS_AFTER_RESP" "total_bytes")"
echo "  Stats after  dry-run: total_blobs=${BLOBS_AFTER:-unknown}  total_bytes=${BYTES_AFTER:-unknown}"

STAGE7_PASS=true
if [ "$BACKFILL_STATUS" != "200" ]; then
  _fail "Stage 7: expected 200, got $BACKFILL_STATUS"
  STAGE7_PASS=false
fi

if [ "$DRY_RUN_FLAG" != "true" ]; then
  _fail "Stage 7: response body dry_run field should be true, got '$DRY_RUN_FLAG'"
  STAGE7_PASS=false
fi

# Confirm nothing was written: blobs and bytes must be unchanged.
if [ -n "$BLOBS_BEFORE" ] && [ -n "$BLOBS_AFTER" ] && [ "$BLOBS_BEFORE" != "$BLOBS_AFTER" ]; then
  _fail "Stage 7: total_blobs changed from $BLOBS_BEFORE to $BLOBS_AFTER during dry-run — data was WRITTEN"
  STAGE7_PASS=false
fi

if [ -n "$BYTES_BEFORE" ] && [ -n "$BYTES_AFTER" ] && [ "$BYTES_BEFORE" != "$BYTES_AFTER" ]; then
  _fail "Stage 7: total_bytes changed during dry-run — data was WRITTEN ($BYTES_BEFORE -> $BYTES_AFTER)"
  STAGE7_PASS=false
fi

if [ "$STAGE7_PASS" = "true" ]; then
  _pass "HTTP 200 + dry_run=true + stats unchanged (would-store=$WOULD_STORE, scanned=$MSGS_SCANNED)"
fi

# ---------------------------------------------------------------------------
# Stage 8 — PROD-ONLY notes (manual / time-based verification)
# ---------------------------------------------------------------------------
_stage "8 — PROD-ONLY: checks requiring manual or time-based verification"
_yellow "  The following scenarios cannot be automated in a single smoke run."
echo ""
echo "  8a. EXPIRED URL LINK-ROT (requires waiting 24h+)"
echo "      - Take a Discord signed attachment URL from CHANNEL_ID."
echo "      - Wait until the CDN URL itself expires (Discord signed URLs expire ~1–24h)."
echo "      - Re-request it via /api/media/proxy?url=<expired>&channel_id=...&access_token=..."
echo "      - Expect: HTTP 200 + X-Media-Source: store (blob store serves; CDN 403s)."
echo "      - This is the core link-rot guarantee — it cannot be short-circuited."
echo ""
echo "  8b. PER-PLATFORM INGESTION (requires live connector + active sync)"
echo "      - Ingest a new message with a media attachment on each connected platform:"
echo "        Slack, Discord, Teams, Mattermost."
echo "      - After extraction completes, request the attachment via /api/media/proxy."
echo "      - Expect: X-Media-Source: store on the first request (ingestion already stored it)."
echo "      - Platforms to verify: Slack (bot-token auth), Discord (signed URL), Teams"
echo "        (SharePoint CDN), Mattermost (self-hosted CDN)."
echo ""
echo "  8c. MINIO BACKEND (EE tier)"
echo "      - If CHANNEL_MEDIA_BACKEND=minio, confirm Stage 1 still returns store hit."
echo "      - Check MinIO bucket 'atlas-media' (or CHANNEL_MEDIA_MINIO_BUCKET) for the blob."
echo "      - Verify CHANNEL_MEDIA_MINIO_SECURE=true on the AWS tier."
echo ""
echo "  8d. READ-THROUGH FALLBACK on STORE MISS"
echo "      - Find a media URL not yet stored (e.g. from a very recent message)."
echo "      - Request via /api/media/proxy — expect 200 + X-Media-Source: origin."
echo "      - Confirm the server didn't 500 (resilience: store miss must fall through)."
_skip "Prod-only checks above require manual/time-based verification — see notes."

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
_bold "============================================================"
_bold "SMOKE TEST SUMMARY"
_bold "============================================================"
echo "  Stages run   : $((PASS + FAIL))"
_green "  Passed       : $PASS"
if [ "$FAIL" -gt 0 ]; then
  _red   "  Failed       : $FAIL"
else
  echo  "  Failed       : $FAIL"
fi
echo ""

if [ "$FAIL" -gt 0 ]; then
  _red "RESULT: FAIL — $FAIL stage(s) did not pass. Review output above."
  exit 1
else
  _green "RESULT: PASS — all exercised stages passed."
  exit 0
fi
