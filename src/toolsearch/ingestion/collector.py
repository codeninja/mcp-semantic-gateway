import asyncio
import os
import json
import signal
from pathlib import Path
from typing import Dict, List, Optional
from toolsearch.config.models import ServerConfig

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
        # In a real impl, we'd validate the init response
        
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
                # Handle pagination if needed
            else:
                # Handle notifications or other messages
                pass
        return tools
