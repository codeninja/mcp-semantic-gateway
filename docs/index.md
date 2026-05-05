# MCP Semantic Gateway

**Stop bloating your agent's context. Start scaling your toolsets.**

MCP Semantic Gateway is an open-source, local-first middleware for the
Model Context Protocol (MCP). It solves the "too many tools" problem by
semantically filtering your tool registry, prompts, and skills on the
fly, ensuring your LLM stays focused, accurate, and cost-efficient.

---

## What is it?

If you have 30+ MCP tools, your agent's context window is saturated
with JSON definitions before it even starts thinking. This leads to
**hallucinations**, **high token costs**, and **declining accuracy**.
MCP Semantic Gateway sits as a proxy between your client (Claude
Desktop, Claude Code, Cursor) and your servers, serving only the tools
relevant to your current task.

---

## Quick start

```bash
# Install
pip install mcp-semantic-gateway

# Initialize the data directory + starter config
mcp-semantic-gateway init

# Edit ~/.mcp_semantic_gateway/config.toml to add sources, then:
mcp-semantic-gateway index
mcp-semantic-gateway doctor          # validate the setup
mcp-semantic-gateway search "list pets"   # sanity-check retrieval
mcp-semantic-gateway proxy           # connect your agent over stdio
```

Full walkthrough: see the [Setup Guide](guide.md).

### Configure sources

Add MCP servers, OpenAPI specs, or skill directories to
`~/.mcp_semantic_gateway/config.toml`:

```toml
[servers.github]
type = "mcp"
command = "npx"
args = ["@modelcontextprotocol/server-github"]

[servers.weather]
type = "openapi"
url = "https://api.weather.gov/openapi.json"
```

### Connect your agent

For Claude Desktop / Code:

```json
"mcpServers": {
  "mcp-semantic-gateway": {
    "command": "mcp-semantic-gateway",
    "args": ["proxy"]
  }
}
```

Tell your agent what you're doing. It calls
`mcp_semantic_gateway_context("debugging kubernetes logs")` and the
gateway activates only the relevant tools.

---

## How it works

MCP Semantic Gateway operates as a **statistical filtering proxy**. It
doesn't just forward requests — it transforms the environment based on
intent.

1. **Ingestion.** The collector harvests tools from native MCP
   servers, forges new tools from OpenAPI / Swagger documents, and
   indexes hand-authored or generated skill packages.
2. **Semantic index.** A local SQLite + hnswlib vector store keeps
   embeddings for every tool, prompt, and skill.
3. **Discovery loop.**
    * The agent sets a context via `mcp_semantic_gateway_context`.
    * The gateway intercepts the next `tools/list` or `prompts/list`
      request.
    * It performs a sub-millisecond k-NN search and returns only the
      top matches.
    * `tools/call` requests are transparently routed to the correct
      upstream server with the right authentication.

For the full architecture — layered domain models, state machines,
synthesis pipeline — see the [design docs](design/use-case-synthesis.md).

---

## Diagnostics

Two operator-facing CLI commands keep installs healthy:

* **`mcp-semantic-gateway doctor`** validates config, index presence,
  auth env vars, OpenAPI reachability, skill paths, and route metadata
  coverage. Exits non-zero on any failure with an actionable
  remediation.
* **`mcp-semantic-gateway search "<query>"`** runs the same retrieval
  path the proxy uses and prints a Rich table of matches with scores —
  the fastest way to triage "why didn't my tool show up?".

Both have a `--json` flag for scripts and CI.

---

## Contributing

* **Bridge a niche API.** Add an example to `/examples` showing how
  you wired up your stack.
* **Improve the forge.** Help refine the OpenAPI → MCP transformation.
* **New backends.** Chroma, pgvector, remote embedding providers — all
  open.
* **Tell us where retrieval misses.** Open an issue with the query
  and the catalog and we'll fix it.

```bash
git checkout -b feat/your-thing
uv run pytest tests/test_e2e.py
# PR it
```

See [Contributing](contributing.md) for local setup, test layout, and
the recipe for adding a new source type.

---

*Built by [codeninja](https://github.com/codeninja).*
