from typing import Dict, List, Optional
from pathlib import Path
import time
import json
import aiosqlite
from mcp_semantic_gateway.config.models import MCP Semantic GatewayConfig, FallbackBehavior
from mcp_semantic_gateway.storage.vector_store import VectorStore
from mcp_semantic_gateway.ingestion.embedder import LocalEmbedder

class SearchCore:
    def __init__(self, config: MCP Semantic GatewayConfig, base_dir: Path):
        self.config = config
        self.base_dir = base_dir
        self.embedder = LocalEmbedder(config.embedding.model_name)
        self.vector_store = VectorStore(self.base_dir / "index" / "vectors.db", dim=config.embedding.dimensions)
        self.vector_store.load()
        self.db_path = self.base_dir / "index" / "metadata.db"
        self.contexts: Dict[str, Dict] = {} # tenant_id -> {query, expires_at}

    def set_context(self, tenant_id: str, query: str, ttl: int = 300):
        self.contexts[tenant_id] = {
            "query": query,
            "expires_at": time.time() + ttl
        }

    async def get_filtered_items(self, tenant_id: str, item_type: str = "tool") -> List[dict]:
        ctx = self.contexts.get(tenant_id)
        query = None
        if ctx and ctx["expires_at"] > time.time():
            query = ctx["query"]

        if not query:
            # Fallback logic
            fallback = self.config.proxy.fallback_on_no_context
            if fallback == FallbackBehavior.NONE:
                return []
            
            items = []
            async with aiosqlite.connect(self.db_path) as db:
                sql = "SELECT name, description, input_schema, item_type FROM tools WHERE item_type = ?"
                async with db.execute(sql, (item_type,)) as cursor:
                    async for row in cursor:
                        item = {
                            "name": row[0],
                            "description": row[1],
                        }
                        if item_type == "tool":
                            item["inputSchema"] = json.loads(row[2]) if row[2] else {"type": "object"}
                        else:
                            # For prompts, arguments is the equivalent
                            item["arguments"] = json.loads(row[2]) if row[2] else []
                        items.append(item)
            return items

        # Semantic retrieval
        query_vector = self.embedder.embed([query])[0]
        # Query more to filter by type
        labels, _ = self.vector_store.knn_query(query_vector, k=min(100, self.config.retrieval.top_k * 5))
        
        items = []
        async with aiosqlite.connect(self.db_path) as db:
            for label in labels:
                async with db.execute("SELECT name, description, input_schema, item_type FROM tools LIMIT 1 OFFSET ?", (label,)) as cursor:
                    row = await cursor.fetchone()
                    if row and row[3] == item_type:
                        item = {
                            "name": row[0],
                            "description": row[1],
                        }
                        if item_type == "tool":
                            item["inputSchema"] = json.loads(row[2]) if row[2] else {"type": "object"}
                        else:
                            item["arguments"] = json.loads(row[2]) if row[2] else []
                        items.append(item)
                        if len(items) >= self.config.retrieval.top_k:
                            break
        return items

    async def get_filtered_tools(self, tenant_id: str) -> List[dict]:
        return await self.get_filtered_items(tenant_id, "tool")

    async def get_filtered_prompts(self, tenant_id: str) -> List[dict]:
        return await self.get_filtered_items(tenant_id, "prompt")

    async def get_filtered_skills(self, tenant_id: str) -> List[dict]:
        return await self.get_filtered_items(tenant_id, "skill")
