# Use Case Synthesis — Design

This document specifies the **use-case mining stage** of the ingest
pipeline: how harvested tools (from OpenAPI specs and live MCP servers)
are chunked, sent to an LLM, and turned into persisted use-case records
that downstream skill synthesis ([skill-generation.md](skill-generation.md))
clusters and consumes.

This is a pragmatic lift of the technique described in
[api-knowledge-gateway/05-pattern-mining.md](api-knowledge-gateway/05-pattern-mining.md)
(use-case mining gradient) and
[api-introspection-skills/05-synthesis-and-validation.md](api-introspection-skills/05-synthesis-and-validation.md)
(use-case generation strategy), reduced to what is shippable on top of
the current `src/mcp_semantic_gateway/` codebase without introducing the
Atlas, code graph, or pattern store.

## Goal

Given the tool list returned by `Collector.collect_all()` for an
opted-in source, produce a set of **use case records** describing
real-world tasks the source's tools enable. Each record is content-
hashed, persisted, and embeds cleanly into the existing vector store
so it can be matched against agent queries even before any skill is
synthesized from it.

## Scope

In scope:

- Per-source chunking of harvested tools.
- LLM-driven use case extraction, one call per chunk, multi-pass.
- Provider-agnostic LLM abstraction (Anthropic native + OpenAI-compatible
  transports).
- Idempotent caching keyed on source content hash.
- Structured observability — JSONL event log + live progress + diagnostics.

Out of scope (deliberately deferred):

- Atlas / code graph construction.
- Statistical or deterministic pattern miners (Tier 1/2 of the pattern
  mining gradient).
- Cross-source use cases.
- Feedback-driven re-mining.

## Decisions log

| # | Decision |
|---|---|
| U-1 | LLM abstraction exposes two transports — Anthropic native and OpenAI-compatible — covering Anthropic, OpenAI, OpenRouter, Google (via OpenAI-compatible endpoint), and local runtimes (Ollama, llama.cpp, vLLM). |
| U-2 | Structured output uses tool-use / function-calling on every transport. No freeform JSON parsing. |
| U-3 | Use case mining runs in chunks. Default chunk size: 12 tools. Per-chunk LLM call. |
| U-4 | OpenAPI chunk grouping ranked by precedence: (1) operation `tags[]`, (2) first path segment, (3) ordered fixed-size. **Open: confirm tags must be plumbed through `ForgeEngine` annotations.** |
| U-5 | Live MCP server chunk grouping ranked by precedence: (1) shared name-prefix, (2) ordered fixed-size. |
| U-6 | Use case mining is opt-in per server (`generate_skills: bool = false` on `ServerConfig`). Existing users get no surprise LLM bills. |
| U-7 | Cache key: `(server_id, source_hash, chunk_hash, model_id, prompt_version)`. Re-running on unchanged source = zero LLM calls. |
| U-8 | Observability ships with every event; not feature-flagged. |

## Module layout

New files under `src/mcp_semantic_gateway/`:

```
llm/
├── __init__.py
├── base.py                          # Protocol, Message, ToolSpec, LLMResponse, UsageStats
├── anthropic_provider.py
├── openai_compatible_provider.py
└── factory.py                       # build_llm(LLMConfig) -> LLMProvider

ingestion/
├── chunker.py                       # ToolChunk, chunk_tools(...)
├── use_case_miner.py                # UseCaseRecord, mine_chunk(...), mine_source(...)
└── observability.py                 # SynthesisRun, EventEmitter, Stage events

config/
└── models.py                        # +LLMConfig, +SkillGenerationConfig, ServerConfig.generate_skills

cli/
└── main.py                          # +synth, +synth status

storage/
└── metadata_db.py                   # +use_cases table
```

## LLM abstraction

### Contract

```python
# llm/base.py
class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str

class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict      # JSON schema

class UsageStats(BaseModel):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    estimated_cost_usd: float | None = None

class LLMResponse(BaseModel):
    tool_name: str | None
    tool_arguments: dict | None
    text: str | None
    stop_reason: str
    usage: UsageStats

class LLMProvider(Protocol):
    model_id: str
    async def call(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        force_tool: str | None = None,        # routes through tool_choice
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> LLMResponse: ...
```

The structured-output path is "force a single tool call" — every
provider supports this. Use case extraction registers one tool whose
input schema *is* the use case record schema; the model's tool call
arguments become the parsed output. No JSON repair needed.

### Provider configuration

