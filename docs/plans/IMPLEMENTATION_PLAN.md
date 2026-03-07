# ToolSearch — Implementation Plan

**Spec**: `docs/specs/SPEC.md` (v1.0.0)
**Date**: 2026-03-07
**Status**: Draft

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                         CLI (typer)                          │
│  init │ index │ proxy │ search │ bootstrap │ status │ config │
└───┬──────┬──────┬──────┬──────┬──────┬──────┬───────────────┘
    │      │      │      │      │      │      │
    ▼      ▼      ▼      ▼      ▼      ▼      ▼
┌──────────────────────────────────────────────────────────────┐
│                    Core Library (toolsearch/)                 │
│                                                              │
│  ┌────────────┐  ┌────────────┐  ┌──────────────────────┐   │
│  │ Collector   │  │ Embedder   │  │ IndexWriter/Reader   │   │
│  │ (MCP client)│  │ (ONNX /    │  │ (SQLite + HNSW)      │   │
│  │             │  │  remote)   │  │                      │   │
│  └─────┬──────┘  └─────┬──────┘  └──────┬───────────────┘   │
│        │               │                │                    │
│        └───────┬───────┘                │                    │
│                ▼                        ▼                    │
│        ┌──────────────┐        ┌──────────────┐             │
│        │ QueryEngine  │───────►│ Vector Store │             │
│        └──────────────┘        │ (hnswlib)    │             │
│                                └──────────────┘             │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────┐                 │
│  │ MCP Proxy        │  │ Search Tool Srv  │                 │
│  │ (stdio↔stdio)    │  │ (toolsearch_find │                 │
│  │                  │  │  _context)        │                 │
│  └──────────────────┘  └──────────────────┘                 │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────┐                 │
│  │ Bootstrap Engine │  │ Audit Logger     │                 │
│  └──────────────────┘  └──────────────────┘                 │
└──────────────────────────────────────────────────────────────┘
         │
         ▼
  ~/.toolsearch/
  ├── config.toml
  ├── index/ (vectors.db, metadata.db)
  ├── models/ (ONNX model files)
  └── logs/ (discovery.jsonl, index.jsonl)
```

---

## Implementation Phases

### Phase 1: Foundation — Project Scaffold + Config + Storage
**Rationale**: Everything else depends on a loadable config, a writable data directory, and database schemas.
**Effort**: M
**Risk**: Low. Standard infrastructure.

| Task | Description |
|------|-------------|
| 1 | Project scaffold (pyproject.toml, directory structure, Makefile) |
| 2 | Configuration loading (TOML + env var overrides + validation) |
| 3 | Data directory initialization + SQLite metadata schema |

### Phase 2: Ingestion — Embedding + Collection + Index Writing
**Rationale**: Must be able to harvest tools and embed them before retrieval is possible.
**Effort**: L
**Risk**: Medium. MCP protocol compliance and ONNX model loading are the main risks.

| Task | Description |
|------|-------------|
| 4 | Embedding backend interface + local ONNX implementation |
| 5 | MCP Collector (spawn servers, `tools/list`, pagination) |
| 6 | IndexWriter (vector store + metadata, atomic writes) |
| 7 | `tool-search index` CLI command (full rebuild orchestration) |

### Phase 3: Retrieval — Query Engine + Search
**Rationale**: Core value prop — embed a query and find matching tools.
**Effort**: M
**Risk**: Low. Straightforward ANN search + filtering.

| Task | Description |
|------|-------------|
| 8 | IndexReader (ANN search via hnswlib/usearch) |
| 9 | QueryEngine (embed → search → filter → rank) |
| 10 | `tool-search search` CLI command |

### Phase 4: Integration — MCP Proxy + Search Tool Server
**Rationale**: The two primary integration surfaces. Proxy is the most complex component.
**Effort**: L
**Risk**: High. stdio bidirectional message passing, context state management, and correct passthrough are all error-prone.

| Task | Description |
|------|-------------|
| 11 | MCP Proxy — stdio transport + message routing |
| 12 | MCP Proxy — tools/list filtering + context state |
| 13 | MCP Proxy — tools/call routing + passthrough |
| 14 | Search Tool Server (toolsearch_find + toolsearch_context) |
| 15 | `tool-search proxy` CLI command |

### Phase 5: Observability — Audit Logging + Status
**Rationale**: Required by spec (Section 9). Separate from core logic but mandatory for conformance.
**Effort**: S
**Risk**: Low.

| Task | Description |
|------|-------------|
| 16 | Audit logger (DiscoveryEvent + IndexEvent to JSONL, rotation) |
| 17 | `tool-search status` + `tool-search logs` CLI commands |

### Phase 6: Polish — Bootstrap + Sidecar + Error Handling
**Rationale**: Convenience features and hardening. Not on the critical path.
**Effort**: M
**Risk**: Low–Medium. Bootstrap connectors depend on target runtime config formats.

| Task | Description |
|------|-------------|
| 18 | Bootstrap engine (Claude Code, Cursor, Gemini CLI templates) |
| 19 | HTTP sidecar (context endpoint + health check) |
| 20 | `tool-search init` + `tool-search config` CLI commands |
| 21 | Remote embedding backend support |

### Phase 7: Deployment — Docker + CI/CD
**Rationale**: Packaging and automation, after the product works.
**Effort**: S
**Risk**: Low.

| Task | Description |
|------|-------------|
| 22 | Dockerfile + docker-compose.yml |
| 23 | GitHub Actions CI (lint, test, build) |

---

## Dependency Graph

```
Phase 1: [1] ──► [2] ──► [3]
                   │
