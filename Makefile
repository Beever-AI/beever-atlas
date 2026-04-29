.PHONY: install test lint dev stop docker-up docker-down clean demo demo-regenerate-fixtures

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

demo:
	docker compose -f docker-compose.yml -f demo/docker-compose.demo.yml up --build

demo-regenerate-fixtures:
	python demo/seed.py --live --write-fixtures

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf web/dist bot/dist .pytest_cache .ruff_cache
