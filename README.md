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

**Deep Dive**: For a full breakdown of the layered architecture, domain models, and state machines, see our [Full Technical Specification](docs/specs/SPEC.md).

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

### Community
Join Dallas and the team in the [OpenClaw Discord](https://discord.gg/clawd) to discuss the future of agentic infrastructure.

---

*Built by [codeninja](https://github.com/codeninja) and the OpenClaw Agentic Dev Team.*
