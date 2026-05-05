---
name: mcp-gateway-configure-sources
description: Use when adding, removing, or troubleshooting upstream sources for the mcp-semantic-gateway — native MCP servers, OpenAPI / Swagger specs, or hand-authored skill libraries. Covers `~/.mcp_semantic_gateway/config.toml` syntax, auth wiring, and the `index` rebuild loop.
---

# Configure upstream sources

Sources live in `~/.mcp_semantic_gateway/config.toml` under `[servers.<name>]` tables. Three source types are supported: `mcp`, `openapi`, and `skill`.

## Native MCP server

Spawned over stdio, just like the agent itself spawns one.

```toml
[servers.github]
type = "mcp"
command = "npx"
args = ["@modelcontextprotocol/server-github"]
env = { GITHUB_PERSONAL_ACCESS_TOKEN = "${GITHUB_TOKEN}" }
```

The gateway harvests `tools/list` once, indexes the descriptions, and forwards `tools/call` transparently when the agent invokes one of the upstream tools.

## OpenAPI / Swagger spec

Point at any reachable spec URL or local file. The gateway forges live MCP tools from the operations.

```toml
[servers.billing]
type = "openapi"
url = "https://internal.example.com/openapi.json"
generate_skills = true                          # opt in to skill synthesis

# Auth — pick one
[servers.billing.auth]
type = "bearer"
token_env = "BILLING_API_TOKEN"

# or:
# type = "basic"
# username_env = "..."
# password_env = "..."

# or:
# type = "header"
# header = "X-Api-Key"
# value_env = "..."
```

Setting `generate_skills = true` opts the source into the `synth` pipeline (see the `mcp-gateway-synthesize-skills` skill). Leave it off when you only want raw tool discovery.

## Hand-authored skill library

A directory of `SKILL.md` packages following the [Agent Skills](https://github.com/anthropics/skills) format.

```toml
[servers.team_skills]
type = "skill"
path = "/abs/path/to/skills"
```

After `mcp-semantic-gateway synth init-skill-source` the gateway adds an entry like this for the auto-generated skill library at `~/.mcp_semantic_gateway/skills/`.

## LLM provider (only needed for synthesis)

```toml
[llm]
provider = "anthropic"            # or "openai-compatible"
model = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"

# OpenAI-compatible example (Ollama, OpenRouter, vLLM, etc.):
# provider = "openai-compatible"
# base_url = "http://localhost:11434/v1"
# model = "qwen2.5-coder"
# api_key_env = "OPENAI_API_KEY"
```

If you only do tool retrieval and never `synth`, you can leave `[llm]` out entirely.

## After every config change

```bash
mcp-semantic-gateway index
```

Re-embeds every tool, prompt, and skill into the local vector store. Cheap on small catalogs; cached per-source on large ones.

## Common pitfalls

- **Env vars not interpolated** — values like `${GITHUB_TOKEN}` are read from the *environment of the gateway process*, not the shell that ran `index`. If you launch the proxy from an MCP client, that client's environment must export the var.
- **OpenAPI spec fails to load** — confirm `curl -fsSL <url>` works from the same machine. Specs that require auth to *fetch* are not supported; mirror them locally first.
- **A new server's tools don't show up** — you forgot to re-run `index`. The proxy reads from the cached registry, not from upstream on every turn.
