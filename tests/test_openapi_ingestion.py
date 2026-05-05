import pytest
import json
import asyncio
from fastapi import FastAPI
from httpx import AsyncClient
import uvicorn
import threading
import time
from mcp_semantic_gateway.ingestion.collector import Collector
from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig, ServerConfig, SourceType

# 1. Mock OpenAPI Server
mock_app = FastAPI()

@mock_app.get("/spec")
def get_spec():
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "description": "Returns a list of users",
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}, "required": False}
                    ]
                },
                "post": {
                    "operationId": "createUser",
                    "description": "Creates a new user",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                    "required": ["name"]
                                }
                            }
                        }
                    }
                }
            }
        }
    }

def run_mock_server():
    uvicorn.run(mock_app, host="127.0.0.1", port=8888)

@pytest.fixture(scope="module")
def openapi_server():
    thread = threading.Thread(target=run_mock_server, daemon=True)
    thread.start()
    time.sleep(2) # Wait for server to start
    return "http://127.0.0.1:8888/spec"

@pytest.mark.asyncio
async def test_openapi_ingestion(openapi_server):
    config = MCPSemanticGatewayConfig(
        servers={
            "test-api": ServerConfig(
                type=SourceType.OPENAPI,
                url=openapi_server,
                enabled=True
            )
        }
    )
    
    collector = Collector(config)
    tools = await collector.collect_all()
    
    # Assertions
    assert len(tools) == 2
    
    # Check listUsers
    list_users = next(t for t in tools if t["name"] == "listUsers")
    assert list_users["description"] == "Returns a list of users"
    assert "limit" in list_users["inputSchema"]["properties"]

    # Check createUser
    create_user = next(t for t in tools if t["name"] == "createUser")
    assert "name" in create_user["inputSchema"]["properties"]
    assert "name" in create_user["inputSchema"]["required"]

    # Route metadata must survive the full Collector path (forge -> harvested).
    assert list_users["_route_metadata"]["method"] == "GET"
    assert list_users["_route_metadata"]["path_template"] == "/users"
    assert create_user["_route_metadata"]["method"] == "POST"
    assert create_user["_route_metadata"]["request_body"]["content_type"] == "application/json"

    print("\n[SUCCESS] OpenAPI ingestion test passed.")
