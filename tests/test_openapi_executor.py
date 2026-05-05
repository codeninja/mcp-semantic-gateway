"""Unit tests for the OpenAPI executor.

These run entirely against ``httpx.MockTransport`` -- no real network. The
end-to-end test in ``test_executor_e2e.py`` covers the full path with a
real uvicorn fixture.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

from mcp_semantic_gateway.config.models import (
    AuthConfig,
    AuthType,
    MCPSemanticGatewayConfig,
    ServerConfig,
    SourceType,
)
from mcp_semantic_gateway.integration.openapi_executor import OpenAPIExecutor
from mcp_semantic_gateway.retrieval.registry import ToolHandle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DEFAULT_SERVERS = ["https://api.example.com"]


def _make_handle(
    *,
    name: str = "getUser",
    server_id: str = "users-api",
    method: str = "GET",
    path: str = "/users/{id}",
    parameters: Optional[List[Dict[str, Any]]] = None,
    request_body: Optional[Dict[str, Any]] = None,
    success_response: Optional[Dict[str, Any]] = None,
    servers: Optional[List[str]] = None,
) -> ToolHandle:
    # ``servers is None`` -> use defaults; ``servers == []`` -> intentional
    # empty (the executor should fail with no base URL).
    effective_servers = _DEFAULT_SERVERS if servers is None else servers
    return ToolHandle(
        tool_id=f"{server_id}::tool::{name}",
        server_id=server_id,
        canonical_name=name,
        raw_name=name,
        item_type="tool",
        description=None,
        input_schema={"type": "object", "properties": {}, "required": []},
        annotations={"source": "openapi"},
        route_metadata={
            "operation_id": name,
            "method": method,
            "path_template": path,
            "servers": effective_servers,
            "parameters": parameters or [],
            "request_body": request_body,
            "success_response": success_response,
            "security": [],
            "tags": [],
        },
    )


def _make_config(
    server_id: str = "users-api",
    auth: Optional[AuthConfig] = None,
    base_url: Optional[str] = None,
) -> MCPSemanticGatewayConfig:
    return MCPSemanticGatewayConfig(
        servers={
            server_id: ServerConfig(
                type=SourceType.OPENAPI,
                url="https://api.example.com/openapi.json",
                enabled=True,
                auth=auth,
                base_url=base_url,
            )
        }
    )


def _make_executor(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: Optional[MCPSemanticGatewayConfig] = None,
) -> OpenAPIExecutor:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return OpenAPIExecutor(config or _make_config(), client=client)


# Capture-and-respond helper: track every request the transport sees.
class _Capture:
    def __init__(self):
        self.requests: List[httpx.Request] = []

    def respond(
        self,
        status: int = 200,
        body: Any = None,
        content_type: str = "application/json",
        headers: Optional[Dict[str, str]] = None,
    ) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            payload: bytes
            if body is None:
                payload = b""
            elif isinstance(body, (bytes, bytearray)):
                payload = bytes(body)
            elif content_type == "application/json" or content_type.endswith("+json"):
                payload = json.dumps(body).encode("utf-8")
            else:
                payload = body.encode("utf-8") if isinstance(body, str) else bytes(body)
            return httpx.Response(
                status,
                content=payload,
                headers={"content-type": content_type, **(headers or {})},
            )
        return handler


# ---------------------------------------------------------------------------
# Path / query / header / cookie / body splitting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_parameter_substitution():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {"id": "123", "name": "ok"}))
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
    )
    result = await executor.call(handle, {"id": "123"})
    assert "isError" not in result
    assert cap.requests[0].url.path == "/users/123"
    assert result["structuredContent"] == {"id": "123", "name": "ok"}


@pytest.mark.asyncio
async def test_path_parameter_value_is_url_encoded():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
    )
    await executor.call(handle, {"id": "a b/c"})
    # quote(safe="") encodes spaces and slashes; httpx exposes the encoded
    # form as ``raw_path`` (``.path`` is the decoded version).
    assert cap.requests[0].url.raw_path == b"/users/a%20b%2Fc"


@pytest.mark.asyncio
async def test_query_array_param_explodes_by_default():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
            {
                "name": "include",
                "in": "query",
                "required": False,
                "schema": {"type": "array", "items": {"type": "string"}},
                "explode": True,
            },
        ],
    )
    await executor.call(handle, {"id": "1", "include": ["a", "b"]})
    raw = str(cap.requests[0].url)
    # Repeated parameter, not comma-joined, when explode=True.
    assert "include=a" in raw and "include=b" in raw
    assert "include=a%2Cb" not in raw


@pytest.mark.asyncio
async def test_query_array_param_joins_when_not_exploded():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
            {
                "name": "tags",
                "in": "query",
                "required": False,
                "schema": {"type": "array", "items": {"type": "string"}},
                "explode": False,
            },
        ],
    )
    await executor.call(handle, {"id": "1", "tags": ["a", "b"]})
    raw = str(cap.requests[0].url)
    assert "tags=a%2Cb" in raw or "tags=a,b" in raw


@pytest.mark.asyncio
async def test_header_parameter_passed_through():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "X-Trace-Id", "in": "header", "required": False, "schema": {"type": "string"}},
        ],
    )
    await executor.call(handle, {"id": "1", "X-Trace-Id": "trace-42"})
    assert cap.requests[0].headers.get("x-trace-id") == "trace-42"


@pytest.mark.asyncio
async def test_cookie_parameter_passed_through():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "session", "in": "cookie", "required": False, "schema": {"type": "string"}},
        ],
    )
    await executor.call(handle, {"id": "1", "session": "sess-abc"})
    cookie = cap.requests[0].headers.get("cookie", "")
    assert "session=sess-abc" in cookie


@pytest.mark.asyncio
async def test_inline_request_body_lifts_properties():
    cap = _Capture()
    executor = _make_executor(cap.respond(201, {"ok": True}))
    handle = _make_handle(
        method="POST",
        path="/users",
        parameters=[],
        request_body={
            "content_type": "application/json",
            "schema": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
            },
            "required": True,
            "input_style": "inline",
        },
    )
    await executor.call(handle, {"name": "alice", "age": 30})
    assert json.loads(cap.requests[0].content) == {"name": "alice", "age": 30}
    assert cap.requests[0].headers.get("content-type", "").startswith("application/json")


@pytest.mark.asyncio
async def test_required_inline_body_with_no_required_fields_sends_empty_object():
    """A required body with an empty ``schema.required`` (e.g. an empty-object
    contract) still sends ``{}`` rather than dropping the body silently."""

    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        method="POST",
        path="/notify",
        parameters=[],
        request_body={
            "content_type": "application/json",
            "schema": {"type": "object", "properties": {}, "required": []},
            "required": True,
            "input_style": "inline",
        },
    )
    result = await executor.call(handle, {})
    assert "isError" not in result, result
    assert json.loads(cap.requests[0].content) == {}
    assert cap.requests[0].headers.get("content-type", "").startswith("application/json")


@pytest.mark.asyncio
async def test_optional_inline_body_with_no_args_sends_no_body():
    """When the body is optional and the caller supplies nothing, the
    executor must NOT spuriously send ``{}`` -- the request goes out with
    no body at all."""

    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        method="POST",
        path="/optional",
        parameters=[],
        request_body={
            "content_type": "application/json",
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "required": False,
            "input_style": "inline",
        },
    )
    await executor.call(handle, {})
    assert cap.requests[0].content == b""


@pytest.mark.asyncio
async def test_wrapped_request_body_uses_requestBody_key():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        method="POST",
        path="/raw",
        parameters=[],
        request_body={
            "content_type": "application/json",
            "schema": {"type": "array", "items": {"type": "integer"}},
            "required": True,
            "input_style": "wrapped",
        },
    )
    await executor.call(handle, {"requestBody": [1, 2, 3]})
    assert json.loads(cap.requests[0].content) == [1, 2, 3]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_in_header(monkeypatch):
    monkeypatch.setenv("API_KEY_VALUE", "supersecret")
    cap = _Capture()
    executor = _make_executor(
        cap.respond(200, {}),
        config=_make_config(
            auth=AuthConfig(
                type=AuthType.API_KEY,
                header_name="X-API-Key",
                api_key_env="API_KEY_VALUE",
            )
        ),
    )
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    await executor.call(handle, {"id": "1"})
    assert cap.requests[0].headers.get("x-api-key") == "supersecret"


@pytest.mark.asyncio
async def test_api_key_in_query(monkeypatch):
    monkeypatch.setenv("KEY_ENV", "keyval")
    cap = _Capture()
    executor = _make_executor(
        cap.respond(200, {}),
        config=_make_config(
            auth=AuthConfig(
                type=AuthType.API_KEY,
                query_name="api_key",
                api_key_env="KEY_ENV",
            )
        ),
    )
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    await executor.call(handle, {"id": "1"})
    assert "api_key=keyval" in str(cap.requests[0].url)


@pytest.mark.asyncio
async def test_bearer_auth(monkeypatch):
    monkeypatch.setenv("TOKEN_ENV", "abc.def.ghi")
    cap = _Capture()
    executor = _make_executor(
        cap.respond(200, {}),
        config=_make_config(
            auth=AuthConfig(type=AuthType.BEARER, bearer_token_env="TOKEN_ENV")
        ),
    )
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    await executor.call(handle, {"id": "1"})
    assert cap.requests[0].headers.get("authorization") == "Bearer abc.def.ghi"


@pytest.mark.asyncio
async def test_basic_auth(monkeypatch):
    monkeypatch.setenv("U", "alice")
    monkeypatch.setenv("P", "wonderland")
    cap = _Capture()
    executor = _make_executor(
        cap.respond(200, {}),
        config=_make_config(
            auth=AuthConfig(
                type=AuthType.BASIC,
                basic_username_env="U",
                basic_password_env="P",
            )
        ),
    )
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    await executor.call(handle, {"id": "1"})
    auth_header = cap.requests[0].headers.get("authorization", "")
    assert auth_header.startswith("Basic ")
    decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode()
    assert decoded == "alice:wonderland"


@pytest.mark.asyncio
async def test_missing_auth_env_var_returns_isError(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    cap = _Capture()
    executor = _make_executor(
        cap.respond(200, {}),
        config=_make_config(
            auth=AuthConfig(
                type=AuthType.BEARER, bearer_token_env="MISSING_KEY"
            )
        ),
    )
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "1"})
    assert result["isError"] is True
    assert "MISSING_KEY" in result["content"][0]["text"]
    assert result["structuredContent"]["error"]["env_var"] == "MISSING_KEY"
    assert cap.requests == []  # no request was made


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_required_path_param_returns_isError():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {})
    assert result["isError"] is True
    assert "id" in result["structuredContent"]["error"]["missing"]
    assert cap.requests == []


@pytest.mark.asyncio
async def test_missing_required_inline_body_field_returns_isError():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        method="POST",
        path="/users",
        parameters=[],
        request_body={
            "content_type": "application/json",
            "schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
            "required": True,
            "input_style": "inline",
        },
    )
    result = await executor.call(handle, {})
    assert result["isError"] is True
    assert "name" in result["structuredContent"]["error"]["missing"]
    assert cap.requests == []


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_config_base_url_overrides_spec_servers():
    cap = _Capture()
    executor = _make_executor(
        cap.respond(200, {}),
        config=_make_config(base_url="https://override.example.com"),
    )
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
        servers=["https://api.example.com"],
    )
    await executor.call(handle, {"id": "1"})
    assert str(cap.requests[0].url).startswith("https://override.example.com")


@pytest.mark.asyncio
async def test_no_base_url_returns_isError():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {}))
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
        servers=[],
    )
    result = await executor.call(handle, {"id": "1"})
    assert result["isError"] is True
    assert "base URL" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_response_yields_structured_and_text():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, {"id": "x"}))
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "x"})
    assert result["structuredContent"] == {"id": "x"}
    assert result["content"][0]["type"] == "text"
    assert json.loads(result["content"][0]["text"]) == {"id": "x"}


@pytest.mark.asyncio
async def test_json_array_response_omits_structured_content():
    """The MCP spec requires ``structuredContent`` to be an object. When
    the upstream returns a JSON array (or scalar), the executor surfaces
    it as text only and omits ``structuredContent`` so the MCP SDK on the
    client side doesn't reject the result."""

    cap = _Capture()
    payload = [{"id": "1", "name": "Fido"}, {"id": "2", "name": "Whiskers"}]
    executor = _make_executor(cap.respond(200, payload))
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "1"})
    assert "structuredContent" not in result
    # The full JSON must still be visible to the model in the text block.
    assert json.loads(result["content"][0]["text"]) == payload


