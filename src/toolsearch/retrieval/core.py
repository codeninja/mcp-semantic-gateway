from typing import Dict, List, Optional
from pathlib import Path
import time
import json
import aiosqlite
from toolsearch.config.models import ToolSearchConfig, FallbackBehavior
from toolsearch.storage.vector_store import VectorStore
from toolsearch.ingestion.embedder import LocalEmbedder

class SearchCore:
    def __init__(self, config: ToolSearchConfig, base_dir: Path):
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

    async def get_filtered_tools(self, tenant_id: str) -> List[dict]:
        ctx = self.contexts.get(tenant_id)
        query = None
        if ctx and ctx["expires_at"] > time.time():
            query = ctx["query"]

        if not query:
            # Fallback logic
            fallback = self.config.proxy.fallback_on_no_context
            if fallback == FallbackBehavior.NONE:
                return []
            
            tools = []
            async with aiosqlite.connect(self.db_path) as db:
                sql = "SELECT name, description, input_schema FROM tools"
                if fallback == FallbackBehavior.TAGGED:
                    # Simplified tag filtering for demo - requires server join
                    pass 
                
                async with db.execute(sql) as cursor:
                    async for row in cursor:
                        tools.append({
                            "name": row[0],
                            "description": row[1],
                            "inputSchema": json.loads(row[2]) if row[2] else {"type": "object"}
                        })
            return tools

        # Semantic retrieval
        query_vector = self.embedder.embed([query])[0]
        labels, _ = self.vector_store.knn_query(query_vector, k=self.config.retrieval.top_k)
        
        tools = []
        async with aiosqlite.connect(self.db_path) as db:
            for label in labels:
                async with db.execute("SELECT name, description, input_schema FROM tools LIMIT 1 OFFSET ?", (label,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        tools.append({
                            "name": row[0],
                            "description": row[1],
                            "inputSchema": json.loads(row[2]) if row[2] else {"type": "object"}
                        })
        return tools
