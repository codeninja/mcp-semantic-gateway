# ToolSearch — Task Breakdown

**Spec**: `docs/specs/SPEC.md` (v1.0.0)
**Plan**: `docs/plans/IMPLEMENTATION_PLAN.md`
**Date**: 2026-03-07

---

## Task 1: Project Scaffold
**Phase:** 1
**Depends on:** none
**Effort:** S
**Priority:** P0

### Description
Create the Python project structure with `pyproject.toml`, source layout (`src/toolsearch/`), test layout, and the Makefile. Set up uv as the package manager. Wire the CLI entry point (`tool-search`) to `toolsearch.cli.main:app`.

### Acceptance Criteria
- [ ] `pyproject.toml` with project metadata, Python ≥3.12, entry point `tool-search`
- [ ] `src/toolsearch/__init__.py` exists with `__version__`
- [ ] All package directories from the plan exist with `__init__.py`
- [ ] `Makefile` with targets: `setup`, `dev`, `test`, `lint`, `format`
- [ ] `uv sync` installs the project in editable mode
- [ ] `tool-search --help` prints the CLI help (empty subcommands OK)

### Files to Create/Modify
- `pyproject.toml` — project definition, dependencies, entry points
- `Makefile` — task runner
- `src/toolsearch/__init__.py` — version
- `src/toolsearch/cli/main.py` — typer app skeleton
- All `__init__.py` files per directory structure in plan

### Tests Required
- [ ] `tool-search --help` exits 0
- [ ] `import toolsearch` succeeds
- [ ] `toolsearch.__version__` is a valid semver string