@pytest.mark.asyncio
async def test_text_response_returns_text_content():
    cap = _Capture()
    executor = _make_executor(cap.respond(200, "hello", content_type="text/plain"))
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "x"})
    assert "structuredContent" not in result
    assert result["content"] == [{"type": "text", "text": "hello"}]


@pytest.mark.asyncio
async def test_image_response_returns_image_content():
    cap = _Capture()
    png_bytes = b"\x89PNG\r\n\x1a\nfakepng"
    executor = _make_executor(cap.respond(200, png_bytes, content_type="image/png"))
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "x"})
    assert result["content"][0]["type"] == "image"
    assert result["content"][0]["mimeType"] == "image/png"
    assert base64.b64decode(result["content"][0]["data"]) == png_bytes


@pytest.mark.asyncio
async def test_other_binary_returns_payload_with_metadata():
    cap = _Capture()
    pdf_bytes = b"%PDF-1.4 fake"
    executor = _make_executor(
        cap.respond(
            200,
            pdf_bytes,
            content_type="application/pdf",
            headers={"content-disposition": "attachment; filename=x.pdf"},
        )
    )
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "x"})
    assert result["content"][0]["type"] == "text"
    text = result["content"][0]["text"]
    # The base64 payload must NOT be duplicated in the text block (token bloat
    # on large downloads); the text is a short summary and points at
    # ``structuredContent.binary.base64`` for the actual bytes.
    payload_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    assert payload_b64 not in text
    assert "application/pdf" in text
    assert str(len(pdf_bytes)) in text
    sc = result["structuredContent"]
    assert sc["binary"]["content_type"] == "application/pdf"
    assert sc["binary"]["byte_length"] == len(pdf_bytes)
    assert sc["binary"]["content-disposition"] == "attachment; filename=x.pdf"
    assert base64.b64decode(sc["binary"]["base64"]) == pdf_bytes


