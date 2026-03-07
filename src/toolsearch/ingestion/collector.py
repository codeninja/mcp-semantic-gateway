import asyncio
import os
import json
import signal
import httpx
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any
from toolsearch.config.models import SourceType, ToolSearchConfig, ServerConfig
from toolsearch.ingestion.forge import ForgeEngine

class MCPClient:
    def __init__(self, server_id: str, config: ServerConfig):
        self.server_id = server_id
        self.config = config
        self.process: Optional[asyncio.subprocess.Process] = None

    async def start(self):
        env = os.environ.copy()
        env.update(self.config.env)
        self.process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )

    async def stop(self):
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()

    async def call_tools_list(self) -> List[dict]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError("Server not started")
            
        # 1. Initialize
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ToolSearch", "version": "1.0.0"}
            }
        }
        self.process.stdin.write((json.dumps(init_req) + "\n").encode())
        await self.process.stdin.drain()
        
        line = await self.process.stdout.readline()
        
        # 2. List tools
        list_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }
        self.process.stdin.write((json.dumps(list_req) + "\n").encode())
        await self.process.stdin.drain()
        
        tools = []
        while True:
            line = await self.process.stdout.readline()
            if not line: break
            resp = json.loads(line)
            if resp.get("id") == 2:
                result = resp.get("result", {})
                tools.extend(result.get("tools", []))
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            else:
                pass
        return tools

class Collector:
    def __init__(self, config: ToolSearchConfig):
        self.config = config

    async def collect_all(self) -> List[dict]:
        all_tools = []
        for server_id, server_cfg in self.config.servers.items():
            if not server_cfg.enabled:
                continue
            
            try:
                if server_cfg.type == SourceType.OPENAPI:
                    tools = await self.collect_openapi(server_id, server_cfg)
                else:
                    client = MCPClient(server_id, server_cfg)
                    await client.start()
                    try:
                        tools = await client.call_tools_list()
                    finally:
                        await client.stop()
                
                # Add server_id prefix to tool info for indexing
                for t in tools:
                    t["_server_id"] = server_id
                all_tools.extend(tools)
            except Exception as e:
                print(f"Error collecting from {server_id}: {e}")
        return all_tools

    async def collect_openapi(self, server_id: str, config: ServerConfig) -> List[dict]:
        if not config.url:
            raise ValueError(f"URL required for OpenAPI source: {server_id}")
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(config.url)
            resp.raise_for_status()
            
            # Detect YAML or JSON
            content = resp.text
            try:
                spec = json.loads(content)
            except json.JSONDecodeError:
                spec = yaml.safe_load(content)
                
            return ForgeEngine.forge_tools(spec)
