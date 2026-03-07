# MCP Semantic Gateway Enhancement Report

The MCP Semantic Gateway library has been upgraded with a production-ready HTTP/SSE server and shared core logic.

## Enhancements
1.  **FastAPI Server**: Added `mcp-semantic-gateway server` command.
    *   **MCP-over-HTTP**: Supports `tools/list` and `tools/call` (partial) via POST.
    *   **SSE Support**: Real-time event stream at `/sse`.
    *   **Multi-Tenancy**: Uses `X-Tenant-ID` header to isolate search contexts (query/TTL) between different agents.
    *   **Security**: API Key authentication via `X-API-Key`.
    *   **CORS**: Configurable cross-origin support for web-based MCP clients.
2.  **Shared Search Core**: Refactored `src/mcp_semantic_gateway/retrieval/core.py` to share semantic filtering logic between the stdio Proxy and the HTTP Server.
3.  **Production Config**: Added `[http]` section to `config.toml` for host, port, api_key, and cors_origins.
4.  **Deployment**: 
    *   **Dockerfile**: Multi-stage build that pre-downloads the embedding model for fast container startup.
    *   **Health Checks**: `/health` endpoint for Kubernetes liveness/readiness.

## Verification
- Shared logic ensures that setting context via `X-Tenant-ID` in HTTP correctly filters tools for that specific tenant.
- Dockerfile verified to pre-load `all-MiniLM-L6-v2`.

## Updated Usage
```bash
# Start the production HTTP server
uv run mcp-semantic-gateway server

# Set context via HTTP
curl -X POST http://localhost:8000/context \
     -H "X-API-Key: your-key" \
     -H "X-Tenant-ID: agent-1" \
     -d '{"query": "kubernetes logs"}'
```
