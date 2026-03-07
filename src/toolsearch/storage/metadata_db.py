import os
import sqlite3
import aiosqlite
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

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
                    FOREIGN KEY(server_id) REFERENCES servers(server_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS index_versions (
                    server_id TEXT PRIMARY KEY,
                    version INTEGER
                )
            """)
            await db.commit()

    async def save_tool(self, tool: ToolRecord):
        async with aiosqlite.connect(self.db_path) as db:
            import json
            await db.execute("""
                INSERT OR REPLACE INTO tools (
                    tool_id, server_id, name, title, description, 
                    input_schema, output_schema, annotations, 
                    embedding_text, indexed_at, index_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tool.tool_id, tool.server_id, tool.name, tool.title, tool.description,
                json.dumps(tool.input_schema) if tool.input_schema else None,
                json.dumps(tool.output_schema) if tool.output_schema else None,
                json.dumps(tool.annotations) if tool.annotations else None,
                tool.embedding_text, tool.indexed_at, tool.index_version
            ))
            await db.commit()
