# Setup Guide

This guide takes you from a fresh checkout to an agent that can talk to
your APIs through the gateway. It covers four flavors of source —
**native MCP servers**, **OpenAPI / Swagger specs**, **hand-authored
skills**, and **auto-generated skills** — and shows how to validate each
step before moving on.

If you only have 30 minutes, the *Quick Start* in the
[project README](https://github.com/codeninja/mcp-semantic-gateway#quick-start)
gets you to a working proxy. This guide goes deeper: troubleshooting,
diagnostics, and production-grade configuration.

---

## 1. Install

The gateway runs as a single Python package. Pick the install style that
matches how you'll deploy it.

=== "From PyPI (recommended)"

    ```bash
    pip install mcp-semantic-gateway
    ```

=== "From source (for contributors)"

    ```bash
    gh repo clone codeninja/mcp-semantic-gateway
    cd mcp-semantic-gateway
    uv sync --dev
    ```

    Replace `mcp-semantic-gateway` in the commands below with `uv run
    mcp-semantic-gateway`.

Verify the install:

```bash
mcp-semantic-gateway --version
```

---

## 2. Initialize the data directory

The gateway keeps its config, vector index, logs, and cache under a
single home directory:

```bash
mcp-semantic-gateway init
```

This creates `~/.mcp_semantic_gateway/` with a starter `config.toml`,
empty `index/`, `logs/`, and `models/` subdirs.

To use a different location (multi-tenant deployments, isolated test
runs, CI), set:

```bash
export MCP_SEMANTIC_GATEWAY_HOME=/var/lib/mcp-semantic-gateway
mcp-semantic-gateway init
```

The gateway honors `MCP_SEMANTIC_GATEWAY_HOME` everywhere — config
lookup, index storage, logs, generated skills.

---

## 3. Wire up sources

All sources live under `[servers.<id>]` in `config.toml`. The `<id>`
is yours to pick — it shows up in tool routing, logs, and diagnostics.

### 3.1 Native MCP servers

Any process that speaks stdio MCP. Examples: `@modelcontextprotocol/server-github`,
`@modelcontextprotocol/server-slack`, your own Python or Node MCP
implementation.

```toml
[servers.github]
type = "mcp"
command = "npx"
args = ["@modelcontextprotocol/server-github"]
```

The gateway spawns `command` with `args` in a child process and
multiplexes JSON-RPC over stdio. The child inherits the parent
process's environment, so the canonical recipe for secrets is:

```bash
export GITHUB_TOKEN=ghp_...
mcp-semantic-gateway proxy
```

If you need to set an env var only for this one server, use the `env`
block — but values are passed **verbatim** (no `$VAR` interpolation),
so put literal values there or leave it out entirely:

```toml
[servers.staging]
type = "mcp"
command = "npx"
args = ["@modelcontextprotocol/server-github"]
env = { GITHUB_API_URL = "https://github.staging.example.com/api/v3" }
```

Never paste live secrets into the config file — keep them in your
shell environment so the file stays committable.

### 3.2 OpenAPI / Swagger specs

Point the gateway at a JSON or YAML spec and it will *forge* MCP tools
from every operation:

```toml
[servers.weather]
type = "openapi"
url = "https://api.weather.gov/openapi.json"
```

For an API behind authentication, set up `auth`:

=== "API key in header"

    ```toml
    [servers.billing]
    type = "openapi"
    url = "https://internal.example.com/openapi.json"

    [servers.billing.auth]
    type = "api_key"
    header_name = "X-API-Key"
    api_key_env = "BILLING_API_KEY"
    ```

=== "Bearer token"

    ```toml
    [servers.api]
    type = "openapi"
    url = "https://api.example.com/openapi.json"

    [servers.api.auth]
    type = "bearer"
    bearer_token_env = "API_TOKEN"
    ```

=== "HTTP Basic"

    ```toml
    [servers.legacy]
    type = "openapi"
    url = "https://legacy.example.com/openapi.yaml"

    [servers.legacy.auth]
    type = "basic"
    basic_username_env = "LEGACY_USER"
    basic_password_env = "LEGACY_PASS"
    ```

!!! note "Secrets live in env vars, not the config file"

    `auth.*_env` fields **name** the environment variable; the executor
    reads it at request time. The config file should be committable to
    version control without leaking credentials.

If your upstream's spec is missing or wrong about the base URL, override
it explicitly:

```toml
[servers.legacy]
type = "openapi"
url = "https://internal.example.com/openapi.json"
base_url = "https://api.legacy.example.com/v2"
```

### 3.3 Hand-authored skills

A skill is a `SKILL.md` file with frontmatter (name, description,
`allowed-tools`) and a markdown body describing a procedure. Drop a
directory of them into the gateway:

```toml
[servers.runbooks]
type = "skill"
path = "./skills/runbooks"
```

Relative paths resolve against the directory containing the config
file, not against the current working directory — so a config moved
between machines keeps its meaning.

### 3.4 Auto-generated skills

Set `generate_skills = true` on an OpenAPI source and the gateway will
mine use cases out of its tool catalog and synthesize a library of
`SKILL.md` packages on top of it:

```toml
[servers.billing]
type = "openapi"
url = "https://internal.example.com/openapi.json"
generate_skills = true

# Required when any source has generate_skills = true.
[llm]
provider = "anthropic"
model = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"
```

`provider = "openai-compatible"` plus a `base_url` works for OpenAI,
OpenRouter, vLLM, Ollama, or any OpenAI-shaped endpoint. See the
[Use-Case Synthesis design doc](design/use-case-synthesis.md) for the
full pipeline and prompt cache strategy.

---

## 4. Build the index

Once sources are configured, run the indexer:

```bash
mcp-semantic-gateway index
```

This walks every enabled source, harvests tools / prompts / skills,
embeds each one with the local `all-MiniLM-L6-v2` model, and writes:

* `~/.mcp_semantic_gateway/index/metadata.db` — SQLite catalog
* `~/.mcp_semantic_gateway/index/vectors.db` — hnswlib HNSW index

Re-run after any config change. Embeddings are recomputed; SQLite
inserts are `INSERT OR REPLACE`, so it's idempotent.

---

## 5. (Optional) Synthesize skills

If any source has `generate_skills = true`:

```bash
mcp-semantic-gateway synth                     # mine + cluster + write
mcp-semantic-gateway synth init-skill-source   # register the output dir
mcp-semantic-gateway index                     # re-index so they're searchable
mcp-semantic-gateway synth status              # cache hits, token spend, rejections
```

The cache key is `(server, source_hash, chunk_hash, model,
prompt_version)`. Re-running against unchanged inputs is a zero-cost
no-op — bump `[skill_generation] prompt_version` to force regeneration.

---

## 6. Validate with `doctor`

Before pointing an agent at the gateway, run the diagnostic command:

```bash
mcp-semantic-gateway doctor
```

It walks six checks and prints an actionable remediation for each
failure:

| Check | What it verifies |
|---|---|
| `config-file` | `config.toml` exists, parses, validates |
| `gateway-home` | data dir exists |
| `index-metadata-db` | metadata DB present and non-empty |
| `index-vector-db` | vector index present |
| `auth-env:<id>` | every named env var is set |
| `openapi-reachable:<id>` | each OpenAPI URL returns 2xx |
| `skill-path:<id>` | every skill source resolves to a real dir |
| `route-metadata` | every indexed OpenAPI tool carries route metadata |

Exit code is non-zero if any check fails, so you can wire it into CI:

```bash
# .github/workflows/gateway.yml
- run: mcp-semantic-gateway doctor --no-network
```

`--no-network` skips OpenAPI reachability — useful when the spec is on
an internal network the runner can't reach. `--json` emits machine-readable
output for scripts.

---

## 7. Sanity-check with `search`

Before connecting an agent, verify retrieval works the way you expect:

```bash
mcp-semantic-gateway search "list pets"
```

Returns a Rich table with rank, score, item type, name, source ID, and
description for the top matches. If the right tool isn't surfacing,
this is the fastest way to triage.

```bash
mcp-semantic-gateway search "refund a customer" --type skill
mcp-semantic-gateway search "list users" --top-k 3 --json
```

`--type` restricts to `tool`, `skill`, or `prompt`. `--json` is for
scripts and tests.

---

## 8. Run the proxy

Two transports are supported.

### Stdio (recommended for desktop agents)

```bash
mcp-semantic-gateway proxy
```

This is what you'll point Claude Desktop, Claude Code, Cursor, and
other MCP-speaking clients at. See *Connect your agent* below.

### HTTP (for remote / multi-tenant)

```bash
mcp-semantic-gateway server
```

Listens on `127.0.0.1:8000` by default (configurable via `[http]` in
config). Each request should carry `X-Tenant-ID` so contexts don't leak
between clients.

The HTTP surface keeps the legacy MCP HTTP+SSE compatibility shape:
`GET /sse` opens the event stream and first emits an `endpoint` event
containing the session-specific `POST /message?sessionId=...` URI.
JSON-RPC responses for that session are delivered as SSE `message`
events; direct non-SSE `POST /message` still returns a JSON-RPC response
body for simple integrations.

---

## 9. Connect your agent

=== "Claude Desktop / Code"

    ```json
    "mcpServers": {
      "mcp-semantic-gateway": {
        "command": "mcp-semantic-gateway",
        "args": ["proxy"]
      }
    }
    ```

=== "From source (worktree dev)"

    ```json
    "mcpServers": {
      "mcp-semantic-gateway": {
        "command": "uv",
        "args": [
          "--directory", "/path/to/mcp-semantic-gateway",
          "run", "mcp-semantic-gateway", "proxy"
        ]
      }
    }
    ```

=== "Cursor / other"

    Use the same stdio command. Cursor's settings expose an `mcpServers`
    block identical to Claude Desktop.

After restarting your agent, it should see four gateway tools:

* `mcp_semantic_gateway_context` — set the search context for this turn
* `mcp_semantic_gateway_find_prompts` — semantic prompt search
* `mcp_semantic_gateway_find_skills` — semantic skill search
* `mcp_semantic_gateway_get_skill` — fetch a skill body by name

…plus the top-`k` upstream tools matching the current context.

---

## 10. Troubleshooting

### "search returns nothing"

* Run `mcp-semantic-gateway doctor` — most empty-result cases are
  unbuilt or stale indexes.
* Try a broader query: `search "pets"` instead of `search "list all
  pets sold yesterday"`.
* Check `[retrieval] min_score` — too aggressive a floor filters
  everything out.

### "OpenAPI tools forged but `tools/call` fails"

* Confirm `auth.*_env` variables are set in the proxy's environment
  (the *child* of your agent, not your shell — desktop apps don't
  always inherit your shell env). Use a launcher wrapper that
  exports first.
* Run `doctor --no-network` for a quick auth-env sanity check.

### "Spec parse fails or the wrong base URL is used"

* Set `[servers.<id>] base_url = "..."` to override the spec's
  `servers` block.
* If the spec is YAML, the gateway reads it transparently — no
  conversion needed.

### "Stale tools after editing config"

Re-run `mcp-semantic-gateway index`. The proxy reads from the static
index; it does not hot-reload config changes.

### "Where do I look for execution failures?"

`~/.mcp_semantic_gateway/logs/` carries structured JSONL events:

* `discovery.jsonl` — what the proxy returned for each `tools/list`
* `index.jsonl` — what was harvested and embedded on each index pass
* `synth/` — per-run synthesis diagnostics, rejections, and token spend

Each line is grep-friendly JSON with a stable schema.

---

## Where to go next

* **[CLI reference](https://github.com/codeninja/mcp-semantic-gateway#cli-reference)** —
  every command and its flags.
* **[Use-Case Synthesis design](design/use-case-synthesis.md)** —
  how the gateway mines workflows from your APIs.
* **[Skill Generation design](design/skill-generation.md)** — how
  generated skills are validated, hashed, and cached.
* **[Petstore demo](https://github.com/codeninja/mcp-semantic-gateway/tree/main/examples/petstore_chat)** —
  end-to-end walkthrough you can run locally in five minutes.
