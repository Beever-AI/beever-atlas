#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HERE/.state"
PUBLIC_IP="$(cat "$STATE/public_ip")"
KEY="$STATE/beever-atlas-key.pem"
exec ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  ubuntu@"$PUBLIC_IP" "$@"