### Implementation Notes
- Use `uv` for everything (not pip). `uv init`, `uv add`.
- CLI framework: `typer[all]` (includes rich for nice output).
- Dev deps: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`.
- Project uses `src/` layout per Python best practices.

---

## Task 2: Configuration Loading + Validation
**Phase:** 1
**Depends on:** Task 1
**Effort:** M
**Priority:** P0

### Description
Implement TOML config loading from `~/.toolsearch/config.toml` with Pydantic validation. Support environment variable overrides (`TOOLSEARCH_` prefix, `__` separator). Implement the full config schema from Spec Section 6.

### Acceptance Criteria
- [ ] `ToolSearchConfig` Pydantic model covers all fields from Spec Section 6.2
- [ ] TOML file is parsed and validated; invalid files raise `ConfigInvalid` with path + line
- [ ] Env var overrides work: `TOOLSEARCH_RETRIEVAL__TOP_K=5` overrides `retrieval.top_k`
- [ ] Defaults match spec exactly (top_k=10, min_score=0.3, etc.)
- [ ] Missing optional fields use documented defaults
- [ ] Config precedence: CLI flags > env vars > TOML file > built-in defaults

### Files to Create/Modify
- `src/toolsearch/config/models.py` — Pydantic models (ServerConfig, RetrievalConfig, EmbeddingConfig, ProxyConfig, IndexConfig, LoggingConfig, ToolSearchConfig)
- `src/toolsearch/config/loader.py` — load_config() function
- `src/toolsearch/config/defaults.py` — DEFAULT_CONFIG_PATH, default values
- `src/toolsearch/models/errors.py` — ConfigInvalid exception

### Tests Required
- [ ] Valid TOML parses correctly with all fields populated
- [ ] Missing optional fields get correct defaults
- [ ] Invalid TOML raises ConfigInvalid with helpful message
- [ ] Env var override for nested key (e.g., retrieval.top_k)
- [ ] Env var override for top-level key
- [ ] Type validation rejects wrong types (string where int expected)
- [ ] Range validation rejects out-of-bounds (top_k=0, top_k=101)
- [ ] Server ID format validation (`[a-z0-9_-]{1,64}`)
- [ ] Tag format validation (`[a-z0-9_-]{1,32}`)

### Implementation Notes
- Use `tomllib` (stdlib in 3.12) for TOML parsing.
- Pydantic `BaseSettings` with `env_prefix="TOOLSEARCH_"` and `env_nested_delimiter="__"`.
- The loader merges TOML dict → Pydantic model → env var overrides → CLI flags.
- `remote_url` required when `backend="remote"` — use a Pydantic model_validator for cross-field validation.

---

## Task 3: Data Directory + SQLite Metadata Schema
**Phase:** 1
**Depends on:** Task 2
**Effort:** S
**Priority:** P0

### Description
Create the `~/.toolsearch/` directory structure and define the SQLite metadata database schema. This stores tool records, server mappings, and index version tracking. The schema supports both full rebuild and incremental updates.

### Acceptance Criteria
- [ ] `initialize_data_dir()` creates `~/.toolsearch/{config.toml, index/, models/, logs/}`
- [ ] Default `config.toml` is written with documented defaults
- [ ] SQLite DB created at `~/.toolsearch/index/metadata.db` with tables: `servers`, `tools`, `index_versions`
- [ ] `servers` table stores ServerRecord fields
- [ ] `tools` table stores ToolRecord fields (except embedding_vector — that's in hnswlib)
- [ ] `index_versions` table tracks per-server index generation
- [ ] WAL mode enabled for concurrent read/write

### Files to Create/Modify
- `src/toolsearch/storage/metadata_db.py` — MetadataDB class with schema init, CRUD operations
- `src/toolsearch/models/domain.py` — ServerRecord, ToolRecord dataclasses

### Tests Required
- [ ] `initialize_data_dir()` creates all expected directories
- [ ] Default config.toml is valid TOML and passes config validation
- [ ] MetadataDB creates tables on init
- [ ] Insert + retrieve a ToolRecord roundtrip
- [ ] Insert + retrieve a ServerRecord roundtrip
- [ ] Index version increment works correctly
- [ ] WAL mode is enabled (PRAGMA check)

### Implementation Notes
- Use `aiosqlite` for async SQLite access.
- UUID PKs for tools table (`tool_id` is `{server_id}::{tool_name}`, stored as TEXT).
- Store `embedding_text`, `indexed_at`, `index_version` in the tools table.
- `embedding_vector` is NOT stored in SQLite — it goes in hnswlib (Task 6).
- Schema migration: for V1, just create-if-not-exists. No Alembic needed for a local tool.

---

## Task 4: Embedding Backend — Interface + Local ONNX
**Phase:** 2
**Depends on:** Task 2
**Effort:** M
**Priority:** P0

### Description
Define the `EmbeddingBackend` protocol and implement the local ONNX backend using `onnxruntime` + `tokenizers`. Build the `embedding_text` construction logic from ToolRecord fields. Support batch embedding with configurable batch size.

### Acceptance Criteria
- [ ] `EmbeddingBackend` protocol: `embed(texts: list[str]) → list[list[float]]`
- [ ] `LocalEmbedder` loads an ONNX model from `embedding.model_path`
- [ ] `embedding_text` construction: `"{name} — {title} — {description}"`, omitting null components
- [ ] Batch embedding respects `embedding.batch_size`
- [ ] Output vectors have dimensionality matching `embedding.dimensions` (384 for default model)
- [ ] Model download: if model path doesn't exist, download from HuggingFace
- [ ] `EMBEDDING_LOAD_FAILED` raised if model can't be loaded
- [ ] `EMBEDDING_INFERENCE_ERROR` on batch failure; retry with halved batch size up to 3 times

### Files to Create/Modify
- `src/toolsearch/ingestion/embedder.py` — EmbeddingBackend protocol, LocalEmbedder, build_embedding_text()
- `scripts/download_model.py` — model download utility

### Tests Required
- [ ] `build_embedding_text` with all fields present
- [ ] `build_embedding_text` with null description (only name)
- [ ] `build_embedding_text` with null title (name + description)
- [ ] LocalEmbedder produces vectors of correct dimensionality
- [ ] Batch of N texts produces N vectors
- [ ] Batch size splitting works (33 texts with batch_size=32 → 2 batches)
- [ ] EMBEDDING_LOAD_FAILED raised when model path is invalid
- [ ] Retry logic on inference error

### Implementation Notes
- Use `onnxruntime` for inference, `tokenizers` for tokenization.
- Default model: `all-MiniLM-L6-v2` from sentence-transformers (ONNX export).
- Mean pooling of token embeddings + L2 normalization to get sentence embeddings.
- Model download: use `huggingface_hub.snapshot_download` or a simple HTTP fetch.
- The embedder is synchronous (CPU-bound); wrap in `asyncio.to_thread` when called from async code.

---

## Task 5: MCP Collector
**Phase:** 2
**Depends on:** Task 2
**Effort:** M
**Priority:** P0

### Description
Implement the Collector that spawns MCP servers, performs the MCP initialize handshake, calls `tools/list` (with pagination), and returns harvested ToolRecords. Handles server spawn failures, timeouts, and protocol errors gracefully.

### Acceptance Criteria
- [ ] `collect_tools(server: ServerConfig) → list[ToolRecord]` spawns a server process
- [ ] MCP `initialize` handshake completes within `server_timeout_seconds`
- [ ] `tools/list` called; pagination handled via `nextCursor`
- [ ] Each tool gets `tool_id = "{server_id}::{tool_name}"`
- [ ] Server process is terminated after collection (shutdown notification → SIGTERM after 5s)
- [ ] `SERVER_SPAWN_FAILED` raised if command doesn't exist
- [ ] `SERVER_TIMEOUT` raised if init exceeds timeout
- [ ] `MCP_PROTOCOL_ERROR` raised for malformed responses; individual tools skipped
- [ ] Concurrent collection up to `max_parallel_servers` via asyncio.Semaphore

### Files to Create/Modify
- `src/toolsearch/ingestion/collector.py` — Collector class
- `src/toolsearch/mcp/protocol.py` — MCP JSON-RPC message builders/parsers
- `src/toolsearch/mcp/transport.py` — stdio read/write helpers (line-delimited JSON-RPC)

### Tests Required
- [ ] Successful collection from a mock MCP server (subprocess fixture)
- [ ] Pagination: server returns 2 pages of tools
- [ ] SERVER_SPAWN_FAILED for nonexistent command
- [ ] SERVER_TIMEOUT for slow server
- [ ] MCP_PROTOCOL_ERROR for malformed tool entry (missing name)
- [ ] Correct tool_id construction
- [ ] Server process is killed after collection
- [ ] Concurrent collection with semaphore (2 servers, max_parallel=1 → sequential)

### Implementation Notes
- Use `asyncio.create_subprocess_exec` to spawn servers.
- MCP protocol over stdio: JSON-RPC 2.0 messages, one per line, on stdin/stdout of the child process.
- The MCP `initialize` request must include `protocolVersion` and `capabilities`.
- Handle `nextCursor` in `tools/list` response — loop until null.
- Each server gets its env vars merged with the parent process env.

---

## Task 6: IndexWriter — Vector Store + Metadata Persistence
**Phase:** 2
**Depends on:** Tasks 3, 4
**Effort:** M
**Priority:** P0

### Description
Implement the IndexWriter that persists ToolRecords and their embedding vectors. Vectors go into hnswlib; metadata goes into SQLite. Support full rebuild (wipe + rewrite) and incremental update (add/remove/update individual tools). Implement atomic swap for index updates.

### Acceptance Criteria
- [ ] `write_full_index(tools: list[ToolRecord], vectors: list[list[float]])` writes complete index
- [ ] `update_server_tools(server_id, tools, vectors)` does incremental update for one server
- [ ] hnswlib index saved to `~/.toolsearch/index/vectors.db`
- [ ] Metadata (ToolRecord fields minus vector) saved to SQLite
- [ ] Atomic swap: new index built in temp location, then renamed into place
- [ ] Old index remains queryable during writes (readers unaffected)
- [ ] `remove_server(server_id)` removes all tools for a server
- [ ] Index version incremented per write operation

### Files to Create/Modify
- `src/toolsearch/storage/vector_store.py` — VectorStore class (hnswlib wrapper)
- `src/toolsearch/ingestion/index_writer.py` — IndexWriter class (coordinates vector store + metadata DB)

### Tests Required
- [ ] Write 10 tools → read back from vector store, verify count
- [ ] Write 10 tools → metadata DB contains all 10 records
- [ ] Full rebuild replaces previous index entirely
- [ ] Incremental update: add 5 tools to server A, then update server A with 3 → only 3 remain for A
- [ ] Atomic swap: simulate crash mid-write → old index intact
- [ ] remove_server removes tools from both vector store and metadata
- [ ] Index version increments on each write

### Implementation Notes
- hnswlib: use `hnswlib.Index` with space='cosine', dim=384.
- For atomic swap: write to `vectors.db.tmp`, then `os.rename` (atomic on POSIX).
- hnswlib labels: use integer IDs, maintain a mapping in SQLite (tool_id → hnsw_label).
- For incremental updates, hnswlib doesn't support true deletion — mark deleted and rebuild periodically, or use `mark_deleted` + `unmark_deleted` if supported.

---

## Task 7: `tool-search index` CLI Command
**Phase:** 2
**Depends on:** Tasks 4, 5, 6
**Effort:** S
**Priority:** P0

### Description
Wire up the full indexing pipeline as the `tool-search index` CLI command. Orchestrates: load config → init embedder → collect tools from all servers → batch embed → write index. Report results. Support `--dry-run`.

### Acceptance Criteria
- [ ] `tool-search index` runs the full pipeline end-to-end
- [ ] Exit code 0 when all servers succeed
- [ ] Exit code 2 when some servers fail (DEGRADED)
- [ ] Exit code 1 when all servers fail or embedding unavailable
- [ ] Summary printed: N tools from M servers, time, any failures
- [ ] `--dry-run` flag: connects, collects, embeds, but doesn't write
- [ ] IndexEvent logged to `index.jsonl`

### Files to Create/Modify
- `src/toolsearch/cli/index_cmd.py` — index subcommand
- `src/toolsearch/cli/main.py` — register index subcommand

### Tests Required
- [ ] Full index with mock servers → exit 0, correct tool count
- [ ] Partial failure → exit 2, degraded message
- [ ] Total failure → exit 1
- [ ] --dry-run → no files written to index/
- [ ] IndexEvent written to logs

### Implementation Notes
- Use `asyncio.run()` to bridge the sync CLI with async internals.
- Print progress with `rich.progress` (server connection, embedding, writing).
- The command should be idempotent — running twice produces the same result.

---

## Task 8: IndexReader — ANN Search
**Phase:** 3
**Depends on:** Task 6
**Effort:** S
**Priority:** P0

### Description
Implement the IndexReader that performs approximate nearest neighbor search against the hnswlib vector store. Returns raw scored results (before filtering).

### Acceptance Criteria
- [ ] `search(query_vector: list[float], top_k: int) → list[ScoredTool]`
- [ ] Results ordered by descending cosine similarity score
- [ ] Score normalization: `(raw + 1.0) / 2.0` to get [0.0, 1.0] range
- [ ] Over-fetch: actually queries `top_k * 2` from hnswlib to allow post-filtering headroom
- [ ] Returns `ScoredTool` with `tool_id`, `score`, `rank`
- [ ] Handles empty index gracefully (returns empty list)
- [ ] Records `retrieval_ms` timing

### Files to Create/Modify
- `src/toolsearch/retrieval/index_reader.py` — IndexReader class
- `src/toolsearch/models/domain.py` — ScoredTool dataclass (add if not already present)

### Tests Required
- [ ] Search with 10 indexed tools returns ranked results
- [ ] Score normalization is correct (verify with known cosine sim values)
- [ ] Over-fetch: top_k=5 actually queries 10 from hnswlib
- [ ] Empty index returns empty list
- [ ] Results are ordered by descending score

### Implementation Notes
- hnswlib `knn_query` returns (labels, distances). For cosine space, distance = 1 - cosine_sim.
- Normalization: `score = 1.0 - distance` gives cosine similarity in [-1, 1], then normalize to [0, 1].
- Map hnswlib integer labels back to tool_id via the metadata DB.

---

## Task 9: QueryEngine — Full Retrieval Pipeline
**Phase:** 3
**Depends on:** Tasks 4, 8
**Effort:** M
**Priority:** P0

### Description
Implement the QueryEngine that orchestrates end-to-end retrieval: embed the query → ANN search → apply filters (min_score, server allowlist/blocklist) → truncate to top_k → return ToolResult.

### Acceptance Criteria
- [ ] `query(text: str, config: RetrievalConfig) → ToolResult`
- [ ] Embeds the query text using the configured Embedder
- [ ] Calls IndexReader for ANN search
- [ ] Filters results below `min_score`
- [ ] Applies `server_allowlist` (only include listed servers)
- [ ] Applies `server_blocklist` (exclude listed servers; blocklist > allowlist)
- [ ] Truncates to `top_k`
- [ ] Assigns ranks 1..N
- [ ] Populates ToolResult with timing (embedding_ms, retrieval_ms), total_candidates, truncated flag
- [ ] Returns full ToolRecord data (not just IDs) for each result

### Files to Create/Modify
- `src/toolsearch/retrieval/query_engine.py` — QueryEngine class
- `src/toolsearch/models/domain.py` — ToolResult dataclass (add if not already present)

### Tests Required
- [ ] Basic query returns ranked results
- [ ] min_score filter removes low-scoring tools
- [ ] server_allowlist includes only allowed servers
- [ ] server_blocklist excludes blocked servers
- [ ] Blocklist takes precedence over allowlist
- [ ] top_k truncation works
- [ ] Truncated flag is True when more candidates exist than top_k
- [ ] ToolResult includes correct timing values
- [ ] Empty index returns ToolResult with empty tools list

### Implementation Notes
- QueryEngine holds references to the Embedder and IndexReader.
- For the full ToolRecord data, join ScoredTool results with MetadataDB lookups.
- The over-fetch in IndexReader (2x top_k) gives headroom for filtering without missing relevant results.

---

## Task 10: `tool-search search` CLI Command
**Phase:** 3
**Depends on:** Task 9
**Effort:** S
**Priority:** P1

### Description
Implement the one-shot search CLI command for testing retrieval without running the proxy.

### Acceptance Criteria
- [ ] `tool-search search "query text"` prints ranked results
- [ ] `--top-k N` overrides retrieval.top_k
- [ ] `--servers kubernetes,github` limits to specific servers
- [ ] Output includes: rank, score, tool name, server, description (truncated)
- [ ] JSON output mode with `--json`
- [ ] Exit 0 on success, 1 on error

### Files to Create/Modify
- `src/toolsearch/cli/search_cmd.py` — search subcommand
- `src/toolsearch/cli/main.py` — register search subcommand

### Tests Required
- [ ] Search with indexed tools returns formatted output
- [ ] --top-k flag limits results
- [ ] --servers flag filters results
- [ ] --json outputs valid JSON array
- [ ] Empty results prints "No matching tools found"

### Implementation Notes
- Use `rich.table` for pretty-printed output.
- Score displayed as percentage (e.g., "94.2%").
- Description truncated to 80 chars in table mode, full in JSON mode.

---

## Task 11: MCP Proxy — stdio Transport + Message Routing
**Phase:** 4
**Depends on:** Task 5
**Effort:** L
**Priority:** P0

### Description
Implement the core MCP Proxy stdio-to-stdio relay. The proxy sits between the MCP client and one or more upstream MCP servers, forwarding messages bidirectionally. This task handles transport, connection management, and the message routing table.

### Acceptance Criteria
- [ ] Proxy reads JSON-RPC messages from stdin (client) and routes to correct upstream server
- [ ] Proxy reads responses from upstream servers and forwards to stdout (client)
- [ ] Message ordering preserved per-direction
- [ ] Internal routing table: `tool_name → server_id` populated from indexed tools
- [ ] Upstream server processes spawned on proxy start
- [ ] Server reconnection with exponential backoff (1s, 2s, 4s, 8s, max 30s, 3 retries)
- [ ] Clean shutdown: SIGTERM → shutdown notification to servers → SIGTERM after 5s → exit
- [ ] Unknown MCP methods forwarded unchanged (forward compatibility)
- [ ] Malformed JSON-RPC → error response (code -32700 or -32600)

### Files to Create/Modify
- `src/toolsearch/integration/proxy.py` — MCPProxy class (transport + routing)
- `src/toolsearch/mcp/transport.py` — extend with bidirectional relay helpers

### Tests Required
- [ ] Message from client is routed to correct upstream server
- [ ] Response from server is forwarded to client stdout
- [ ] Ordering: send 3 messages → received in order
- [ ] Routing table correctly maps tool names to servers
- [ ] Unknown method is forwarded to first server (or broadcast)
- [ ] Malformed JSON returns error response
- [ ] Server reconnection after process exit (mock server exits, proxy retries)
- [ ] Clean shutdown terminates all server processes

### Implementation Notes
- Use `asyncio.create_subprocess_exec` for each upstream server.
- Two tasks per server: reader (server stdout → client stdout) and writer (client stdin → server stdin).
- JSON-RPC framing: read line-by-line, parse JSON, route by method.
- The routing table is built from MetadataDB on startup, updated on re-index.
- For methods that aren't `tools/*`, use a default routing strategy (broadcast to all, or first server).

---

## Task 12: MCP Proxy — tools/list Filtering + Context State
**Phase:** 4
**Depends on:** Tasks 9, 11
**Effort:** M
**Priority:** P0

### Description
Add semantic filtering to the proxy's `tools/list` handling. Implement the context state machine (IDLE / CONTEXT_ACTIVE) and the `toolsearch_context` companion tool. When a context is active, `tools/list` returns only semantically relevant tools.

### Acceptance Criteria
- [ ] Proxy intercepts `tools/list` requests (does not forward to upstream)
- [ ] IDLE state: returns tools based on `fallback_on_no_context` setting ("all" / "none" / "tagged")
- [ ] CONTEXT_ACTIVE state: returns QueryEngine results for the active query
- [ ] `toolsearch_context` tool call sets the context + TTL
- [ ] Context TTL expiry transitions back to IDLE
- [ ] New context replaces old context and resets TTL
- [ ] Proxy ignores `cursor` parameter in tools/list (returns full filtered set)
- [ ] Proxy advertises `tools.listChanged` capability in initialize response
- [ ] `notifications/tools/list_changed` from upstream triggers incremental re-index

### Files to Create/Modify
- `src/toolsearch/integration/proxy.py` — add filtering logic, context state

### Tests Required
- [ ] IDLE + fallback "all" → returns all indexed tools
- [ ] IDLE + fallback "none" → returns empty list
- [ ] IDLE + fallback "tagged" → returns only tools with matching tags
- [ ] Set context → tools/list returns filtered results
- [ ] Context TTL expires → falls back to IDLE behavior
- [ ] New context replaces old → filtered results change
- [ ] tools/list response format matches MCP spec (tools array, nextCursor: null)
- [ ] tools/list_changed notification triggers re-index

### Implementation Notes
- Context state: simple class with `query: str | None`, `expires_at: datetime | None`.
- TTL check on every `tools/list` request. Use `asyncio.get_event_loop().time()` for monotonic timing.
- For "tagged" fallback: filter MetadataDB by server tags.
- The `toolsearch_context` tool is handled internally by the proxy (not forwarded upstream).

---

## Task 13: MCP Proxy — tools/call Routing + Passthrough
**Phase:** 4
**Depends on:** Task 11
**Effort:** M
**Priority:** P0

### Description
Implement correct `tools/call` routing and the passthrough contract for all non-tools/list messages. The proxy must route tool calls to the correct upstream server and forward all other messages unchanged.

### Acceptance Criteria
- [ ] `tools/call` routed to the server that owns the tool (from routing table)
- [ ] Ambiguous tool names (same name, multiple servers): route to highest-scoring server from last retrieval, or first configured server if no context
- [ ] `TOOL_NOT_FOUND` error returned if tool name not in routing table
- [ ] `UPSTREAM_SERVER_UNREACHABLE` error if target server is disconnected
- [ ] All non-tools/list messages forwarded unchanged (resources/*, prompts/*, notifications/*, etc.)
- [ ] Message ordering preserved
- [ ] No fields added, removed, or modified in passthrough messages

### Files to Create/Modify
- `src/toolsearch/integration/proxy.py` — add tools/call routing, passthrough logic

### Tests Required
- [ ] tools/call routes to correct server (tool from server A goes to server A)
- [ ] Ambiguous tool: routes to first configured server when no context
- [ ] TOOL_NOT_FOUND for unknown tool name
- [ ] UPSTREAM_SERVER_UNREACHABLE for disconnected server
- [ ] resources/list forwarded unchanged to upstream
- [ ] notifications/* forwarded unchanged
- [ ] Unknown method forwarded unchanged
- [ ] Response from tools/call forwarded back to client unchanged

### Implementation Notes
- Routing table: `dict[str, list[str]]` mapping tool_name → [server_ids].
- For unambiguous tools (1 server), route directly.
- For ambiguous tools, check the last retrieval's scores. If no retrieval context, use config order.
- Passthrough: for any method not in {"tools/list"}, forward to the appropriate server (or default server).

---

## Task 14: Search Tool Server
**Phase:** 4
**Depends on:** Task 9
**Effort:** M
**Priority:** P1

### Description
Implement the standalone MCP Search Tool Server exposing `toolsearch_find` and `toolsearch_context` as MCP tools. This server can run independently or alongside the proxy.

### Acceptance Criteria
- [ ] MCP server exposing two tools in `tools/list`: `toolsearch_find` and `toolsearch_context`
- [ ] `toolsearch_find` accepts query, top_k, min_score, servers; returns JSON array of tool defs with scores
- [ ] `toolsearch_context` accepts query, ttl_seconds; sets proxy context (if proxy is running)
- [ ] Input schemas match Spec Section 5.5 exactly
- [ ] Server responds to MCP `initialize` with correct capabilities
- [ ] Runs as stdio MCP server (spawnable by any MCP client)

### Files to Create/Modify
- `src/toolsearch/integration/search_tool.py` — SearchToolServer class

### Tests Required
- [ ] tools/list returns exactly 2 tools with correct schemas
- [ ] toolsearch_find returns results matching query
- [ ] toolsearch_find with top_k=3 returns at most 3 results
- [ ] toolsearch_find with servers filter limits to those servers
- [ ] toolsearch_context sets context and returns confirmation message
- [ ] Output format matches spec (JSON array with server_id, name, description, inputSchema, score)
- [ ] initialize response includes correct serverInfo and capabilities

### Implementation Notes
- This is a separate MCP server process. It loads the index on startup and serves queries.
- Communication with the proxy (for context setting) can be via a shared file or IPC socket, but for V1 keeping them in the same process (proxy embeds the search tool) is simplest.
- If running standalone, `toolsearch_context` is a no-op (logs a warning).

---

## Task 15: `tool-search proxy` CLI Command
**Phase:** 4
**Depends on:** Tasks 12, 13
**Effort:** S
**Priority:** P0

### Description
Wire the MCP Proxy into a CLI command that starts the proxy, spawns upstream servers, and begins relaying messages on stdio.

### Acceptance Criteria
- [ ] `tool-search proxy` starts the proxy and blocks on stdio
- [ ] `--servers kubernetes,github` limits which upstream servers to proxy
- [ ] `--passthrough` flag sets mode to passthrough (no filtering)
- [ ] Proxy shuts down cleanly on stdin EOF or SIGTERM
- [ ] Exit 0 on clean shutdown, 1 on error

### Files to Create/Modify
- `src/toolsearch/cli/proxy_cmd.py` — proxy subcommand
- `src/toolsearch/cli/main.py` — register proxy subcommand

### Tests Required
- [ ] Proxy starts and responds to MCP initialize
- [ ] --servers flag limits upstream servers
- [ ] --passthrough disables filtering
- [ ] SIGTERM causes clean shutdown
- [ ] stdin EOF causes clean shutdown

### Implementation Notes
- The proxy runs forever until stdin closes. Use `asyncio.run()` with signal handlers.
- The proxy inherits the search tool (toolsearch_find, toolsearch_context) — it adds them to its own tools/list response.

---

## Task 16: Audit Logger
**Phase:** 5
**Depends on:** Task 3
**Effort:** S
**Priority:** P1

### Description
Implement the structured JSONL audit logger for DiscoveryEvent and IndexEvent records. Support log rotation by file size.

### Acceptance Criteria
- [ ] `AuditLogger.log_discovery(event: DiscoveryEvent)` appends to `discovery.jsonl`
- [ ] `AuditLogger.log_index(event: IndexEvent)` appends to `index.jsonl`
- [ ] Events serialized as single-line JSON (JSONL)
- [ ] Log rotation at `max_file_size_mb`, keeping `max_files` rotated files
- [ ] Logging respects `logging.enabled` master switch
- [ ] Log write failure → stderr warning, does NOT fail the operation
- [ ] Logs are append-only (never modified after writing)

### Files to Create/Modify
- `src/toolsearch/logging/audit.py` — AuditLogger class
- `src/toolsearch/models/events.py` — DiscoveryEvent, IndexEvent dataclasses

### Tests Required
- [ ] DiscoveryEvent written as valid JSONL
- [ ] IndexEvent written as valid JSONL
- [ ] Log rotation triggers at configured size
- [ ] logging.enabled=false → no writes
- [ ] Write failure → stderr warning, no exception raised
- [ ] Multiple events written on separate lines

### Implementation Notes
- Use Python's `logging.handlers.RotatingFileHandler` for rotation.
- Events are Pydantic models serialized with `.model_dump_json()`.
- The audit logger is injected into QueryEngine, IndexWriter, and Proxy.

---

## Task 17: `tool-search status` + `tool-search logs` CLI Commands
**Phase:** 5
**Depends on:** Task 16
**Effort:** S
**Priority:** P1

### Description
Implement status reporting (JSON snapshot of system state) and log tailing commands.

### Acceptance Criteria
- [ ] `tool-search status` prints JSON snapshot per Spec Section 9.3
- [ ] Status includes: index state, tool count, server statuses, embedding info, storage sizes
- [ ] `tool-search logs` dumps recent discovery + index log entries
- [ ] `tool-search logs --follow` tails logs in real-time
- [ ] `tool-search logs --level warn` filters by level
- [ ] Exit 0 on success, 1 if toolsearch is not initialized

### Files to Create/Modify
- `src/toolsearch/cli/status_cmd.py` — status subcommand
- `src/toolsearch/cli/logs_cmd.py` — logs subcommand
- `src/toolsearch/cli/main.py` — register subcommands

### Tests Required
- [ ] Status output is valid JSON matching the schema
- [ ] Status shows correct index state (READY / EMPTY / DEGRADED)
- [ ] Status shows correct tool count
- [ ] Logs command reads and formats JSONL entries
- [ ] --level flag filters entries

### Implementation Notes
- Status reads from MetadataDB + filesystem (index dir, log files, config).
- For `--follow`, use `watchdog` or simple polling with `tail -f` behavior.
- Format log entries with `rich` for human-readable output, or raw JSONL with `--json`.

---

## Task 18: Bootstrap Engine
**Phase:** 6
**Depends on:** Task 2
**Effort:** M
**Priority:** P2

### Description
Implement bootstrap connectors that generate client-specific configuration snippets for Claude Code, Cursor, and Gemini CLI.

### Acceptance Criteria
- [ ] `tool-search bootstrap claude-code` generates config snippet for Claude Code's MCP settings
- [ ] `tool-search bootstrap cursor` generates config snippet for Cursor
- [ ] `tool-search bootstrap gemini-cli` generates config snippet for Gemini CLI
- [ ] Each snippet configures ToolSearch as either proxy or search tool (operator choice)
- [ ] `BOOTSTRAP_TARGET_NOT_FOUND` if target runtime config not found
- [ ] `--write` flag writes directly to the target's config file (with backup)
- [ ] `--dry-run` prints what would be written

### Files to Create/Modify
- `src/toolsearch/integration/bootstrap.py` — BootstrapEngine class with templates
- `src/toolsearch/cli/bootstrap_cmd.py` — bootstrap subcommand

### Tests Required
- [ ] Claude Code snippet is valid JSON for claude_desktop_config.json
- [ ] Cursor snippet is valid JSON for .cursor/mcp.json
- [ ] Gemini CLI snippet is valid JSON
- [ ] BOOTSTRAP_TARGET_NOT_FOUND for missing target
- [ ] --dry-run prints snippet without writing

### Implementation Notes
- Templates are Python dicts/strings, not external files.
- Claude Code config location: `~/.claude/claude_desktop_config.json` (or platform-specific).
- Cursor config: `.cursor/mcp.json` in project root.
- Gemini CLI: `~/.gemini/settings.json` or equivalent.
- Each template maps `tool-search proxy` or `tool-search search-tool` into the client's `mcpServers` config.

---

## Task 19: HTTP Sidecar
**Phase:** 6
**Depends on:** Task 12
**Effort:** S
**Priority:** P2

### Description
Implement the optional HTTP sidecar for setting proxy context via HTTP POST instead of the MCP tool.

### Acceptance Criteria
- [ ] Sidecar starts on `proxy.sidecar_port` when configured
- [ ] `POST /context` with `{ "query": "...", "ttl_seconds": 300 }` sets proxy context
- [ ] `GET /health` returns JSON health check response
- [ ] 400 response for missing `query` field
- [ ] 5s request timeout
- [ ] Sidecar only starts when `sidecar_port` is non-null

### Files to Create/Modify
- `src/toolsearch/integration/sidecar.py` — Starlette/uvicorn sidecar app

### Tests Required
- [ ] POST /context sets context successfully
- [ ] POST /context without query → 400
- [ ] GET /health returns correct status
- [ ] Sidecar respects configured port
- [ ] No sidecar started when sidecar_port is null

### Implementation Notes
- Use Starlette for minimal overhead. Run uvicorn in a background asyncio task.
- The sidecar shares the proxy's context state object (same process).

---

## Task 20: `tool-search init` + `tool-search config` CLI Commands
**Phase:** 6
**Depends on:** Task 2
**Effort:** S
**Priority:** P1

### Description
Implement the initialization command (creates data directory, default config, downloads model) and the config management command.

### Acceptance Criteria
- [ ] `tool-search init` creates `~/.toolsearch/` with config.toml, directories
- [ ] `tool-search init` downloads the default embedding model if not present
- [ ] Re-running init on existing directory is safe (no data loss)
- [ ] `tool-search config show` prints current config
- [ ] `tool-search config set retrieval.top_k 5` updates a config value
- [ ] `tool-search config validate` checks config file validity

### Files to Create/Modify
- `src/toolsearch/cli/init_cmd.py` — init subcommand
- `src/toolsearch/cli/config_cmd.py` — config subcommand
- `src/toolsearch/cli/main.py` — register subcommands

### Tests Required
- [ ] init creates all directories
- [ ] init writes valid default config
- [ ] init is idempotent (run twice without error)
- [ ] config show prints TOML
- [ ] config set modifies value
- [ ] config validate reports errors for invalid config

### Implementation Notes
- Model download: call `scripts/download_model.py` or inline the logic.
- Use `tomlkit` (not tomllib) for config writing (preserves comments/formatting).
- Config set: parse the key path, update the value, write back.

---

## Task 21: Remote Embedding Backend
**Phase:** 6
**Depends on:** Task 4
**Effort:** S
**Priority:** P2

### Description
Implement the remote embedding backend that calls an external API (e.g., OpenAI embeddings) instead of running ONNX locally.

### Acceptance Criteria
- [ ] `RemoteEmbedder` implements `EmbeddingBackend` protocol
- [ ] Sends HTTP POST to `embedding.remote_url` with texts
- [ ] Uses `embedding.remote_api_key` for auth (or `TOOLSEARCH_EMBEDDING_API_KEY` env var)
- [ ] Respects `embedding.batch_size` for request batching
- [ ] Handles HTTP errors gracefully (timeout, 4xx, 5xx)
- [ ] Token accounting: tracks tokens sent and estimated cost

### Files to Create/Modify
- `src/toolsearch/ingestion/remote_embedder.py` — RemoteEmbedder class

### Tests Required
- [ ] Remote embedder sends correct HTTP request (mock server)
- [ ] API key included in Authorization header
- [ ] Batch splitting for large inputs
- [ ] HTTP 500 → EMBEDDING_INFERENCE_ERROR
- [ ] HTTP 401 → clear auth error message
- [ ] Token counting in response

### Implementation Notes
- Use `httpx` (async) for HTTP requests.
- OpenAI embeddings API format: `POST /v1/embeddings` with `{"model": "...", "input": [...]}`.
- The implementation should be generic enough for OpenAI, Cohere, and similar APIs.
- Env var `TOOLSEARCH_EMBEDDING_API_KEY` takes precedence over config file.

---

## Task 22: Dockerfile + docker-compose.yml
**Phase:** 7
**Depends on:** All previous tasks
**Effort:** S
**Priority:** P2

### Description
Create Docker packaging for ToolSearch.

### Acceptance Criteria
- [ ] `Dockerfile` builds a working ToolSearch image
- [ ] Image includes the default ONNX model (pre-downloaded)
- [ ] `docker-compose.yml` with a basic service definition
- [ ] `make docker-up` / `make docker-down` work
- [ ] Image size < 2GB (model is ~90MB, ONNX runtime is ~200MB)

### Files to Create/Modify
- `Dockerfile` — multi-stage build
- `docker-compose.yml` — service definition
- `Makefile` — add docker-up, docker-down targets

### Tests Required
- [ ] Docker build completes without errors
- [ ] Container starts and `tool-search status` works
- [ ] `tool-search search "test query"` works inside container

### Implementation Notes
- Multi-stage build: build stage installs deps, runtime stage copies only what's needed.
- Pre-download the model during build (`RUN tool-search init`).
- Mount `~/.toolsearch` as a volume for config/index persistence.

---

## Task 23: GitHub Actions CI
**Phase:** 7
**Depends on:** Task 1
**Effort:** S
**Priority:** P1

### Description
Set up GitHub Actions for continuous integration: lint, test, and coverage reporting.

### Acceptance Criteria
- [ ] CI runs on push to main and on PRs
- [ ] Steps: install uv → install deps → lint (ruff) → test (pytest) → coverage report
- [ ] Coverage threshold: ≥85% (fail if below)
- [ ] Matrix: Python 3.12, 3.13
- [ ] Artifacts: coverage report uploaded

### Files to Create/Modify
- `.github/workflows/ci.yml` — CI workflow

### Tests Required
- [ ] (meta) CI workflow is valid YAML
- [ ] (meta) CI passes on a clean checkout

### Implementation Notes
- Use `actions/setup-python` + uv.
- Cache uv dependencies for faster runs.
- Run `ruff check` and `ruff format --check` for linting.
- `pytest --cov=toolsearch --cov-fail-under=85`.