Phase 2:           ├──► [4] ──┐
                   │          ├──► [6] ──► [7]
                   └──► [5] ──┘
                              │
Phase 3:                      └──► [8] ──► [9] ──► [10]
                                           │
Phase 4:                                   ├──► [11] ──► [12] ──► [13] ──► [15]
                                           │              │
                                           └──► [14] ─────┘
                                                │
Phase 5:                       [3] ──────► [16] ──► [17]
                                                │
Phase 6:                       [2] ──► [18]     │
                              [12] ──► [19]     │
                               [2] ──► [20]     │
                               [4] ──► [21]     │
                                                │
Phase 7:                            [all] ──► [22] ──► [23]
```

**Critical path**: 1 → 2 → 3 → 4/5 → 6 → 7 → 8 → 9 → 11 → 12 → 13 → 15

---

## Risk Assessment

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| ONNX model loading complexity (Task 4) | High — blocks all embedding | Medium | Validate with a known model early. Fall back to remote backend. |
| MCP protocol edge cases in proxy (Tasks 11-13) | High — incorrect passthrough breaks tool execution | Medium | Write protocol-level tests with recorded MCP message traces. |
| stdio bidirectional relay (Task 11) | Medium — deadlocks, buffering issues | Medium | Use asyncio with separate read/write tasks per direction. |
| Server spawn reliability (Task 5) | Low — degraded but functional | High | Spec already defines DEGRADED state. Test with intentionally broken servers. |
| Vector store corruption on crash (Task 6) | Medium — index must be rebuilt | Low | Atomic swap pattern per spec. WAL mode for SQLite. |

---

## Effort Summary

| Phase | Effort | Tasks |
|-------|--------|-------|
| 1. Foundation | M | 3 |
| 2. Ingestion | L | 4 |
| 3. Retrieval | M | 3 |
| 4. Integration | L | 5 |
| 5. Observability | S | 2 |
| 6. Polish | M | 4 |
| 7. Deployment | S | 2 |
| **Total** | — | **23 tasks** |

---

## Stack Decisions

| Concern | Choice | Rationale |
|---------|--------|-----------|
| Language | Python 3.12+ | ONNX Runtime has best Python support; MCP SDK is Python-first |
| Package manager | uv | Per project conventions |
| Embedding | onnxruntime + sentence-transformers model | Local-first, no API key needed |
| Vector store | hnswlib (via hnswlib Python bindings) | Lightweight, no server, good for ≤10K vectors |
| Metadata DB | SQLite (aiosqlite for async) | Bundled, zero config |
| Config | tomllib (stdlib) + pydantic BaseSettings | TOML parsing + validation |
| CLI | typer | Clean subcommand support, auto-generated help |
| MCP transport | asyncio streams (stdin/stdout) | Spec requires stdio; asyncio handles bidirectional relay |
| HTTP sidecar | uvicorn + starlette (minimal) | Lightweight, async-native |
| Testing | pytest + pytest-asyncio | Per conventions, ≥85% coverage target |
| CI | GitHub Actions | Per conventions |

---

## Directory Structure

```
toolsearch/
├── pyproject.toml
├── Makefile
├── CLAUDE.md
├── .github/
│   └── workflows/
│       └── ci.yml
├── Dockerfile
├── docker-compose.yml
├── src/
│   └── toolsearch/
│       ├── __init__.py
│       ├── cli/
│       │   ├── __init__.py
│       │   ├── main.py          # typer app, subcommand registration
│       │   ├── init_cmd.py
│       │   ├── index_cmd.py
│       │   ├── proxy_cmd.py
│       │   ├── search_cmd.py
│       │   ├── status_cmd.py
│       │   ├── config_cmd.py
│       │   ├── bootstrap_cmd.py
│       │   └── logs_cmd.py
│       ├── config/
│       │   ├── __init__.py
│       │   ├── models.py        # Pydantic config models
│       │   ├── loader.py        # TOML + env var loading
│       │   └── defaults.py      # Built-in defaults
│       ├── ingestion/
│       │   ├── __init__.py
│       │   ├── collector.py     # MCP server spawning + tools/list
│       │   ├── embedder.py      # EmbeddingBackend interface + local impl
│       │   ├── remote_embedder.py  # Remote embedding backend
│       │   └── index_writer.py  # Vector + metadata persistence
│       ├── retrieval/
│       │   ├── __init__.py
│       │   ├── index_reader.py  # ANN search
│       │   └── query_engine.py  # Full retrieval pipeline
│       ├── integration/
│       │   ├── __init__.py
│       │   ├── proxy.py         # MCP Proxy (stdio relay)
│       │   ├── search_tool.py   # Search Tool Server
│       │   ├── sidecar.py       # HTTP sidecar
│       │   └── bootstrap.py     # Bootstrap engine
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── vector_store.py  # hnswlib wrapper
│       │   └── metadata_db.py   # SQLite metadata
│       ├── logging/
│       │   ├── __init__.py
│       │   └── audit.py         # JSONL audit logger with rotation
│       ├── models/
│       │   ├── __init__.py
│       │   ├── domain.py        # ToolRecord, ScoredTool, ToolResult, etc.
│       │   ├── events.py        # DiscoveryEvent, IndexEvent
│       │   └── errors.py        # Error taxonomy (error codes, exceptions)
│       └── mcp/
│           ├── __init__.py
│           ├── protocol.py      # JSON-RPC message parsing/building
│           └── transport.py     # stdio read/write helpers
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_config.py
│   │   ├── test_embedder.py
│   │   ├── test_collector.py
│   │   ├── test_index_writer.py
│   │   ├── test_index_reader.py
│   │   ├── test_query_engine.py
│   │   ├── test_proxy.py
│   │   ├── test_search_tool.py
│   │   ├── test_audit.py
│   │   └── test_domain_models.py
│   ├── integration/
│   │   ├── test_index_roundtrip.py
│   │   ├── test_proxy_e2e.py
│   │   └── test_sidecar.py
│   └── fixtures/
│       ├── sample_config.toml
│       ├── mcp_tools_list_response.json
│       └── mcp_messages/
│           ├── initialize.json
│           ├── tools_list.json
│           └── tools_call.json
└── scripts/
    └── download_model.py
```
