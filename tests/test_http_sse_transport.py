import asyncio
import json

import httpx
import pytest

from mcp_semantic_gateway.integration import server as server_mod


class _OpenRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_sse_generator_starts_with_endpoint_event():
    session_id = "test-session"
    endpoint = "http://test/message?sessionId=test-session"
    queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
    server_mod._sse_sessions[session_id] = server_mod._SSESession(
        tenant_id="tenant-a",
        queue=queue,
    )

    generator = server_mod._sse_event_generator(
        _OpenRequest(),
        session_id,
        endpoint,
    )
    try:
        first = await generator.__anext__()
        assert first == {"event": "endpoint", "data": endpoint}

        await queue.put({"event": "message", "data": '{"jsonrpc":"2.0"}'})
        second = await asyncio.wait_for(generator.__anext__(), timeout=1.0)
        assert second == {"event": "message", "data": '{"jsonrpc":"2.0"}'}
    finally:
        await generator.aclose()

    assert session_id not in server_mod._sse_sessions


@pytest.mark.asyncio
async def test_legacy_sse_message_post_queues_json_rpc_response(monkeypatch):
    seen_tenants: list[str] = []

    class StubCore:
        async def get_filtered_tools(self, tenant_id: str):
            seen_tenants.append(tenant_id)
            return []

    monkeypatch.setattr(server_mod, "core", StubCore())

    session_id = "queue-session"
    queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
    server_mod._sse_sessions[session_id] = server_mod._SSESession(
        tenant_id="sse-tenant",
        queue=queue,
    )

    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/message?sessionId={session_id}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    try:
        assert response.status_code == 202
        assert response.content == b""
        event = queue.get_nowait()
        assert event["event"] == "message"
        payload = json.loads(event["data"])
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == 1
        assert "result" in payload
        assert any(
            tool["name"] == "mcp_semantic_gateway_context"
            for tool in payload["result"]["tools"]
        )
        assert seen_tenants == ["sse-tenant"]
    finally:
        server_mod._sse_sessions.pop(session_id, None)


@pytest.mark.asyncio
async def test_direct_message_initialize_returns_json_response():
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/message",
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 9
    assert payload["result"]["protocolVersion"] == "2024-11-05"
    assert payload["result"]["capabilities"] == {"tools": {}, "prompts": {}}


@pytest.mark.asyncio
async def test_notifications_are_acknowledged_without_json_rpc_response():
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/message",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

    assert response.status_code == 202
    assert response.content == b""


@pytest.mark.asyncio
async def test_transport_origin_validation_rejects_untrusted_origin(monkeypatch):
    monkeypatch.setattr(
        server_mod.config.http,
        "cors_origins",
        ["http://localhost", "http://127.0.0.1"],
    )

    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/message",
            headers={"Origin": "https://evil.example"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_invalid_mcp_protocol_version_header_is_rejected():
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/message",
            headers={"MCP-Protocol-Version": "not-a-version"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )

    assert response.status_code == 400
