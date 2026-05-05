from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import asyncio
import json

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from mcp_semantic_gateway.config.loader import load_config
from mcp_semantic_gateway.integration.openapi_executor import OpenAPIExecutor
from mcp_semantic_gateway.integration.router import ToolNotFound, ToolRouter
from mcp_semantic_gateway.retrieval.core import SearchCore
from mcp_semantic_gateway.retrieval.registry import ToolRegistry
from mcp_semantic_gateway.storage.init import initialize_data_dir

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

MCP_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_MCP_PROTOCOL_VERSION = MCP_PROTOCOL_VERSIONS[0]
SSE_QUEUE_MAX = 100

_GATEWAY_TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "mcp_semantic_gateway_context",
        "description": "Set discovery context",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "mcp_semantic_gateway_find_prompts",
        "description": "Search for prompts semantically",
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


@dataclass
class _SSESession:
    tenant_id: str
    queue: asyncio.Queue[Dict[str, str] | None]


_sse_sessions: Dict[str, _SSESession] = {}


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


async def validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    if not _origin_allowed(origin, config.http.cors_origins):
        raise HTTPException(status_code=403, detail="Invalid Origin")


async def validate_protocol_version(
    mcp_protocol_version: Optional[str] = Header(None, alias="MCP-Protocol-Version"),
) -> None:
    if mcp_protocol_version and mcp_protocol_version not in MCP_PROTOCOL_VERSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported MCP Protocol Version",
        )


def _origin_allowed(origin: str, allowed_origins: List[str]) -> bool:
    if "*" in allowed_origins:
        return True
    if origin in allowed_origins:
        return True

    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.hostname:
        return False

    for allowed in allowed_origins:
        allowed_parsed = urlparse(allowed)
        if not allowed_parsed.scheme or not allowed_parsed.hostname:
            continue
        if parsed.scheme != allowed_parsed.scheme:
            continue
        if parsed.hostname != allowed_parsed.hostname:
            continue
        # Common MCP browser clients use loopback origins with arbitrary
        # dev-server ports. Treat a portless loopback allow-list entry as
        # allowing any port for that loopback host.
        if allowed_parsed.port is None and parsed.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            return True
    return False


@app.get("/health")
def health():
    return {"status": "healthy", "index_ready": True}


@app.post("/context")
async def set_context(
    req: Request,
    x_tenant_id: str = Header("default"),
    _api_key: Optional[str] = Depends(verify_api_key),
    _origin: None = Depends(validate_origin),
):
    body = await req.json()
    query = body.get("query")
    ttl = body.get("ttl_seconds", 300)
    if not query:
        raise HTTPException(status_code=400, detail="Missing query")
    core.set_context(x_tenant_id, query, ttl)
    return {"status": "ok", "message": "Context set"}


