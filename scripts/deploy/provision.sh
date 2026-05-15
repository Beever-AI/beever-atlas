#!/usr/bin/env bash
# Provisions AWS resources for a single-instance Beever Atlas deploy.
# Idempotent: re-running reuses existing resources.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HERE/.state"
mkdir -p "$STATE"

REGION="${AWS_REGION:-us-east-2}"
# Set NAME to scope AWS resources (keypair, security group, EIP tag).
# Defaults to `beever-atlas`; use NAME=beever-atlas-ee for the EE side-by-side deploy.
NAME="${NAME:-beever-atlas}"
KEY_NAME="${NAME}-key"
SG_NAME="${NAME}-sg"
INSTANCE_TYPE="${INSTANCE_TYPE:-t4g.large}"
DISK_GB="${DISK_GB:-30}"

log() { echo "[provision] $*" >&2; }

KEY_FILE="$STATE/${KEY_NAME}.pem"
if [[ ! -f "$KEY_FILE" ]]; then
  log "generating SSH keypair → $KEY_FILE"
  ssh-keygen -t ed25519 -N "" -f "$KEY_FILE" -q
  chmod 600 "$KEY_FILE"
fi

if ! aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEY_NAME" >/dev/null 2>&1; then
  log "importing keypair to AWS as $KEY_NAME"
  aws ec2 import-key-pair --region "$REGION" \
    --key-name "$KEY_NAME" \
    --public-key-material "fileb://${KEY_FILE}.pub" >/dev/null
fi

VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" \
  --filters Name=is-default,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
SUBNET_ID=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=default-for-az,Values=true" \
  --query 'Subnets[0].SubnetId' --output text)
log "using VPC=$VPC_ID subnet=$SUBNET_ID"

MY_IP="$(curl -s https://checkip.amazonaws.com | tr -d '[:space:]')/32"
log "your public IP: $MY_IP"

SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")

if [[ "$SG_ID" == "None" || -z "$SG_ID" ]]; then
  log "creating security group $SG_NAME"
  SG_ID=$(aws ec2 create-security-group --region "$REGION" \
    --group-name "$SG_NAME" --description "Beever Atlas internal testing" \
    --vpc-id "$VPC_ID" --query 'GroupId' --output text)
fi

# SSH restricted to your IP; HTTP/HTTPS open to world (HTTPS via Caddy/Let's Encrypt)
aws ec2 revoke-security-group-ingress --region "$REGION" \
  --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$MY_IP" 2>/dev/null || true
aws ec2 authorize-security-group-ingress --region "$REGION" \
  --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$MY_IP" >/dev/null 2>&1 || true
for port in 80 443; do
  aws ec2 authorize-security-group-ingress --region "$REGION" \
    --group-id "$SG_ID" --protocol tcp --port "$port" --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
done
# Open SSH to world too — GitHub Actions runners need it for push-to-deploy.
# Still gated by ed25519 key auth (no passwords).
aws ec2 authorize-security-group-ingress --region "$REGION" \
  --group-id "$SG_ID" --protocol tcp --port 22 --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
log "SG $SG_ID: ssh from world (key-only), 80/443 from world"

AMI_ID=$(aws ec2 describe-images --region "$REGION" \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*" \
            "Name=state,Values=available" \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text)
log "AMI=$AMI_ID"

INSTANCE_ID=""
if [[ -f "$STATE/instance_id" ]]; then
  INSTANCE_ID="$(cat "$STATE/instance_id")"
  STATE_NAME=$(aws ec2 describe-instances --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo "missing")
  if [[ "$STATE_NAME" != "running" && "$STATE_NAME" != "pending" ]]; then
    INSTANCE_ID=""
  fi
fi

if [[ -z "$INSTANCE_ID" ]]; then
  log "launching EC2 instance ($INSTANCE_TYPE, ${DISK_GB}GB)"
  INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
    --image-id "$AMI_ID" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --security-group-ids "$SG_ID" \
    --subnet-id "$SUBNET_ID" \
    --associate-public-ip-address \
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$DISK_GB,VolumeType=gp3}" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
    --query 'Instances[0].InstanceId' --output text)
  echo "$INSTANCE_ID" > "$STATE/instance_id"
  log "instance $INSTANCE_ID launched; waiting for running state"
  aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"
fi

EIP_ALLOC=""
if [[ -f "$STATE/eip_alloc" ]]; then
  EIP_ALLOC="$(cat "$STATE/eip_alloc")"
  aws ec2 describe-addresses --region "$REGION" --allocation-ids "$EIP_ALLOC" >/dev/null 2>&1 || EIP_ALLOC=""
fi
if [[ -z "$EIP_ALLOC" ]]; then
  log "allocating Elastic IP"
  EIP_ALLOC=$(aws ec2 allocate-address --region "$REGION" --domain vpc \
    --query 'AllocationId' --output text)
  echo "$EIP_ALLOC" > "$STATE/eip_alloc"
fi
aws ec2 associate-address --region "$REGION" \
  --instance-id "$INSTANCE_ID" --allocation-id "$EIP_ALLOC" >/dev/null

PUBLIC_IP=$(aws ec2 describe-addresses --region "$REGION" \
  --allocation-ids "$EIP_ALLOC" --query 'Addresses[0].PublicIp' --output text)

# Hostname for HTTPS via Let's Encrypt — nip.io resolves <dashed-ip>.nip.io → <ip>
HOSTNAME="${PUBLIC_IP//./-}.nip.io"

echo "$PUBLIC_IP" > "$STATE/public_ip"
echo "$SG_ID"    > "$STATE/sg_id"
echo "$HOSTNAME" > "$STATE/hostname"

log "public IP: $PUBLIC_IP"
log "hostname:  $HOSTNAME"
log "provision complete."
