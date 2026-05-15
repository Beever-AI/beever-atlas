# Beever Atlas — AWS Deploy Automation

Single-instance deploy of the full stack (backend, web, bot, Weaviate, Neo4j, MongoDB, Redis) to a single EC2 host via `docker compose`. Intended for internal testing.

## Prerequisites
- AWS CLI configured (`aws sts get-caller-identity` works)
- `rsync`, `ssh`, `jq` installed locally
- Region: `us-east-2`

## Usage

```bash
# One-shot deploy (provisions AWS infra + uploads code + starts services)
./scripts/deploy/deploy.sh

# Re-deploy after code changes (reuses existing instance)
./scripts/deploy/deploy.sh

# SSH into the box
./scripts/deploy/ssh.sh

# Tear everything down
./scripts/deploy/destroy.sh
```

## Files

- `deploy.sh` — end-to-end entrypoint
- `provision.sh` — creates AWS infra (keypair, SG, EC2, EIP)
- `bootstrap.sh` — runs on the instance; installs Docker + starts compose
- `ssh.sh` — convenience SSH wrapper
- `destroy.sh` — deletes all created AWS resources
- `env.template` — .env generator template
- `.state/` — gitignored; stores resource IDs, SSH key, generated secrets

## After first deploy

The app boots with **placeholder** API keys. To make it actually work:

```bash
./scripts/deploy/ssh.sh
cd /opt/beever-atlas-v2
sudo nano .env            # fill GOOGLE_API_KEY, JINA_API_KEY, TAVILY_API_KEY
sudo docker compose up -d --build
```

## Access

After deploy completes, URLs are printed:
- Web UI: `http://<EIP>/`
- API:    `http://<EIP>:8000/api/health`

Ports 22, 80, 8000 are restricted to **your current public IP** only. Re-run `./scripts/deploy/update-ip.sh` if your IP changes.
