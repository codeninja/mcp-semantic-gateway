import asyncio
import os
import json
import signal
import httpx
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from mcp_semantic_gateway.config.models import SourceType, MCPSemanticGatewayConfig, ServerConfig
from mcp_semantic_gateway.ingestion.forge import ForgeEngine


def _parse_skill_md(content: str, skill_file: Path) -> Tuple[str, str, List[str]]:
    """Parse a SKILL.md file.

    Returns (name, description, allowed_tools). Supports the agent-skills
    spec format (YAML frontmatter delimited by ``---``) and falls back to
    the legacy heuristic (line 1 = name) when no frontmatter is present.
    """

    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            front_text = content[4:end]
            body = content[end + 5:].strip()
            try:
                front = yaml.safe_load(front_text) or {}
            except yaml.YAMLError:
                front = {}
            if isinstance(front, dict):
                name = (front.get("name") or skill_file.parent.name).strip()
                description = (front.get("description") or "").strip()
                allowed = front.get("allowed-tools") or []
                if not isinstance(allowed, list):
                    allowed = []
                # Combine description + body so the embedder gets the full
                # surface area of the skill, not just the YAML summary.
                combined = description if not body else f"{description}\n\n{body}".strip()
                return name, combined, [str(t) for t in allowed]
    # Legacy fallback: first non-empty line = name; rest = description.
    lines = content.splitlines()
    if lines:
        name = lines[0].lstrip("#").strip() or skill_file.parent.name
        description = "\n".join(lines[1:]).strip()
    else:
        name = skill_file.parent.name
        description = ""
    return name, description, []

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
                "clientInfo": {"name": "MCPSemanticGateway", "version": "1.0.0"}
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

    async def call_prompts_list(self) -> List[dict]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise RuntimeError("Server not started")
            
        list_req = {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "prompts/list",
            "params": {}
        }
        self.process.stdin.write((json.dumps(list_req) + "\n").encode())
        await self.process.stdin.drain()
        
        prompts = []
        while True:
            line = await self.process.stdout.readline()
            if not line: break
            resp = json.loads(line)
            if resp.get("id") == 102:
                result = resp.get("result", {})
                prompts.extend(result.get("prompts", []))
                if not result.get("nextCursor"):
                    break
        return prompts

class Collector:
    def __init__(self, config: MCPSemanticGatewayConfig):
        self.config = config

    async def collect_all(self) -> List[dict]:
        all_items = []
        for server_id, server_cfg in self.config.servers.items():
            if not server_cfg.enabled:
                continue
            
            try:
                if server_cfg.type == SourceType.OPENAPI:
                    items = await self.collect_openapi(server_id, server_cfg)
                    for i in items: i["_item_type"] = "tool"
                elif server_cfg.type == SourceType.SKILL:
                    items = await self.collect_skills(server_id, server_cfg)
                    for i in items: i["_item_type"] = "skill"
                else:
                    client = MCPClient(server_id, server_cfg)
                    await client.start()
                    try:
                        tools = await client.call_tools_list()
                        for t in tools: t["_item_type"] = "tool"
                        
                        prompts = await client.call_prompts_list()
                        for p in prompts: p["_item_type"] = "prompt"
                        
                        items = tools + prompts
                    finally:
                        await client.stop()
                
                for i in items:
                    i["_server_id"] = server_id
                all_items.extend(items)
            except Exception as e:
                print(f"Error collecting from {server_id}: {e}")
        return all_items

    async def collect_skills(self, server_id: str, config: ServerConfig) -> List[dict]:
        if not config.path:
            raise ValueError(f"Path required for Skill source: {server_id}")

        skills = []
        path = Path(config.path).expanduser()
        if not path.exists():
            return []

        # Recursive scan for SKILL.md
        for skill_file in path.rglob("SKILL.md"):
            try:
                content = skill_file.read_text()
                name, description, allowed_tools = _parse_skill_md(content, skill_file)
                annotations: Dict[str, Any] = {"path": str(skill_file)}
                if allowed_tools:
                    annotations["allowed_tools"] = allowed_tools
                skills.append({
                    "name": name,
                    "description": description[:2000],
                    "annotations": annotations,
                })
            except Exception as e:
                print(f"Error reading skill {skill_file}: {e}")
        return skills

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
