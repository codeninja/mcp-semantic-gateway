from mcp_semantic_gateway.config.loader import load_config
from mcp_semantic_gateway.config.models import SourceType
from mcp_semantic_gateway.storage.init import initialize_data_dir
from mcp_semantic_gateway.retrieval.core import SearchCore
from mcp_semantic_gateway.retrieval.registry import ToolRegistry
from mcp_semantic_gateway.integration.openapi_executor import OpenAPIExecutor
from mcp_semantic_gateway.integration.router import ToolRouter, ToolNotFound
from mcp_semantic_gateway.ingestion.collector import MCPClient
import asyncio
import sys
import json
import os
import time
from typing import Optional, TextIO


_GATEWAY_TOOL_DEFS = [
    {
        "name": "mcp_semantic_gateway_context",
        "description": "Set context",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "mcp_semantic_gateway_find_prompts",
        "description": "Search for prompts",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "mcp_semantic_gateway_find_skills",
        "description": (
            "Search the skill catalog by free-text query. Returns matching "
            "skills with name + frontmatter description so an agent can "
            "decide whether to fetch the full procedure via "
            "mcp_semantic_gateway_get_skill."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "mcp_semantic_gateway_get_skill",
        "description": (
            "Fetch the full SKILL.md body (procedural steps + tool list) "
            "for a skill discovered via mcp_semantic_gateway_find_skills. "
            "Pass the skill 'name' from the find_skills result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name from a prior find_skills result.",
                }
            },
            "required": ["name"],
        },
    },
]


class MCPSemanticGatewayProxy:
    def __init__(self):
        self.config = load_config()
        self.base_dir = initialize_data_dir()
        self.registry = ToolRegistry(
            self.base_dir / "index" / "metadata.db",
            force_namespace=self.config.proxy.namespace_collisions,
        )
        self.core = SearchCore(self.config, self.base_dir, registry=self.registry)
        self.openapi_executor = OpenAPIExecutor(self.config)
        self.clients = {}
        self.router: ToolRouter
        # Optional raw-RPC line log for debugging / demos. When
        # ``MCP_SEMANTIC_GATEWAY_LOG_RPC`` points at a path, every line
        # received on stdin and every response written to stdout is
        # appended with a timestamp and direction marker (``in`` / ``out``).
        # Parents can tail this file to render the live MCP event stream.
        self._rpc_log: Optional[TextIO] = None
        rpc_path = os.environ.get("MCP_SEMANTIC_GATEWAY_LOG_RPC")
        if rpc_path:
            self._rpc_log = open(rpc_path, "a", buffering=1)  # line-buffered

    def _log_rpc(self, direction: str, payload: str) -> None:
        if self._rpc_log is None:
            return
        ts = f"{time.time():.3f}"
        line = payload.rstrip("\n").replace("\n", "\\n")
        self._rpc_log.write(f"{ts}\t{direction}\t{line}\n")

    async def start(self):
        for server_id, server_config in self.config.servers.items():
            # Only start child processes for stdio MCP servers. OpenAPI and
            # Skill sources are static and need no live process.
            if not server_config.enabled:
                continue
            if server_config.type != SourceType.MCP:
                continue
            if not server_config.command:
                continue
            client = MCPClient(server_id, server_config)
            await client.start()
            self.clients[server_id] = client
        await self.registry.initialize()
        self.router = ToolRouter(
            self.config,
            self.registry,
            self.openapi_executor,
            self.core,
            mcp_clients=self.clients,
        )

    async def run(self):
        await self.start()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        try:
            await self._read_loop(reader)
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        """Release the executor's HTTP client and stop any child MCP processes
        so we don't leak resources when stdin closes."""

        await self.openapi_executor.aclose()
        for client in self.clients.values():
            try:
                await client.stop()
            except Exception:
                # Best effort: continue stopping the rest even if one hangs.
                pass
        if self._rpc_log is not None:
            try:
                self._rpc_log.close()
            except Exception:
                pass

    def _emit(self, resp: dict) -> None:
        payload = json.dumps(resp)
        self._log_rpc("out", payload)
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line: break

            self._log_rpc("in", line.decode("utf-8", errors="replace"))
            req = json.loads(line)
            method = req.get("method")

            if method == "tools/list":
                tools = await self.core.get_filtered_tools("local-proxy")
                tools.extend(_GATEWAY_TOOL_DEFS)
                self._emit({"jsonrpc": "2.0", "id": req["id"], "result": {"tools": tools}})
            elif method == "prompts/list":
                prompts = await self.core.get_filtered_prompts("local-proxy")
                self._emit({"jsonrpc": "2.0", "id": req["id"], "result": {"prompts": prompts}})
            elif method == "tools/call":
                name = req["params"]["name"]
                args = req["params"].get("arguments", {})
                try:
                    result = await self.router.call(name, args, tenant_id="local-proxy")
                    resp = {"jsonrpc": "2.0", "id": req["id"], "result": result}
                except ToolNotFound as e:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "error": {"code": -32601, "message": str(e)},
                    }
                self._emit(resp)
            elif method == "initialize":
                self._emit({"jsonrpc": "2.0", "id": req["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "MCPSemanticGatewayProxy", "version": "1.0.0"}}})
