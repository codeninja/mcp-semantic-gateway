from fastmcp import FastMCP
import httpx
import asyncio

# Create the MCP bridge
mcp = FastMCP("API-Bridge")
API_BASE = "http://127.0.0.1:8080"

@mcp.tool()
async def get_remote_greeting() -> str:
    """
    Fetch greeting from the remote REST API.
    Use this for welcoming users.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{API_BASE}/items/greetings")
        return resp.json()["message"]

@mcp.tool()
async def get_remote_billing() -> dict:
    """
    Fetch billing info from the remote REST API.
    Use this for financial status queries.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{API_BASE}/items/billing")
        return resp.json()

@mcp.tool()
async def get_remote_security() -> dict:
    """
    Fetch security status from the remote REST API.
    Use this for safety checks.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{API_BASE}/items/security")
        return resp.json()

if __name__ == "__main__":
    mcp.run()
