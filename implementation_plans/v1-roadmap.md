# ToolSearch Implementation Plan (V1)

## Phase 1: Local Foundation (Days 1-2)
- [ ] Initialize Python (Typer + FastEmbed + SQLite) project in `src/tool_search/`.
- [ ] Implement `MCP-Scanner`: Scan `~/.config/Claude/claude_desktop_config.json` and other common MCP config locations.
- [ ] Implement `Tool-Indexer`: Use `fastembed` for sub-millisecond local embedding of tool descriptions.

## Phase 2: Search & Retrieval (Days 3-4)
- [ ] Implement `Tool-Searcher`: Vector retrieval using `chromadb` or simple cosine similarity over NumPy.
- [ ] Implement `Tool-Injector`: Utility to format discovered tool definitions into LLM-readable JSON schemas (Claude/OpenAI/Gemini formats).
- [ ] Implement `Tool-Registry`: SQLite persistence for tool metadata, usage counts, and server paths.

## Phase 3: CLI & Connectors (Days 5-6)
- [ ] Build `tool-search bootstrap <client>`:
    - [ ] `claude`: Inject a specialized `find_tools` tool into the Claude Code configuration.
    - [ ] `cursor`: Proxy setup for Cursor's MCP layer.
    - [ ] `gemini-cli`: Wrapper script to pre-filter tools before calling the Gemini API.

## Phase 4: Packaging & Distribution (Days 7-8)
- [ ] Add **PyPI Packaging**: Create `pyproject.toml` and `setup.cfg`.
- [ ] Add **Audit Log**: A searchable history of every tool discovery and retrieval event.
- [ ] **Release**: Publish `tool-search` 1.0.0 on PyPI.

## V1 Milestone: "The 30-Tool Agent"
- 1 Local Registry indexing 5+ MCP servers (30+ tools total).
- 1 Agent (Claude or Gemini) that only has 3 tools active at a time, but can "search" and activate any of the 30 tools on demand.
- Full context window optimization (40%+ token reduction).
