.PHONY: setup dev lint type-check test build

setup:            ## uv sync + install pre-commit hooks
	uv sync
	uv run pre-commit install
	uv run pre-commit install --hook-type commit-msg

dev:              ## run a dev REPL-ish sanity: sync + test
	uv sync
	uv run pytest

lint:             ## ruff format check + ruff lint + import-linter
	uv run ruff format --check .
	uv run ruff check .
	uv run lint-imports --no-logo

type-check:       ## ty static check
	uv run ty check

test:             ## pytest (offline suite only)
	uv run pytest

build:            ## wheel + sdist
	uv build
