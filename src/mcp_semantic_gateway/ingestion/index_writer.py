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

        # Only NULL the prior vector_ids once collection + embedding have
        # succeeded — otherwise a 0-item collection or a transient embedder
        # failure would invalidate the on-disk index without producing a
        # replacement (vectors.db still has old labels, but every metadata
        # row would carry ``vector_id = NULL`` so lookups never match).
        # Note: this still doesn't make the rewrite atomic — a crash
        # between this line and ``vector_store.save()`` below can leave a
        # partially-rewritten metadata.db. Full atomicity (single
        # transaction or temp-DB swap) is tracked separately.
        log("Saving index...")
        await db.clear_vector_ids()
        for tool in all_tools:
            await db.save_tool(tool)

        vector_store.add_items(vectors, list(range(len(all_tools))))
        vector_store.save()
        log("Done.")
    else:
        # Don't touch ``vector_id`` when nothing was harvested — the previous
        # successful index keeps serving searches.
        log("No tools found to index.")
    return len(all_tools)


async def run_indexing():
    base_dir = initialize_data_dir()
    config = load_config()
    await index_all(config, base_dir)


if __name__ == "__main__":
    asyncio.run(run_indexing())
