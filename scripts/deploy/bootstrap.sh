#!/usr/bin/env bash
# Runs ON the EC2 instance. Installs Docker + Caddy + starts compose stack.
# Caddy listens on 80/443 with auto Let's Encrypt; reverse-proxies to web (:3000).
set -euo pipefail

HOSTNAME_ARG="${1:?hostname required}"
cd /opt/beever-atlas-v2

if ! command -v docker >/dev/null 2>&1; then
  echo "[bootstrap] installing Docker"
  sudo apt-get update -y || sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker ubuntu
fi

if ! command -v caddy >/dev/null 2>&1; then
  echo "[bootstrap] installing Caddy"
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
    sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | \
    sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y caddy
fi

echo "[bootstrap] writing Caddyfile for $HOSTNAME_ARG"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$HOSTNAME_ARG {
    encode gzip
    reverse_proxy localhost:3000
}
EOF
sudo systemctl reload caddy || sudo systemctl restart caddy

echo "[bootstrap] starting compose stack"
sudo docker compose pull 2>/dev/null || true
sudo docker compose up -d --build

echo "[bootstrap] waiting for backend health (up to 5 min)..."
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8000/api/health >/dev/null 2>&1; then
    echo "[bootstrap] backend healthy"
    break
  fi
  sleep 5
done

sudo docker compose ps
