from toolsearch.storage.init import initialize_data_dir
from toolsearch.config.loader import load_config
from toolsearch.storage.metadata_db import MetadataDB, ToolRecord
from toolsearch.storage.vector_store import VectorStore
from toolsearch.ingestion.collector import MCPClient
from toolsearch.ingestion.embedder import LocalEmbedder, build_embedding_text
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
    
    all_tools = []
    all_texts = []
    
    for server_id, server_config in config.servers.items():
        if not server_config.enabled:
            continue
            
        typer.echo(f"Collecting tools from {server_id}...")
        client = MCPClient(server_id, server_config)
        await client.start()
        try:
            mcp_tools = await client.call_tools_list()
            for t in mcp_tools:
                text = build_embedding_text(t["name"], t.get("title"), t.get("description"))
                record = ToolRecord(
                    tool_id=f"{server_id}::{t['name']}",
                    server_id=server_id,
                    name=t["name"],
                    title=t.get("title"),
                    description=t.get("description"),
                    input_schema=t.get("inputSchema"),
                    embedding_text=text,
                    indexed_at=datetime.utcnow().isoformat(),
                    index_version=1
                )
                all_tools.append(record)
                all_texts.append(text)
        finally:
            await client.stop()
            
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
