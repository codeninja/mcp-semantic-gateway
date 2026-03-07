# API -> MCP -> ToolSearch Bridge Demo

This example demonstrates how to take a raw REST API, bridge it into MCP, and then plug that into ToolSearch for semantic discovery.

## Components
1. `api_server.py`: A raw FastAPI server running on port 8080 (Existing Infra).
2. `mcp_bridge.py`: A FastMCP server that wraps the API endpoints as tools (The Bridge).
3. `ToolSearch`: The middleware that provides semantic discovery for these tools.

## Setup Instructions

1. **Start the API Server**:
   ```bash
   cd examples/api-bridge/
   uv run api_server.py
   ```

2. **Test the MCP Bridge**:
   ```bash
   uv run mcp_bridge.py
   ```

3. **Configure ToolSearch**:
   Add the bridge to your `~/.toolsearch/config.toml`:
   ```toml
   [servers.api-bridge]
   command = "uv"
   args = ["--directory", "/absolute/path/to/tool-search/examples/api-bridge", "run", "mcp_bridge.py"]
   enabled = true
   ```

4. **Index & Discover**:
   ```bash
   tool-search index
   tool-search search "financial and billing tools"
   ```

## Why this matters
By wrapping the API in an MCP bridge, you transform static endpoints into semantic tools. ToolSearch then ensures that if you have 1000 such endpoints, only the 5 relevant ones are sent to your LLM based on the current user intent.
