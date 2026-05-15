#!/usr/bin/env bash
# Stop the EC2 instance (keeps disk + EIP; pay only storage ~$5/mo).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_REGION:-us-east-2}"
ID="$(cat "$HERE/.state/instance_id")"
aws ec2 stop-instances --region "$REGION" --instance-ids "$ID" >/dev/null
echo "[stop] stopping $ID — billing paused for compute"
aws ec2 wait instance-stopped --region "$REGION" --instance-ids "$ID"
echo "[stop] stopped."
