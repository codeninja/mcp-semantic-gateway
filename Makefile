setup:
	uv sync

dev:
	uv sync --all-extras

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

docs-serve:
	uv run mkdocs serve

docs-build:
	uv run mkdocs build

.PHONY: setup dev test lint format docs-serve docs-build
