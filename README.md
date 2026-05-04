# MCP Semantic Gateway: Semantic Discovery Middleware for MCP 

**Stop Bloating Your Agent's Context. Start Scaling Your Toolsets.**

MCP Semantic Gateway is an open-source, local-first middleware for the Model Context Protocol (MCP). It solves the "Too Many Tools" problem by semantically filtering your tool registry, prompts, and skills on-the-fly, ensuring your LLM stays focused, accurate, and cost-efficient.

---

## 🚀 Part 1: Quick Start

### What is it?
If you have 30+ MCP tools, your agent's context window is saturated with JSON definitions before it even starts thinking. This leads to **hallucinations**, **high token costs**, and **declining accuracy**. MCP Semantic Gateway sits as a proxy between your client (Claude Code, Cursor) and your servers, serving only the tools relevant to your current task.

### Installation

**From PyPI (Recommended)**
```bash
pip install mcp-semantic-gateway
mcp-semantic-gateway init
```

**From Source**
```bash
# Clone the repository
gh repo clone codeninja/mcp-semantic-gateway
cd mcp-semantic-gateway

# Install dependencies and initialize
uv sync
uv run mcp-semantic-gateway init
```

### 1. Configure Your Sources
Add your MCP servers or OpenAPI specs to `~/.mcp_semantic_gateway/config.toml`:
```toml
[servers.github]
type = "mcp"
command = "npx"
args = ["@modelcontextprotocol/server-github"]

[servers.weather-api]
type = "openapi"
url = "https://api.weather.gov/openapi.json"
```

### 2. Build the Semantic Index
MCP Semantic Gateway embeds your tool descriptions locally using `all-MiniLM-L6-v2`.
```bash
mcp-semantic-gateway index
```

### 2a. (Optional) Synthesize Use Cases & Skills
For OpenAPI sources you opt into skill generation, the gateway can mine
real-world use cases out of the harvested tools and synthesize agent-skills-spec
SKILL.md packages so retrieval can match on workflow intent, not just tool
names.

```toml
[llm]
provider = "anthropic"          # or "openai-compatible"
model = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"

[servers.petstore]
type = "openapi"
url = "https://petstore3.swagger.io/api/v3/openapi.yaml"
generate_skills = true          # opt-in per server
```

```bash
# Mine + cluster + synthesize
mcp-semantic-gateway synth

# Or layer a provider config without editing config.toml:
mcp-semantic-gateway synth --config-overlay .env.openai

# Add a Skill-type server entry pointing at the generated skills
mcp-semantic-gateway synth init-skill-source

# Re-index so generated skills land in the vector store
mcp-semantic-gateway index

# Inspect status
mcp-semantic-gateway synth status
```

Re-running `synth` against the same source with the same model + prompt
version is a free no-op (cache hit).

### 3. Connect Your Agent
Point your client to the MCP Semantic Gateway Proxy.

**For PyPI Installation (Claude Desktop):**
```json
"mcpServers": {
  "mcp-semantic-gateway": {
    "command": "mcp-semantic-gateway",
    "args": ["proxy"]
  }
}
```

**For Source Installation (Claude Desktop):**
```json
"mcpServers": {
  "mcp-semantic-gateway": {
    "command": "uv",
    "args": ["--directory", "/path/to/mcp-semantic-gateway", "run", "mcp-semantic-gateway", "proxy"]
  }
}
```

### 4. Use It
Simply tell your agent what you're doing. The agent will call `mcp_semantic_gateway_context("debugging kubernetes logs")`, and MCP Semantic Gateway will instantly activate the relevant tools in the agent's registry.

---

## 🧠 Part 2: Technical Architecture

MCP Semantic Gateway operates as a **Statistical Filtering Proxy**. It doesn't just forward requests; it transforms the environment based on intent.

### How it Works:
1.  **Ingestion**: The `Collector` harvests tools from native MCP servers, "forges" new tools from OpenAPI/Swagger docs, and indexes local Agent Skills (`SKILL.md`).
2.  **Semantic Index**: A local SQLite + hnswlib vector store maintains embeddings for every tool, prompt, and skill.
3.  **The Discovery Loop**:
    *   The Agent sets a context via `mcp_semantic_gateway_context`.
    *   MCP Semantic Gateway intercepts the next `tools/list` or `prompts/list` request.
    *   It performs a sub-millisecond k-NN search and returns only the top-k matches.
    *   `tools/call` requests are transparently routed back to the correct upstream server.

### How the Use-Case & Skill Engine Works

Naked tool descriptions are great for `findPetsByStatus`-style queries but terrible at workflow intent ("clean up old orders"). Opt a source into `generate_skills = true` and the gateway runs an offline **synthesis pipeline** that turns harvested tools into discoverable, agent-readable workflows. The pipeline is invoked with `mcp-semantic-gateway synth` and works in five stages:

