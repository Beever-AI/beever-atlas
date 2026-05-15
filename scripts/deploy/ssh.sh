#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HERE/.state"
NAME="${NAME:-beever-atlas}"
PUBLIC_IP="$(cat "$STATE/public_ip")"
KEY="$STATE/${NAME}-key.pem"
exec ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  ubuntu@"$PUBLIC_IP" "$@"
