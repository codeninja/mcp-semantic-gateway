# Petstore chat demo

Single-shot agent CLI: ask one question to a model that has **native MCP
access** to a petstore backend, mediated by `mcp-semantic-gateway`.

```
chat.py ─▶ openai-agents Runner ─▶ Agent
                │
                │ mcp_servers=[ MCPServerStdio(...) ]
                ▼
         mcp-semantic-gateway proxy   (real stdio MCP server)
                │
                ▼
         OpenAPIExecutor ─▶ petstore HTTP backend (FastAPI)
```

The OpenAI Agents SDK's `MCPServerStdio` spawns the gateway as a
subprocess, discovers tools, and dispatches calls natively. The chat
script does **no** tool-shape translation, no result reshaping, no
JSON-RPC plumbing — that's all inside the SDK.

## Install

The example needs `openai-agents` (which pulls in `mcp`), `click`,
`tomlkit`, etc. — all in the dev dependency group:

```bash
uv sync --dev
```

## Run

### Real OpenAI

```bash
export OPENAI_API_KEY=sk-...
python examples/petstore_chat/chat.py "list the available pets"
```

Default model is `gpt-4o-mini`. Override via `--model`.

### Any OpenAI-compatible endpoint (Ollama, OpenRouter, vLLM, …)

Pass `--base-url`. The script switches to `OpenAIChatCompletionsModel`
and disables the SDK's tracing exporter (which posts to OpenAI proper).

```bash
# Local Ollama
python examples/petstore_chat/chat.py \
  --model qwen3.5:latest \
  --base-url http://localhost:11434/v1 \
  --api-key-env OLLAMA_API_KEY \
  "Add a turtle named Speedy with id 99, then list all pending pets"

# OpenRouter
python examples/petstore_chat/chat.py \
  --model anthropic/claude-sonnet-4-5 \
  --base-url https://openrouter.ai/api/v1 \
  --api-key-env OPENROUTER_API_KEY \
  "what's pet 99 status?"
```

No history, no memory: each invocation is fresh.

## What happens at startup

Each invocation:

1. Starts the FastAPI petstore in-process on an ephemeral port.
2. Creates a temp `MCP_SEMANTIC_GATEWAY_HOME` directory and writes a
   minimal `config.toml` pointing at the petstore.
3. Runs the indexer (`index_all`) to populate the gateway's SQLite +
   vector store from the petstore's auto-generated OpenAPI 3 spec.
4. Constructs `MCPServerStdio` with `command=python -m
   mcp_semantic_gateway.cli.main proxy` and the env override, so the
   subprocess reads from the temp dir (no clobbering of your real
   `~/.mcp_semantic_gateway/`).
5. Builds an `Agent` with `mcp_servers=[gateway]` and runs it once.

The proxy advertises four tools (`createPet`, `deletePet`, `getPet`,
`listPets`) and the agent picks among them via tool use.

## Use the same gateway with other MCP clients

The `mcp-semantic-gateway proxy` command is a stock stdio MCP server.
To use it with Claude Desktop, the MCP Inspector, or any other MCP
client:

1. Configure `~/.mcp_semantic_gateway/config.toml` with your OpenAPI source.
2. `mcp-semantic-gateway index`
3. Point the client at `mcp-semantic-gateway proxy`.

The chat CLI is just one MCP client among many — the gateway's stdio
contract is identical regardless of who's on the other side.

## Files

- `petstore_backend.py` — FastAPI app with four operations and seeded data.
- `chat.py` — single-shot agent CLI (Click + openai-agents).
