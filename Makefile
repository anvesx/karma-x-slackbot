.PHONY: check test lint run

check: lint test

lint:
	uv run --frozen ruff check src tests scripts
	uv run --frozen ruff format --check src tests scripts
	uv run --frozen mypy src/karma_x

test:
	uv run --frozen pytest -q --cov=karma_x --cov-report=term-missing

run:
	uv run --frozen --env-file .env python -m karma_x.app
