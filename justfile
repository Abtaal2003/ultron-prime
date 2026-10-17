# Ultron Prime task runner. Install `just` (https://just.systems) or run the commands directly.
set dotenv-load := false

default:
    @just --list

# Install Python dependencies
sync:
    uv sync --all-extras --group dev

# Run API with reload and the Vite dev server together
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'kill 0' EXIT
    uv run uvicorn src.api.app:create_app --factory --reload --host 127.0.0.1 --port 8787 &
    (cd web && npm install && npm run dev) &
    wait

# Build the frontend into web/dist (served by FastAPI)
build:
    cd web && npm ci && npm run build

# Run the production server (one process, serves web/dist)
up:
    uv run ultron

# Full verification: lint, types, tests, frontend
test:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    uv run pytest --cov
    cd web && npm run lint && npm run typecheck && npm test

# Regenerate TypeScript types from the OpenAPI and event schemas
types:
    cd web && npm run gen:types

# Playwright end-to-end (desktop, iPhone and Android profiles) against a fake-LLM server
e2e:
    cd web && npm ci && npx vite build && npx playwright test

# Export pinned requirement files from uv.lock
export:
    uv export --no-hashes --no-dev --no-emit-project -o requirements.txt
    uv export --no-hashes --no-emit-project -o requirements-dev.txt
