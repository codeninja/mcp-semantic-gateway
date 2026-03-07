import pytest
import asyncio
import httpx
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np

from mcp_semantic_gateway.integration.server import app
from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig, ServerConfig, SourceType
from mcp_semantic_gateway.integration import server as server_mod

# Configure test environment
TEST_DIR = Path("/tmp/mcp_semantic_gateway_e2e_test")

@pytest.fixture(autouse=True)
def setup_test_env():
    """Ensure a clean test directory and override config for each test."""
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    TEST_DIR.mkdir(parents=True)
    
    # Create required subdirectories
    (TEST_DIR / "index").mkdir()
    
    # Force CPU for tests
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["MCP_SEMANTIC_GATEWAY_DATA_DIR"] = str(TEST_DIR)
    
    # Manually re-initialize the server's core with the test directory
    from mcp_semantic_gateway.config.loader import load_config
    from mcp_semantic_gateway.storage.init import initialize_data_dir
    from mcp_semantic_gateway.retrieval.core import SearchCore
    
    test_config = load_config()
    test_config.servers = {
        "test-mcp": ServerConfig(type=SourceType.MCP, command="echo", args=["{}"])
    }
    
    # Create SearchCore
    core = SearchCore(test_config, TEST_DIR)
    
    # MOCK THE EMBEDDER to avoid segfaults and keep tests fast
    # MCPSemanticGatewayCore uses LocalEmbedder which loads heavy models
    mock_embedder = MagicMock()
    # all-MiniLM-L6-v2 has 384 dimensions
    mock_embedder.embed.return_value = np.zeros((1, 384))
    core.embedder = mock_embedder
    
    # Update global server state
    server_mod.config = test_config
    server_mod.base_dir = TEST_DIR
    server_mod.core = core
    
    yield
    
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)

@pytest.mark.asyncio
async def test_e2e_flow_in_process():
    """
    Test the gateway flow using httpx.AsyncClient(app=app).
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        print("🚀 Starting MCPSemanticGateway E2E Test (Mocked Embedder)...")
        
        # 1. Set context
        resp = await client.post("/context", 
                               json={"query": "searching for tools", "ttl_seconds": 60},
                               headers={"X-Tenant-ID": "test-agent"})
        assert resp.status_code == 200

        # 2. List tools
        resp = await client.post("/message", 
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                               headers={"X-Tenant-ID": "test-agent"})
        
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        tools = data["result"]["tools"]
        
        # Verify internal search tools are present
        tool_names = [t["name"] for t in tools]
        assert "mcp_semantic_gateway_context" in tool_names
        
        print("\n✅ Mocked E2E test passed.")

@pytest.mark.asyncio
async def test_health_check():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"
