#!/usr/bin/env bash
# End-to-end deploy: provision AWS → rsync code → generate .env → bootstrap (Docker + Caddy + HTTPS).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STATE="$HERE/.state"

log() { echo -e "\033[1;34m[deploy]\033[0m $*"; }

log "1/5  provisioning AWS infra"
bash "$HERE/provision.sh"

PUBLIC_IP="$(cat "$STATE/public_ip")"
HOSTNAME="$(cat "$STATE/hostname")"
NAME="${NAME:-beever-atlas}"
KEY_FILE="$STATE/${NAME}-key.pem"
SSH_OPTS=(-i "$KEY_FILE" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10)

log "2/5  waiting for SSH on $PUBLIC_IP"
for i in $(seq 1 40); do
  if ssh "${SSH_OPTS[@]}" ubuntu@"$PUBLIC_IP" 'echo ok' >/dev/null 2>&1; then
    log "      SSH ready"; break
  fi
  sleep 5
  [[ $i -eq 40 ]] && { echo "SSH never came up" >&2; exit 1; }
done

log "3/5  generating .env from .env.example (host=$HOSTNAME)"
gen_secret() { openssl rand -hex 32; }
gen_password() { openssl rand -base64 24 | tr -d '=+/' | head -c 24; }

for f in master_key api_key admin_token weaviate_key bridge_key; do
  [[ -f "$STATE/$f" ]] || gen_secret > "$STATE/$f"
done
[[ -f "$STATE/neo4j_password" ]]  || gen_password > "$STATE/neo4j_password"
[[ -f "$STATE/nebula_password" ]] || gen_password > "$STATE/nebula_password"

ENV_OUT="$STATE/.env.generated"
cp "$REPO/.env.example" "$ENV_OUT"

patch_env() {
  python3 - "$ENV_OUT" "$1" "$2" <<'PY'
import re, sys
p, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(p).read()
new, n = re.subn(rf'^{re.escape(k)}=.*$', f'{k}={v}', s, count=1, flags=re.M)
if n == 0:
    new = s.rstrip() + f'\n{k}={v}\n'
open(p, 'w').write(new)
PY
}

NEO4J_PW="$(cat "$STATE/neo4j_password")"
patch_env BEEVER_ENV            "production"
patch_env BEEVER_API_URL        "https://$HOSTNAME"
patch_env CORS_ORIGINS          "https://$HOSTNAME"
patch_env VITE_API_URL          "https://$HOSTNAME"
patch_env BEEVER_API_KEYS       "$(cat "$STATE/api_key")"
patch_env VITE_BEEVER_API_KEY   "$(cat "$STATE/api_key")"
patch_env BEEVER_ADMIN_TOKEN    "$(cat "$STATE/admin_token")"
patch_env WEAVIATE_API_KEY      "$(cat "$STATE/weaviate_key")"
patch_env NEO4J_AUTH            "neo4j/$NEO4J_PW"
patch_env NEO4J_PASSWORD        "$NEO4J_PW"
patch_env NEBULA_PASSWORD       "$(cat "$STATE/nebula_password")"
patch_env BRIDGE_API_KEY        "$(cat "$STATE/bridge_key")"
patch_env CREDENTIAL_MASTER_KEY "$(cat "$STATE/master_key")"
patch_env ADAPTER_MOCK          "false"

log "4/5  syncing code to instance"
ssh "${SSH_OPTS[@]}" ubuntu@"$PUBLIC_IP" \
  'sudo mkdir -p /opt/beever-atlas-v2 && sudo chown -R ubuntu:ubuntu /opt/beever-atlas-v2'

rsync -az --delete \
  --exclude='.git' --exclude='node_modules' --exclude='web/node_modules' \
  --exclude='web/dist' --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='scripts/deploy/.state' --exclude='.omc' --exclude='memory' \
  -e "ssh ${SSH_OPTS[*]}" \
  "$REPO/" ubuntu@"$PUBLIC_IP":/opt/beever-atlas-v2/

scp "${SSH_OPTS[@]}" "$ENV_OUT" ubuntu@"$PUBLIC_IP":/opt/beever-atlas-v2/.env

log "5/5  running bootstrap on instance"
ssh "${SSH_OPTS[@]}" ubuntu@"$PUBLIC_IP" \
  "cd /opt/beever-atlas-v2 && bash scripts/deploy/bootstrap.sh '$HOSTNAME'"

cat <<EOF

\033[1;32m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m
\033[1;32m  DEPLOY COMPLETE\033[0m
\033[1;32m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m

  URL:          https://$HOSTNAME
  IP (raw):     $PUBLIC_IP
  API key:      $(cat "$STATE/api_key")
  Admin token:  $(cat "$STATE/admin_token")

  SSH:          ./scripts/deploy/ssh.sh
  Stop billing: ./scripts/deploy/stop.sh
  Destroy:      ./scripts/deploy/destroy.sh

  \033[1;33mAUTO-DEPLOY ON GIT PUSH:\033[0m
    1. Add these GitHub repo secrets (Settings → Secrets → Actions):
         EC2_HOST     = $PUBLIC_IP
         EC2_SSH_KEY  = (contents of $KEY_FILE)
    2. Push to main → .github/workflows/deploy.yml updates the server.

  \033[1;33mLLM keys still placeholders:\033[0m
    ssh in, edit /opt/beever-atlas-v2/.env (GOOGLE_API_KEY, JINA_API_KEY),
    then sudo docker compose up -d --build

EOF
