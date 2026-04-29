#!/usr/bin/env bash
#
# End-to-end tests for ./atlas
#
# Exercises every spec scenario in specs/oss-setup-installer/spec.md
# without touching real Docker or the real .env. Each test runs in its
# own throwaway workspace with a tightly-controlled PATH composed of:
#
#   - ${WS}/stubs/       docker, python3 fakes (per-test)
#   - ${TMP_ROOT}/minbin symlinks to the system utilities the script
#                        needs (grep, sed, cut, cp, …), carefully NOT
#                        including docker or python so their presence
#                        can be controlled by the stubs dir alone.
#
# Run:   bash tests/test_setup_script.sh
# Exit:  0 on success, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETUP_SH="${REPO_ROOT}/atlas"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"

PASS=0
FAIL=0
FAILED_TESTS=()

TMP_ROOT="$(mktemp -d -t beever-setup-test-XXXXXX)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# ---------------------------------------------------------------------
# Deterministic system-bin dir: symlinks for every utility the script
# might invoke, EXCEPT docker/docker-compose/python — those are stubbed
# per-test so we can simulate missing-tool scenarios.
# ---------------------------------------------------------------------
MINBIN="${TMP_ROOT}/minbin"
mkdir -p "$MINBIN"
for tool in grep sed cut head tail cat cp rm mv chmod ls mkdir rmdir printf echo test tr wc diff sh bash dash awk sort uniq find xargs env dirname basename tee od od which sleep; do
  src=$(command -v "$tool" 2>/dev/null || true)
  if [ -n "$src" ]; then
    ln -sf "$src" "$MINBIN/$tool"
  fi
done

# ---------------------------------------------------------------------
# Test harness helpers
# ---------------------------------------------------------------------

# Create a throwaway workspace with atlas + .env.example and a
# per-test stubs/ dir.
#
# Modes:
#   ok          all stubs pass; docker/python3 available
#   no-docker   omit docker stub
#   no-python   omit python3 stub
#   fail-up     docker compose up exits 1
mk_workspace() {
  local dir="$1"
  local mode="${2:-ok}"
  mkdir -p "$dir/stubs"
  cp "$SETUP_SH" "$dir/atlas"
  chmod +x "$dir/atlas"
  cp "$ENV_EXAMPLE" "$dir/.env.example"

  local log="$dir/stubs/.calls"
  : > "$log"

  if [ "$mode" != "no-docker" ]; then
    # docker stub — handles:
    #   docker compose version    (exit 0, to detect the plugin)
    #   docker compose up -d      (exit 0 by default; exit 1 if fail-up)
    cat > "$dir/stubs/docker" <<EOF
#!/usr/bin/env bash
echo "docker \$*" >> "${log}"
if [ "\$1" = "compose" ]; then
  if [ "\$2" = "version" ]; then exit 0; fi
  if [ "\$2" = "up" ]; then
    if [ "${mode}" = "fail-up" ]; then
      echo "Cannot connect to the Docker daemon (stub)" >&2
      exit 1
    fi
    exit 0
  fi
fi
exit 0
EOF
    chmod +x "$dir/stubs/docker"
  fi

  if [ "$mode" != "no-python" ]; then
    # python3 stub — returns a deterministic 64-hex key for assertions.
    # The fake key MUST NOT contain the substring "deadbeef", which is
    # what the regeneration assertion looks for as a negative signal.
    cat > "$dir/stubs/python3" <<EOF
#!/usr/bin/env bash
echo "python3 \$*" >> "${log}"
if [ "\$1" = "-c" ] && [[ "\$2" == *"token_hex"* ]]; then
  echo "aaaa111122223333444455556666777788889999aaaabbbbccccddddeeeeffff"
  exit 0
fi
exit 0
EOF
    chmod +x "$dir/stubs/python3"
  fi
}

run_in_workspace() {
  local dir="$1"
  shift
  (
    cd "$dir"
    # ATLAS_HEALTH_POLL_TIMEOUT=0 skips the health poll so tests don't wait 15s.
    # Individual tests that want to exercise the poll set their own value.
    ATLAS_HEALTH_POLL_TIMEOUT=0 PATH="${dir}/stubs:${MINBIN}" bash ./atlas "$@"
  )
}

assert() {
  local label="$1"
  local condition="$2"
  if eval "$condition"; then
    PASS=$((PASS + 1))
    printf "  ✓ %s\n" "$label"
  else
    FAIL=$((FAIL + 1))
    FAILED_TESTS+=("$label")
    printf "  ✗ %s\n" "$label"
    printf "    failed condition: %s\n" "$condition"
  fi
}

# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

# ────────────────────────────────────────────────────────────────────
# Test 1: fresh workspace, --non-interactive — happy path
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 1: fresh workspace, --non-interactive"
WS="$TMP_ROOT/t1"
mk_workspace "$WS"
run_in_workspace "$WS" --non-interactive > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert ".env created from .env.example"          "[ -f '$WS/.env' ]"
assert "master key regenerated (not deadbeef)"   "! grep -qF 'deadbeef' '$WS/.env'"
assert "master key regenerated to stub value"    "grep -qF 'aaaa111122223333444455556666777788889999aaaabbbbccccddddeeeeffff' '$WS/.env'"
assert "WEAVIATE_API_KEY populated (not blank)"  "! grep -qE '^WEAVIATE_API_KEY=\$' '$WS/.env'"
assert "docker compose up -d was invoked"        "grep -qF 'compose up -d' '$WS/stubs/.calls'"
assert "compose invoked with --build"            "grep -qF 'compose up -d --build' '$WS/stubs/.calls'"
assert "compose invoked with --force-recreate"   "grep -qF -- '--force-recreate' '$WS/stubs/.calls'"
assert "compose invoked with --remove-orphans"   "grep -qF -- '--remove-orphans' '$WS/stubs/.calls'"
assert "env file permissions restricted to 600"  "[ \"\$(stat -f '%A' '$WS/.env' 2>/dev/null || stat -c '%a' '$WS/.env')\" = '600' ]"
assert "no .env.bak leftover"                    "[ ! -f '$WS/.env.bak' ]"
assert "exited with status 0"                    "[ $status -eq 0 ]"
assert "stdout mentions web UI URL"              "grep -qF 'http://localhost:3000' '$WS/stdout'"

# ────────────────────────────────────────────────────────────────────
# Test 2: second run — idempotent, master key NOT re-rolled
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 2: second run on an existing .env"
KEY1=$(grep -E '^CREDENTIAL_MASTER_KEY=' "$WS/.env" | cut -d'=' -f2-)
: > "$WS/stubs/.calls"
run_in_workspace "$WS" --non-interactive > "$WS/stdout2" 2> "$WS/stderr2"
status=$?
KEY2=$(grep -E '^CREDENTIAL_MASTER_KEY=' "$WS/.env" | cut -d'=' -f2-)
assert "stdout says .env already present"        "grep -qF '.env already present' '$WS/stdout2'"
assert "master key is unchanged on re-run"       "[ '$KEY1' = '$KEY2' ]"
assert "docker compose up still invoked"         "grep -qF 'compose up -d' '$WS/stubs/.calls'"
assert "exited with status 0"                    "[ $status -eq 0 ]"

