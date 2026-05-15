#!/usr/bin/env bash
# Tears down all AWS resources created by provision.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HERE/.state"
REGION="${AWS_REGION:-us-east-2}"
NAME="${NAME:-beever-atlas}"

log() { echo "[destroy] $*"; }

read -r -p "Destroy AWS resources for NAME=$NAME? [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "aborted"; exit 0; }

if [[ -f "$STATE/instance_id" ]]; then
  INSTANCE_ID="$(cat "$STATE/instance_id")"
  log "terminating instance $INSTANCE_ID"
  aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE_ID" >/dev/null || true
  aws ec2 wait instance-terminated --region "$REGION" --instance-ids "$INSTANCE_ID" || true
  rm -f "$STATE/instance_id"
fi

if [[ -f "$STATE/eip_alloc" ]]; then
  EIP_ALLOC="$(cat "$STATE/eip_alloc")"
  log "releasing EIP $EIP_ALLOC"
  aws ec2 release-address --region "$REGION" --allocation-id "$EIP_ALLOC" || true
  rm -f "$STATE/eip_alloc"
fi

if [[ -f "$STATE/sg_id" ]]; then
  SG_ID="$(cat "$STATE/sg_id")"
  log "deleting SG $SG_ID"
  aws ec2 delete-security-group --region "$REGION" --group-id "$SG_ID" || true
  rm -f "$STATE/sg_id"
fi

log "deleting keypair"
aws ec2 delete-key-pair --region "$REGION" --key-name "${NAME}-key" || true

rm -f "$STATE/public_ip" "$STATE/.env.generated"
log "done. (Kept SSH key + secrets in $STATE for reuse; delete manually if you want)"