```toml
[llm]
provider = "anthropic"          # | "openai-compatible"
model = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"
base_url = ""                   # required for openai-compatible
max_concurrency = 4
request_timeout_seconds = 60
retry_max_attempts = 3
retry_initial_backoff_seconds = 1.0
```

`provider = "openai-compatible"` + `base_url = "..."` covers every
non-Anthropic target:

| Target | base_url |
|---|---|
| OpenAI | `https://api.openai.com/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| Ollama (local) | `http://localhost:11434/v1` |
| llama.cpp / vLLM | depends on local serve |

### Anthropic-only optimizations

The Anthropic adapter applies prompt caching to the system prompt and
the chunk-prelude (tool catalog) when `cache_system_prompt = true`
(default). Cache hits are reflected in `UsageStats.cached_input_tokens`
and surfaced in observability.

OpenAI-compatible adapters set `cached_input_tokens = 0`. Future work
can detect provider-specific cache fields when standardized.

## Chunking

```python
# ingestion/chunker.py
class ToolChunk(BaseModel):
    chunk_id: str                   # 'chunk-<server>-<group>-<idx>'
    server_id: str
    source_hash: str                # sha256 of full source
    chunk_hash: str                 # sha256 of canonical tool list
    group_label: str                # tag name, path-prefix, or 'mixed'
    tools: list[dict]               # full harvested tool dicts
```

OpenAPI chunking precedence (decision U-4), ranked top to bottom:

1. **Tag grouping** — when operations carry `tags[]`, group by tag.
   Each tag becomes one or more chunks (split when group exceeds
   `chunk_size`).
2. **Path-prefix grouping** — when no tags exist, group by first path
   segment (`/users/...` → `users`).
3. **Ordered fixed-size** — when neither is informative, emit fixed-size
   chunks of `chunk_size` in source order.

Live MCP chunking precedence:

1. **Name-prefix grouping** — when tool names share a prefix (`gh_`,
   `slack_`), group by prefix.
2. **Ordered fixed-size** — emit fixed-size chunks in source order.

`chunk_size` default = 12. Override via `[skill_generation] chunk_size`.

**Required upstream change**: `ForgeEngine.forge_tools` currently
strips operation tags. It must surface them into
`annotations.tags: list[str]` so the chunker has the data path-1
requires. Tracked as a task below.

## Use case record

```python
# ingestion/use_case_miner.py
class UseCaseRecord(BaseModel):
    id: str                                 # 'uc-<server>-<chunk>-<seq>'
    server_id: str
    source_hash: str
    chunk_id: str
    description: str                        # agent-vocab, 50-400 chars
    linked_tool_names: list[str]            # references into harvested tool set
    prerequisites: list[str] = []           # natural-language preconditions
    confidence: float                       # 0..1, model-reported
    generated_by: str                       # '<model_id>@<prompt_version>'
    generated_at: datetime
    use_case_hash: str                      # sha256(canonical_form)
```

Persisted to a new `use_cases` SQLite table; embedded into the existing
vector store under a separate logical "kind" so retrieval can return
use cases distinctly from tools/skills/prompts.

```sql
CREATE TABLE use_cases (
    id TEXT PRIMARY KEY,
    server_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    description TEXT NOT NULL,
    linked_tool_names TEXT NOT NULL,        -- JSON array
    prerequisites TEXT NOT NULL,            -- JSON array
    confidence REAL NOT NULL,
    generated_by TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    use_case_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_use_cases_server ON use_cases(server_id);
CREATE INDEX idx_use_cases_source_hash ON use_cases(source_hash);
```

## Mining pipeline

```
mine_source(server_id, harvested_tools):
  1. Compute source_hash from harvested tool list (canonical form).
  2. If (server_id, source_hash) already cached AND model_id unchanged:
     emit 'cache_hit', return persisted use cases.
  3. chunks = chunker.chunk_tools(harvested_tools, server_id, source_hash)
  4. emit 'mining_started' (total_chunks, total_tools, server_id)
  5. async with semaphore(max_concurrency):
       for chunk in chunks:
         emit 'chunk_started' (chunk_id, group_label, tool_count)
         result = await llm.call(
           messages = [system_prompt, user_prompt(chunk)],
           tools = [USE_CASE_EXTRACTION_TOOL_SCHEMA],
           force_tool = 'emit_use_cases',
         )
         records = parse_records(result, chunk)
         for r in records:
           validate_record(r, chunk)        # tool_names resolve, lengths in bounds
         persist_records(records)
         emit 'chunk_completed' (chunk_id, use_cases_emitted, usage)
  6. emit 'mining_completed' (total_use_cases, total_usage, duration_ms)
```