# ────────────────────────────────────────────────────────────────────
# Test 3: interactive mode — values piped as stdin
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 3: interactive mode with piped key values"
WS="$TMP_ROOT/t3"
mk_workspace "$WS"
printf 'my-fake-google-key-123\nmy-fake-jina-key-456\n' | (
  cd "$WS"
  PATH="${WS}/stubs:${MINBIN}" bash ./atlas
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "GOOGLE_API_KEY was written to .env"      "grep -qE '^GOOGLE_API_KEY=my-fake-google-key-123$' '$WS/.env'"
assert "JINA_API_KEY was written to .env"        "grep -qE '^JINA_API_KEY=my-fake-jina-key-456$' '$WS/.env'"
assert "exited with status 0"                    "[ $status -eq 0 ]"
assert "no .env.bak leftover"                    "[ ! -f '$WS/.env.bak' ]"

# ────────────────────────────────────────────────────────────────────
# Test 4: interactive mode — empty input means skip
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 4: interactive mode, both prompts skipped"
WS="$TMP_ROOT/t4"
mk_workspace "$WS"
printf '\n\n' | (
  cd "$WS"
  PATH="${WS}/stubs:${MINBIN}" bash ./atlas
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "GOOGLE_API_KEY remains empty (as in example)" "grep -qE '^GOOGLE_API_KEY=$' '$WS/.env'"
assert "JINA_API_KEY remains empty"                   "grep -qE '^JINA_API_KEY=$' '$WS/.env'"
assert "stdout reports 'skipped' at least twice"      "[ \$(grep -c 'skipped' '$WS/stdout') -ge 2 ]"
assert "exited with status 0"                         "[ $status -eq 0 ]"

# ────────────────────────────────────────────────────────────────────
# Test 5: missing docker — clean error, no side-effects
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 5: docker missing"
WS="$TMP_ROOT/t5"
mk_workspace "$WS" no-docker
run_in_workspace "$WS" --non-interactive > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited non-zero"                          "[ $status -ne 0 ]"
assert "stderr names the problem"                 "grep -qF 'docker is not installed' '$WS/stderr'"
assert "stderr includes install link"             "grep -qF 'docs.docker.com/get-docker' '$WS/stderr'"
assert ".env NOT created (fail-fast before work)" "[ ! -f '$WS/.env' ]"

# ────────────────────────────────────────────────────────────────────
# Test 6: missing python — falls back, still finishes successfully
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 6: python missing, docker present"
WS="$TMP_ROOT/t6"
mk_workspace "$WS" no-python
run_in_workspace "$WS" --non-interactive > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited with status 0"                            "[ $status -eq 0 ]"
assert "stdout warns about missing python"               "grep -qF 'Python not found' '$WS/stdout'"
assert "master key NOT regenerated (still placeholder)"  "grep -qF 'CREDENTIAL_MASTER_KEY=00000000000000000000000000000000000000000000000000000000deadbeef' '$WS/.env'"

# ────────────────────────────────────────────────────────────────────
# Test 7: unknown flag — usage error
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 7: unknown flag"
WS="$TMP_ROOT/t7"
mk_workspace "$WS"
run_in_workspace "$WS" --badflag > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited non-zero"                          "[ $status -ne 0 ]"
assert "stderr names the bad flag"                "grep -qF 'Unknown flag: --badflag' '$WS/stderr'"
assert ".env NOT created"                         "[ ! -f '$WS/.env' ]"

# ────────────────────────────────────────────────────────────────────
# Test 8: docker compose up fails — .env edits still persist
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 8: compose launch failure"
WS="$TMP_ROOT/t8"
mk_workspace "$WS" fail-up
run_in_workspace "$WS" --non-interactive > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited non-zero (compose failure)"        "[ $status -ne 0 ]"
assert ".env still created"                       "[ -f '$WS/.env' ]"
assert "master key was still regenerated"         "! grep -qF 'deadbeef' '$WS/.env'"
assert "stderr has recovery hint"                 "grep -qF 'Fix the error above' '$WS/stderr'"

# ────────────────────────────────────────────────────────────────────
# Test 10: interactive — full "configure everything" path
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 10: interactive, configure all optional integrations + rotate"
WS="$TMP_ROOT/t10"
mk_workspace "$WS"
# Piped input sequence (one line per prompt):
#   1. GOOGLE_API_KEY        (skip)
#   2. JINA_API_KEY          (skip)
#   3. Configure integrations? y
#   4. TAVILY_API_KEY        my-tavily-key
#   5. Ollama?               y
#   6. MCP server?           y
#   7. Graph backend         [1 = default neo4j]
#   8. Rotate auth tokens?   y
printf '\n\ny\nmy-tavily-key\ny\ny\n\ny\n' | (
  cd "$WS"
  PATH="${WS}/stubs:${MINBIN}" bash ./atlas
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited with status 0"                     "[ $status -eq 0 ]"
assert "TAVILY_API_KEY was written"               "grep -qE '^TAVILY_API_KEY=my-tavily-key$' '$WS/.env'"
assert "OLLAMA_ENABLED=true"                      "grep -qE '^OLLAMA_ENABLED=true$' '$WS/.env'"
assert "BEEVER_MCP_ENABLED=true"                  "grep -qE '^BEEVER_MCP_ENABLED=true$' '$WS/.env'"
assert "BEEVER_MCP_API_KEYS auto-generated"       "grep -qE '^BEEVER_MCP_API_KEYS=mcp-' '$WS/.env'"
assert "GRAPH_BACKEND=neo4j (default choice)"     "grep -qE '^GRAPH_BACKEND=neo4j$' '$WS/.env'"
assert "BEEVER_API_KEYS rotated (not dev)"        "! grep -qE '^BEEVER_API_KEYS=dev-key-change-me$' '$WS/.env'"
assert "VITE_BEEVER_API_KEY mirrors rotation"     "[ \"\$(grep -E '^VITE_BEEVER_API_KEY=' '$WS/.env' | cut -d= -f2-)\" = \"\$(grep -E '^BEEVER_API_KEYS=' '$WS/.env' | cut -d= -f2-)\" ]"

# ────────────────────────────────────────────────────────────────────
# Test 11: interactive — graph backend = none
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 11: interactive, graph backend = none"
WS="$TMP_ROOT/t11"
mk_workspace "$WS"
# Inputs: skip both keys, skip integrations, pick 2 for graph, skip rotation
printf '\n\nn\n2\nn\n' | (
  cd "$WS"
  PATH="${WS}/stubs:${MINBIN}" bash ./atlas
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited with status 0"                     "[ $status -eq 0 ]"
assert "GRAPH_BACKEND=none"                       "grep -qE '^GRAPH_BACKEND=none$' '$WS/.env'"
assert "BEEVER_API_KEYS kept at dev default"      "grep -qE '^BEEVER_API_KEYS=dev-key-change-me$' '$WS/.env'"

# ────────────────────────────────────────────────────────────────────
# Test 12: interactive — rotate auth tokens
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 12: interactive, rotate auth tokens"
WS="$TMP_ROOT/t12"
mk_workspace "$WS"
# Inputs: skip both keys, skip integrations, default graph, rotate=y
printf '\n\nn\n\ny\n' | (
  cd "$WS"
  PATH="${WS}/stubs:${MINBIN}" bash ./atlas
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited with status 0"                     "[ $status -eq 0 ]"
assert "BEEVER_API_KEYS rotated away from dev"    "! grep -qE '^BEEVER_API_KEYS=dev-key-change-me$' '$WS/.env'"
assert "BEEVER_ADMIN_TOKEN rotated"               "! grep -qE '^BEEVER_ADMIN_TOKEN=dev-admin-change-me$' '$WS/.env'"
assert "BRIDGE_API_KEY now non-empty"             "! grep -qE '^BRIDGE_API_KEY=\$' '$WS/.env'"
assert "VITE_BEEVER_API_KEY mirrors API keys"     "[ \"\$(grep -E '^VITE_BEEVER_API_KEY=' '$WS/.env' | cut -d= -f2-)\" = \"\$(grep -E '^BEEVER_API_KEYS=' '$WS/.env' | cut -d= -f2-)\" ]"
assert "VITE_BEEVER_ADMIN_TOKEN mirrors admin"    "[ \"\$(grep -E '^VITE_BEEVER_ADMIN_TOKEN=' '$WS/.env' | cut -d= -f2-)\" = \"\$(grep -E '^BEEVER_ADMIN_TOKEN=' '$WS/.env' | cut -d= -f2-)\" ]"

# ────────────────────────────────────────────────────────────────────
# Test 9: invoked from a different CWD — script self-locates
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 9: invoked from outside the workspace"
WS="$TMP_ROOT/t9"
mk_workspace "$WS"
(
  cd "$TMP_ROOT"
  PATH="${WS}/stubs:${MINBIN}" bash "${WS}/atlas" --non-interactive
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert ".env created in the workspace, not CWD"   "[ -f '$WS/.env' ] && [ ! -f '$TMP_ROOT/.env' ]"
assert "exited with status 0"                     "[ $status -eq 0 ]"

# ────────────────────────────────────────────────────────────────────
# Test 13: --non-interactive with shell env pre-seed
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 13: --non-interactive with shell env pre-seed"
WS="$TMP_ROOT/t13"
mk_workspace "$WS"
(
  cd "$WS"
  ATLAS_HEALTH_POLL_TIMEOUT=0 GOOGLE_API_KEY=env-google JINA_API_KEY=env-jina \
    PATH="${WS}/stubs:${MINBIN}" bash ./atlas --non-interactive
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited with status 0"                          "[ $status -eq 0 ]"
assert "GOOGLE_API_KEY pre-seeded in .env"             "grep -qF 'GOOGLE_API_KEY=env-google' '$WS/.env'"
assert "JINA_API_KEY pre-seeded in .env"               "grep -qF 'JINA_API_KEY=env-jina' '$WS/.env'"
assert "stdout reports saved for GOOGLE_API_KEY"       "grep -q 'GOOGLE_API_KEY.*saved' '$WS/stdout'"
assert "stdout reports saved for JINA_API_KEY"         "grep -q 'JINA_API_KEY.*saved' '$WS/stdout'"

# ────────────────────────────────────────────────────────────────────
# Test 14: --non-interactive without env — keys stay blank
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 14: --non-interactive without env vars — keys stay blank"
WS="$TMP_ROOT/t14"
mk_workspace "$WS"
(
  cd "$WS"
  # Ensure neither key is in the environment
  env -u GOOGLE_API_KEY -u JINA_API_KEY -u TAVILY_API_KEY \
    ATLAS_HEALTH_POLL_TIMEOUT=0 PATH="${WS}/stubs:${MINBIN}" bash ./atlas --non-interactive
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited with status 0"                          "[ $status -eq 0 ]"
assert "GOOGLE_API_KEY remains blank in .env"          "grep -qE '^GOOGLE_API_KEY=\$' '$WS/.env'"
assert "JINA_API_KEY remains blank in .env"            "grep -qE '^JINA_API_KEY=\$' '$WS/.env'"

# ────────────────────────────────────────────────────────────────────
# Test 15: health poll times out — warn and exit 0
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 15: health poll times out — warn and exit 0"
WS="$TMP_ROOT/t15"
mk_workspace "$WS"
# Add a curl stub that always fails (simulates backend not yet up)
cat > "$WS/stubs/curl" <<'CURLEOF'
#!/usr/bin/env bash
exit 1
CURLEOF
chmod +x "$WS/stubs/curl"
(
  cd "$WS"
  # Also stub 'sleep' so the poll runs fast without actually sleeping
  ATLAS_HEALTH_POLL_TIMEOUT=2 PATH="${WS}/stubs:${MINBIN}" bash ./atlas --non-interactive
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "exited with status 0 despite slow backend"     "[ $status -eq 0 ]"
assert "health poll warning emitted"                   "grep -qF 'backend not yet responding' '$WS/stdout'"

# ────────────────────────────────────────────────────────────────────
# Test 16: existing-key prompt masks all but the last 4 chars (#52)
# ────────────────────────────────────────────────────────────────────
echo ""
echo "Test 16: prompt for existing key masks prefix"
WS="$TMP_ROOT/t16"
mk_workspace "$WS"
# Seed .env: a long Google key (full mask) + a short Jina key (length-only).
# Both contain unmistakable substrings the test asserts must NOT leak.
sed -i.bak 's/^GOOGLE_API_KEY=.*/GOOGLE_API_KEY=AIzaSyB-fake-prefix-LONG-SUFFIX-WXYZ/' "$WS/.env.example"
sed -i.bak 's/^JINA_API_KEY=.*/JINA_API_KEY=jina_S3CR/' "$WS/.env.example"
rm -f "$WS/.env.example.bak"
# Use --non-interactive once to materialise .env (preserves seeded values),
# then drive interactive mode pressing Enter through every prompt.
run_in_workspace "$WS" --non-interactive > /dev/null 2> /dev/null
: > "$WS/stubs/.calls"
printf '\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n' | (
  cd "$WS"
  ATLAS_HEALTH_POLL_TIMEOUT=0 PATH="${WS}/stubs:${MINBIN}" bash ./atlas
) > "$WS/stdout" 2> "$WS/stderr"
status=$?
assert "long key shows last 4 chars (…WXYZ)"          "grep -qF '…WXYZ' '$WS/stdout'"
assert "long key prefix NOT leaked"                   "! grep -qF 'AIzaSyB-' '$WS/stdout'"
assert "long key middle NOT leaked"                   "! grep -qF 'fake-prefix' '$WS/stdout'"
assert "short key shows length-only marker"           "grep -qF '9-char value set' '$WS/stdout'"
assert "short key value NOT leaked"                   "! grep -qF 'jina_S3CR' '$WS/stdout'"
assert "GOOGLE_API_KEY unchanged on Enter"            "grep -qF 'GOOGLE_API_KEY=AIzaSyB-fake-prefix-LONG-SUFFIX-WXYZ' '$WS/.env'"
assert "JINA_API_KEY unchanged on Enter"              "grep -qF 'JINA_API_KEY=jina_S3CR' '$WS/.env'"
assert "exited with status 0"                         "[ $status -eq 0 ]"

# ────────────────────────────────────────────────────────────────────
# Results
# ────────────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────"
echo "Passed: ${PASS}"
echo "Failed: ${FAIL}"
if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo "Failed assertions:"
  for t in "${FAILED_TESTS[@]}"; do
    echo "  - $t"
  done
  exit 1
fi
echo "All assertions passed."
exit 0
