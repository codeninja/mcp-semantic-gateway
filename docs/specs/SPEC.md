# ToolSearch — Semantic Tool Discovery Middleware for MCP
## Production-Grade Technical Specification

**Version**: 1.0.0  
**Status**: Approved  
**Date**: 2026-03-07  
**Author**: OpenClaw Technical Specification Team

## Table of Contents

1. [Purpose & Scope](#1-purpose--scope)
2. [Architecture Overview](#2-architecture-overview)
3. [Domain Model](#3-domain-model)
4. [State & Lifecycle](#4-state--lifecycle)
5. [Behavioral Contracts](#5-behavioral-contracts)
6. [Configuration](#6-configuration)
7. [Error Handling](#7-error-handling)
8. [Integration Protocol](#8-integration-protocol)
9. [Observability](#9-observability)
10. [Portability Invariants](#10-portability-invariants)
11. [Quick Implementation Reference](#11-quick-implementation-reference)

### Supporting Documents

- `00_PROBLEM_STATEMENT.md` — Problem definition, goals, anti-goals, scope boundaries.
- `01_EXPLORATION.md` — Use cases, technical challenges, patterns, prior art.

---

# 1. Purpose & Scope

## 1.1 Purpose

ToolSearch is a semantic tool discovery middleware for the Model Context Protocol (MCP). It solves the "too many tools" problem — the degradation in LLM reasoning quality, tool-selection accuracy, and token efficiency that occurs when agents are presented with large numbers of tool definitions in their context window.

ToolSearch indexes tool definitions from connected MCP servers, generates semantic embeddings, and at query time returns only the tools most relevant to the user's current intent. It operates as local-first middleware: no tool definitions or user queries leave the user's machine unless the operator explicitly configures a remote embedding endpoint.

## 1.2 Operational Problems Solved

1. **Context window saturation**: Tool definitions from multiple MCP servers consume 20–30% of available context before the model begins reasoning. ToolSearch reduces this to only the relevant subset (target: ≥50% token reduction).
2. **Declining tool-selection accuracy**: LLMs select incorrect tools or hallucinate parameters when presented with >10–15 options. ToolSearch narrows the candidate set to a ranked top-k (target: correct tool in top-5 ≥90% of the time).
3. **Manual tool curation burden**: Developers currently hand-manage which servers to connect per task. ToolSearch automates discovery, making large tool inventories practical.
4. **Ecosystem fragmentation**: Without a standard middleware layer, each agent framework builds its own ad-hoc tool filtering. ToolSearch provides a client-agnostic solution that works across MCP-compatible runtimes.

## 1.3 In Scope

- Semantic indexing of MCP tool definitions (name, description, input schema).
- **Direct OpenAPI/Swagger ingestion**: native discovery and indexing of REST endpoints without a separate MCP bridge.
- Query-time retrieval of top-k relevant tools via embedding similarity.
- MCP Proxy Mode: stdio-to-stdio wrapper that filters `tools/list` responses and transparently passes all other MCP messages.
- Search Tool Mode: a standalone MCP tool (`toolsearch_find`) for on-demand mid-session discovery.
- Query context acquisition via companion tool (`toolsearch_context`), HTTP sidecar, or static filters.
- CLI for initialization, indexing, proxy startup, and client bootstrap.
- Configurable embedding model, retrieval parameters, and server management.
- Audit logging of all discovery events.
- Bootstrap connectors for Claude Code, Cursor, and Gemini CLI.

## 1.4 Out of Scope

- Tool execution (`tools/call` passthrough only — ToolSearch never interprets or modifies tool invocation).
- MCP server implementation, lifecycle management, or health checking.
- [ ] Cloud/hosted deployment, multi-tenant operation, or remote API service.
- Tool description enrichment, rewriting, or quality improvement.
- User prompt rewriting or NLP preprocessing.
- GUI or web dashboard.
- MCP resource or prompt indexing (tools only).
- LLM-in-the-loop re-ranking (embedding-only retrieval in V1).

---

# 2. Architecture Overview

## 2.1 Component Responsibilities

ToolSearch is composed of six logical components organized in three layers: **Ingestion**, **Retrieval**, and **Integration**.

### Ingestion Layer

| Component | Responsibility |
|-----------|---------------|
| **Collector** | Reads the operator's MCP server or OpenAPI configuration. Enumerates all sources. For MCP: spawns process and calls `tools/list`. For OpenAPI: fetches the spec (JSON/YAML) and dynamically generates `ToolRecord` definitions for each operation. Handles pagination and spec resolution. |
| **Forge Engine** | Internal component used by the Collector to "agentize" OpenAPI operations on-the-fly. It converts REST endpoints into MCP-compliant tool definitions (name, description, input schema) for indexing and proxy-time execution. |
| **Embedder** | Accepts a list of `ToolRecord` objects and produces a vector embedding for each. Delegates to a pluggable `EmbeddingBackend` interface. Ships with a default local backend (ONNX-based sentence transformer). Supports optional remote backends (e.g., OpenAI embeddings API) via configuration. |
| **IndexWriter** | Persists `ToolRecord` objects and their embedding vectors into the local vector store. Supports full rebuild and incremental update (add/remove/replace individual tools). Maintains a metadata table mapping each tool to its source server for incremental updates. |

### Retrieval Layer

| Component | Responsibility |
|-----------|---------------|
| **IndexReader** | Accepts a query embedding vector and retrieval parameters (top-k, similarity threshold). Performs approximate nearest neighbor (ANN) search against the vector store. Returns a ranked list of `ScoredTool` results. |
| **QueryEngine** | Orchestrates a full retrieval cycle: accepts a text query, calls the Embedder to produce a query vector, calls IndexReader for ANN search, applies post-retrieval filters (server allowlist/blocklist, minimum score threshold), and returns the final `ToolResult` set. |

### Integration Layer

| Component | Responsibility |
|-----------|---------------|
| **MCP Proxy** | A stdio-to-stdio MCP server wrapper. Spawns the upstream MCP server as a child process. Intercepts and transforms `tools/list` responses using the QueryEngine. Transparently passes all other MCP messages (`tools/call`, `notifications/*`, `resources/*`, `prompts/*`, etc.) bidirectionally without modification. Manages query context state set by the companion `toolsearch_context` tool. |
| **Search Tool Server** | A standalone MCP server that exposes two tools: `toolsearch_find` (semantic search returning full tool definitions) and `toolsearch_context` (sets the query context for a subsequent proxy-mode `tools/list` call). Can run alongside the proxy or independently. |
| **HTTP Server (SSE)** | A public-facing FastAPI server that implements the MCP-over-HTTP (SSE) transport. Supports multi-tenant query context via session headers. |
| **CLI** | User-facing command-line interface. Subcommands: `init`, `index`, `proxy`, `serve`, `search`, `bootstrap`, `config`, `status`. Orchestrates the other components. |
| **Bootstrap Engine** | Generates client-specific configuration snippets for supported agent runtimes. Each bootstrap target is a template that maps ToolSearch's proxy or search tool into the client's native configuration format. |

## 2.2 Layered Architecture Diagram

```
┌─────────────────────────────────────────────────────┐
│                   MCP Client                         │
│              (Claude Code, Cursor, etc.)             │
└──────────────┬──────────────────────┬───────────────┘
               │ stdio                │ MCP tools/call
               ▼                      │
┌──────────────────────────┐          │
│    Integration Layer     │          │
│ ┌──────────────────────┐ │          │
│ │     MCP Proxy        │◄┼──────────┘
│ │  (stdio-to-stdio)    │ │
│ └──────┬───────────────┘ │
│        │                 │
│ ┌──────▼───────────────┐ │
│ │  Search Tool Server  │ │
│ │ (toolsearch_find,    │ │
│ │  toolsearch_context) │ │
│ └──────┬───────────────┘ │
└────────┼─────────────────┘
         │
┌────────▼─────────────────┐
│    Retrieval Layer       │
│ ┌──────────────────────┐ │
│ │    QueryEngine       │ │
│ │  (embed → search →   │ │
│ │   filter → return)   │ │
│ └──────┬───────────────┘ │
│        │                 │
│ ┌──────▼───────────────┐ │
│ │    IndexReader       │ │
│ │  (ANN search)        │ │
│ └──────────────────────┘ │
└──────────────────────────┘
         │
┌────────▼─────────────────┐
│    Ingestion Layer       │
│ ┌────────┐ ┌──────────┐  │
│ │Collector│ │Embedder  │  │
│ └────┬───┘ └────┬─────┘  │
│      │          │         │
│ ┌────▼──────────▼──────┐  │
│ │    IndexWriter       │  │
│ │  (vector store)      │  │
│ └──────────────────────┘  │
└───────────────────────────┘
         │
┌────────▼─────────────────┐
│    Storage               │
│  ~/.toolsearch/          │
│  ├── config.toml         │
│  ├── index/              │
│  │   ├── vectors.db      │
│  │   └── metadata.db     │
│  └── logs/               │
│      └── discovery.jsonl │
└──────────────────────────┘
```

## 2.3 External Dependencies

| Dependency | Type | Purpose | Required? |
|-----------|------|---------|-----------|
| MCP Servers | Runtime | Tool definition sources. ToolSearch calls `tools/list` on each. | Yes (≥1) |
| ONNX Runtime | Bundled | Default local embedding inference engine. | Yes (for default backend) |
| Default Embedding Model | Downloaded | Sentence transformer model (e.g., all-MiniLM-L6-v2). Downloaded on first `tool-search init`. | Yes (for default backend) |
| SQLite | Bundled | Metadata storage (tool records, server mapping, index versions). | Yes |
| ANN Library | Bundled | Vector similarity search (hnswlib, usearch, or equivalent). | Yes |
| Remote Embedding API | Optional | User-configured alternative to local embedding (e.g., OpenAI, Cohere). | No |

## 2.4 Integration Surfaces

1. **Upstream (to MCP Servers)**: stdio transport. ToolSearch speaks MCP client protocol to upstream servers. Messages: `tools/list`, `tools/call` (passthrough), `notifications/tools/list_changed` (subscription).
2. **Downstream (to MCP Clients)**: stdio transport. ToolSearch speaks MCP server protocol. Exposes filtered `tools/list` responses and passes through all other messages.
3. **Search Tool API**: Standard MCP tool interface. Two tools: `toolsearch_find(query, top_k?, threshold?)` and `toolsearch_context(query, ttl_seconds?)`.
4. **CLI**: Shell commands. Exit codes: 0 (success), 1 (error), 2 (partial success with warnings).
5. **Configuration**: TOML file at `~/.toolsearch/config.toml`. Environment variable overrides with `TOOLSEARCH_` prefix.
6. **Audit Log**: Append-only JSONL file at `~/.toolsearch/logs/discovery.jsonl`.

---

# 3. Domain Model

All data structures are described as named records with typed fields. Nullability, defaults, and constraints are specified for every field. Implementations may use any internal representation; the field names and semantics are normative.

## 3.1 Core Records

### ServerRecord

Represents a configured MCP server that ToolSearch connects to for tool harvesting.

| Field | Type | Nullable | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `server_id` | string | No | — | Unique within configuration. Format: `[a-z0-9_-]{1,64}`. | Stable identifier for the server. Operator-assigned in configuration. |
| `display_name` | string | Yes | `null` | Max 128 chars. | Human-readable label. When absent, `server_id` is used for display. |
| `command` | string | No | — | Non-empty. | Executable command to spawn the server process (e.g., `npx`, `python`). |
| `args` | list[string] | Yes | `[]` | — | Arguments passed to the command. When absent, treated as empty list. |
| `env` | map[string, string] | Yes | `{}` | — | Additional environment variables for the server process. When absent, treated as empty map. Merged with the parent process environment; entries here take precedence. |
| `enabled` | boolean | Yes | `true` | — | Whether this server is included in indexing and proxy routing. When absent, defaults to `true`. |
| `tags` | list[string] | Yes | `[]` | Each tag: `[a-z0-9_-]{1,32}`. | Operator-assigned tags for static filtering (e.g., `devops`, `productivity`). When absent, treated as empty list. |

### ToolRecord

Represents a single tool definition harvested from an MCP server, augmented with ToolSearch metadata.

| Field | Type | Nullable | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `tool_id` | string | No | — | Globally unique. Format: `{server_id}::{tool_name}`. | Composite identifier that namespaces the tool to its source server. Resolves multi-server name collisions. |
| `server_id` | string | No | — | Must reference an existing `ServerRecord.server_id`. | Source server that exposed this tool. |
| `name` | string | No | — | As provided by MCP `tools/list`. | The tool's MCP name, used for `tools/call` routing. |
| `title` | string | Yes | `null` | As provided by MCP `tools/list`. | Optional human-readable title from the MCP server. When absent, not included in embedding input. |
| `description` | string | Yes | `null` | As provided by MCP `tools/list`. | Tool description from the MCP server. Primary input for embedding. When absent, only `name` and `title` are embedded. |
| `input_schema` | object | Yes | `null` | Valid JSON Schema or null. | The tool's `inputSchema` from MCP. Stored verbatim for inclusion in filtered `tools/list` responses. Not used for embedding in V1. |
| `output_schema` | object | Yes | `null` | Valid JSON Schema or null. | The tool's `outputSchema` from MCP, if provided. Stored verbatim. When absent, omitted from responses. |
| `annotations` | object | Yes | `null` | As provided by MCP. | The tool's `annotations` object from MCP. Stored and returned verbatim. When absent, omitted from responses. |
| `embedding_text` | string | No | — | Computed field. | The text string that was embedded. Constructed as: `"{name} — {title} — {description}"`, omitting null components. Stored for debugging and re-embedding without re-fetching. |
| `embedding_vector` | list[float] | No | — | Dimensionality must match the configured embedding model. | The embedding vector for this tool. |
| `indexed_at` | ISO 8601 timestamp | No | — | UTC. | When this tool was last indexed (embedded and stored). |
| `index_version` | integer | No | — | Monotonically increasing per server. | The index generation in which this tool was last updated. Used for incremental update tracking. |

### ScoredTool

A retrieval result: a ToolRecord annotated with its similarity score for a given query.

| Field | Type | Nullable | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `tool_id` | string | No | — | References `ToolRecord.tool_id`. | The matched tool. |
| `score` | float | No | — | Range [0.0, 1.0]. Higher is more similar. | Cosine similarity between the query embedding and the tool embedding. |
| `rank` | integer | No | — | 1-indexed. | Position in the result list, ordered by descending score. |

### ToolResult

The complete result of a retrieval operation.

| Field | Type | Nullable | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `query` | string | No | — | — | The original text query. |
| `tools` | list[ScoredTool] | No | — | Ordered by descending `score`. Length ≤ `top_k`. | The ranked list of matching tools. |
| `total_candidates` | integer | No | — | ≥ 0. | Total number of tools in the index that were searched. |
| `retrieval_ms` | float | No | — | ≥ 0. | Time spent on vector similarity search (excludes embedding time). |
| `embedding_ms` | float | No | — | ≥ 0. | Time spent embedding the query. |
| `truncated` | boolean | No | — | — | `true` if results were limited by `top_k` (i.e., more candidates scored above the threshold than `top_k` allows). |

## 3.2 Configuration Records

### RetrievalConfig

Parameters controlling the retrieval behavior of the QueryEngine.

| Field | Type | Nullable | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `top_k` | integer | Yes | `10` | Range [1, 100]. | Maximum number of tools to return per query. When absent, defaults to 10. |
| `min_score` | float | Yes | `0.3` | Range [0.0, 1.0]. | Minimum cosine similarity score. Tools below this threshold are excluded even if within top-k. When absent, defaults to 0.3. |
| `server_allowlist` | list[string] | Yes | `null` | Each entry must be a valid `server_id`. | When non-null, only tools from these servers are considered. When null, all enabled servers are included. |
| `server_blocklist` | list[string] | Yes | `[]` | Each entry must be a valid `server_id`. | Tools from these servers are excluded. When absent, treated as empty (no exclusions). Blocklist takes precedence over allowlist. |

### EmbeddingConfig

Parameters for the embedding backend.

| Field | Type | Nullable | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `backend` | enum | Yes | `"local"` | One of: `"local"`, `"remote"`. | Which embedding backend to use. When absent, defaults to `"local"`. |
| `model_name` | string | Yes | `"all-MiniLM-L6-v2"` | — | For `"local"` backend: name of the ONNX model in the models directory. For `"remote"`: the model identifier sent to the API. When absent, uses the default model. |
| `model_path` | string | Yes | `"~/.toolsearch/models/{model_name}"` | Must be a valid directory path. | Path to the local model files. When absent, derived from `model_name`. |
| `dimensions` | integer | Yes | `384` | Range [64, 4096]. | Expected embedding dimensionality. Must match the model's output. When absent, defaults to 384 (matching default model). |
| `remote_url` | string | Yes | `null` | Valid HTTP(S) URL. Required when `backend` is `"remote"`. | Endpoint for the remote embedding API. When absent and backend is `"remote"`, configuration is invalid. |
| `remote_api_key` | string | Yes | `null` | — | API key for remote embedding endpoint. May also be set via `TOOLSEARCH_EMBEDDING_API_KEY` environment variable. Env var takes precedence. When absent for `"remote"` backend, requests are sent without authentication. |
| `batch_size` | integer | Yes | `32` | Range [1, 512]. | Maximum number of texts to embed in a single batch. When absent, defaults to 32. |

### ProxyConfig

Parameters for the MCP Proxy integration mode.

| Field | Type | Nullable | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `mode` | enum | Yes | `"filtered"` | One of: `"filtered"`, `"passthrough"`. | `"filtered"`: apply semantic filtering to `tools/list`. `"passthrough"`: forward all messages unmodified (useful for debugging). When absent, defaults to `"filtered"`. |
| `context_ttl_seconds` | integer | Yes | `300` | Range [1, 3600]. | How long a query context set via `toolsearch_context` remains active. After expiry, the proxy returns all tools (passthrough behavior). When absent, defaults to 300 seconds. |
| `fallback_on_no_context` | enum | Yes | `"all"` | One of: `"all"`, `"none"`, `"tagged"`. | What to return when no query context is set: `"all"` returns all tools, `"none"` returns empty list, `"tagged"` returns only tools matching configured tags. When absent, defaults to `"all"`. |
| `sidecar_port` | integer | Yes | `null` | Range [1024, 65535]. | When set, starts an HTTP sidecar on this port accepting POST `/context` with a JSON body `{ "query": "..." }` as an alternative to the `toolsearch_context` MCP tool. When absent, no sidecar is started. |

## 3.3 Event Records

### DiscoveryEvent

Audit log entry for a single tool discovery operation. Written to `discovery.jsonl`.

| Field | Type | Nullable | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `timestamp` | ISO 8601 timestamp | No | — | UTC. | When the discovery operation occurred. |
| `event_type` | enum | No | — | One of: `"proxy_filter"`, `"search_tool"`, `"cli_search"`. | Which integration surface triggered this discovery. |
| `query` | string | No | — | — | The text query used for retrieval. |
| `candidates` | list[object] | No | — | Each: `{ "tool_id": string, "score": float }`. Ordered by descending score. | All tools that scored above `min_score`, including those beyond `top_k`. |
| `selected` | list[string] | No | — | Subset of `candidates[*].tool_id`. | The tool IDs actually returned to the caller (after top-k truncation and any additional filters). |
| `config_snapshot` | object | No | — | Contains `top_k`, `min_score`, `server_allowlist`, `server_blocklist` at time of query. | The retrieval configuration in effect for this operation. |
| `latency_ms` | float | No | — | ≥ 0. | Total time for the discovery operation (embedding + retrieval + filtering). |

### IndexEvent

Audit log entry for index mutations. Written to `index.jsonl`.

| Field | Type | Nullable | Default | Constraints | Description |
|-------|------|----------|---------|-------------|-------------|
| `timestamp` | ISO 8601 timestamp | No | — | UTC. | When the index operation occurred. |
| `event_type` | enum | No | — | One of: `"full_rebuild"`, `"incremental_update"`, `"server_added"`, `"server_removed"`, `"tool_added"`, `"tool_removed"`. | The type of index mutation. |
| `server_id` | string | Yes | `null` | — | The affected server, if applicable. Null for full rebuilds. |
| `tools_added` | integer | No | `0` | ≥ 0. | Number of tools added to the index. |
| `tools_removed` | integer | No | `0` | ≥ 0. | Number of tools removed from the index. |
| `tools_updated` | integer | No | `0` | ≥ 0. | Number of tools re-embedded (description changed). |
| `total_tools` | integer | No | — | ≥ 0. | Total tools in index after this operation. |
| `duration_ms` | float | No | — | ≥ 0. | Total time for the index operation. |

## 3.4 Namespace Convention

To resolve tool name collisions across MCP servers, ToolSearch uses a composite `tool_id` with the format `{server_id}::{tool_name}`. This is an internal identifier only — when returning tool definitions to MCP clients in `tools/list` responses, the original `name` field is preserved unmodified.

When two servers expose a tool with the same `name`:
- Both tools are indexed independently with distinct `tool_id` values.
- Both may appear in retrieval results if they are semantically relevant.
- The `tools/list` response includes both, each with its original `name`. The MCP client must disambiguate (or ToolSearch may optionally prefix the name with `{server_id}/` if `proxy.namespace_collisions` is set to `true` in configuration — default: `false`).
- `tools/call` requests are routed to the correct upstream server by matching the tool name against the server that provided it. If ambiguous (same name, multiple servers), the tool from the highest-scoring server in the most recent retrieval is preferred.

---

# 4. State & Lifecycle

## 4.1 Index Lifecycle

The index is the central stateful artifact. It transitions through the following states:

```
                    ┌──────────────┐
                    │  UNINITIALIZED│
                    └──────┬───────┘
                           │ tool-search init
                           ▼
                    ┌──────────────┐
                    │    EMPTY     │
                    └──────┬───────┘
                           │ tool-search index (first run)
                           ▼
                    ┌──────────────┐
              ┌────►│   READY      │◄────┐
              │     └──┬───────┬───┘     │
              │        │       │         │
              │  index │       │ list_   │ incremental
              │  (full)│       │ changed │ update
              │        ▼       ▼         │ completes
              │  ┌─────────────────┐     │
              │  │   UPDATING      ├─────┘
              │  └────────┬────────┘
              │           │ error
              │           ▼
              │  ┌─────────────────┐
              └──┤   DEGRADED      │
                 │ (partial index) │
                 └─────────────────┘
```

### State Definitions

| State | Description | Query Behavior |
|-------|-------------|---------------|
| **UNINITIALIZED** | No ToolSearch data directory exists. No configuration file. | All operations return an error instructing the user to run `tool-search init`. |
| **EMPTY** | Data directory and configuration exist, but no tools have been indexed. | Retrieval returns empty results. Proxy mode returns empty `tools/list`. CLI warns that indexing is needed. |
| **READY** | Index contains ≥1 tool with valid embeddings. The index is consistent and up-to-date. | Normal operation. Retrieval returns ranked results. |
| **UPDATING** | An index operation (full rebuild or incremental update) is in progress. | Retrieval continues against the previous index snapshot. The in-progress update is built in a separate write buffer and swapped atomically on completion. Queries are never blocked by index writes. |
| **DEGRADED** | An index operation failed partway (e.g., one server unreachable, embedding error). The index contains tools from some servers but not all. | Retrieval operates on available tools. The proxy logs a warning listing which servers are missing. The `status` command reports degraded state with details. |

### Transitions

| From | To | Trigger | Side Effects |
|------|----|---------|-------------|
| UNINITIALIZED → EMPTY | `tool-search init` | Creates `~/.toolsearch/` directory structure, writes default `config.toml`, creates empty index database. |
| EMPTY → READY | `tool-search index` (success) | Connects to all configured servers, harvests tools, embeds, writes index. Logs `IndexEvent(full_rebuild)`. |
| EMPTY → DEGRADED | `tool-search index` (partial failure) | Same as above but one or more servers failed. Logs `IndexEvent(full_rebuild)` with error details. |
| READY → UPDATING | `tool-search index` OR `notifications/tools/list_changed` received | Begins index operation in background. Previous index remains queryable. |
| UPDATING → READY | Index operation completes successfully | Atomically swaps new index into place. Logs `IndexEvent`. |
| UPDATING → DEGRADED | Index operation completes with partial failure | Swaps new index (partial) into place. Logs `IndexEvent` with errors. |
| DEGRADED → READY | Subsequent full index succeeds for all servers | Full rebuild replaces partial index. |
| DEGRADED → UPDATING | `tool-search index` or `list_changed` | Re-attempts indexing. |

## 4.2 Proxy Session Lifecycle

Each MCP Proxy instance manages a session between one MCP client and one or more upstream MCP servers.

```
  ┌────────────┐
  │   IDLE     │  (proxy started, no context set)
  └─────┬──────┘
        │ toolsearch_context(query) received
        ▼
  ┌────────────┐
  │  CONTEXT   │  (query context active, TTL counting)
  │  ACTIVE    │
  └──┬─────┬───┘
     │     │ TTL expires
     │     ▼
     │ ┌────────────┐
     │ │   IDLE     │  (context expired, back to fallback)
     │ └────────────┘
     │
     │ new toolsearch_context(query) received
     └──► (stays CONTEXT_ACTIVE, resets TTL with new query)
```

### State Definitions

| State | Description | `tools/list` Behavior |
|-------|-------------|----------------------|
| **IDLE** | No query context is set. | Behavior determined by `fallback_on_no_context` config: `"all"` (return all tools), `"none"` (empty list), or `"tagged"` (return tools matching configured tags). |
| **CONTEXT_ACTIVE** | A query context has been set and TTL has not expired. | `tools/list` returns semantically filtered tools based on the active query. Each `tools/list` call during this state uses the same query context. |

### Context Replacement

When `toolsearch_context` is called while a context is already active, the new query replaces the old one and the TTL resets. There is no context stacking or history — only the most recent query is active.

## 4.3 Server Connection Lifecycle

ToolSearch manages stdio connections to upstream MCP servers. Each connection has its own lifecycle:

```
  ┌──────────────┐
  │ DISCONNECTED │
  └──────┬───────┘
         │ proxy start / index command
         ▼
  ┌──────────────┐
  │ CONNECTING   │──── timeout (30s default) ───► FAILED
  └──────┬───────┘
         │ server responds to initialize
         ▼
  ┌──────────────┐
  │  CONNECTED   │──── server process exits ───► DISCONNECTED
  └──────┬───────┘
         │ proxy shutdown / SIGTERM
         ▼
  ┌──────────────┐
  │ DISCONNECTED │
  └──────────────┘
```

| State | Description |
|-------|-------------|
| **DISCONNECTED** | No active connection to this server. |
| **CONNECTING** | Server process has been spawned; waiting for MCP `initialize` response. |
| **CONNECTED** | Server is responsive and tools have been harvested. |
| **FAILED** | Server process failed to start, timed out, or crashed. Tools from this server are unavailable. ToolSearch logs the failure and continues with remaining servers. |

### Reconnection Policy

When a connected server's process exits unexpectedly:
1. ToolSearch logs the disconnection.
2. If the proxy is running, ToolSearch attempts to respawn the server process after a backoff delay (1s, 2s, 4s, 8s, max 30s).
3. After 3 consecutive reconnection failures, the server is marked FAILED and no further attempts are made until the operator restarts the proxy or runs `tool-search index`.
4. Tools from a FAILED server remain in the index (stale but searchable) and are annotated with a `server_status: "disconnected"` flag in retrieval results.

## 4.4 Embedding Model Lifecycle

| State | Description | Transition |
|-------|-------------|-----------|
| **NOT_DOWNLOADED** | Default model is not present on disk. | `tool-search init` or first `tool-search index` triggers download. |
| **DOWNLOADING** | Model files are being fetched. | On success → LOADED. On failure → NOT_DOWNLOADED (with error). |
| **LOADED** | Model is loaded into memory and ready for inference. | Stays loaded for the duration of the process. |
| **LOAD_FAILED** | Model file is present but cannot be loaded (corrupt, incompatible). | `tool-search index` reports the error. Operator must re-download or configure alternative. |

For remote embedding backends, there is no model lifecycle — the backend is always considered available, and failures are handled per-request.

---

# 5. Behavioral Contracts

## 5.1 Tool Indexing Sequence

**Operation**: `tool-search index` (full rebuild or incremental)

**Preconditions**: Index state is EMPTY, READY, or DEGRADED. Configuration is valid. At least one server is configured and enabled.

**Steps**:

1. **Load configuration**: Read `~/.toolsearch/config.toml`. Validate all fields against the schema (Section 6). If invalid, abort with error code `CONFIG_INVALID`.
2. **Initialize embedding backend**: Load the configured model (local) or validate the remote endpoint (remote). If the model cannot be loaded, abort with error code `EMBEDDING_LOAD_FAILED`.
3. **Enumerate servers**: Filter configured servers to those with `enabled: true`. If zero servers remain, abort with error code `NO_SERVERS`.
4. **For each enabled server** (concurrency: up to `index.max_parallel_servers`, default 4):
   a. Spawn the server process using `command` and `args` from its `ServerRecord`.
   b. Send MCP `initialize` request. Wait up to `index.server_timeout_seconds` (default 30) for response.
   c. If timeout or error: log `IndexEvent(server_id, error)`, mark server as FAILED, continue to next server.
   d. Send `tools/list` request. Handle pagination: repeat with `cursor` until no `nextCursor` is returned.
   e. For each tool in the response: construct a `ToolRecord` with `tool_id = {server_id}::{tool.name}`.
   f. Construct `embedding_text` for each tool: `"{name}"` if description is null, otherwise `"{name} — {description}"`. If title is non-null, prepend: `"{name} — {title} — {description}"`.
   g. Terminate the server process (send `shutdown` notification, then SIGTERM after 5s if still running).
5. **Batch embed**: Send all `embedding_text` values to the Embedder in batches of `embedding.batch_size`. Collect vectors.
6. **Write index**: Atomically replace the index contents:
   a. Write all `ToolRecord` entries with their vectors to a new index segment.
   b. Update the metadata table with server → tool mappings and `index_version`.
   c. Swap the new segment into the active index (readers continue on old segment until swap completes).
7. **Log**: Write an `IndexEvent` entry with `tools_added`, `tools_removed`, `tools_updated`, `total_tools`, `duration_ms`.
8. **Report**: Print summary to stdout. Exit code 0 if all servers succeeded, 2 if some servers failed (DEGRADED).

**Postconditions**: Index state is READY (all servers) or DEGRADED (partial). All successfully harvested tools are searchable.

**Incremental update variant**: When triggered by `notifications/tools/list_changed` for a specific server, only steps 4a–4g execute for that server, followed by steps 5–7 for the affected tools only. The existing index entries for unchanged servers are preserved.

## 5.2 Semantic Retrieval Sequence

**Operation**: QueryEngine processes a text query.

**Preconditions**: Index state is READY or DEGRADED (at least one tool indexed). Embedding backend is loaded.

**Steps**:

1. **Embed query**: Pass the query text to the Embedder. Record `embedding_ms`.
2. **ANN search**: Pass the query vector and `top_k * 2` (over-fetch factor) to the IndexReader. The over-fetch factor ensures sufficient candidates survive post-filtering. Record `retrieval_ms`.
3. **Score normalization**: Cosine similarity scores are in range [-1.0, 1.0]. Normalize to [0.0, 1.0] via `normalized = (raw + 1.0) / 2.0`.
4. **Apply minimum score filter**: Remove candidates with `normalized_score < retrieval.min_score`.
5. **Apply server filters**: If `server_allowlist` is set, remove candidates not in the allowlist. Remove candidates in `server_blocklist`. Blocklist takes precedence.
6. **Truncate to top_k**: Keep only the top `retrieval.top_k` candidates by descending score.
7. **Assign ranks**: Number results 1 through N.
8. **Construct ToolResult**: Populate all fields including timing, truncation flag, and total_candidates (count before truncation).
9. **Return**: Return the `ToolResult` to the caller.

**Postconditions**: Caller receives a ranked list of ≤ `top_k` tools, each above `min_score`, respecting allow/blocklist constraints.

**Latency budget**: Steps 1 (embedding) should complete within 400ms on the default model/hardware. Step 2 (ANN search) should complete within 100ms for ≤1,000 tools. Total end-to-end target: <500ms p95.

## 5.3 Proxy Filter Sequence

**Operation**: MCP Proxy receives a `tools/list` request from the MCP client.

**Preconditions**: Proxy is running. At least one upstream server is CONNECTED.

**Steps**:

1. **Check context state**:
   - If IDLE and `fallback_on_no_context` is `"all"`: collect ALL tool definitions from all connected servers, skip to step 5.
   - If IDLE and `fallback_on_no_context` is `"none"`: return empty `tools/list` response immediately.
   - If IDLE and `fallback_on_no_context` is `"tagged"`: collect tools matching configured tags, skip to step 5.
   - If CONTEXT_ACTIVE: proceed to step 2.
2. **Retrieve active query**: Read the current query text from the context store.
3. **Execute retrieval**: Call the QueryEngine with the active query (Section 5.2).
4. **Resolve tool definitions**: For each `ScoredTool` in the result, fetch the full MCP tool definition (name, title, description, inputSchema, outputSchema, annotations) from the stored `ToolRecord`.
5. **Construct MCP response**: Build a `tools/list` response containing only the selected tool definitions. Preserve original MCP field names and values. Do not add, remove, or modify any fields within individual tool definitions.
6. **Handle pagination**: If the client sent a `cursor` parameter, the proxy ignores it (ToolSearch returns the full filtered set in a single response, never paginated). Future versions may support proxy-level pagination for very large filtered sets.
7. **Log**: Write a `DiscoveryEvent` to the audit log.
8. **Return**: Send the response to the MCP client via stdio.

**Postconditions**: Client receives a `tools/list` response containing only semantically relevant tools (or all tools / empty / tagged set if no context).

## 5.4 Proxy Passthrough Contract

For ALL MCP messages other than `tools/list` responses:

1. The proxy MUST forward the message to its destination without modification.
2. The proxy MUST preserve message ordering within a single direction (client→server, server→client).
3. The proxy MUST NOT buffer messages beyond what is necessary for framing (JSON-RPC message boundary detection).
4. The proxy MUST forward `tools/call` requests to the correct upstream server. Routing is by tool name: the proxy maintains an internal map of `tool_name → server_id` from the most recent index. If the tool name is ambiguous (exists in multiple servers), route to the server whose tool scored highest in the most recent retrieval for the active context. If no context is active, route to the first server in configuration order.
5. The proxy MUST forward `notifications/tools/list_changed` from upstream servers and trigger an incremental re-index for the affected server.
6. The proxy MUST NOT inject, drop, or reorder any MCP JSON-RPC messages.
7. Unknown message types (methods not recognized by ToolSearch) MUST be forwarded unchanged.

## 5.5 Search Tool Behavioral Contract

### `toolsearch_find`

**Input Schema**:
```
{
  "type": "object",
  "properties": {
    "query": { "type": "string", "description": "Natural language description of the desired tool capability." },
    "top_k": { "type": "integer", "minimum": 1, "maximum": 50, "default": 5 },
    "min_score": { "type": "number", "minimum": 0.0, "maximum": 1.0 },
    "servers": { "type": "array", "items": { "type": "string" }, "description": "Optional: limit search to these server IDs." }
  },
  "required": ["query"]
}
```

**Behavior**: Calls the QueryEngine with the provided parameters. Returns full MCP tool definitions for each matched tool, including `inputSchema`, so the caller can immediately invoke them.

**Output**: A text content block containing a JSON array of tool definitions with their scores:
```
[
  {
    "server_id": "kubernetes",
    "name": "k8s_get_logs",
    "description": "...",
    "inputSchema": { ... },
    "score": 0.94
  }
]
```

### `toolsearch_context`

**Input Schema**:
```
{
  "type": "object",
  "properties": {
    "query": { "type": "string", "description": "The current user intent or task description. Sets context for subsequent tools/list filtering." },
    "ttl_seconds": { "type": "integer", "minimum": 1, "maximum": 3600, "default": 300 }
  },
  "required": ["query"]
}
```

**Behavior**: Stores the query as the active proxy context. Resets TTL. If a context was already active, it is replaced.

**Output**: A text content block confirming the context was set: `"Context set: '{query}' (TTL: {ttl_seconds}s, expires: {expiry_timestamp})"`.

## 5.6 Concurrency and Thread Safety

- **Index reads and writes are concurrent**: Writers build a new index segment while readers query the old one. Swap is atomic (pointer/symlink swap).
- **Query context is per-proxy-instance**: Each proxy process maintains its own context state. There is no shared state between proxy instances.
- **Embedding requests are serialized per-backend**: The local ONNX backend processes one batch at a time. Multiple concurrent queries queue at the embedder. Remote backends may support concurrent requests as configured.
- **Server connections are per-proxy-instance**: Each proxy spawns and manages its own set of upstream server processes. Proxy instances do not share server connections.

---

# 6. Configuration

## 6.1 Configuration File

Primary configuration file: `~/.toolsearch/config.toml`

Created by `tool-search init` with defaults. Editable by the operator at any time. Changes take effect on next command invocation or proxy restart; there is no hot-reload mechanism.

## 6.2 Configuration Fields

### `[servers.<server_id>]` — Tool Source Definitions

| Key Path | Type | Default | Validation | Description |
|----------|------|---------|------------|-------------|
| `servers.<id>.type` | enum | `"mcp"` | `"mcp"`, `"openapi"` | The source type. Defaults to `"mcp"`. |
| `servers.<id>.url` | string | `null` | Valid URL | Required if type is `"openapi"`. URL to the `swagger.json` or `openapi.yaml`. |
| `servers.<id>.command` | string | `null` | — | Required if type is `"mcp"`. Command to spawn the MCP server. |
| `servers.<id>.args` | list[string] | `[]` | — | Arguments to the command. When absent, no arguments are passed. |
| `servers.<id>.env` | map[string, string] | `{}` | — | Extra environment variables. Merged with parent process env; these take precedence. When absent, no extra env vars. |
| `servers.<id>.enabled` | boolean | `true` | — | Whether to include this server in indexing and proxy. When absent, the server is enabled. |
| `servers.<id>.tags` | list[string] | `[]` | Each: `[a-z0-9_-]{1,32}`. | Tags for static filtering. When absent, the server has no tags. |
| `servers.<id>.display_name` | string | `null` | Max 128 chars. | Human label. When absent, `<id>` is used. |

### `[retrieval]` — Retrieval Parameters

| Key Path | Type | Default | Validation | Description |
|----------|------|---------|------------|-------------|
| `retrieval.top_k` | integer | `10` | [1, 100] | Max tools returned per query. When absent, 10 tools are returned. |
| `retrieval.min_score` | float | `0.3` | [0.0, 1.0] | Minimum normalized cosine similarity. Tools below this score are excluded. When absent, threshold is 0.3. |
| `retrieval.server_allowlist` | list[string] | `null` | Each must be a configured server ID. | When set, only these servers' tools are searchable. When absent, all enabled servers are included. |
| `retrieval.server_blocklist` | list[string] | `[]` | Each must be a configured server ID. | Tools from these servers are excluded. Blocklist takes precedence over allowlist. When absent, nothing is blocked. |

### `[embedding]` — Embedding Backend

| Key Path | Type | Default | Validation | Description |
|----------|------|---------|------------|-------------|
| `embedding.backend` | enum | `"local"` | `"local"` or `"remote"` | Embedding computation mode. When absent, uses local ONNX inference. |
| `embedding.model_name` | string | `"all-MiniLM-L6-v2"` | — | Model identifier. When absent, uses the default model. |
| `embedding.model_path` | string | `"~/.toolsearch/models/{model_name}"` | Valid directory. | Path to local model files. When absent, derived from model_name. |
| `embedding.dimensions` | integer | `384` | [64, 4096] | Embedding vector dimensionality. Must match the model. When absent, 384 (matching default model). |
| `embedding.remote_url` | string | `null` | Valid HTTPS URL. Required if backend is `"remote"`. | Remote embedding API endpoint. When absent and backend is remote, validation fails. |
| `embedding.remote_api_key` | string | `null` | — | API key for remote backend. Env var `TOOLSEARCH_EMBEDDING_API_KEY` takes precedence. When absent for remote backend, requests are unauthenticated. |
| `embedding.batch_size` | integer | `32` | [1, 512] | Texts per embedding batch. When absent, batches of 32. |

### `[proxy]` — Proxy Mode Settings

| Key Path | Type | Default | Validation | Description |
|----------|------|---------|------------|-------------|
| `proxy.mode` | enum | `"filtered"` | `"filtered"` or `"passthrough"` | Operating mode. `"passthrough"` disables semantic filtering (debugging). When absent, filtering is active. |
| `proxy.context_ttl_seconds` | integer | `300` | [1, 3600] | Seconds before query context expires. When absent, context lives for 5 minutes. |
| `proxy.fallback_on_no_context` | enum | `"all"` | `"all"`, `"none"`, `"tagged"` | Behavior when no query context is set. When absent, all tools are returned. |
| `proxy.fallback_tags` | list[string] | `[]` | Each: `[a-z0-9_-]{1,32}` | Tags to match when `fallback_on_no_context` is `"tagged"`. When absent and mode is `"tagged"`, no tools match (equivalent to `"none"`). |
| `proxy.sidecar_port` | integer | `null` | [1024, 65535] | HTTP sidecar port for context setting. When absent, no sidecar is started. |
| `proxy.namespace_collisions` | boolean | `false` | — | When `true`, prefix tool names with `{server_id}/` in `tools/list` responses to disambiguate same-named tools. When absent, original names are preserved. |

### `[index]` — Indexing Behavior

| Key Path | Type | Default | Validation | Description |
|----------|------|---------|------------|-------------|
| `index.max_parallel_servers` | integer | `4` | [1, 16] | Max servers indexed concurrently. When absent, 4 servers in parallel. |
| `index.server_timeout_seconds` | integer | `30` | [5, 300] | Timeout for server initialization during indexing. When absent, 30 seconds. |
| `index.auto_reindex_on_change` | boolean | `true` | — | Whether to automatically re-index when `tools/list_changed` is received in proxy mode. When absent, auto re-index is enabled. |

### `[logging]` — Audit Logging

| Key Path | Type | Default | Validation | Description |
|----------|------|---------|------------|-------------|
| `logging.discovery_log` | string | `"~/.toolsearch/logs/discovery.jsonl"` | Valid file path (parent directory must exist or be creatable). | Path to the discovery audit log. When absent, uses default path. |
| `logging.index_log` | string | `"~/.toolsearch/logs/index.jsonl"` | Valid file path. | Path to the index mutation log. When absent, uses default path. |
| `logging.enabled` | boolean | `true` | — | Master switch for audit logging. When absent, logging is enabled. |
| `logging.max_file_size_mb` | integer | `100` | [1, 10000] | Max log file size before rotation. When absent, rotates at 100MB. |
| `logging.max_files` | integer | `5` | [1, 100] | Number of rotated log files to keep. When absent, keeps 5 files. |

## 6.3 Precedence Order

Configuration values are resolved in the following order (highest precedence first):

1. **CLI flags** (e.g., `--top-k 5` overrides `retrieval.top_k`).
2. **Environment variables** with `TOOLSEARCH_` prefix. Nested keys use `__` as separator (e.g., `TOOLSEARCH_RETRIEVAL__TOP_K=5`).
3. **Configuration file** (`~/.toolsearch/config.toml`).
4. **Built-in defaults** (as documented in the tables above).

## 6.4 Dynamic Reload Semantics

ToolSearch does NOT support dynamic configuration reload. Changes to `config.toml` take effect only when:
- A new CLI command is invoked.
- The proxy is restarted.

Rationale: The proxy's state (loaded model, server connections, index) is tightly coupled to configuration. Hot-reloading would require graceful re-initialization of all components, which adds complexity disproportionate to the benefit for a local, single-user tool.

## 6.5 Configuration Quick Reference (Flat List)

```
servers.<id>.command          string     (required)
servers.<id>.args             [string]   []
servers.<id>.env              {k: v}     {}
servers.<id>.enabled          bool       true
servers.<id>.tags             [string]   []
servers.<id>.display_name     string     null
retrieval.top_k               int        10
retrieval.min_score           float      0.3
retrieval.server_allowlist    [string]   null
retrieval.server_blocklist    [string]   []
embedding.backend             enum       "local"
embedding.model_name          string     "all-MiniLM-L6-v2"
embedding.model_path          string     ~/.toolsearch/models/{model_name}
embedding.dimensions          int        384
embedding.remote_url          string     null
embedding.remote_api_key      string     null
embedding.batch_size          int        32
proxy.mode                    enum       "filtered"
proxy.context_ttl_seconds     int        300
proxy.fallback_on_no_context  enum       "all"
proxy.fallback_tags           [string]   []
proxy.sidecar_port            int        null
proxy.namespace_collisions    bool       false
index.max_parallel_servers    int        4
index.server_timeout_seconds  int        30
index.auto_reindex_on_change  bool       true
logging.discovery_log         string     ~/.toolsearch/logs/discovery.jsonl
logging.index_log             string     ~/.toolsearch/logs/index.jsonl
logging.enabled               bool       true
logging.max_file_size_mb      int        100
logging.max_files             int        5
```

---

# 7. Error Handling

## 7.1 Error Taxonomy

Errors are organized by subsystem and severity. All errors include:
- **Error code** (alphanumeric, e.g., `CONFIG_INVALID`)
- **HTTP-like status** (if applicable to the HTTP sidecar)
- **Recovery behavior** (automatic, manual, or fatal)
- **Visibility** (operator logs, user-facing message, silent)

### Ingestion Errors

| Code | Severity | Trigger | Recovery | Visibility |
|------|----------|---------|----------|-----------|
| `CONFIG_INVALID` | FATAL | Configuration file is malformed (invalid TOML, missing required fields, type mismatch). | Operator must fix the config file and re-run the command. | User-facing error with path and line number of the problem. |
| `EMBEDDING_LOAD_FAILED` | FATAL | Embedding model file is missing or corrupt. | Operator must re-download the model (`tool-search bootstrap` or delete `~/.toolsearch/models/` to trigger re-download on next run). | User-facing error with model name and path. |
| `EMBEDDING_INFERENCE_ERROR` | WARN | Embedding computation fails for a single batch (OOM, numerical error). | Retry the batch with reduced batch size. If all retries fail, fail the entire indexing operation. | Logged; operation may fail with partial index (DEGRADED state). |
| `SERVER_SPAWN_FAILED` | WARN | Server process fails to start (command not found, permission denied). | Mark the server as FAILED. Skip it and continue with other servers. Indexing completes in DEGRADED state. | Logged with the server ID and error details. Included in indexing summary. |
| `SERVER_TIMEOUT` | WARN | MCP `initialize` response not received within `index.server_timeout_seconds`. | Terminate the server process. Mark as FAILED and continue. | Logged; included in indexing summary. |
| `MCP_PROTOCOL_ERROR` | WARN | Server response is malformed MCP (invalid JSON, missing fields). | Log the error, skip the tool, continue processing. | Logged; tool is not indexed. |
| `TOOL_DUPLICATE` | INFO | Two servers expose tools with identical names (after server ID namespacing). | Both tools are indexed with distinct `tool_id` values. Both may appear in results. | Silent (no user action needed). |
| `NO_SERVERS` | FATAL | Configuration contains no enabled servers. | Operator must configure at least one server and re-run. | User-facing error listing configured servers and their `enabled` status. |

### Retrieval Errors

| Code | Severity | Trigger | Recovery | Visibility |
|------|----------|---------|----------|-----------|
| `INDEX_EMPTY` | INFO | Retrieval is attempted but the index contains zero tools. | Return empty `ToolResult`. Proxy returns empty `tools/list`. | Silent (expected behavior for cold start). |
| `EMBEDDING_UNAVAILABLE` | ERROR | Embedding backend is not loaded (e.g., model load failed during init). | If in proxy mode, fall back to `fallback_on_no_context` behavior. If in search-tool mode, return error. | Logged; user-facing message if in search-tool mode. |
| `CONTEXT_EXPIRED` | INFO | Proxy context TTL has elapsed. | Switch to `fallback_on_no_context` behavior for next `tools/list` call. | Silent. |
| `NO_MATCHES` | INFO | Query returns no results above `min_score`. | Return empty `ToolResult` or `ToolResult` with empty tools list. | Silent (expected behavior). |
| `FILTERED_TO_EMPTY` | INFO | Retrieval finds matches, but server allowlist/blocklist filters them all out. | Return empty `ToolResult`. | Silent. |

### Proxy Errors

| Code | Severity | Trigger | Recovery | Visibility |
|------|----------|---------|----------|-----------|
| `UPSTREAM_SERVER_UNREACHABLE` | ERROR | `tools/call` is routed to a server but the server is DISCONNECTED or FAILED. | Log the error. Proxy returns an MCP error response to the client. The server's reconnection logic (backoff/retry) is triggered. | Logged; user sees MCP error. |
| `TOOL_NOT_FOUND` | ERROR | Client calls `tools/call` with a tool name that was never indexed (stale reference or typo). | Return MCP error response. Do not route to any server. | Logged; user sees MCP error. |
| `AMBIGUOUS_TOOL_ROUTING` | WARN | `tools/call` references a tool name that exists in multiple servers and there is no context to disambiguate. | Route to the first server in configuration order. | Logged. |
| `PROXY_STDIO_ERROR` | FATAL | stdin/stdout of the proxy is broken (pipe closed, I/O error). | Proxy process exits immediately. | Logged to stderr before exit. |
| `MCP_MESSAGE_MALFORMED` | ERROR | Client sends malformed MCP JSON-RPC message. | Proxy returns a JSON-RPC error response (code -32700 or -32600). | Logged. |

### Bootstrap Errors

| Code | Severity | Trigger | Recovery | Visibility |
|------|----------|---------|----------|-----------|
| `BOOTSTRAP_TARGET_NOT_FOUND` | ERROR | Target runtime (e.g., `claude-code`) is not installed or its config file cannot be located. | Operator must install the target runtime or specify an alternate config path. | User-facing error with suggested paths to check. |
| `BOOTSTRAP_CONFIG_INVALID` | ERROR | Target's configuration file is malformed (invalid JSON, unreadable). | Operator must fix the config file manually, or ToolSearch can attempt a backup-and-rewrite. | User-facing error with the config path and details. |
| `BOOTSTRAP_WRITE_FAILED` | ERROR | ToolSearch cannot write to the target's config file (permissions). | Operator must grant write permissions to the file. | User-facing error with the config path. |

## 7.2 Retry Policies

### Automatic Retry

- **Server reconnection in proxy mode** (Section 4.3): 1s, 2s, 4s, 8s, up to 30s max. After 3 consecutive failures, stop retrying until manual restart.
- **Embedding batch retry**: Reduce batch size by half and retry. Up to 3 attempts. If all fail, fail the entire index operation.
- **HTTP sidecar requests**: Timeout 5s per request. No automatic retry (client retries if needed).

### Manual Retry (Operator Action Required)

- **Server spawn failures**: Operator must diagnose (is the command valid? does the process start manually?). Run `tool-search index` again.
- **Configuration errors**: Operator must edit `~/.toolsearch/config.toml` and re-run the command.
- **Model download failure**: Operator can delete `~/.toolsearch/models/` and re-run `tool-search index` to retry download.

## 7.3 Fatal vs. Logged-and-Ignored

| Scenario | Behavior |
|----------|----------|
| Embedding fails for one tool in a batch. | Log the tool ID, skip it, continue indexing. Result: DEGRADED (partial index). |
| All tools fail to embed. | Fail the entire indexing operation. Exit code 1. |
| One of N servers fails during indexing. | Log the server failure, continue with other servers. Result: DEGRADED (partial index). Exit code 2. |
| All servers fail during indexing. | Fail the entire operation. Exit code 1. |
| Query context expires mid-session. | Silent transition to fallback behavior. No error. |
| `tools/call` targets a tool from a disconnected server. | Return MCP error. Do not fail the proxy. |
| Audit log write fails. | Log to stderr (best effort), but do not fail the operation. |

## 7.4 Error Response Formats

### CLI Errors

```bash
# Fatal error (exit code 1)
$ tool-search index
Error [CONFIG_INVALID]: Configuration file is malformed.
  Path: ~/.toolsearch/config.toml
  Details: Invalid TOML syntax at line 42: unexpected character ']'
  
# Partial failure (exit code 2)
$ tool-search index
Index complete: 145 tools from 7 servers (2 servers failed).
  Failed:
    - kubernetes (SERVER_SPAWN_FAILED): command 'kubectl-mcp' not found
    - datadog (SERVER_TIMEOUT): initialization timed out after 30s
  Degraded index written to ~/.toolsearch/index/
  Run 'tool-search status' for details.
```

### MCP Error Responses (Proxy and Search Tool)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32601,
    "message": "Method not found",
    "data": {
      "toolsearch_error": "TOOL_NOT_FOUND",
      "details": "Tool 'unknown_tool' was not found in the index."
    }
  }
}
```

### HTTP Sidecar Error Responses

```json
{
  "timestamp": "2026-03-07T18:35:00Z",
  "status": 400,
  "error": "INVALID_REQUEST",
  "message": "Request body must contain 'query' field"
}
```

---

# 8. Integration Protocol

## 8.1 MCP Proxy Protocol

ToolSearch acts as an MCP server to its downstream client and an MCP client to its upstream servers.

### Proxy Initialization

When the MCP client (e.g., Claude Code) starts the proxy, the proxy:

1. Loads configuration from `~/.toolsearch/config.toml`.
2. Loads the embedding model (or validates the remote endpoint).
3. For each configured, enabled server: spawns the server process and subscribes to `notifications/tools/list_changed`.
4. Responds to the client's MCP `initialize` request with:
   ```json
   {
     "jsonrpc": "2.0",
     "id": 1,
     "result": {
       "protocolVersion": "2024-11-05",
       "capabilities": {
         "tools": {
           "listChanged": true
         }
       },
       "serverInfo": {
         "name": "ToolSearch",
         "version": "1.0.0"
       }
     }
   }
   ```

The proxy advertises `tools.listChanged` capability because it supports `notifications/tools/list_changed` from upstream servers and re-indexes incrementally.

### `tools/list` Request (Filtered)

**Client request**:
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list",
  "params": {
    "cursor": null
  }
}
```

**Proxy behavior**:
1. Check the current query context state.
2. If CONTEXT_ACTIVE: call the QueryEngine with the active query.
3. If IDLE: apply `fallback_on_no_context` logic.
4. Retrieve full tool definitions from the index for each selected tool.
5. Return response:
   ```json
   {
     "jsonrpc": "2.0",
     "id": 2,
     "result": {
       "tools": [
         {
           "name": "k8s_get_logs",
           "title": "Get Pod Logs",
           "description": "Fetch logs from a Kubernetes pod...",
           "inputSchema": { ... }
         }
       ],
       "nextCursor": null
     }
   }
   ```

The proxy **ignores the `cursor` parameter** and returns the complete filtered set without pagination. Rationale: the filtered set is typically ≤20 tools; pagination adds complexity without proportional benefit in this case.

### `tools/call` Request (Passthrough with Routing)

**Client request**:
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "k8s_get_logs",
    "arguments": { "pod_name": "my-pod", "namespace": "default" }
  }
}
```

**Proxy behavior**:
1. Look up `k8s_get_logs` in the internal routing table (maintained from indexing): find that it comes from the `kubernetes` server.
2. Route the `tools/call` request to the `kubernetes` server's stdio.
3. Forward the server's response back to the client verbatim.

If the tool name is ambiguous (exists in multiple servers) and no context is available to disambiguate, route to the first server in configuration order and log a WARN.

### Message Passthrough (All Other Messages)

For any message other than `tools/list` responses:
- Forward the message to its destination without modification.
- Preserve message ordering.
- Do not inspect or transform the payload.

Examples of passthrough messages: `tools/call`, `resources/list`, `resources/read`, `prompts/list`, `prompts/get`, and any future MCP messages.

### `notifications/tools/list_changed`

**Upstream notification**:
```json
{
  "jsonrpc": "2.0",
  "method": "notifications/tools/list_changed",
  "params": null
}
```

When the proxy receives this notification from an upstream server:
1. Identify which server sent it (based on stdio connection).
2. Re-fetch `tools/list` from that server.
3. Update the index incrementally (Section 5.2: Incremental Update).
4. If configured, forward the notification downstream (optional; most clients do not use it).

## 8.2 Search Tool Protocol

The search tools (`toolsearch_find` and `toolsearch_context`) are exposed as standard MCP tools.

### Search Tool Server Registration

The Search Tool Server registers itself as an MCP server in the client's configuration:
```json
{
  "mcpServers": {
    "toolsearch": {
      "command": "tool-search",
      "args": ["search-tool", "--port", "9999"]
    }
  }
}
```

The client treats ToolSearch as a regular MCP server and calls its tools via standard `tools/call` messages.

### `toolsearch_find` Behavior

Full spec in Section 5.5. The tool returns matched tool definitions with their scores, allowing the agent to immediately invoke any of them.

### `toolsearch_context` Behavior

Full spec in Section 5.5. The tool sets the query context for the proxy's next `tools/list` call and returns a confirmation message.

## 8.3 HTTP Sidecar Protocol (Optional)

When `proxy.sidecar_port` is configured, the proxy starts an HTTP server on that port accepting POST requests.

### Context Setting Endpoint

**Endpoint**: `POST /context`

**Request body**:
```json
{
  "query": "read logs from the database",
  "ttl_seconds": 300
}
```

**Success response** (200 OK):
```json
{
  "status": "ok",
  "message": "Context set",
  "expires_at": "2026-03-07T18:40:00Z"
}
```

**Error response** (400 Bad Request):
```json
{
  "status": "error",
  "error": "INVALID_REQUEST",
  "message": "Missing required field 'query'"
}
```

### Health Check Endpoint (Optional)

**Endpoint**: `GET /health`

**Response** (200 OK):
```json
{
  "status": "healthy",
  "index_ready": true,
  "tools_indexed": 145,
  "uptime_seconds": 3600
}
```

## 8.4 Completion Conditions

### For Indexing Operations

**Success**: All servers respond successfully to `tools/list`. All tools are embedded. Index is written.

**Partial success (DEGRADED)**: Some servers failed to respond or had embedding errors, but at least one server's tools are indexed.

**Failure**: All servers failed, or embedding is impossible (model unavailable). Exit code 1.

### For Retrieval Operations

Retrieval always completes (no failures): it returns whatever results are available or an empty set. The only failure condition is if the embedding backend is unavailable, which is caught at initialization time.

### For Proxy Operations

The proxy remains running as long as stdin is open. When the client closes stdin, the proxy exits gracefully (shutdown all server processes, flush audit logs).

## 8.5 Timeout Behavior

| Operation | Timeout | Behavior on Timeout |
|-----------|---------|-------------------|
| Server initialization during `tool-search index` | `index.server_timeout_seconds` (default 30s) | Mark server FAILED, continue with next server. |
| Embedding a batch of texts | 60s (hardcoded) | Reduce batch size, retry. If all retries fail, fail indexing. |
| HTTP sidecar request | 5s | Return HTTP 504 Gateway Timeout. |
| MCP message handling (client → proxy) | 30s (per message) | If no response within 30s, proxy may timeout and close the connection. |

## 8.6 Extension Points

### Custom Embedding Backends

The `EmbeddingBackend` interface is pluggable:
- Implementations provide: `embed(texts: List[str]) → List[Vector]`.
- Default: ONNX-based local inference.
- Extensible to: OpenAI, Cohere, local LLMs (llama.cpp, ollama), or any other endpoint.

### Custom Retrieval Backends

The `IndexReader` interface (ANN search) is pluggable:
- Implementations provide: `search(query_vector, top_k) → List[ScoredTool]`.
- Default: HNSW-based (hnswlib or usearch).
- Extensible to: Faiss, pgvector, or other vector databases.

### Custom Bootstrap Targets

New bootstrap targets can be added by providing a template that maps ToolSearch to the target runtime's native configuration format. No code changes are required; templates are configuration-driven.

## 8.7 Forward Compatibility

- **Unknown MCP methods**: The proxy forwards them unchanged to upstream servers.
- **Unknown fields in messages**: The proxy preserves unknown fields in passthrough messages.
- **Future MCP capabilities**: If a future MCP spec adds new capabilities (e.g., resource discovery), the proxy automatically forwards notifications and messages for those capabilities without modification.
- **Configuration extension**: Unknown keys in `config.toml` are ignored (clients that don't recognize a field skip it).

---

# 9. Observability

## 9.1 Required Log Context Fields

Every log entry MUST include:

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | ISO 8601 | UTC time when the event occurred. |
| `level` | enum | One of: `DEBUG`, `INFO`, `WARN`, `ERROR`, `FATAL`. |
| `component` | string | Which ToolSearch component emitted the log: `collector`, `embedder`, `indexwriter`, `queryengine`, `proxy`, `searchtool`, `cli`. |
| `event_type` | string | Semantic category: `server_spawn`, `tools_collected`, `embedding_batch`, `index_written`, `query_executed`, `context_set`, `tool_called`, etc. |
| `message` | string | Human-readable description. |

### Optional Context Fields (Logged when Relevant)

| Field | Type | When Logged |
|-------|------|-----------|
| `server_id` | string | Server-related operations (spawn, connect, harvest). |
| `tool_id` | string | Tool-related operations (embed, route, search). |
| `query` | string | Query execution. First 256 chars only; truncated if longer. |
| `latency_ms` | float | Operations with measurable duration (embedding, retrieval, server init). |
| `error_code` | string | When an error occurs (e.g., `SERVER_SPAWN_FAILED`). |
| `error_detail` | string | Error details (e.g., error message from subprocess). |
| `tools_affected` | integer | Number of tools affected by an index mutation or retrieval. |

## 9.2 Audit Logs

In addition to operational logs, ToolSearch writes structured audit logs to track all discovery and index mutation events.

### Discovery Log (`~/.toolsearch/logs/discovery.jsonl`)

One JSONL entry per discovery operation (either proxy `tools/list` request or `toolsearch_find` call).

**Schema**: `DiscoveryEvent` (Section 3.3).

**Retention**: Log rotation based on `logging.max_file_size_mb` and `logging.max_files`. Default: rotate at 100MB, keep 5 files (500MB total).

**Example entry**:
```json
{
  "timestamp": "2026-03-07T18:35:42.123Z",
  "event_type": "proxy_filter",
  "query": "read logs from kubernetes pod",
  "candidates": [
    { "tool_id": "kubernetes::k8s_get_logs", "score": 0.94 },
    { "tool_id": "kubernetes::k8s_describe_pod", "score": 0.87 },
    { "tool_id": "datadog::get_logs", "score": 0.65 }
  ],
  "selected": ["kubernetes::k8s_get_logs", "kubernetes::k8s_describe_pod"],
  "config_snapshot": {
    "top_k": 10,
    "min_score": 0.3,
    "server_allowlist": null,
    "server_blocklist": []
  },
  "latency_ms": 127.5
}
```

### Index Log (`~/.toolsearch/logs/index.jsonl`)

One JSONL entry per index mutation (full rebuild, incremental update, server added/removed).

**Schema**: `IndexEvent` (Section 3.3).

**Retention**: Same as discovery log.

**Example entry**:
```json
{
  "timestamp": "2026-03-07T18:35:00.000Z",
  "event_type": "full_rebuild",
  "server_id": null,
  "tools_added": 145,
  "tools_removed": 0,
  "tools_updated": 0,
  "total_tools": 145,
  "duration_ms": 2340.5
}
```

## 9.3 Runtime Snapshot Schema

The `tool-search status` command returns a JSON snapshot of the current system state.

```json
{
  "timestamp": "2026-03-07T18:35:00Z",
  "version": "1.0.0",
  "uptime_seconds": 3600,
  "index": {
    "state": "READY",
    "total_tools": 145,
    "servers": {
      "kubernetes": {
        "status": "CONNECTED",
        "tools_indexed": 35,
        "last_indexed_at": "2026-03-07T18:00:00Z",
        "last_error": null
      },
      "github": {
        "status": "CONNECTED",
        "tools_indexed": 28,
        "last_indexed_at": "2026-03-07T18:00:00Z",
        "last_error": null
      },
      "slack": {
        "status": "FAILED",
        "tools_indexed": 0,
        "last_indexed_at": null,
        "last_error": "SERVER_SPAWN_FAILED: command 'slack-mcp' not found"
      }
    }
  },
  "proxy": {
    "is_running": true,
    "context_active": true,
    "context_query": "read logs",
    "context_expires_at": "2026-03-07T18:40:00Z",
    "uptime_seconds": 1800,
    "requests_served": 42
  },
  "embedding": {
    "backend": "local",
    "model": "all-MiniLM-L6-v2",
    "dimensions": 384,
    "status": "LOADED"
  },
  "storage": {
    "config_path": "~/.toolsearch/config.toml",
    "index_size_bytes": 2457600,
    "logs_size_bytes": 512000,
    "discovery_log_entries": 1240,
    "index_log_entries": 15
  }
}
```

## 9.4 Token/Resource Accounting

ToolSearch tracks resource consumption for transparency and debugging.

### Token Accounting (If Remote Embedding Used)

| Metric | Frequency | Description |
|--------|-----------|-------------|
| `embedding_tokens_sent` | Per batch | Tokens sent to remote embedding API (if using OpenAI or equivalent). Logged with each batch. |
| `embedding_tokens_total` | Per index operation | Total tokens used in a full `tool-search index` run. Printed to CLI. |
| `estimated_cost_usd` | Per index operation | Estimated cost at current API pricing (if remote backend is used). Printed to CLI. |

### Local Resource Accounting

| Metric | Frequency | Description |
|--------|-----------|-------------|
| `memory_peak_mb` | Per operation | Peak memory usage during operation. Measured on process exit or via `ps`. Logged. |
| `embedding_model_size_mb` | Per startup | Size of loaded model on disk. Logged at startup. |
| `index_size_bytes` | Per index write | Total bytes used by the vector index and metadata. Logged with each index operation. |

### Example CLI Output (with Remote Embedding)

```
$ tool-search index
Indexing servers...
  kubernetes: 35 tools
  github: 28 tools
  datadog: 22 tools
  ...
Index complete: 145 tools indexed in 23.4s

Embedding stats:
  Total texts embedded: 145
  Batch size: 32
  Total tokens sent: 18,450
  Estimated cost: $0.0018
  
Resource usage:
  Peak memory: 512 MB
  Index size: 2.4 MB
  Time: 23.4s
```

## 9.5 Optional Observability Surfaces

These are NOT required but recommended for operational visibility:

### Prometheus Metrics (Optional)

If ToolSearch is deployed in an instrumented environment, the following metrics SHOULD be exported:

```
# Retrieval
toolsearch_retrieval_latency_ms{quantile="p50,p95,p99"}
toolsearch_retrieval_top_k_used
toolsearch_retrieval_candidates_filtered

# Indexing
toolsearch_index_tools_total
toolsearch_index_duration_seconds
toolsearch_embedding_batch_duration_seconds

# Proxy
toolsearch_proxy_requests_total{method="tools_list,tools_call,..."}
toolsearch_proxy_context_active{bool}
toolsearch_proxy_upstream_errors_total{server_id,error_code}

# Server Health
toolsearch_upstream_server_status{server_id,status="connected|failed|..."}
```

### Structured Error Reporting (Optional)

If integrated with error tracking (e.g., Sentry), ToolSearch SHOULD send:
- `EMBEDDING_INFERENCE_ERROR` and above
- Server reconnection failures after max retries
- Index operation partial failures

Errors from normal operation (e.g., `CONTEXT_EXPIRED`, `NO_MATCHES`, `NO_SERVERS_REACHABLE`) SHOULD NOT be reported as system errors.

## 9.6 Debugging Aids

### Verbose Logging

When `DEBUG` logging level is enabled (via `--log-level debug` or `TOOLSEARCH_LOG_LEVEL=debug`):
- All embedding operations log the input text and output vector (first 10 dimensions).
- All retrieval operations log the query vector and similarity scores for all candidates (not just top-k).
- All MCP messages are logged (both directions, full JSON).
- Server connection state transitions are logged.

### Dry-Run Mode

`tool-search index --dry-run`:
- Loads configuration and embedding model.
- Connects to servers and fetches tools.
- Computes embeddings.
- Reports what would be indexed but does NOT write to the index.
- Useful for debugging configuration or server connectivity issues without mutating state.

### Log Tail

`tool-search logs --follow`:
- Tails the discovery and index logs in real-time, formatted for human readability.

---

# 10. Portability Invariants

These invariants define the mandatory constraints for any implementation claiming to be ToolSearch-compatible. They use RFC 2119 language (MUST, SHOULD, MAY).

## 10.1 Normative Invariants (MUST)

### Semantic Correctness

1. **Retrieval MUST respect all filters** (Section 5.2): Every tool returned MUST pass the `min_score` threshold, `server_allowlist`, and `server_blocklist` constraints.

2. **Scoring MUST be deterministic**: Given the same query, embedding model, and index state, the system MUST return the same ranked results with the same scores (within floating-point precision).

3. **Tool definitions MUST be preserved verbatim**: No field within a tool definition returned in `tools/list` responses MUST be modified (name, description, inputSchema, etc.). Exception: if `proxy.namespace_collisions` is enabled, tool names MAY be prefixed with `{server_id}/`.

4. **Passthrough MUST be transparent**: All MCP messages except `tools/list` responses MUST be forwarded to their destination without modification or inspection (Section 5.4).

5. **Routing MUST be correct**: A `tools/call` request MUST be routed to the upstream server that originally exposed the tool (as determined by the `tool_id` namespace). If a tool exists in multiple servers (name collision) and no context disambiguates, route to the first configured server.

6. **Index consistency MUST be maintained**: At any point in time, a query against the index MUST return results that are consistent with the most recent successful index operation (not intermediate states mid-update).

### Protocol Compliance

7. **MCP protocol version MUST match client expectations**: The proxy MUST declare and implement the MCP protocol version advertised to the client.

8. **Configuration schema MUST validate**: Any configuration that does not conform to Section 6 MUST be rejected with a clear error message before any operation proceeds.

9. **Error codes MUST be as specified** (Section 7): When an error occurs, the response MUST include an error code from the taxonomy. New error codes may be added in future versions, but existing codes MUST retain their semantics.

10. **Timeout behavior MUST be as specified** (Section 8.5): Operations MUST not exceed their defined timeouts without explicit operator configuration.

### Data Integrity

11. **Audit logs MUST be immutable after writing**: Once an entry is written to `discovery.jsonl` or `index.jsonl`, it MUST NOT be modified or deleted by the system.

12. **Index atomicity MUST be guaranteed**: Index writes MUST be atomic — a partial failure MUST NOT leave the index in a half-updated state. Either the new index is fully written or the old one is preserved.

13. **Embedding vectors MUST match their model**: A vector in the index MUST have a dimensionality matching the embedding model that produced it. Mixing vectors from different models in the same index is NOT allowed.

### Security & Privacy

14. **No data exfiltration**: Tool definitions and user queries MUST NOT be sent to any external service without explicit operator configuration and consent (Section 4.0: Local-first principle).

15. **Credentials MUST not be logged**: If a server's `env` contains credentials (API keys, tokens, passwords), they MUST NOT appear in logs or audit trails.

## 10.2 Illustrative Invariants (SHOULD)

These are best practices that conform implementations SHOULD follow but are not strictly required for correctness.

### Performance

1. **Retrieval latency SHOULD stay below 500ms** (end-to-end, embedding + search) on commodity hardware (4-core CPU, ≤512MB memory usage).

2. **Index writes SHOULD not block queries**: While an index update is in progress, the old index SHOULD remain queryable.

3. **Batch embedding SHOULD be efficient**: Embedding requests SHOULD be grouped into batches to amortize model-loading overhead.

### Usability

4. **Error messages SHOULD be actionable**: Errors SHOULD include suggestions for resolution (e.g., "run `tool-search index` to rebuild the index").

5. **Configuration SHOULD have sensible defaults**: A user running `tool-search init` with no prior experience SHOULD be able to start using the tool immediately without extensive configuration.

6. **CLI SHOULD provide progress feedback**: Long-running operations (indexing, downloading models) SHOULD print progress updates.

### Reliability

7. **Server reconnection SHOULD use exponential backoff** (as specified in Section 4.3) to avoid overwhelming failed servers.

8. **Index operations SHOULD not be aborted by transient failures**: A single server's timeout SHOULD not prevent indexing of other servers.

9. **Audit logs SHOULD be human-readable**: Log formats SHOULD include timestamps, clear event descriptions, and sufficient context for debugging.

## 10.3 Optional Capabilities (MAY)

Implementations MAY support these features without breaking compatibility:

1. **LLM-assisted re-ranking**: After embedding-based retrieval, MAY apply a small LLM to re-rank results.

2. **Usage-based ranking boosts**: MAY track which tools are frequently invoked and boost their scores in subsequent queries.

3. **Multi-modal matching**: MAY support matching tools based on code context, file types, or images in addition to text queries.

4. **Remote/shared indexes**: MAY allow multiple machines to share a pre-built tool index.

5. **Custom retrieval backends**: MAY support alternative vector stores (Faiss, pgvector, etc.) beyond the default HNSW.

6. **Resource and prompt indexing**: MAY extend semantic search to MCP resources and prompts (currently tools-only).

7. **Plugin architecture**: MAY allow third-party plugins for custom retrieval, embedding, or filtering logic.

8. **Real-time tool updates**: MAY support streaming tool definition updates rather than event-driven snapshots.

## 10.4 Forward Compatibility

Implementations MUST support forward compatibility for:

1. **Unknown MCP messages**: Any MCP method not recognized by ToolSearch MUST be forwarded unchanged.

2. **Unknown configuration keys**: Any key in `config.toml` not recognized by the implementation MUST be ignored (not treated as an error).

3. **Unknown fields in MCP responses**: Any field in an MCP tool definition not recognized by the implementation MUST be preserved and forwarded.

4. **Future MCP capabilities**: If the MCP specification adds new capabilities or messages, ToolSearch MUST forward them transparently without requiring code changes.

## 10.5 Breaking Changes

The following changes would break compatibility with existing deployments and MUST only occur in a major version change:

1. Changing the semantic meaning of an existing configuration key.
2. Adding a new required (non-default) configuration field.
3. Changing the error codes in the taxonomy (removing or redefining).
4. Changing the format of audit log entries in a non-backward-compatible way.
5. Changing the embedding vector dimensionality of the default model.
6. Removing support for MCP protocol versions that were previously supported.

## 10.6 Semantic Versioning

ToolSearch SHOULD use semantic versioning: `MAJOR.MINOR.PATCH`

- **MAJOR**: Breaking changes (see Section 10.5).
- **MINOR**: New features, new optional configuration fields, new error codes.
- **PATCH**: Bug fixes, performance improvements, documentation.

## 10.7 Implementation Completeness

A conforming implementation MUST provide:

- [x] Semantic indexing and retrieval (Sections 5.2, 3.1).
- [x] MCP proxy (Sections 5.3, 8.1).
- [x] Search tool (Sections 5.5, 8.2).
- [x] Local configuration file (`config.toml`).
- [x] Audit logging (Section 9.2).
- [x] Error handling (Section 7).
- [x] CLI interface (at least: `init`, `index`, `proxy`, `status`).

A conforming implementation MAY omit:

- [ ] Bootstrap connectors for specific runtimes (Claude Code, Cursor, etc.) — optional convenience features.
- [ ] HTTP sidecar (only if using search tool mode or companion MCP tool for context).
- [ ] Prometheus metrics export.
- [ ] GUI or web dashboard.

---

# 11. Quick Implementation Reference

This section is a flat appendix for developers implementing ToolSearch. All config, states, error codes, and endpoints are listed for fast lookup.

## 11.1 All Configuration Keys

**Quick reference flat list** (for copy-paste):

```
servers.<id>.command          string     (required)
servers.<id>.args             [string]   []
servers.<id>.env              {k: v}     {}
servers.<id>.enabled          bool       true
servers.<id>.tags             [string]   []
servers.<id>.display_name     string     null

retrieval.top_k               int        10
retrieval.min_score           float      0.3
retrieval.server_allowlist    [string]   null
retrieval.server_blocklist    [string]   []

embedding.backend             enum       "local"
embedding.model_name          string     "all-MiniLM-L6-v2"
embedding.model_path          string     ~/.toolsearch/models/{model_name}
embedding.dimensions          int        384
embedding.remote_url          string     null
embedding.remote_api_key      string     null
embedding.batch_size          int        32

proxy.mode                    enum       "filtered"
proxy.context_ttl_seconds     int        300
proxy.fallback_on_no_context  enum       "all"
proxy.fallback_tags           [string]   []
proxy.sidecar_port            int        null
proxy.namespace_collisions    bool       false

index.max_parallel_servers    int        4
index.server_timeout_seconds  int        30
index.auto_reindex_on_change  bool       true

logging.discovery_log         string     ~/.toolsearch/logs/discovery.jsonl
logging.index_log             string     ~/.toolsearch/logs/index.jsonl
logging.enabled               bool       true
logging.max_file_size_mb      int        100
logging.max_files             int        5
```

## 11.2 All Error Codes

**By subsystem:**

**Ingestion:**
- `CONFIG_INVALID` — Configuration file is malformed.
- `EMBEDDING_LOAD_FAILED` — Model cannot be loaded.
- `EMBEDDING_INFERENCE_ERROR` — Embedding computation fails for a batch.
- `SERVER_SPAWN_FAILED` — Server process fails to start.
- `SERVER_TIMEOUT` — Server init timeout (30s default).
- `MCP_PROTOCOL_ERROR` — Server response is malformed MCP.
- `TOOL_DUPLICATE` — Two servers expose tools with identical names.
- `NO_SERVERS` — Configuration contains no enabled servers.

**Retrieval:**
- `INDEX_EMPTY` — Index contains zero tools.
- `EMBEDDING_UNAVAILABLE` — Embedding backend not loaded.
- `CONTEXT_EXPIRED` — Proxy context TTL elapsed.
- `NO_MATCHES` — Query returns no results above threshold.
- `FILTERED_TO_EMPTY` — Filters eliminate all candidates.

**Proxy:**
- `UPSTREAM_SERVER_UNREACHABLE` — Target server is DISCONNECTED/FAILED.
- `TOOL_NOT_FOUND` — Tool name in `tools/call` not found.
- `AMBIGUOUS_TOOL_ROUTING` — Tool exists in multiple servers, no context to disambiguate.
- `PROXY_STDIO_ERROR` — stdin/stdout broken.
- `MCP_MESSAGE_MALFORMED` — Client sends malformed JSON-RPC.

**Bootstrap:**
- `BOOTSTRAP_TARGET_NOT_FOUND` — Target runtime not installed.
- `BOOTSTRAP_CONFIG_INVALID` — Target's config file is malformed.
- `BOOTSTRAP_WRITE_FAILED` — Cannot write to target's config file.

## 11.3 All Index States

```
UNINITIALIZED —→ EMPTY —→ READY ⟷ UPDATING ←→ DEGRADED
                                       ↑            ↓
                                       └────────────┘
```

| State | Queryable | Proxy Operable | Next Action |
|-------|-----------|----------------|------------|
| UNINITIALIZED | No | No | `tool-search init` |
| EMPTY | No | No | `tool-search index` |
| READY | Yes | Yes | (stable) |
| UPDATING | Yes (stale data) | Yes (stale data) | Wait or `tool-search index` completes |
| DEGRADED | Yes (partial) | Yes (partial) | `tool-search index` to rebuild |

## 11.4 All Proxy Session States

| State | Context Set? | `tools/list` Behavior |
|-------|--------------|----------------------|
| IDLE | No | Return `fallback_on_no_context` set (all/none/tagged) |
| CONTEXT_ACTIVE | Yes, TTL counting | Return filtered tools for active query; reset TTL on new context |

## 11.5 All CLI Subcommands

| Command | Purpose | Exit Codes |
|---------|---------|-----------|
| `tool-search init` | Initialize `~/.toolsearch/`. Create config, dirs, download model. | 0 (success), 1 (error) |
| `tool-search index` | Rebuild/update index from configured servers. | 0 (all success), 1 (failure), 2 (partial success/DEGRADED) |
| `tool-search proxy [--servers <id>]` | Start the MCP proxy wrapper. Forward stdin/stdout. | 0 (clean shutdown), 1 (error) |
| `tool-search search <query> [--top-k 5] [--servers <id>]` | One-shot semantic search. Print results. | 0 (success), 1 (error) |
| `tool-search bootstrap <target>` | Generate config snippet for target runtime. | 0 (success), 1 (error) |
| `tool-search status` | Print system status snapshot as JSON. | 0 (success), 1 (error) |
| `tool-search config` | View/edit configuration. Subcommands: `show`, `set <key> <value>`, `validate`. | 0 (success), 1 (error) |
| `tool-search logs [--follow] [--level <level>]` | Tail or dump audit logs. | 0 (success), 1 (error) |

## 11.6 All MCP Tool Interfaces

### `toolsearch_find`

```
Input:
  query: string (required) — natural language description
  top_k: integer (default 5) — max results
  min_score: float (default uses config) — score threshold
  servers: [string] (optional) — server filter

Output:
  [{ server_id, name, description, inputSchema, score }, ...]
```

### `toolsearch_context`

```
Input:
  query: string (required) — intent for semantic filtering
  ttl_seconds: integer (default 300) — context lifetime

Output:
  "Context set: '{query}' (expires: {timestamp})"
```

## 11.7 All HTTP Sidecar Endpoints

| Endpoint | Method | Purpose | Status Code |
|----------|--------|---------|-----------|
| `/context` | POST | Set query context for proxy. Body: `{ "query": "...", "ttl_seconds": 300 }` | 200 (success), 400 (bad request), 500 (error) |
| `/health` | GET | Health check. Returns JSON with system status. | 200 (healthy), 503 (unhealthy) |

## 11.8 All Event Types (Logs)

**Discovery events** (`discovery.jsonl`):
- `proxy_filter` — `tools/list` request filtered by proxy.
- `search_tool` — `toolsearch_find` call.
- `cli_search` — `tool-search search` CLI command.

**Index events** (`index.jsonl`):
- `full_rebuild` — Full re-indexing of all servers.
- `incremental_update` — Single server's tools updated.
- `server_added` — New server configured and indexed.
- `server_removed` — Server removed from configuration.
- `tool_added` — New tool added to index.
- `tool_removed` — Tool removed from index.

## 11.9 Example config.toml

```toml
# ToolSearch Configuration

[servers.kubernetes]
command = "kubectl"
args = ["mcp", "server"]
env = { KUBECONFIG = "~/.kube/config" }
enabled = true
tags = ["devops", "infrastructure"]
display_name = "Kubernetes"

[servers.github]
command = "npx"
args = ["@modelcontextprotocol/server-github"]
env = { GITHUB_TOKEN = "ghp_..." }
enabled = true
tags = ["productivity", "vcs"]

[retrieval]
top_k = 10
min_score = 0.3
server_allowlist = null
server_blocklist = []

[embedding]
backend = "local"
model_name = "all-MiniLM-L6-v2"
dimensions = 384
batch_size = 32

[proxy]
mode = "filtered"
context_ttl_seconds = 300
fallback_on_no_context = "all"
sidecar_port = null

[index]
max_parallel_servers = 4
server_timeout_seconds = 30
auto_reindex_on_change = true

[logging]
enabled = true
max_file_size_mb = 100
max_files = 5
```

## 11.10 Example Discovery Event (JSON)

```json
{
  "timestamp": "2026-03-07T18:35:42.123Z",
  "event_type": "proxy_filter",
  "query": "get logs from kubernetes",
  "candidates": [
    { "tool_id": "kubernetes::k8s_get_logs", "score": 0.94 },
    { "tool_id": "kubernetes::k8s_describe_pod", "score": 0.87 },
    { "tool_id": "datadog::query_logs", "score": 0.62 }
  ],
  "selected": ["kubernetes::k8s_get_logs", "kubernetes::k8s_describe_pod"],
  "config_snapshot": {
    "top_k": 10,
    "min_score": 0.3,
    "server_allowlist": null,
    "server_blocklist": []
  },
  "latency_ms": 127.5
}
```

## 11.11 Example Status Snapshot (JSON)

```json
{
  "timestamp": "2026-03-07T18:35:00Z",
  "version": "1.0.0",
  "uptime_seconds": 3600,
  "index": {
    "state": "READY",
    "total_tools": 145,
    "servers": {
      "kubernetes": {
        "status": "CONNECTED",
        "tools_indexed": 35,
        "last_indexed_at": "2026-03-07T18:00:00Z"
      },
      "github": {
        "status": "CONNECTED",
        "tools_indexed": 28,
        "last_indexed_at": "2026-03-07T18:00:00Z"
      }
    }
  },
  "proxy": {
    "is_running": true,
    "context_active": true,
    "context_query": "read logs"
  },
  "embedding": {
    "backend": "local",
    "model": "all-MiniLM-L6-v2",
    "status": "LOADED"
  },
  "storage": {
    "index_size_bytes": 2457600,
    "discovery_log_entries": 1240
  }
}
```

## 11.12 Environment Variables (Overrides)

Prefix: `TOOLSEARCH_`
Separator for nested keys: `__` (double underscore)

Examples:
- `TOOLSEARCH_RETRIEVAL__TOP_K=5` → `retrieval.top_k = 5`
- `TOOLSEARCH_EMBEDDING__BACKEND=remote` → `embedding.backend = "remote"`
- `TOOLSEARCH_EMBEDDING__API_KEY=sk-xxx` → `embedding.remote_api_key = "sk-xxx"`
- `TOOLSEARCH_LOG_LEVEL=debug` → Enable DEBUG logging

## 11.13 File Structure

```
~/.toolsearch/
├── config.toml                    # Main configuration file
├── index/
│   ├── vectors.db                 # Vector store (HNSW, Faiss, or equivalent)
│   └── metadata.db                # Tool records, server mapping, index version
├── models/
│   └── all-MiniLM-L6-v2/          # Downloaded embedding model
│       ├── config.json
│       ├── tokenizer.json
│       └── model.onnx
└── logs/
    ├── discovery.jsonl            # Audit: all discovery operations
    └── index.jsonl                # Audit: all index mutations
```

## 11.14 Timing Budgets

| Operation | Budget | Notes |
|-----------|--------|-------|
| Embedding a query (CPU, default model) | 400ms | On commodity hardware |
| Vector similarity search (≤1000 tools) | 100ms | ANN lookup only, excludes embedding |
| Full index rebuild (≤500 tools) | 60 seconds | Includes server connection, tool collection, embedding, writing |
| Server initialization (MCP handshake) | 30 seconds | Configurable via `index.server_timeout_seconds` |
| Context TTL (proxy) | 300 seconds | Configurable via `proxy.context_ttl_seconds` |
| HTTP sidecar request timeout | 5 seconds | Hardcoded |
| MCP message handling | 30 seconds | Soft timeout; implementation-dependent |

## 11.15 Concurrency Limits

| Resource | Default Limit | Configurable |
|----------|---------------|-------------|
| Parallel server indexing | 4 servers | Yes, `index.max_parallel_servers` |
| Embedding batch size | 32 texts | Yes, `embedding.batch_size` |
| Concurrent proxy requests | Unlimited | No (single-threaded stdin/stdout) |
| Concurrent retrieval queries | Unlimited (serialized at embedder) | No |
