from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from toolsearch.config.loader import load_config
from toolsearch.storage.init import initialize_data_dir
from toolsearch.retrieval.core import SearchCore
from pathlib import Path
from typing import Optional, Dict, List
import asyncio
import json

app = FastAPI(title="ToolSearch HTTP Server")
config = load_config()
base_dir = initialize_data_dir()
core = SearchCore(config, base_dir)

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
                "name": "toolsearch_context",
                "description": "Set discovery context",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}
            },
            {
                "name": "toolsearch_find_prompts",
                "description": "Search for prompts semantically",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}
            },
            {
                "name": "toolsearch_find_skills",
                "description": "Search for skills semantically",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}
            }
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
        if name == "toolsearch_find_prompts":
            query = args.get("query", "")
            # Temporarily override context for this search
            core.set_context(f"{x_tenant_id}_search", query, ttl=10)
            items = await core.get_filtered_prompts(f"{x_tenant_id}_search")
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": json.dumps(items, indent=2)}]}}
        if name == "toolsearch_find_skills":
            query = args.get("query", "")
            core.set_context(f"{x_tenant_id}_search", query, ttl=10)
            items = await core.get_filtered_skills(f"{x_tenant_id}_search")
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": json.dumps(items, indent=2)}]}}
    return {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "Method not implemented"}}

def start_server():
    import uvicorn
    uvicorn.run(app, host=config.http.host, port=config.http.port)
