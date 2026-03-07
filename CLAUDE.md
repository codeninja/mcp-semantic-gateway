# CLAUDE.md — ToolSearch

Semantic tool discovery middleware for MCP. Indexes tool definitions from connected MCP servers, generates embeddings, and returns only the tools most relevant to the user's current intent.

## Commands

```bash
make setup          # Install dependencies (uv sync)
make dev            # Run in development mode
make test           # Run tests (pytest, ≥85% coverage)
make lint           # Lint (ruff check + ruff format --check)
make format         # Auto-format (ruff format)
make docker-up      # Start Docker Compose
make docker-down    # Stop Docker Compose
```

## Architecture

```
src/toolsearch/
├── cli/                # CLI commands (typer) — init, index, proxy, search, status, config, bootstrap, logs
├── config/             # Configuration loading (TOML + env vars + Pydantic validation)
├── ingestion/          # Tool collection, embedding, index writing
│   ├── collector.py    # MCP server spawning + tools/list harvesting
│   ├── embedder.py     # EmbeddingBackend protocol + local ONNX implementation
│   ├── remote_embedder.py  # Remote embedding backend (OpenAI, etc.)
│   └── index_writer.py # Persists vectors (hnswlib) + metadata (SQLite)
├── retrieval/          # Query pipeline
│   ├── index_reader.py # ANN search via hnswlib
│   └── query_engine.py # Embed → search → filter → rank
├── integration/        # MCP integration surfaces
│   ├── proxy.py        # MCP Proxy (stdio↔stdio, tools/list filtering, tools/call routing)
│   ├── search_tool.py  # Search Tool Server (toolsearch_find, toolsearch_context)
│   ├── sidecar.py      # Optional HTTP sidecar for context setting
│   └── bootstrap.py    # Config generators for Claude Code, Cursor, Gemini CLI
├── storage/            # Persistence layer
│   ├── vector_store.py # hnswlib wrapper
│   └── metadata_db.py  # SQLite (async via aiosqlite, WAL mode)
├── logging/            # Audit logging
│   └── audit.py        # JSONL audit logger with rotation (DiscoveryEvent, IndexEvent)
├── models/             # Domain types
│   ├── domain.py       # ToolRecord, ScoredTool, ToolResult, ServerRecord
│   ├── events.py       # DiscoveryEvent, IndexEvent
│   └── errors.py       # Error taxonomy (error codes + exceptions)
└── mcp/                # MCP protocol helpers
    ├── protocol.py     # JSON-RPC message builders/parsers
    └── transport.py    # stdio read/write helpers
```

## Key Patterns

**DO:**
- Use `uv` for all package management (not pip)
- Use Pydantic BaseSettings for config, `tomllib` for parsing, `tomlkit` for writing
- Use `asyncio` for all I/O (subprocess, network, file)
- Use `aiosqlite` for database access (WAL mode enabled)
- Wrap CPU-bound work (ONNX inference) in `asyncio.to_thread`
- Use atomic file swaps for index updates (`write tmp → os.rename`)
- Preserve MCP messages verbatim in passthrough (no field mutation)
- Return structured errors with codes from the error taxonomy
- Log to JSONL audit files; never fail an operation because logging failed

**DON'T:**
- Don't use pip, npm, or any other package manager
- Don't use fat `__init__.py` with re-exports
- Don't use Alembic (overkill for local SQLite)
- Don't modify tool definitions in tools/list responses (except namespace_collisions prefix)
- Don't send tool definitions or queries to external services unless operator configured remote embedding
- Don't log credentials (API keys, tokens) — ever
- Don't block the event loop with synchronous I/O
- Don't use manual router imports — use `pkgutil.iter_modules` for CLI subcommand discovery if needed

## Agent Workflow

- **Branch naming:** `feat/issue-{N}-{slug}` (e.g., `feat/issue-1-project-scaffold`)
- **Commits:** Conventional commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`)
- **Tests:** Every task includes test criteria. Run `make test` before committing. ≥85% coverage.
- **PR scope:** One task per PR. Each PR should be reviewable independently.

## Environment Variables

```
TOOLSEARCH_RETRIEVAL__TOP_K=10         # Override retrieval.top_k
TOOLSEARCH_EMBEDDING__BACKEND=local    # Override embedding.backend
TOOLSEARCH_EMBEDDING__API_KEY=sk-...   # Remote embedding API key (takes precedence over config)
TOOLSEARCH_LOG_LEVEL=debug             # Enable debug logging
```

Prefix: `TOOLSEARCH_`, nested separator: `__` (double underscore).

## Data Directory

```
~/.toolsearch/
├── config.toml         # Main configuration
├── index/
│   ├── vectors.db      # hnswlib vector store
│   └── metadata.db     # SQLite metadata
├── models/
│   └── all-MiniLM-L6-v2/  # Default ONNX embedding model
└── logs/
    ├── discovery.jsonl  # Discovery audit log
    └── index.jsonl      # Index mutation log
```

## Spec & Plans

- **Spec:** `docs/specs/SPEC.md` — full technical specification
- **Implementation Plan:** `docs/plans/IMPLEMENTATION_PLAN.md` — phases, dependencies, risks
- **Tasks:** `docs/plans/TASKS.md` — 23 ordered implementation tasks with acceptance criteria
