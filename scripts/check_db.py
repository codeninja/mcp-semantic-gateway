import aiosqlite
import asyncio
from pathlib import Path

async def check():
    db_path = Path("/home/codeninja/.mcp_semantic_gateway/index/metadata.db")
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT tool_id, name, description FROM tools LIMIT 5") as cursor:
            async for row in cursor:
                print(row)

if __name__ == "__main__":
    asyncio.run(check())
