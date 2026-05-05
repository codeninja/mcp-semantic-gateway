---
name: mcp-gateway-getting-started
description: Use when the user is setting up the mcp-semantic-gateway for the first time, asks how to install or initialize it, or wants to point a coding agent (Claude Desktop, Claude Code, Cursor, Codex, opencode, pi) at the gateway. Covers `init`, the data directory layout, and the MCP client connection block.
---

# Getting started with the MCP Semantic Gateway

The gateway is a single MCP server that fronts every other tool source you have — native MCP servers, OpenAPI specs, hand-authored skills, generated skills — and serves only the semantically relevant subset to the agent on each turn.

## Install

```bash
pip install mcp-semantic-gateway
# or, from a clone:
uv sync
```

The `mcp-semantic-gateway` console command is registered on install.

## Initialize the data directory

```bash
mcp-semantic-gateway init
```

This creates `~/.mcp_semantic_gateway/` with a starter `config.toml`. Everything the gateway owns lives under that directory:

- `config.toml` — sources, LLM provider, retrieval knobs
- `index/metadata.db` — SQLite tool registry
- `index/embeddings/` — local hnswlib vectors
- `skills/<server>/...` — generated and hand-authored `SKILL.md` packages

No data leaves the box during indexing. Embeddings are computed locally with `all-MiniLM-L6-v2`.

## Point an agent at it

The gateway speaks stdio MCP via the `proxy` subcommand. Add this block to whichever MCP client config the agent uses:

```json
{
  "mcpServers": {
    "mcp-semantic-gateway": {
      "command": "mcp-semantic-gateway",
      "args": ["proxy"]
    }
  }
}
```

Common config locations:

- **Claude Desktop**: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows)
- **Claude Code**: `~/.claude.json` or per-project `.mcp.json`
- **Cursor**: `~/.cursor/mcp.json`
- **Codex / opencode / pi**: see each tool's MCP server registration docs

After restarting the client, the agent gains four gateway tools:

- `mcp_semantic_gateway_context` — set retrieval context
- `mcp_semantic_gateway_find_prompts` — search prompt library
- `mcp_semantic_gateway_find_skills` — search skill catalog
- `mcp_semantic_gateway_get_skill` — fetch full `SKILL.md` body

Upstream tools and skills surface dynamically through these four; the agent never sees the long tail unless it asks.

## Verify it works

```bash
# Confirm the binary is on PATH
mcp-semantic-gateway --version

# Sanity-check retrieval (after `index`)
mcp-semantic-gateway search "list pull requests"
```

If `search` returns no results, you have not run `mcp-semantic-gateway index` yet, or no sources are configured. See the `mcp-gateway-configure-sources` skill.

## Next steps

- **Add sources** — `mcp-gateway-configure-sources`
- **Generate skills from OpenAPI** — `mcp-gateway-synthesize-skills`
- **Use the gateway from inside an agent** — `mcp-gateway-discover-by-intent`