```
harvest ──► chunk ──► mine use cases ──► cluster ──► synthesize skills
                                              │
                                              ▼
                       .mcp_semantic_gateway/skills/<server>/<src_hash>/<id>/v1/
                                              │
                                       (next index pass)
                                              ▼
                                    semantic vector store
```

1.  **Chunking** (`ingestion/chunker.py`) — Harvested tools are grouped to fit an LLM call cleanly. OpenAPI sources prefer operation `tags` (decision U-4), then path-prefix (`/users/...`), then ordered fixed-size. Live MCP sources prefer shared name-prefixes (`gh_`, `slack_`). Each chunk gets a deterministic `chunk_id` and `chunk_hash` so re-runs are byte-stable.
2.  **Use-case mining** (`ingestion/use_case_miner.py`) — One LLM call per chunk via the provider abstraction in `llm/` (Anthropic native or OpenAI-compatible: OpenAI, OpenRouter, Gemini, Ollama, vLLM). Structured output is forced through a single `emit_use_cases` tool — no JSON repair, no freeform parsing. Each emitted record is then **deterministically validated**: description length 50–400 chars, every `linked_tool_names` entry must resolve in the chunk, confidence in `[0, 1]`. Hallucinated tool names are dropped before they reach disk and emitted as `record_rejected` events.
3.  **Caching** (decision U-7) — Cache key is `(server_id, source_hash, chunk_hash, model_id, prompt_version)`. A re-run on unchanged inputs makes **zero LLM calls**. Bumping `prompt_version` invalidates per-chunk cache; bumping the source bumps everything downstream. Use cases land in a dedicated `use_cases` SQLite table for richer provenance.
4.  **Clustering** (`ingestion/skill_clusterer.py`) — Use-case descriptions are embedded with the same `LocalEmbedder` the index already uses, then greedy-agglomerative-clustered by cosine similarity (default threshold `0.78`). Each cluster's medoid description becomes its representative; `cluster_hash` is sha256 of sorted member hashes.
5.  **Skill synthesis** (`ingestion/skill_synthesizer.py`) — One LLM call per cluster via a forced `emit_skill_package` tool. The output is a structured `SkillPackage`: `name`, `description`, `body_markdown`, `tool_dependencies`, optional `references`. Three deterministic passes gate publication (`ingestion/skill_validator.py`):
    *   **spec-conformance**: name matches `^[a-z][a-z0-9-]{1,63}$`; description in length bounds.
    *   **tool-grounding**: every name in `tool_dependencies` resolves in the harvested catalog (the dominant LLM hallucination path); body backticks are advisory and only flag obvious tool shapes (length ≥ 8 + underscore) so parameter names like `petId` aren't false-positives.
    *   **length-bounds**: body and per-reference lengths in range.
6.  **Atomic write** (`ingestion/skill_writer.py`) — Skills land at `.mcp_semantic_gateway/skills/<server_id>/<source_hash[:12]>/<skill-id>/v1/SKILL.md` (agent-skills-spec-conformant frontmatter + procedural body) plus a `.meta.json` sidecar. Writes go through `tmp + os.replace` so a half-written file can never be observed by the collector. Same `cluster_hash` → idempotent rewrite at the same slot; different cluster → numeric-suffix collision (`<id>-2`, `<id>-3`).

The `Collector.collect_skills()` path discovers the generated `SKILL.md` files on the next `mcp-semantic-gateway index` pass — no retrieval-side changes required. From the agent's perspective, generated skills look identical to hand-authored ones in the vector store, but they're keyed on workflow intent ("triage stale issues", "onboard a pet") rather than mechanical tool names.

**Observability** (`ingestion/observability.py`) — Every stage emits a structured event to `~/.mcp_semantic_gateway/logs/synthesis.jsonl`. Failures and rejected records produce per-chunk diagnostics under `.mcp_semantic_gateway/diagnostics/synthesis/<run_id>/`. The CLI prints a Rich-rendered run summary with token counts, cache hits, rejection breakdowns, and per-source cost (when the provider reports it).

**Deep Dive**: full design lives in [docs/design/use-case-synthesis.md](docs/design/use-case-synthesis.md) and [docs/design/skill-generation.md](docs/design/skill-generation.md). For the layered architecture, domain models, and state machines, see the [Full Technical Specification](docs/specs/SPEC.md).

---

## 🤝 Part 3: Contributing

We are building the "Garbage Collector for the Context Window," and we want your help!

### How to Contribute
- **Create a Bridge**: Have a niche API? Add an example to `/examples` showing how to bridge it.
- **Improve the Forge**: Help us refine the OpenAPI-to-MCP transformation logic.
- **New Backends**: We want to support more vector stores (Chroma, pgvector) and remote embedding providers.
- **Feedback**: Open an issue if you find a tool-selection hallucination we can't solve.

### Development Workflow
1.  Fork the repo.
2.  Create a feature branch: `feat/issue-number-description`.
3.  Run the E2E suite: `uv run pytest tests/test_e2e.py`.
4.  Submit a PR!

---

*Built by [codeninja](https://github.com/codeninja) and a Custom Agentic Development Engine*
