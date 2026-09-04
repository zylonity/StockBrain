# Development helpers. Production runs through compose/TrueNAS, not this file.

BACKEND := backend
FRONTEND := frontend
PY := $(BACKEND)/.venv/bin

# Credentials are never hardcoded here. POSTGRES_PASSWORD is read from the
# git-ignored .env, so this file stays safe to commit and always matches the
# password the compose stack actually started with.
-include .env
export POSTGRES_PASSWORD

TEST_DATABASE_URL ?= postgresql+asyncpg://stockbrain:$(POSTGRES_PASSWORD)@127.0.0.1:5432/stockbrain_test

.PHONY: help setup up down logs ps migrate revision fmt lint typecheck test guard-db audit check verify frontend-build clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create the backend venv and install frontend dependencies
	cd $(BACKEND) && uv venv --python 3.12 && uv pip install -e ".[dev]"
	cd $(FRONTEND) && npm install

up: ## Start the stack (with the development overlay)
	docker compose -f compose.yaml -f compose.dev.yaml up -d --build

down: ## Stop the stack, keeping volumes
	docker compose down

logs: ## Follow application logs
	docker compose logs -f stockbrain

ps: ## Show container status
	docker compose ps

migrate: ## Apply migrations to the running database
	docker compose exec stockbrain alembic upgrade head

revision: guard-db ## Autogenerate a migration: make revision m="add widgets"
	cd $(BACKEND) && DATABASE_URL=$(TEST_DATABASE_URL) PATH="$(PWD)/$(BACKEND)/.venv/bin:$$PATH" .venv/bin/alembic revision --autogenerate -m "$(m)"

fmt: ## Format backend code
	cd $(BACKEND) && .venv/bin/ruff format .

lint: ## Lint backend and frontend
	cd $(BACKEND) && .venv/bin/ruff check .
	cd $(FRONTEND) && npm run lint

typecheck: ## Type-check backend and frontend
	cd $(BACKEND) && .venv/bin/mypy stockbrain tests
	cd $(FRONTEND) && npm run typecheck

test: guard-db ## Run the backend test suite (needs the stockbrain_test database)
	cd $(BACKEND) && DATABASE_URL_TEST=$(TEST_DATABASE_URL) .venv/bin/python -m pytest

guard-db:
	@if [ -z "$(POSTGRES_PASSWORD)" ]; then \
		echo "POSTGRES_PASSWORD is not set. Copy .env.example to .env and fill it in," >&2; \
		echo "or pass TEST_DATABASE_URL=... explicitly." >&2; \
		exit 1; \
	fi

audit: ## Dependency vulnerability scan (Python + npm)
	cd $(BACKEND) && .venv/bin/pip-audit --progress-spinner off
	cd $(FRONTEND) && npm audit --audit-level=high

check: fmt lint typecheck test audit ## Everything CI would run

verify: check ## Full baseline verification, including containers
	docker compose -f compose.yaml config --quiet && echo "compose config: VALID"
	docker build -f $(BACKEND)/Dockerfile -t stockbrain:local .
	docker compose -f compose.yaml -f compose.dev.yaml up -d --force-recreate
	@echo "waiting for container health..."
	@for i in $$(seq 1 40); do \
		s=$$(docker inspect --format='{{.State.Health.Status}}' stockbrain-stockbrain-1 2>/dev/null); \
		[ "$$s" = "healthy" ] && break; sleep 3; \
	done; echo "stockbrain health: $$s"
	curl -sf localhost:8080/api/health | python3 -m json.tool
	curl -sf localhost:8080/api/v1/system/execution-status | python3 -m json.tool

frontend-build: ## Build the production frontend bundle
	cd $(FRONTEND) && npm run build

clean: ## Remove build artefacts and caches
	rm -rf $(FRONTEND)/dist $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
	find $(BACKEND) -name __pycache__ -type d -prune -exec rm -rf {} +
