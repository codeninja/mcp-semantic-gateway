from mcp_semantic_gateway.storage.init import initialize_data_dir
from mcp_semantic_gateway.config.loader import load_config
from mcp_semantic_gateway.storage.metadata_db import MetadataDB, ToolRecord
from mcp_semantic_gateway.storage.vector_store import VectorStore
from mcp_semantic_gateway.ingestion.collector import MCPClient
from mcp_semantic_gateway.ingestion.embedder import LocalEmbedder, build_embedding_text
import asyncio
from datetime import datetime
import typer

async def run_indexing():
    base_dir = initialize_data_dir()
    config = load_config()
    
    db = MetadataDB(base_dir / "index" / "metadata.db")
    await db.initialize()
    
    embedder = LocalEmbedder(config.embedding.model_name)
    vector_store = VectorStore(base_dir / "index" / "vectors.db", dim=config.embedding.dimensions)
    
    from mcp_semantic_gateway.ingestion.collector import Collector
    collector = Collector(config)
    
    typer.echo("Collecting items from all sources...")
    all_harvested = await collector.collect_all()
    
    all_tools = []
    all_texts = []
    
    for t in all_harvested:
        server_id = t["_server_id"]
        item_type = t["_item_type"]
        text = build_embedding_text(t["name"], t.get("title"), t.get("description"))
        record = ToolRecord(
            tool_id=f"{server_id}::{item_type}::{t['name']}",
            server_id=server_id,
            name=t["name"],
            title=t.get("title"),
            description=t.get("description"),
            input_schema=t.get("inputSchema") if item_type == "tool" else t.get("arguments"),
            annotations=t.get("annotations"),
            embedding_text=text,
            indexed_at=datetime.utcnow().isoformat(),
            index_version=1,
            item_type=item_type
        )
        all_tools.append(record)
        all_texts.append(text)
            
    if all_texts:
        typer.echo(f"Embedding {len(all_texts)} tools...")
        vectors = embedder.embed(all_texts)
        
        typer.echo("Saving index...")
        for i, tool in enumerate(all_tools):
            await db.save_tool(tool)
            
        vector_store.add_items(vectors, list(range(len(all_tools))))
        vector_store.save()
        typer.echo("Done.")
    else:
        typer.echo("No tools found to index.")

if __name__ == "__main__":
    asyncio.run(run_indexing())