### Validation (no LLM)

Each emitted use case is validated before persistence:

- `description` length in [50, 400] chars.
- `linked_tool_names` non-empty; every name resolves in the chunk's
  tool set (catches hallucinated tool names cheaply).
- `confidence` in [0, 1].

Failed records are dropped and emitted as `record_rejected` events
with reason; never persisted.

## Configuration

```toml
[skill_generation]
enabled = false                      # global default; per-server can override
chunk_size = 12
prompt_version = "v1"
cache_system_prompt = true
output_dir = ".mcp_semantic_gateway"

[servers.github-api]
type = "openapi"
url = "..."
generate_skills = true               # opt-in per server
```

`enabled` global default is `false`; per-server `generate_skills` opts
in. The pipeline runs only on servers where `generate_skills = true`
AND `[llm]` is configured.

## Observability

### Event log

JSONL stream at `~/.mcp_semantic_gateway/logs/synthesis.jsonl`. One
record per stage transition.

```json
{
  "ts": "2026-05-04T10:22:31.412Z",
  "run_id": "run-7f3a...",
  "server_id": "github-api",
  "stage": "chunk_completed",
  "chunk_id": "chunk-github-api-issues-0",
  "group_label": "issues",
  "tool_count": 9,
  "use_cases_emitted": 4,
  "use_cases_rejected": 1,
  "rejection_reasons": ["tool_name_unresolved"],
  "usage": {
    "input_tokens": 4180,
    "output_tokens": 612,
    "cached_input_tokens": 3920,
    "estimated_cost_usd": 0.0042
  },
  "duration_ms": 1842,
  "status": "ok"
}
```

Stages: `mining_started`, `cache_hit`, `chunk_started`,
`chunk_completed`, `chunk_failed`, `record_rejected`, `mining_completed`,
`mining_failed`.

### Live progress

Rich-based progress bar (Rich is already a dependency). One outer bar
per server, inner bars per chunk. Running totals: tokens, est. cost,
use cases emitted, use cases rejected.

### Diagnostics

Per-chunk diagnostic file when a chunk fails or rejects records:
`.mcp_semantic_gateway/diagnostics/synthesis/<run_id>/<chunk_id>.json`.
Contains the prompt sent, model response, validation errors, and
canonical chunk hash.

### Run summary

Printed at end of `mcp-semantic-gateway synth`:

```
Synthesis run run-7f3a complete (4m 12s)
  Sources processed:        2
  Chunks executed:          18
  Cache hits:               6
  Use cases emitted:        47
  Use cases rejected:       3 (tool_name_unresolved=2, length_out_of_bounds=1)
  Tokens in / out:          82,140 / 14,610
  Cached tokens in:         51,220 (62%)
  Estimated cost:           $0.184
  Diagnostics:              .mcp_semantic_gateway/diagnostics/synthesis/run-7f3a/
```

## Idempotency and re-runs

Cache keys at three levels:

| Key | Meaning |
|---|---|
| `(server_id, source_hash)` | Source content identity |
| `(chunk_hash, model_id, prompt_version)` | Per-chunk LLM call identity |
| `use_case_hash` | Per-record content identity |

A re-run with unchanged source + unchanged model + unchanged prompt
template = **zero LLM calls**, only cache reads. Bumping
`prompt_version` invalidates per-chunk cache; bumping the source bumps
the source-level key.

## CLI surface

```
mcp-semantic-gateway synth                # run mining over opted-in servers
mcp-semantic-gateway synth --server <id>  # restrict to one server
mcp-semantic-gateway synth --dry-run      # chunk + log, no LLM calls
mcp-semantic-gateway synth status         # list use cases per source with last-run, count
```

`synth` is intentionally separate from `index`; `index` continues to
work without any LLM dependency.

## Tasks

Implementation order. Each task is a single commit boundary unless
noted. Skill-generation tasks live in
[skill-generation.md](skill-generation.md).

### Phase A — LLM abstraction

