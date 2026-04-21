.PHONY: install test lint dev docker-up docker-down clean demo demo-regenerate-fixtures

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

dev:
	uv run uvicorn beever_atlas.server.app:app --reload &
	cd web && npm run dev &
	cd bot && npm run dev

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
