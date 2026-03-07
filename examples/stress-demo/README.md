# ToolSearch Proxy Stress Test Demo

This demo sets up an MCP server with **250+ tools** to demonstrate how ToolSearch prevents "Context Window Bloat."

## Prerequisites
- **ToolSearch** (built from the spec in `~/tool-search/`)
- **Python 3.10+** (with `uv` installed)

## Setup

1. **Start the Demo Server**:
   ```bash
   cd ~/tool-search-demo/
   uv run server.py
   ```

2. **Configure ToolSearch**:
   Add the demo server to your `~/.toolsearch/config.toml`:
   ```toml
   [servers.stress-demo]
   command = "uv"
   args = ["--directory", "/home/codeninja/tool-search-demo", "run", "server.py"]
   enabled = true
   ```

3. **Index the Server**:
   ```bash
   tool-search index
   ```
   *ToolSearch will now index all 250 tools from the demo server.*

4. **Connect your Agent (Claude Code / Cursor)**:
   Point your client to the ToolSearch Proxy instead of the server directly:
   ```json
   "toolsearch-proxy": {
     "command": "tool-search",
     "args": ["proxy"]
   }
   ```

## Test Scenario

### Without ToolSearch
If you connected the demo server directly, the agent would receive a `tools/list` response containing **250+ JSON tool definitions**. This would consume ~150k tokens and cause the agent to hallucinate or time out.

### With ToolSearch
1. Ask the agent: *"I need a billing phrase for Dallas."*
2. **The Discovery Loop**:
   - The agent (via `CLAUDE.md` instructions) calls `toolsearch_context("Billing phrase for Dallas")`.
   - ToolSearch sets the active semantic context.
   - The agent calls `tools/list`.
   - ToolSearch filters the 250 tools and returns only `billing_phrase_1` through `billing_phrase_10`.
   - The agent correctly selects and calls `billing_phrase_4`.

## Files
- `server.py`: The FastMCP server generating 250 category-based tools.
- `pyproject.toml`: Dependency management via `uv`.
