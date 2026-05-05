# Petstore chat demo

Interactive multi-turn chat with a model that has **native MCP access** to a
petstore backend, mediated by `mcp-semantic-gateway`. Renders the live MCP
event stream so you can watch every `tools/list` and `tools/call` go by.

```
chat.py ─▶ openai-agents Runner ─▶ Agent
                │
                │ mcp_servers=[ MCPServerStdio(...) ]
                ▼
        mcp-semantic-gateway proxy   (real stdio MCP server)
                │  (every JSON-RPC line tee'd to mcp-events.log)
                ▼
        OpenAPIExecutor ─▶ petstore HTTP backend (FastAPI)
```

The chat does **no** tool transposition or result reshaping — that's all
inside the OpenAI Agents SDK's `MCPServerStdio`.

## Install

The example needs `openai-agents` (which pulls in `mcp`), `click`,
`tomlkit`, and `rich` — all in the dev dependency group:

```bash
uv sync --dev
```

## Run

### Multi-turn (default)

```bash
export OPENAI_API_KEY=sk-...
python examples/petstore_chat/chat.py
```

You'll see a status panel showing what was created, then a `you ▸` prompt.
Each turn carries history; type `/clear` to reset, `/quit` (or Ctrl-D) to
leave.

### Single-shot

Pass a query and the script exits after one turn:

```bash
python examples/petstore_chat/chat.py "list available pets"
```

### Any OpenAI-compatible endpoint (Ollama, OpenRouter, vLLM, …)

```bash
# Local Ollama
python examples/petstore_chat/chat.py \
  --model qwen3.5:latest \
  --base-url http://localhost:11434/v1 \
  --api-key-env OLLAMA_API_KEY

# OpenRouter
python examples/petstore_chat/chat.py \
  --model anthropic/claude-sonnet-4-5 \
  --base-url https://openrouter.ai/api/v1 \
  --api-key-env OPENROUTER_API_KEY
```

### Other flags

- `--rebuild-index` — discard the persistent index and re-index from the
  petstore's auto-generated OpenAPI spec.
- `--verbose-rpc` — print full JSON for every MCP request and response,
  not just a one-line summary.
- `--generate-skills` — run the gateway's synthesis pipeline (mine →
  cluster → SKILL.md) using the chat's LLM, then add the generated
  skills as a Skill source and re-index so the agent can discover them
  via `mcp_semantic_gateway_find_skills`. See below.

## Skill synthesis (`--generate-skills`)

The gateway has a use-case mining + skill synthesis pipeline that turns a
tool catalog into agent-skills-spec `SKILL.md` packages. Pass the flag and
the chat will:

1. Index the petstore tools (as usual).
2. Mine use cases from each tool chunk via the chat's configured LLM.
3. Cluster the use cases by embedding similarity.
4. Synthesize one `SKILL.md` per cluster.
5. Write a `[servers.petstore-skills]` Skill source to `config.toml`
   (path is **relative to the config file**, not absolute).
6. Re-index so the skills land in the gateway's catalog as `item_type =
   "skill"` rows alongside the OpenAPI tools.

Output goes to `gateway_state/synth/skills/<server_id>/<source_hash>/<skill_id>/v1/`:

```
gateway_state/synth/skills/petstore/6feecd37843a/manage-petstore-inventory/v1/
├── SKILL.md       ← agent-skills-spec frontmatter + procedure
└── .meta.json     ← skill_id, source, tool_dependencies, prompt version
```

Both files are committed with the example so consumers can read what the
LLM produced without running synthesis themselves.

After generation, the chat shows a status panel like:

```
indexed tools     4: createPet, deletePet, getPet, listPets
indexed skills    1: manage-petstore-inventory
```

…plus a "generated skills" panel listing each skill's tool dependencies
and on-disk path. Inside the chat, an agent will discover the skill via
`mcp_semantic_gateway_find_skills` when the user asks something
multi-step like "set up petstore inventory" — visible in the live RPC
event stream.

## Visibility

On startup you get a status panel showing exactly what was created:

```
╭─ petstore-chat ──────────────────────────────────────────────────╮
│ petstore backend  http://127.0.0.1:51041                         │
│ gateway state     examples/petstore_chat/gateway_state           │
│ config            examples/petstore_chat/gateway_state/config.toml │
│ index             …/index/metadata.db  [reused from disk]        │
│ rpc event log     …/mcp-events.log                               │
│ indexed tools     4: createPet, deletePet, getPet, listPets      │
╰──────────────────────────────────────────────────────────────────╯
```

Every MCP request and response prints live:

```
[21:22:01] → MCP initialize #0
[21:22:01] ← MCP result #0  proto 2024-11-05
[21:22:01] → MCP tools/list #1
[21:22:01] ← MCP result #1  7 tools
[21:22:02] → MCP tools/call #2  listPets({"status": "available"})
[21:22:02] ← MCP result #2  1 content block(s)
```

`isError` results are flagged inline so you can see when an upstream tool
call failed without scrolling logs:

```
[21:22:36] → MCP tools/call #5  deletePet({"pet_id": "99"})
[21:22:36] ← MCP result #5  isError; 2 content block(s)
```

The full raw RPC log is at `gateway_state/mcp-events.log` (regenerated
each session); pass `--verbose-rpc` to dump full JSON to the console.

## What's in `gateway_state/` (and what's committed)

```
gateway_state/
├── .gitignore                  ← committed
├── config.toml                 ← gitignored (regenerated each run; URL embeds an ephemeral port)
├── index/
│   ├── metadata.db             ← committed (port-independent)
│   └── vectors.db              ← committed (port-independent)
├── synth/                      ← committed when --generate-skills has run
│   └── skills/petstore/<hash>/<skill_id>/v1/
│       ├── SKILL.md
│       └── .meta.json
├── logs/                       ← gitignored
├── models/                     ← gitignored (sentence-transformer cache)
└── mcp-events.log              ← gitignored (regenerated each session)
```

Why the index is safe to commit: FastAPI's auto-generated OpenAPI spec
contains no `servers` block, so `route_metadata.servers` ends up empty in
the SQLite index. The runtime `ServerConfig.base_url` (set fresh from the
ephemeral petstore port each session) drives URL choice at execution
time. Re-run with `--rebuild-index` if you change `petstore_backend.py`.

## Use the same gateway with other MCP clients

`mcp-semantic-gateway proxy` is a stock stdio MCP server. To plug it into
Claude Desktop, MCP Inspector, or any other MCP client:

1. Configure `~/.mcp_semantic_gateway/config.toml` with your OpenAPI source
   (or set `MCP_SEMANTIC_GATEWAY_HOME` to point elsewhere).
2. `mcp-semantic-gateway index`
3. Point the client at `mcp-semantic-gateway proxy`.

The chat CLI is just one MCP client among many — the gateway's stdio
contract is identical regardless of who's on the other side.

## Files

- `petstore_backend.py` — FastAPI app with four operations and seeded data.
- `chat.py` — interactive chat CLI (Click + openai-agents + rich).
- `gateway_state/` — gateway working directory; see breakdown above.
