.PHONY: install test lint dev stop docker-up docker-down clean demo demo-regenerate-fixtures reembed-all reembed-resume reembed-dry-run tunnel tunnel-up tunnel-install tunnel-uninstall

install:
	uv sync --extra dev
	cd web && npm ci
	cd bot && npm ci

test:
	uv run pytest
	cd web && npm test -- --run
	cd bot && npm test

lint:
	uv run ruff check src/ tests/
	cd web && npm run lint && npm run typecheck
	cd bot && npm run lint

# Issue #46 — run all 3 dev servers in ONE shell so they share a process
# group; `trap 'kill 0' EXIT` reaps the group when Ctrl-C lands. Without
# this, each `&`-backgrounded process was orphaned in its own subshell
# and held its port (8000 / 5173 / 3001), forcing the next `make dev` to
# error with "address already in use".
dev:
	@trap 'kill 0' EXIT INT TERM; \
	uv run uvicorn beever_atlas.server.app:app --reload & \
	(cd web && npm run dev) & \
	(cd bot && npm run dev) & \
	wait

# Force-kill any orphans from a previous `make dev` that didn't tear down
# cleanly (process killed with -9, container restart, etc.). Idempotent
# (`|| true` on each pkill so missing processes don't fail the recipe).
stop:
	@pkill -f "uvicorn beever_atlas" || true
	@pkill -f "vite" || true
	@pkill -f "npm run dev" || true
	@echo "stopped any orphan dev servers (uvicorn / vite / npm)"

docker-up:
	docker compose up -d

docker-down:
	docker compose down

# Expose the bot's inbound webhooks (port 3001) over a public HTTPS tunnel so
# Slack (Events API) and Microsoft Teams can deliver events in local dev.
# Discord, Mattermost, and Slack Socket Mode do NOT need this.
#
# Set NGROK_DOMAIN to a reserved static domain (free tier includes one — see
# https://dashboard.ngrok.com/domains) so the URL survives reboots and you only
# configure Slack/Teams once. Then put the same https URL in PUBLIC_BOT_URL.
#   make tunnel NGROK_DOMAIN=your-name.ngrok-free.app
# Without a domain it falls back to an ephemeral URL (changes every restart).
tunnel:
	@command -v ngrok >/dev/null 2>&1 || { echo "ngrok not installed: brew install ngrok"; exit 1; }
	@if [ -n "$(NGROK_DOMAIN)" ]; then \
		echo "Starting ngrok on static domain https://$(NGROK_DOMAIN) → :3001"; \
		ngrok http 3001 --url=https://$(NGROK_DOMAIN); \
	else \
		echo "Starting ngrok on an EPHEMERAL url → :3001 (set NGROK_DOMAIN for a stable one)"; \
		ngrok http 3001; \
	fi

# Reboot-proof tunnel for inbound platforms (Slack Events API, Teams): start the
# tunnel, write PUBLIC_BOT_URL into .env, restart the backend, and re-point the
# Teams messaging endpoint — then hold the tunnel open. Config via .env
# (NGROK_DOMAIN, TEAMS_APP_ID) or flags. `make tunnel-up DRY_RUN=1` to preview.
tunnel-up:
	uv run python -m scripts.tunnel_up $(if $(NGROK_DOMAIN),--domain $(NGROK_DOMAIN),) $(if $(DRY_RUN),--dry-run,)

# Install/uninstall the macOS launchd agent so the tunnel comes up at login and
# survives reboots (re-syncs PUBLIC_BOT_URL + Teams endpoint each start).
tunnel-install:
	@sed -e "s#__REPO_DIR__#$(CURDIR)#g" -e "s#__UV__#$$(command -v uv)#g" \
		deploy/launchd/ai.beever.tunnel.plist.template \
		> $(HOME)/Library/LaunchAgents/ai.beever.tunnel.plist
	launchctl load -w $(HOME)/Library/LaunchAgents/ai.beever.tunnel.plist
	@echo "Installed launchd agent. Logs: /tmp/beever-tunnel.{out,err}.log"

tunnel-uninstall:
	-launchctl unload -w $(HOME)/Library/LaunchAgents/ai.beever.tunnel.plist
	-rm -f $(HOME)/Library/LaunchAgents/ai.beever.tunnel.plist
	@echo "Removed launchd tunnel agent."

# `make demo` is zero-config: on first run it bootstraps a .env from
# .env.example (demo defaults need NO API keys to seed — only /api/ask needs a
# free GOOGLE_API_KEY), then brings up the full stack + seed-loader. It never
# clobbers an existing .env.
demo:
	@if [ ! -f .env ]; then \
		cp .env.example .env && \
		echo "→ Created .env from .env.example (demo defaults — no API keys needed to seed)."; \
	fi
	docker compose -f docker-compose.yml -f demo/docker-compose.demo.yml up --build

demo-regenerate-fixtures:
	python demo/seed.py --live --write-fixtures

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf web/dist bot/dist .pytest_cache .ruff_cache

# Re-embed every Weaviate AtomicFact + Neo4j Entity.name_vector under the
# currently configured EMBEDDING_PROVIDER / EMBEDDING_MODEL. Required when
# switching providers if the dimension changes.
# See docs/runbooks/embedding-migration.md for the full operator playbook.
reembed-all:
	uv run python -m scripts.reembed_facts

reembed-resume:
	uv run python -m scripts.reembed_facts --resume

reembed-dry-run:
	uv run python -m scripts.reembed_facts --dry-run
