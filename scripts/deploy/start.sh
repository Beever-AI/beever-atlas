#!/usr/bin/env bash
# Start a previously stopped instance. EIP is retained so URL stays the same.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_REGION:-us-east-2}"
ID="$(cat "$HERE/.state/instance_id")"
aws ec2 start-instances --region "$REGION" --instance-ids "$ID" >/dev/null
aws ec2 wait instance-running --region "$REGION" --instance-ids "$ID"
IP="$(cat "$HERE/.state/public_ip")"
echo "[start] running. URL: http://$IP/"
