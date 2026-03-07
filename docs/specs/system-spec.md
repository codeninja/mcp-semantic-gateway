# ToolSearch System Specification (V1)

## 1. Goal
Provide a decentralized, open-source protocol for semantic tool discovery in any Agentic AI workflow. 

## 2. Core Architecture: The Proxy
ToolSearch acts as a "Statistical Filtering Proxy" between the LLM and its MCP clients.

### 2.1 The Semantic Index
1. **Collector**: Scans `mcp-config.json` or equivalent to find all active MCP servers.
2. **Indexer**: Fetches the `list_tools` output from each server.
3. **Embedder**: Generates semantic embeddings for each tool's `name` and `description`.
4. **Vector Store**: Local SQLite/ChromaDB store for sub-millisecond retrieval.

### 2.2 The Discovery Loop (Middleware)
1. **User Prompt**: "Read the logs from the GKE pod."
2. **ToolSearch Interceptor**: Performs a vector search for "GKE pod logs".
3. **Filtering**: Identifies `k8s-get-logs` as a 95% match.
4. **Injection**: Injects *only* the `k8s-get-logs` tool definition into the system prompt.
5. **Execution**: The LLM calls the tool with standard MCP semantics.

## 3. Bootstrap Support (Connectors)

### 3.1 Claude Code / Codex
- **Method**: Injects a specialized "Tool Finder" tool into the base `claude` / `codex` configuration.
- **Workflow**: If the agent needs a tool it doesn't have, it calls `tool-search search <intent>`. ToolSearch returns the full definition, and the agent "learns" it mid-session.

### 3.2 Cursor
- **Method**: A background proxy that intercepts MCP tool definitions in Cursor's hidden system prompt and filters them based on the active file/chat context.

### 3.3 Gemini CLI
- **Method**: CLI wrapper that automatically pre-filters the `tools` list passed to the Gemini-3-Flash API.

## 4. CLI Commands
- `tool-search init`: Setup local registry.
- `tool-search index`: Re-index all available MCP servers.
- `tool-search proxy --port 8000`: Start the semantic discovery proxy.
- `tool-search bootstrap <client>`: Automatic configuration for Claude, Cursor, Gemini.

## 5. Security & Privacy
- **Local-First**: All embeddings and indexing happen on the user's machine.
- **No Data Leakage**: Original user prompts are only used for tool matching; tool definitions never leave the local environment.
- **Audit Logs**: Every "Tool Discovery" event is logged for human review.
