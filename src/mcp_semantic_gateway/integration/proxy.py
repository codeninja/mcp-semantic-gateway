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
        "description": "Search for skills",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
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

        while True:
            line = await reader.readline()
            if not line: break

            req = json.loads(line)
            method = req.get("method")

            if method == "tools/list":
                tools = await self.core.get_filtered_tools("local-proxy")
                tools.extend(_GATEWAY_TOOL_DEFS)
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"tools": tools}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
            elif method == "prompts/list":
                prompts = await self.core.get_filtered_prompts("local-proxy")
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"prompts": prompts}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
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
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
            elif method == "initialize":
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "MCPSemanticGatewayProxy", "version": "1.0.0"}}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
