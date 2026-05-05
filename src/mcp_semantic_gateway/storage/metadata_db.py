import json
import aiosqlite
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING
from dataclasses import dataclass
from datetime import datetime, timezone

if TYPE_CHECKING:
    from mcp_semantic_gateway.ingestion.chunker import ToolChunk
    from mcp_semantic_gateway.ingestion.use_case_miner import UseCaseRecord


# Conservative bound-parameter chunk size for ``WHERE tool_id IN (?, ?, ...)``
# queries. SQLite's default ``SQLITE_MAX_VARIABLE_NUMBER`` was 999 before
# 3.32 and 32766 since; 500 is safely under both.
_SQLITE_PARAM_BATCH = 500

@dataclass
class ToolRecord:
    tool_id: str
    server_id: str
    name: str
    title: Optional[str] = None
    description: Optional[str] = None
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None
    annotations: Optional[dict] = None
    embedding_text: str = ""
    indexed_at: str = ""
    index_version: int = 0
    item_type: str = "tool"
    # Structured execution metadata for OpenAPI-sourced tools (method, path,
    # parameter locations, body schema, security, server overrides). NULL for
    # native MCP tools, skills, or prompts.
    route_metadata: Optional[dict] = None
    # Stable hnswlib label assigned at the last successful ``index_all`` pass.
    # ``index_writer.index_all`` clears this column on every row before
    # re-saving fresh records, so stale rows from removed/renamed sources
    # carry ``vector_id = NULL`` and never satisfy ``WHERE vector_id = ?``.
    vector_id: Optional[int] = None

class MetadataDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def initialize(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    server_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    command TEXT,
                    args TEXT,
                    env TEXT,
                    enabled INTEGER,
                    tags TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tools (
                    tool_id TEXT PRIMARY KEY,
                    server_id TEXT,
                    name TEXT,
                    title TEXT,
                    description TEXT,
                    input_schema TEXT,
                    output_schema TEXT,
                    annotations TEXT,
                    embedding_text TEXT,
                    indexed_at TEXT,
                    index_version INTEGER,
                    item_type TEXT DEFAULT 'tool',
                    route_metadata TEXT,
                    vector_id INTEGER,
                    FOREIGN KEY(server_id) REFERENCES servers(server_id)
                )
            """)
            # Migrate older databases that predate route_metadata / vector_id.
            cursor = await db.execute("PRAGMA table_info(tools)")
            existing_cols = {row[1] for row in await cursor.fetchall()}
            if "route_metadata" not in existing_cols:
                await db.execute("ALTER TABLE tools ADD COLUMN route_metadata TEXT")
            if "vector_id" not in existing_cols:
                await db.execute("ALTER TABLE tools ADD COLUMN vector_id INTEGER")
            # Lookup index for the executor's tool_name -> route_metadata path.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tools_name ON tools(name)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tools_server_name "
                "ON tools(server_id, name)"
            )
            # Lookup index for the SearchCore vector-label -> tool path.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tools_vector_id "
                "ON tools(vector_id)"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS index_versions (
                    server_id TEXT PRIMARY KEY,
                    version INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS use_cases (
                    id TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    linked_tool_names TEXT NOT NULL,
                    prerequisites TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    generated_by TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    use_case_hash TEXT NOT NULL UNIQUE
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_use_cases_server "
                "ON use_cases(server_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_use_cases_source_hash "
                "ON use_cases(source_hash)"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chunk_cache (
                    chunk_hash TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (chunk_hash, model_id, prompt_version)
                )
            """)
            await db.commit()

    async def list_tool_summaries(self) -> List[tuple]:
        """Return ``(tool_id, server_id, name, item_type)`` for every row in
        ``tools``. Used by the registry to compute canonical names in memory.
        """

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT tool_id, server_id, name, item_type FROM tools "
                "ORDER BY server_id, name"
            )
            rows = await cursor.fetchall()
        return [tuple(r) for r in rows]

    async def list_tools_by_ids(self, tool_ids: List[str]) -> List[ToolRecord]:
        """Hydrate many ``ToolRecord``s in batched bulk queries.

        Used by ``ToolRegistry.list_handles`` to avoid the N+1 connection
        pattern of calling :meth:`get_tool_by_id` in a loop. Batches IDs
        under SQLite's bound-parameter limit (default 999 in older SQLite,
        higher in newer; we cap at a safe 500) so very large registries
        don't trip ``too many SQL variables``.
        """

        if not tool_ids:
            return []
        results: List[ToolRecord] = []
        async with aiosqlite.connect(self.db_path) as db:
            for i in range(0, len(tool_ids), _SQLITE_PARAM_BATCH):
                chunk = tool_ids[i:i + _SQLITE_PARAM_BATCH]
                placeholders = ",".join("?" for _ in chunk)
                sql = (
                    "SELECT tool_id, server_id, name, title, description, "
                    "input_schema, output_schema, annotations, embedding_text, "
                    "indexed_at, index_version, item_type, route_metadata, "
                    "vector_id "
                    f"FROM tools WHERE tool_id IN ({placeholders})"
                )
                cursor = await db.execute(sql, chunk)
                rows = await cursor.fetchall()
                results.extend(_row_to_tool_record(r) for r in rows)
        return results

    async def get_tool_by_id(self, tool_id: str) -> Optional[ToolRecord]:
        """Hydrate a full ``ToolRecord`` from the primary key."""

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT tool_id, server_id, name, title, description,
                       input_schema, output_schema, annotations,
                       embedding_text, indexed_at, index_version, item_type,
                       route_metadata, vector_id
                FROM tools
                WHERE tool_id = ?
                """,
                (tool_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_tool_record(row)

    async def save_tool(self, tool: ToolRecord):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO tools (
                    tool_id, server_id, name, title, description,
                    input_schema, output_schema, annotations,
                    embedding_text, indexed_at, index_version, item_type,
                    route_metadata, vector_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tool.tool_id, tool.server_id, tool.name, tool.title, tool.description,
                json.dumps(tool.input_schema) if tool.input_schema else None,
                json.dumps(tool.output_schema) if tool.output_schema else None,
                json.dumps(tool.annotations) if tool.annotations else None,
                tool.embedding_text, tool.indexed_at, tool.index_version, tool.item_type,
                json.dumps(tool.route_metadata) if tool.route_metadata else None,
                tool.vector_id,
            ))
            await db.commit()

    async def clear_vector_ids(self) -> None:
        """Set ``vector_id = NULL`` on every tools row.

        Called by ``index_writer.index_all`` at the start of every ingestion
        pass so stale rows from removed/renamed sources cannot accidentally
        match a fresh hnswlib label. Cleanup of those stale rows themselves
        is handled separately."""

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE tools SET vector_id = NULL")
            await db.commit()

    # ------------------------------------------------------------------
    # Use-case persistence (Phase C)
    # ------------------------------------------------------------------

    async def save_use_case(self, uc: "UseCaseRecord") -> None:
        """Persist a UseCaseRecord. Uses INSERT OR IGNORE on the
        UNIQUE(use_case_hash) constraint so duplicates from re-runs are
        no-ops."""

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO use_cases (
                    id, server_id, source_hash, chunk_id, description,
                    linked_tool_names, prerequisites, confidence,
                    generated_by, generated_at, use_case_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uc.id,
                    uc.server_id,
                    uc.source_hash,
                    uc.chunk_id,
                    uc.description,
                    json.dumps(list(uc.linked_tool_names)),
                    json.dumps(list(uc.prerequisites)),
                    float(uc.confidence),
                    uc.generated_by,
                    uc.generated_at.isoformat()
                    if isinstance(uc.generated_at, datetime)
                    else str(uc.generated_at),
                    uc.use_case_hash,
                ),
            )
            await db.commit()

    async def list_use_cases_for_source(
        self, server_id: str, source_hash: str
    ) -> List["UseCaseRecord"]:
        from mcp_semantic_gateway.ingestion.use_case_miner import UseCaseRecord

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, server_id, source_hash, chunk_id, description,
                       linked_tool_names, prerequisites, confidence,
                       generated_by, generated_at, use_case_hash
                FROM use_cases
                WHERE server_id = ? AND source_hash = ?
                ORDER BY id
                """,
                (server_id, source_hash),
            )
            rows = await cursor.fetchall()

        return [_row_to_use_case(row, UseCaseRecord) for row in rows]

    async def list_use_cases(self) -> List["UseCaseRecord"]:
        from mcp_semantic_gateway.ingestion.use_case_miner import UseCaseRecord

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, server_id, source_hash, chunk_id, description,
                       linked_tool_names, prerequisites, confidence,
                       generated_by, generated_at, use_case_hash
                FROM use_cases
                ORDER BY id
                """
            )
            rows = await cursor.fetchall()

        return [_row_to_use_case(row, UseCaseRecord) for row in rows]

    async def use_case_count_for_source(
        self, server_id: str, source_hash: str
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM use_cases
                WHERE server_id = ? AND source_hash = ?
                """,
                (server_id, source_hash),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def is_chunk_cached(
        self, chunk_hash: str, model_id: str, prompt_version: str
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT 1 FROM chunk_cache
                WHERE chunk_hash = ? AND model_id = ? AND prompt_version = ?
                LIMIT 1
                """,
                (chunk_hash, model_id, prompt_version),
            )
            row = await cursor.fetchone()
        return row is not None

    async def mark_chunk_cached(
        self,
        chunk: "ToolChunk",
        model_id: str,
        prompt_version: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO chunk_cache (
                    chunk_hash, model_id, prompt_version,
                    server_id, chunk_id, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.chunk_hash,
                    model_id,
                    prompt_version,
                    chunk.server_id,
                    chunk.chunk_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()

    async def is_source_cached(
        self, server_id: str, source_hash: str
    ) -> bool:
        """Source-level cache: True iff at least one persisted use-case
        record exists for ``(server_id, source_hash)``.

        Bumping ``prompt_version`` does NOT invalidate this flag — the
        per-chunk cache table handles that, and any LLM re-execution
        deduplicates via the ``use_case_hash`` UNIQUE constraint."""

        return (
            await self.use_case_count_for_source(server_id, source_hash) > 0
        )


def _row_to_tool_record(row) -> ToolRecord:
    """Hydrate a SQLite row into a ``ToolRecord``. Column order must match
    the SELECT clauses in :meth:`MetadataDB.get_tool_by_id` and
    :meth:`MetadataDB.list_tools_by_ids`.
    """

    return ToolRecord(
        tool_id=row[0],
        server_id=row[1],
        name=row[2],
        title=row[3],
        description=row[4],
        input_schema=json.loads(row[5]) if row[5] else None,
        output_schema=json.loads(row[6]) if row[6] else None,
        annotations=json.loads(row[7]) if row[7] else None,
        embedding_text=row[8] or "",
        indexed_at=row[9] or "",
        index_version=int(row[10]) if row[10] is not None else 0,
        item_type=row[11] or "tool",
        route_metadata=json.loads(row[12]) if row[12] else None,
        vector_id=int(row[13]) if row[13] is not None else None,
    )


def _row_to_use_case(row, UseCaseRecord):
    """Hydrate a SQLite row into a UseCaseRecord."""

    (
        id_,
        server_id,
        source_hash,
        chunk_id,
        description,
        linked_tool_names_json,
        prerequisites_json,
        confidence,
        generated_by,
        generated_at_iso,
        use_case_hash,
    ) = row
    return UseCaseRecord(
        id=id_,
        server_id=server_id,
        source_hash=source_hash,
        chunk_id=chunk_id,
        description=description,
        linked_tool_names=json.loads(linked_tool_names_json),
        prerequisites=json.loads(prerequisites_json),
        confidence=float(confidence),
        generated_by=generated_by,
        generated_at=datetime.fromisoformat(generated_at_iso),
        use_case_hash=use_case_hash,
    )
