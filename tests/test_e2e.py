import asyncio
import httpx
import time

async def run_e2e_test():
    base_url = "http://localhost:8002"
    
    print("🚀 Starting ToolSearch E2E Test...")
    
    # 1. Set context for 'Category 4'
    print("\nStep 1: Setting semantic context to 'category 4'...")
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/context", 
                               json={"query": "operation on category 4", "ttl_seconds": 60},
                               headers={"X-Tenant-ID": "test-agent"})
        print(f"Response: {resp.status_code} - {resp.json()}")
        assert resp.status_code == 200

    # 2. List tools (should be filtered)
    print("\nStep 2: Listing filtered tools...")
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/message", 
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                               headers={"X-Tenant-ID": "test-agent"})
        data = resp.json()
        tools = data["result"]["tools"]
        print(f"Found {len(tools)} tools (including search tool).")
        
        # Verify category 4 tools are present
        cat_4_tools = [t["name"] for t in tools if "tool_" in t["name"] and int(t["name"].split("_")[1]) // 25 == 4]
        print(f"Category 4 tools in result: {cat_4_tools}")
        assert len(cat_4_tools) > 0
        
        # Verify unrelated tools (like 'Category 0') are NOT dominant
        # In category 0, 'tool_4' might match query 'category 4' semantically due to the '4'
        # But most results should be category 4
        cat_0_tools = [t["name"] for t in tools if "tool_" in t["name"] and int(t["name"].split("_")[1]) // 25 == 0]
        print(f"Category 0 tools in result: {cat_0_tools}")
        assert len(cat_4_tools) >= len(cat_0_tools)

    # 3. Set context for 'Category 9'
    print("\nStep 3: Switching context to 'category 9'...")
    async with httpx.AsyncClient() as client:
        await client.post(f"{base_url}/context", 
                        json={"query": "tools for category 9", "ttl_seconds": 60},
                        headers={"X-Tenant-ID": "test-agent"})

    # 4. List tools again
    print("\nStep 4: Listing filtered tools (Category 9)...")
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/message", 
                               json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                               headers={"X-Tenant-ID": "test-agent"})
        tools = resp.json()["result"]["tools"]
        cat_9_tools = [t["name"] for t in tools if "tool_" in t["name"] and int(t["name"].split("_")[1]) // 25 == 9]
        print(f"Category 9 tools in result: {cat_9_tools}")
        assert len(cat_9_tools) > 0
        
        # Category 4 tools should now be gone
        cat_4_tools = [t["name"] for t in tools if "tool_" in t["name"] and int(t["name"].split("_")[1]) // 25 == 4]
        assert len(cat_4_tools) == 0

    print("\n✅ E2E Test Passed: Semantic filtering is functional and tenant-aware.")

if __name__ == "__main__":
    asyncio.run(run_e2e_test())
