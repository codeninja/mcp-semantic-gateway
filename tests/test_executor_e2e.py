"""End-to-end acceptance test: discover an OpenAPI source, then call it.

This is the line between "semantic discovery demo" and "agent-usable internal
API gateway." We:

1. Spin up a small FastAPI fixture on an ephemeral port that serves a real
   OpenAPI spec at ``/openapi.json`` and a real ``/users/{id}`` endpoint.
2. Configure the gateway with that source and run the full ingestion path
   (collector -> forge -> index_writer -> SQLite + vectors).
3. Build the registry, router, and OpenAPI executor exactly as the HTTP
   server / stdio proxy do.
4. Resolve the tool by name and call it via the router.
5. Assert the upstream endpoint was hit AND the MCP result contains the API
   response in ``structuredContent``.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException

from mcp_semantic_gateway.config.models import (
    MCPSemanticGatewayConfig,
    ServerConfig,
    SourceType,
)
from mcp_semantic_gateway.integration.openapi_executor import OpenAPIExecutor
from mcp_semantic_gateway.integration.router import ToolRouter
from mcp_semantic_gateway.ingestion.index_writer import index_all
from mcp_semantic_gateway.retrieval.core import SearchCore
from mcp_semantic_gateway.retrieval.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Fixture HTTP API
# ---------------------------------------------------------------------------


def _make_fixture_app() -> FastAPI:
    """A tiny user-store API: GET /users/{id}, POST /users."""

    app = FastAPI(title="Fixture User API", version="1.0.0")
    state = {"hits": []}
    app.state.hits = state["hits"]

    @app.get("/users/{user_id}")
    def get_user(user_id: str):
        app.state.hits.append(("GET", f"/users/{user_id}"))
        if user_id == "missing":
            raise HTTPException(status_code=404, detail="not found")
        return {"id": user_id, "name": f"user-{user_id}", "active": True}

    @app.post("/users")
    def create_user(body: dict):
        app.state.hits.append(("POST", "/users", body))
        return {"id": "new", **body}

    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running_fixture_server() -> Iterator[tuple[FastAPI, str]]:
    """Run the fixture FastAPI app in a background thread on an ephemeral port."""

    app = _make_fixture_app()
    port = _free_port()
    server_config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(server_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    # Wait until the server is accepting connections.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/openapi.json", timeout=0.5)
            break
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
            time.sleep(0.05)
    else:
        raise RuntimeError("fixture server failed to start")

    try:
        yield app, base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_data_dir(tmp_path: Path) -> Path:
    base = tmp_path / "gateway"
    (base / "index").mkdir(parents=True)
    return base


# ---------------------------------------------------------------------------
# Acceptance test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openapi_tool_call_end_to_end(tmp_path):
    with _running_fixture_server() as (fixture_app, base_url):
        spec_url = f"{base_url}/openapi.json"
        config = MCPSemanticGatewayConfig(
            servers={
                "fixture-api": ServerConfig(
                    type=SourceType.OPENAPI,
                    url=spec_url,
                    enabled=True,
                    base_url=base_url,
                ),
            },
        )
        base_dir = _make_data_dir(tmp_path)

        # 1. Run ingestion: spec downloaded, forge converts it, persisted to
        # SQLite + vector store.
        # Run the real CLI indexing path (not a reimplementation) so this
        # test also covers ``index_writer`` -> ``route_metadata`` plumbing.
        n = await index_all(config, base_dir, log=lambda *_: None)
        assert n >= 1

        # 2. Build the runtime layer the same way ``server.py`` does.
        registry = ToolRegistry(base_dir / "index" / "metadata.db")
        await registry.initialize()
        core = SearchCore(config, base_dir, registry=registry)
        executor = OpenAPIExecutor(config)
        router = ToolRouter(config, registry, executor, core, mcp_clients={})

        try:
            # 3. Look the tool up by its method + path in route_metadata.
            # FastAPI's auto-generated operationId isn't stable across
            # versions, so we filter on the route the spec describes.
            tools = await registry.list_handles()
            tool_names = [h.canonical_name for h in tools]
            assert any("user" in n.lower() for n in tool_names), tool_names

            get_user_handle = next(
                h for h in tools
                if h.route_metadata
                and h.route_metadata.get("path_template") == "/users/{user_id}"
                and h.route_metadata.get("method") == "GET"
            )

            # 4. Call the tool through the router (the same entry point the
            # HTTP server and stdio proxy use).
            result = await router.call(
                get_user_handle.canonical_name,
                {"user_id": "42"},
            )

            # 5. Acceptance assertions.
            assert "isError" not in result, result
            assert result["structuredContent"] == {
                "id": "42",
                "name": "user-42",
                "active": True,
            }
            assert ("GET", "/users/42") in fixture_app.state.hits
        finally:
            await executor.aclose()


@pytest.mark.asyncio
async def test_openapi_tool_call_surfaces_upstream_404_as_isError(tmp_path):
    with _running_fixture_server() as (fixture_app, base_url):
        spec_url = f"{base_url}/openapi.json"
        config = MCPSemanticGatewayConfig(
            servers={
                "fixture-api": ServerConfig(
                    type=SourceType.OPENAPI,
                    url=spec_url,
                    enabled=True,
                    base_url=base_url,
                ),
            },
        )
        base_dir = _make_data_dir(tmp_path)
        # Run the real CLI indexing path (not a reimplementation) so this
        # test also covers ``index_writer`` -> ``route_metadata`` plumbing.
        n = await index_all(config, base_dir, log=lambda *_: None)
        assert n >= 1

        registry = ToolRegistry(base_dir / "index" / "metadata.db")
        await registry.initialize()
        core = SearchCore(config, base_dir, registry=registry)
        executor = OpenAPIExecutor(config)
        router = ToolRouter(config, registry, executor, core, mcp_clients={})

        try:
            tools = await registry.list_handles()
            get_user_handle = next(
                h for h in tools
                if h.route_metadata
                and h.route_metadata.get("path_template") == "/users/{user_id}"
                and h.route_metadata.get("method") == "GET"
            )
            result = await router.call(
                get_user_handle.canonical_name,
                {"user_id": "missing"},
            )
            # Upstream 404 surfaces as isError with structured detail.
            assert result["isError"] is True
            assert result["structuredContent"]["error"]["status_code"] == 404
            assert result["structuredContent"]["error"]["kind"] == "http_error"
            # The model can still see the upstream response body.
            assert "body" in result["structuredContent"]
        finally:
            await executor.aclose()
