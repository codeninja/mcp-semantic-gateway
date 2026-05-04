from mcp_semantic_gateway.config.loader import load_config
from mcp_semantic_gateway.config.models import SourceType
from mcp_semantic_gateway.storage.init import initialize_data_dir
from mcp_semantic_gateway.retrieval.core import SearchCore
from mcp_semantic_gateway.ingestion.collector import MCPClient
import asyncio
import sys
import json
import os

class MCPSemanticGatewayProxy:
    def __init__(self):
        self.config = load_config()
        self.base_dir = initialize_data_dir()
        self.core = SearchCore(self.config, self.base_dir)
        self.clients = {}

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
                tools.extend([
                    {
                        "name": "mcp_semantic_gateway_context",
                        "description": "Set context",
                        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}
                    },
                    {
                        "name": "mcp_semantic_gateway_find_prompts",
                        "description": "Search for prompts",
                        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}
                    },
                    {
                        "name": "mcp_semantic_gateway_find_skills",
                        "description": "Search for skills",
                        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}
                    }
                ])
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
                if name == "mcp_semantic_gateway_context":
                    query = args["query"]
                    self.core.set_context("local-proxy", query)
                    resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": "Context set"}]}}
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
                elif name == "mcp_semantic_gateway_find_prompts":
                    query = args.get("query", "")
                    self.core.set_context("search_tmp", query, ttl=10)
                    items = await self.core.get_filtered_prompts("search_tmp")
                    resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": json.dumps(items)}]}}
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
                elif name == "mcp_semantic_gateway_find_skills":
                    query = args.get("query", "")
                    self.core.set_context("search_tmp", query, ttl=10)
                    items = await self.core.get_filtered_skills("search_tmp")
                    resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": json.dumps(items)}]}}
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
                else:
                    # Generic routing
                    for client in self.clients.values():
                        client.process.stdin.write(line)
                        await client.process.stdin.drain()
            elif method == "initialize":
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "MCPSemanticGatewayProxy", "version": "1.0.0"}}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
