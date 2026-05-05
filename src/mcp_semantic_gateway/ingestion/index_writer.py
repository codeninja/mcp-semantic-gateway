from mcp_semantic_gateway.storage.init import initialize_data_dir
from mcp_semantic_gateway.config.loader import load_config
from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig
from mcp_semantic_gateway.storage.metadata_db import MetadataDB, ToolRecord
from mcp_semantic_gateway.storage.vector_store import VectorStore
from mcp_semantic_gateway.ingestion.collector import Collector
from mcp_semantic_gateway.ingestion.embedder import LocalEmbedder, build_embedding_text
import asyncio
from datetime import datetime
from pathlib import Path
import typer


async def index_all(config: MCPSemanticGatewayConfig, base_dir: Path, *, log=typer.echo) -> int:
    """Run the full ingestion pipeline against ``config`` and persist into
    ``base_dir/index/``. Returns the number of items written.

    Split out from ``run_indexing`` so the same code path is exercised by
    the E2E acceptance test and the CLI entry point.
    """

    db = MetadataDB(base_dir / "index" / "metadata.db")
    await db.initialize()
    # Neutralize any stale rows from previous runs: removed or renamed
    # sources keep their primary key (``tool_id``) so ``INSERT OR REPLACE``
    # will not overwrite them, but their ``vector_id`` must not collide
    # with a fresh hnswlib label. ``SearchCore`` joins on
    # ``WHERE vector_id = ?`` so NULL-ing them out makes them invisible to
    # search until they are explicitly cleaned up.
    await db.clear_vector_ids()

    embedder = LocalEmbedder(config.embedding.model_name)
    vector_store = VectorStore(
        base_dir / "index" / "vectors.db", dim=config.embedding.dimensions
    )

    collector = Collector(config)

    log("Collecting items from all sources...")
    all_harvested = await collector.collect_all()

    all_tools = []
    all_texts = []

    for i, t in enumerate(all_harvested):
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
            item_type=item_type,
            route_metadata=t.get("_route_metadata"),
            # Same int we hand to ``vector_store.add_items`` below so
            # ``SearchCore`` can join on ``WHERE vector_id = ?``.
            vector_id=i,
        )
        all_tools.append(record)
        all_texts.append(text)

    if all_texts:
        log(f"Embedding {len(all_texts)} tools...")
        vectors = embedder.embed(all_texts)

        log("Saving index...")
        for tool in all_tools:
            await db.save_tool(tool)

        vector_store.add_items(vectors, list(range(len(all_tools))))
        vector_store.save()
        log("Done.")
    else:
        log("No tools found to index.")
    return len(all_tools)


async def run_indexing():
    base_dir = initialize_data_dir()
    config = load_config()
    await index_all(config, base_dir)


if __name__ == "__main__":
    asyncio.run(run_indexing())