- [ ] `llm/base.py` — `Message`, `ToolSpec`, `UsageStats`, `LLMResponse`, `LLMProvider` Protocol.
- [ ] `llm/anthropic_provider.py` — Anthropic SDK adapter, prompt caching on system + tool-catalog blocks, retry/backoff.
- [ ] `llm/openai_compatible_provider.py` — OpenAI SDK adapter with `base_url` override, function-calling for structured output.
- [ ] `llm/factory.py` — `build_llm(LLMConfig) -> LLMProvider` dispatch.
- [ ] `config/models.py` — `LLMConfig` model, validation (api_key_env resolves, base_url required for openai-compatible).
- [ ] Stub provider for tests (`tests/_stub_llm.py`) returning canned tool-call responses.
- [ ] Unit test: each adapter invoked with mocked HTTP; structured-output path returns parsed args.

### Phase B — Chunking

- [ ] Plumb `tags[]` through `ForgeEngine.forge_tools` into `annotations.tags`. **Decision U-4 confirmation needed.**
- [ ] `ingestion/chunker.py` — `ToolChunk`, `chunk_tools(harvested, server_id, source_hash)` with the precedence rules above.
- [ ] Source-hash helpers: canonical form for OpenAPI specs and for live MCP `(name, description)` lists.
- [ ] Unit tests: tagged OpenAPI spec chunks by tag; untagged chunks by path-prefix; oversized groups split correctly; live MCP path produces stable chunk_ids.

### Phase C — Use case mining

- [ ] `storage/metadata_db.py` — `use_cases` table + migration.
- [ ] `ingestion/use_case_miner.py` — `UseCaseRecord` dataclass, `mine_chunk(chunk, llm)`, `mine_source(server_id, tools, llm)`.
- [ ] Use-case extraction tool schema + system/user prompt templates (versioned `v1`).
- [ ] Per-record validation (length, tool-name resolution, confidence range).
- [ ] Per-chunk + per-source cache lookup before LLM call.
- [ ] Embed + persist use case records to existing vector store under a `use_case` item kind.
- [ ] Integration test: mock OpenAPI spec → stub LLM → expected use case rows in DB + vectors.

### Phase D — Observability

- [ ] `ingestion/observability.py` — `SynthesisRun`, `EventEmitter`, structured event types.
- [ ] JSONL writer to configured `synthesis_log` path (default `~/.mcp_semantic_gateway/logs/synthesis.jsonl`).
- [ ] Rich progress bars wired into `EventEmitter`.
- [ ] Diagnostics writer for `chunk_failed` and `record_rejected` events.
- [ ] Run summary renderer.
- [ ] Test: emitter produces well-formed events; JSONL roundtrips; progress bar advances on emitted events.

### Phase E — CLI

- [ ] `cli/main.py` — `synth` command (with `--server`, `--dry-run`).
- [ ] `cli/main.py` — `synth status` command (table from `use_cases` table).
- [ ] Smoke test: `synth --dry-run` on the existing OpenAPI fixture prints chunks without LLM calls.

### Phase F — Documentation and exit

- [ ] Update `README.md` with synth opt-in instructions.
- [ ] Update this doc's decisions log with any deltas observed during implementation.
- [ ] Confirm decision U-4 outcome (tags-through-Forge) and remove the open marker.

## Open questions

- **U-4 confirmation** — does the OpenAPI chunker get tags from
  `ForgeEngine` annotations? Required for the tag-grouping path; without
  it the chunker can only use the path-segment grouping rule for every
  OpenAPI source.

  - Yes; tags are plumbed through `annotations.tags` as a list of strings.
    - Task: update `ForgeEngine.forge_tools` to extract operation tags into `annotations.tags`.
- **Embedding kind separation** — should use cases share the existing
  HNSW index with tools/prompts/skills, or live in a sibling index?
  Sharing is simpler; separating lets us tune per-kind. Default:
  share for v1.
  
  - They may share the index. Use-case records have a `kind` field set to `use_case`, and retrieval filters on that. Future work can migrate to a separate index if needed.

- **Cost-estimate source** — hard-code provider rate cards into the
  observability layer, or read from `[llm.pricing]` config? Default:
  config block with sensible defaults.

  - we don't need to track costs but we should report usage, tokens, and convert to cost if cost per token is available. It's a nice to have feature which we should be able to introspect on the provider to get. Irrelevant for local models.  

## What this design intentionally does NOT cover

- The skill side of the pipeline. See
  [skill-generation.md](skill-generation.md).
- Pattern mining (co-occurrence, sequence, idiom, constraint).
- Cross-source use cases.
- Feedback aggregation or re-mining.
- Atlas / code graph / structured entity model.