@app.get("/sse")
async def sse_endpoint(
    request: Request,
    x_tenant_id: str = Header("default"),
    _api_key: Optional[str] = Depends(verify_api_key),
    _origin: None = Depends(validate_origin),
    _protocol_version: None = Depends(validate_protocol_version),
):
    session_id = uuid4().hex
    _sse_sessions[session_id] = _SSESession(
        tenant_id=x_tenant_id,
        queue=asyncio.Queue(maxsize=SSE_QUEUE_MAX),
    )
    message_endpoint = _message_endpoint_uri(request, session_id)

    return EventSourceResponse(
        _sse_event_generator(request, session_id, message_endpoint),
        ping=15,
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/message")
async def message_endpoint(
    req: Request,
    session_id: Optional[str] = Query(None, alias="sessionId"),
    session_id_legacy: Optional[str] = Query(None, alias="session_id"),
    x_tenant_id: str = Header("default"),
    _api_key: Optional[str] = Depends(verify_api_key),
    _origin: None = Depends(validate_origin),
    _protocol_version: None = Depends(validate_protocol_version),
):
    active_session_id = session_id or session_id_legacy
    sse_session = _sse_sessions.get(active_session_id) if active_session_id else None
    if active_session_id and sse_session is None:
        raise HTTPException(status_code=404, detail="Unknown SSE session")

    try:
        body = await req.json()
    except json.JSONDecodeError:
        return JSONResponse(
            _json_rpc_error(None, -32700, "Parse error"),
            status_code=400,
        )

    response = await _handle_rpc_message(
        body,
        tenant_id=sse_session.tenant_id if sse_session else x_tenant_id,
    )

    if sse_session is None:
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    if response is not None:
        try:
            sse_session.queue.put_nowait(
                {"event": "message", "data": json.dumps(response)}
            )
        except asyncio.QueueFull:
            raise HTTPException(status_code=429, detail="SSE session queue is full")
    return Response(status_code=202)


def _message_endpoint_uri(request: Request, session_id: str) -> str:
    query = urlencode({"sessionId": session_id})
    return f"{request.url_for('message_endpoint')}?{query}"


async def _sse_event_generator(
    request: Request,
    session_id: str,
    message_endpoint: str,
) -> AsyncIterator[Dict[str, str]]:
    try:
        yield {"event": "endpoint", "data": message_endpoint}
        while not await request.is_disconnected():
            session = _sse_sessions.get(session_id)
            if session is None:
                break
            try:
                event = await asyncio.wait_for(session.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if event is None:
                break
            yield event
    finally:
        _sse_sessions.pop(session_id, None)


async def _handle_rpc_message(body: Any, *, tenant_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(body, dict):
        return _json_rpc_error(None, -32600, "Invalid Request")

    request_id = body.get("id")
    if body.get("jsonrpc") != "2.0":
        return _json_rpc_error(request_id, -32600, "Invalid Request")

    method = body.get("method")
    if method is None:
        if "result" in body or "error" in body:
            return None
        return _json_rpc_error(request_id, -32600, "Invalid Request")
    if not isinstance(method, str):
        return _json_rpc_error(request_id, -32600, "Invalid Request")

    # Notifications do not receive JSON-RPC responses. The transport layer
    # acknowledges accepted notifications with HTTP 202.
    if "id" not in body:
        return None

    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _json_rpc_error(request_id, -32602, "Invalid params")

    if method == "initialize":
        return _json_rpc_result(request_id, _initialize_result(params))

    if method == "ping":
        return _json_rpc_result(request_id, {})

    if method == "tools/list":
        tools = await core.get_filtered_tools(tenant_id)
        tools.extend(_GATEWAY_TOOL_DEFS)
        return _json_rpc_result(request_id, {"tools": tools})

    if method == "prompts/list":
        prompts = await core.get_filtered_prompts(tenant_id)
        return _json_rpc_result(request_id, {"prompts": prompts})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            return _json_rpc_error(
                request_id,
                -32602,
                "tools/call requires params.name",
            )
        if not isinstance(args, dict):
            return _json_rpc_error(
                request_id,
                -32602,
                "tools/call params.arguments must be an object",
            )
        try:
            result = await router.call(name, args, tenant_id=tenant_id)
        except ToolNotFound as e:
            return _json_rpc_error(request_id, -32601, str(e))
        return _json_rpc_result(request_id, result)

    return _json_rpc_error(request_id, -32601, "Method not implemented")


def _initialize_result(params: Dict[str, Any]) -> Dict[str, Any]:
    requested = params.get("protocolVersion")
    protocol_version = (
        requested if requested in MCP_PROTOCOL_VERSIONS else DEFAULT_MCP_PROTOCOL_VERSION
    )
    return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {}, "prompts": {}},
        "serverInfo": {
            "name": "MCPSemanticGatewayHTTPServer",
            "version": "1.0.0",
        },
    }


def _json_rpc_result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _json_rpc_error(
    request_id: Any,
    code: int,
    message: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def start_server():
    import uvicorn

    uvicorn.run(app, host=config.http.host, port=config.http.port)
