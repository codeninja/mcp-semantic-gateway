from toolsearch.config.loader import load_config
from toolsearch.storage.init import initialize_data_dir
from toolsearch.retrieval.core import SearchCore
from toolsearch.ingestion.collector import MCPClient
import asyncio
import sys
import json
import os

class ToolSearchProxy:
    def __init__(self):
        self.config = load_config()
        self.base_dir = initialize_data_dir()
        self.core = SearchCore(self.config, self.base_dir)
        self.clients = {}

    async def start(self):
        for server_id, server_config in self.config.servers.items():
            if server_config.enabled:
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
                tools.append({
                    "name": "toolsearch_context",
                    "description": "Set context",
                    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}
                })
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"tools": tools}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
            elif method == "tools/call":
                name = req["params"]["name"]
                if name == "toolsearch_context":
                    query = req["params"]["arguments"]["query"]
                    self.core.set_context("local-proxy", query)
                    resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": "Context set"}]}}
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
                else:
                    # Generic routing
                    for client in self.clients.values():
                        client.process.stdin.write(line)
                        await client.process.stdin.drain()
            elif method == "initialize":
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "ToolSearchProxy", "version": "1.0.0"}}}
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