@pytest.mark.asyncio
async def test_spec_promised_json_actual_text_that_parses_warns_and_coerces(caplog):
    """Spec said JSON but server returned text/plain that's valid JSON anyway."""

    cap = _Capture()
    executor = _make_executor(
        cap.respond(200, '{"ok": true}', content_type="text/plain")
    )
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
        success_response={"status": "200", "content_type": "application/json"},
    )
    with caplog.at_level("WARNING"):
        result = await executor.call(handle, {"id": "x"})
    assert result["structuredContent"] == {"ok": True}
    # Coercion warning surfaced through the logger.
    assert any("coerced" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_spec_promised_json_actual_text_that_doesnt_parse_warns_and_falls_back(caplog):
    cap = _Capture()
    executor = _make_executor(
        cap.respond(200, "not json at all", content_type="text/plain")
    )
    handle = _make_handle(
        parameters=[
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
        success_response={"status": "200", "content_type": "application/json"},
    )
    with caplog.at_level("WARNING"):
        result = await executor.call(handle, {"id": "x"})
    assert "structuredContent" not in result
    assert result["content"][0]["text"] == "not json at all"
    assert any("did not parse" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# HTTP errors / network errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_4xx_returns_isError_with_body():
    cap = _Capture()
    executor = _make_executor(
        cap.respond(404, {"error": "not found"}, content_type="application/json")
    )
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "missing"})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["status_code"] == 404
    assert result["structuredContent"]["error"]["kind"] == "http_error"
    # Upstream body is preserved for the model to inspect.
    assert result["structuredContent"]["body"] == {"error": "not found"}


@pytest.mark.asyncio
async def test_http_5xx_returns_isError():
    cap = _Capture()
    executor = _make_executor(
        cap.respond(500, "internal", content_type="text/plain")
    )
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "x"})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["status_code"] == 500


