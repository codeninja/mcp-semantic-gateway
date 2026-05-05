---
name: mcp-gateway-dev-setup
description: Use when contributing to the mcp-semantic-gateway repository — cloning, installing dev dependencies with uv, navigating the source tree, or running the Makefile targets. Triggers on tasks about local development environment for this codebase.
---

# Local development setup

This is a `uv`-managed Python 3.12+ package using the `uv_build` backend.

## Bootstrap

```bash
git clone https://github.com/codeninja/mcp-semantic-gateway && cd mcp-semantic-gateway
uv sync           # runtime deps only
uv sync --dev     # also install dev tools (pytest, ruff, mkdocs)
```

The console entry point `mcp-semantic-gateway` is registered against `mcp_semantic_gateway.cli.main:app`. Run it via `uv run mcp-semantic-gateway ...` during development to use the in-tree code.

## Source layout

```
src/mcp_semantic_gateway/
├── cli/                 # typer app — `main.py`, `synth.py`, `onboard.py`
├── config/              # pydantic models + `loader.py` for config.toml
├── ingestion/           # collectors and the index-builder pipeline
├── integration/         # MCP proxy, HTTP server, OpenAPI executor, router
├── llm/                 # provider abstractions (Anthropic + OpenAI-compat)
├── retrieval/           # SearchCore, ToolRegistry, embedding store
├── storage/             # data dir initialization, on-disk layout
└── skills/              # bundled skills shipped to onboard targets
```

Tests live under `tests/`, fixtures under `tests/fixtures/`. The end-to-end suite (`test_e2e.py`) spins up a real proxy against stub upstreams.

## Makefile targets

| Target | Action |
|---|---|
| `make setup` | `uv sync` |
| `make dev` | `uv sync --all-extras` |
| `make test` | `uv run pytest` |
| `make lint` | `uv run ruff check .` |
| `make format` | `uv run ruff format .` |
| `make docs-serve` | `uv run mkdocs serve` |
| `make docs-build` | `uv run mkdocs build` |
| `make build-and-publish` | `rm -rf dist/ && uv build && uv publish` |

## Run the gateway against a scratch config

```bash
# Use a throwaway data dir so you don't clobber your real ~/.mcp_semantic_gateway
export MCP_SEMANTIC_GATEWAY_HOME=$(mktemp -d)
uv run mcp-semantic-gateway init
uv run mcp-semantic-gateway index
uv run mcp-semantic-gateway proxy
```

## Common gotchas

- **`uv sync` complains about Python version** — repo pins `>=3.12`. `pyenv install 3.12` and `uv python pin 3.12` if needed.
- **Embeddings download on first run** — `all-MiniLM-L6-v2` (~80 MB) is fetched from HuggingFace by `sentence-transformers` on the first `index`. Subsequent runs are offline.
- **Ruff is the source of truth for style** — `make format` before committing. Pre-commit is not enforced, but CI runs `ruff check`.
