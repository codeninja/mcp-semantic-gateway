from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from mcp_semantic_gateway.config.loader import load_config
from mcp_semantic_gateway.storage.init import initialize_data_dir
from mcp_semantic_gateway.retrieval.core import SearchCore
from mcp_semantic_gateway.retrieval.registry import ToolRegistry
from mcp_semantic_gateway.integration.openapi_executor import OpenAPIExecutor
from mcp_semantic_gateway.integration.router import ToolRouter, ToolNotFound
from typing import Optional
import asyncio
import json

app = FastAPI(title="MCPSemanticGateway HTTP Server")
config = load_config()
base_dir = initialize_data_dir()
registry = ToolRegistry(
    base_dir / "index" / "metadata.db",
    force_namespace=config.proxy.namespace_collisions,
)
core = SearchCore(config, base_dir, registry=registry)
openapi_executor = OpenAPIExecutor(config)
# The HTTP server has no native MCP child processes; native MCP routing
# only applies to the stdio proxy. Calls to native MCP tools through this
# transport surface as "no upstream" errors via the router.
router = ToolRouter(config, registry, openapi_executor, core, mcp_clients={})


@app.on_event("startup")
async def _initialize_registry() -> None:
    await registry.initialize()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await openapi_executor.aclose()

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.http.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    if config.http.api_key and x_api_key != config.http.api_key:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return x_api_key

@app.get("/health")
def health():
    return {"status": "healthy", "index_ready": True}

@app.post("/context")
async def set_context(
    req: Request, 
    x_tenant_id: str = Header("default"),
    _ = Depends(verify_api_key)
):
    body = await req.json()
    query = body.get("query")
    ttl = body.get("ttl_seconds", 300)
    if not query:
        raise HTTPException(status_code=400, detail="Missing query")
    core.set_context(x_tenant_id, query, ttl)
    return {"status": "ok", "message": "Context set"}

@app.get("/sse")
async def sse_endpoint(request: Request, x_tenant_id: str = Header("default")):
    async def event_generator():
        # Minimal MCP-over-HTTP SSE placeholder
        # In a full implementation, we'd handle JSON-RPC over SSE
        while True:
            if await request.is_disconnected():
                break
            yield {"data": json.dumps({"type": "ping"})}
            await asyncio.sleep(15)
            
    return EventSourceResponse(event_generator())

@app.post("/message")
async def message_endpoint(
    req: Request, 
    x_tenant_id: str = Header("default"),
    _ = Depends(verify_api_key)
):
    # Handle MCP POST requests (tools/list, tools/call)
    body = await req.json()
    method = body.get("method")
    
    if method == "tools/list":
        tools = await core.get_filtered_tools(x_tenant_id)
        # Add internal search tools
        tools.extend([
            {
                "name": "mcp_semantic_gateway_context",
                "description": "Set discovery context",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}
            },
            {
                "name": "mcp_semantic_gateway_find_prompts",
                "description": "Search for prompts semantically",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}
            },
            {
                "name": "mcp_semantic_gateway_find_skills",
                "description": (
                    "Search the skill catalog by free-text query. Returns "
                    "matching skills with name + frontmatter description so "
                    "an agent can decide whether to fetch the full procedure "
                    "via mcp_semantic_gateway_get_skill."
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
                    "Fetch the full SKILL.md body (procedural steps + tool "
                    "list) for a skill discovered via "
                    "mcp_semantic_gateway_find_skills. Pass the skill "
                    "'name' from the find_skills result."
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
        ])
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {"tools": tools}
        }
    
    if method == "prompts/list":
        prompts = await core.get_filtered_prompts(x_tenant_id)
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {"prompts": prompts}
        }
    
    if method == "tools/call":
        name = body["params"]["name"]
        args = body["params"].get("arguments", {})
        try:
            result = await router.call(name, args, tenant_id=x_tenant_id)
        except ToolNotFound as e:
            return {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": -32601, "message": str(e)},
            }
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
    return {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "Method not implemented"}}

def start_server():
    import uvicorn
    uvicorn.run(app, host=config.http.host, port=config.http.port)
