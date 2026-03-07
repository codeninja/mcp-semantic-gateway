import asyncio
from mcp_semantic_gateway.config.loader import load_config
from mcp_semantic_gateway.storage.init import initialize_data_dir
from mcp_semantic_gateway.storage.vector_store import VectorStore
from mcp_semantic_gateway.ingestion.embedder import LocalEmbedder
from mcp_semantic_gateway.storage.metadata_db import MetadataDB
import aiosqlite
import json

async def run_query(query_text: str):
    base_dir = initialize_data_dir()
    config = load_config()
    
    embedder = LocalEmbedder(config.embedding.model_name)
    vector_store = VectorStore(base_dir / "index" / "vectors.db", dim=config.embedding.dimensions)
    vector_store.load()
    
    db_path = base_dir / "index" / "metadata.db"
    
    query_vector = embedder.embed([query_text])[0]
    labels, scores = vector_store.knn_query(query_vector, k=config.retrieval.top_k)
    
    print(f"Query: {query_text}")
    print("-" * 40)
    
    async with aiosqlite.connect(db_path) as db:
        for label, score in zip(labels, scores):
            # Distance is returned, so score is 1 - distance for cosine
            sim = 1.0 - score
            # Fetch tool name from DB by label? 
            # In index_writer we just used list(range(len(all_tools)))
            # We need to map labels back to tool IDs.
            # For simplicity in this demo, let's just use the label as index into the tools list.
            # But the metadata DB should ideally store this.
            
            # Since we inserted 250 tools, tool_0...tool_249.
            # The labels correspond to the insertion order.
            async with db.execute("SELECT name, description FROM tools LIMIT 1 OFFSET ?", (label,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    name, desc = row
                    print(f"[{sim:.4f}] {name}: {desc[:100]}...")

if __name__ == "__main__":
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "operation on category 4"
    asyncio.run(run_query(query))
