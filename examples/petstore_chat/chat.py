"""Single-shot agent CLI: ask one question against a model that has native
MCP access to the petstore via the OpenAI Agents SDK.

Pieces:

* The petstore is a stock FastAPI app started in-process on an ephemeral port.
* The gateway runs as a real stdio MCP server (``mcp-semantic-gateway proxy``),
  pointed at the petstore via a temp ``MCP_SEMANTIC_GATEWAY_HOME``.
* ``agents.mcp.MCPServerStdio`` spawns and supervises that subprocess; it
  discovers tools, dispatches calls, and surfaces results to the model
  natively. **No tool transposition or result reshaping in this file.**
* For OpenAI proper: the default Responses API is used. For OpenAI-compatible
  endpoints (Ollama, OpenRouter, vLLM, etc.) ``--base-url`` swaps in a
  ``OpenAIChatCompletionsModel`` configured against that endpoint.

No history, no memory: each invocation is a fresh question with a fresh
gateway.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import click
import httpx
import tomlkit
import uvicorn
from agents import (
    Agent,
    OpenAIChatCompletionsModel,
    Runner,
    set_default_openai_api,
    set_tracing_disabled,
)
from agents.mcp import MCPServerStdio
from openai import AsyncOpenAI

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_HERE))

from mcp_semantic_gateway.config.models import (  # noqa: E402
    MCPSemanticGatewayConfig,
    ServerConfig,
    SourceType,
)
from mcp_semantic_gateway.ingestion.index_writer import index_all  # noqa: E402

from petstore_backend import app as petstore_app  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running_petstore() -> Iterator[str]:
    port = _free_port()
    cfg = uvicorn.Config(
        petstore_app, host="127.0.0.1", port=port,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/openapi.json", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("petstore failed to start")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def _prepare_gateway(home_dir: Path, backend_url: str) -> None:
    """Write a config pointing at the petstore and run the indexer."""

    (home_dir / "index").mkdir(parents=True, exist_ok=True)
    (home_dir / "logs").mkdir(parents=True, exist_ok=True)
    (home_dir / "models").mkdir(parents=True, exist_ok=True)

    cfg_doc = tomlkit.document()
    servers = tomlkit.table()
    petstore = tomlkit.table()
    petstore["type"] = "openapi"
    petstore["url"] = f"{backend_url}/openapi.json"
    petstore["base_url"] = backend_url
    petstore["enabled"] = True
    servers["petstore"] = petstore
    cfg_doc["servers"] = servers
    (home_dir / "config.toml").write_text(tomlkit.dumps(cfg_doc))

    config = MCPSemanticGatewayConfig(
        servers={
            "petstore": ServerConfig(
                type=SourceType.OPENAPI,
                url=f"{backend_url}/openapi.json",
                base_url=backend_url,
                enabled=True,
            ),
        },
    )
    await index_all(config, home_dir, log=lambda *_: None)


def _build_model(model: str, base_url: Optional[str], api_key: str):
    """Return a model spec the Agents SDK can run.

    On real OpenAI we pass the model id and the SDK uses the Responses API.
    For OpenAI-compatible endpoints (Ollama, etc.) we wrap an ``AsyncOpenAI``
    client in ``OpenAIChatCompletionsModel`` because most non-OpenAI providers
    don't implement the Responses API.
    """

    if base_url is None:
        return model
    set_tracing_disabled(True)
    set_default_openai_api("chat_completions")
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    return OpenAIChatCompletionsModel(model=model, openai_client=client)


@click.command()
@click.argument("query")
@click.option("--model", default="gpt-4o-mini", show_default=True,
              help="LLM model id.")
@click.option("--base-url", default=None,
              help="OpenAI-compatible endpoint URL (e.g. http://localhost:11434/v1 for Ollama). "
                   "Omit to use real OpenAI.")
@click.option("--api-key-env", default="OPENAI_API_KEY", show_default=True,
              help="Environment variable holding the API key.")
def main(query: str, model: str, base_url: Optional[str], api_key_env: str) -> None:
    """Ask QUERY of an LLM agent that has MCP access to the petstore."""

    api_key = os.environ.get(api_key_env, "not-needed")

    async def _run() -> None:
        with _running_petstore() as backend_url:
            with tempfile.TemporaryDirectory(prefix="petstore-chat-") as tdir:
                home = Path(tdir)
                await _prepare_gateway(home, backend_url)

                # The SDK supervises the gateway subprocess: lifecycle, JSON-RPC
                # framing, tool discovery, tool dispatch -- all native, no glue.
                async with MCPServerStdio(
                    name="petstore-gateway",
                    params={
                        "command": sys.executable,
                        "args": ["-m", "mcp_semantic_gateway.cli.main", "proxy"],
                        "env": {**os.environ, "MCP_SEMANTIC_GATEWAY_HOME": str(home)},
                    },
                ) as gateway:
                    agent = Agent(
                        name="PetstoreAgent",
                        instructions=(
                            "You are a helpful assistant with tools to query a "
                            "petstore. Use them to answer user questions."
                        ),
                        mcp_servers=[gateway],
                        model=_build_model(model, base_url, api_key),
                    )
                    result = await Runner.run(agent, query)
                    click.echo(result.final_output)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
