from typing import Dict, List, Optional
from pathlib import Path
import time
import json
import aiosqlite
from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig, FallbackBehavior
from mcp_semantic_gateway.storage.vector_store import VectorStore
from mcp_semantic_gateway.ingestion.embedder import LocalEmbedder

class SearchCore:
    def __init__(
        self,
        config: MCPSemanticGatewayConfig,
        base_dir: Path,
        registry: Optional[object] = None,
    ):
        self.config = config
        self.base_dir = base_dir
        self.embedder = LocalEmbedder(config.embedding.model_name)
        self.vector_store = VectorStore(self.base_dir / "index" / "vectors.db", dim=config.embedding.dimensions)
        self.vector_store.load()
        self.db_path = self.base_dir / "index" / "metadata.db"
        self.contexts: Dict[str, Dict] = {} # tenant_id -> {query, expires_at}
        # Optional ``ToolRegistry``. When provided, item names in
        # ``tools/list`` output are rewritten to the registry's canonical form
        # so collisions are visible to MCP clients. Untyped here to avoid an
        # import cycle with :mod:`retrieval.registry`.
        self.registry = registry

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
                sql = "SELECT name, description, input_schema, item_type, server_id FROM tools WHERE item_type = ?"
                async with db.execute(sql, (item_type,)) as cursor:
                    async for row in cursor:
                        items.append(self._row_to_item(row, item_type))
            return items

        # Semantic retrieval
        query_vector = self.embedder.embed([query])[0]
        # Query more to filter by type
        labels, _ = self.vector_store.knn_query(query_vector, k=min(100, self.config.retrieval.top_k * 5))

        items = []
        async with aiosqlite.connect(self.db_path) as db:
            for label in labels:
                async with db.execute(
                    "SELECT name, description, input_schema, item_type, server_id "
                    "FROM tools WHERE vector_id = ?",
                    (int(label),),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[3] == item_type:
                        items.append(self._row_to_item(row, item_type))
                        if len(items) >= self.config.retrieval.top_k:
                            break
        return items

    def _row_to_item(self, row, item_type: str) -> dict:
        name, description, input_schema_json, _item_type, server_id = row
        canonical = name
        if self.registry is not None and item_type == "tool":
            resolved = self.registry.canonical_for(server_id, name)
            if resolved:
                canonical = resolved
        item = {"name": canonical, "description": description}
        if item_type == "tool":
            item["inputSchema"] = json.loads(input_schema_json) if input_schema_json else {"type": "object"}
        else:
            # For prompts, arguments is the equivalent
            item["arguments"] = json.loads(input_schema_json) if input_schema_json else []
        return item

    async def search_with_scores(
        self,
        query: str,
        top_k: Optional[int] = None,
        item_types: Optional[List[str]] = None,
    ) -> List[dict]:
        """Run a semantic search and return scored matches.

        Each match is a dict with ``name``, ``server_id``, ``item_type``,
        ``description``, and ``score`` (cosine similarity in ``[0, 1]``,
        higher = more similar). Used by the ``mcp-semantic-gateway search``
        CLI to sanity-check what the gateway would return for a given
        query directly against the same retrieval path the proxy uses.
        """

        if not query or not query.strip():
            return []
        k = top_k if top_k is not None else self.config.retrieval.top_k
        query_vector = self.embedder.embed([query])[0]
        # Pull a wider candidate set so item_type filtering can still
        # produce ``k`` results in the common case.
        oversample = k * 5 if item_types else k
        labels, distances = self.vector_store.knn_query(
            query_vector, k=min(100, oversample)
        )
        if not labels:
            return []

        items: list[dict] = []
        async with aiosqlite.connect(self.db_path) as db:
            for label, distance in zip(labels, distances):
                async with db.execute(
                    "SELECT name, description, item_type, server_id "
                    "FROM tools WHERE vector_id = ?",
                    (int(label),),
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    continue
                name, description, item_type, server_id = row
                if item_types and item_type not in item_types:
                    continue
                if self.registry is not None and item_type == "tool":
                    resolved = self.registry.canonical_for(server_id, name)
                    if resolved:
                        name = resolved
                items.append(
                    {
                        "name": name,
                        "server_id": server_id,
                        "item_type": item_type,
                        "description": description or "",
                        "score": max(0.0, 1.0 - float(distance)),
                    }
                )
                if len(items) >= k:
                    break
        return items

    async def get_filtered_tools(self, tenant_id: str) -> List[dict]:
        return await self.get_filtered_items(tenant_id, "tool")

    async def get_filtered_prompts(self, tenant_id: str) -> List[dict]:
        return await self.get_filtered_items(tenant_id, "prompt")

    async def get_filtered_skills(self, tenant_id: str) -> List[dict]:
        return await self.get_filtered_items(tenant_id, "skill")

    async def get_skill_by_name(self, name: str) -> Optional[dict]:
        """Return the full skill payload for ``name``, or ``None`` if no
        such skill is indexed.

        Skills are looked up by name (since that's the handle the agent
        already has from ``find_skills``). The SKILL.md file referenced by
        ``annotations.path`` is read on demand and the entire body is
        returned as ``body_markdown`` so the caller can follow the
        skill's procedure verbatim.
        """

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT name, description, annotations, server_id "
                "FROM tools WHERE item_type = 'skill' AND name = ? LIMIT 1",
                (name,),
            ) as cursor:
                row = await cursor.fetchone()

        if row is None:
            return None

        skill_name, description, annotations_json, server_id = row
        annotations: dict = (
            json.loads(annotations_json) if annotations_json else {}
        )
        path_str = annotations.get("path")
        body_markdown = ""
        if path_str:
            try:
                body_markdown = Path(path_str).read_text(encoding="utf-8")
            except OSError:
                body_markdown = ""

        return {
            "name": skill_name,
            "description": description,
            "server_id": server_id,
            "allowed_tools": annotations.get("allowed_tools", []),
            "path": path_str,
            "body_markdown": body_markdown,
        }