@pytest.mark.asyncio
async def test_network_error_returns_isError():
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=_request)

    transport = httpx.MockTransport(boom)
    client = httpx.AsyncClient(transport=transport)
    executor = OpenAPIExecutor(_make_config(), client=client)
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "x"})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["kind"] == "transport_error"
    assert result["structuredContent"]["error"]["exception"] == "ConnectError"


@pytest.mark.asyncio
async def test_timeout_returns_isError():
    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=_request)

    transport = httpx.MockTransport(slow)
    client = httpx.AsyncClient(transport=transport)
    executor = OpenAPIExecutor(_make_config(), client=client)
    handle = _make_handle(parameters=[
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    ])
    result = await executor.call(handle, {"id": "x"})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["kind"] == "timeout"


# ---------------------------------------------------------------------------
# Programming-error guards
# ---------------------------------------------------------------------------


def test_auth_config_rejects_whitespace_only_header_name():
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        AuthConfig(
            type=AuthType.API_KEY,
            header_name="   ",
            api_key_env="K",
        )


def test_auth_config_rejects_whitespace_only_query_name():
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        AuthConfig(
            type=AuthType.API_KEY,
            query_name="\t",
            api_key_env="K",
        )


def test_auth_config_strips_header_name_whitespace():
    cfg = AuthConfig(
        type=AuthType.API_KEY,
        header_name="  X-API-Key  ",
        api_key_env="K",
    )
    assert cfg.header_name == "X-API-Key"


@pytest.mark.asyncio
async def test_handle_without_route_metadata_raises():
    executor = _make_executor(_Capture().respond(200, {}))
    handle = ToolHandle(
        tool_id="x", server_id="users-api", canonical_name="x", raw_name="x",
        item_type="tool", description=None, input_schema=None,
        annotations=None, route_metadata=None,
    )
    with pytest.raises(ValueError):
        await executor.call(handle, {})
