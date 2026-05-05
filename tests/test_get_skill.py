"""Tests for the ``mcp_semantic_gateway_get_skill`` internal tool.

Covers both layers:

* :meth:`SearchCore.get_skill_by_name` — DB lookup + on-disk SKILL.md read.
* :meth:`ToolRouter.call` for ``mcp_semantic_gateway_get_skill`` —
  validation, not-found, and happy-path response shape.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig
from mcp_semantic_gateway.integration.openapi_executor import OpenAPIExecutor
from mcp_semantic_gateway.integration.router import ToolRouter
from mcp_semantic_gateway.retrieval.core import SearchCore
from mcp_semantic_gateway.retrieval.registry import ToolRegistry
from mcp_semantic_gateway.storage.metadata_db import MetadataDB, ToolRecord


SKILL_BODY = """---
name: manage-pet-orders
description: |
  Place orders, track inventory, and audit pending pet orders.
allowed-tools:
  - getInventory
  - placeOrder
  - getOrderById
---

## When to use this skill

Use this skill when a user wants to place a new order or audit existing
order state.

## Procedure

1. Call `getInventory` to see current stock by status.
2. Call `placeOrder` with id, petId, and quantity.
3. Call `getOrderById` to confirm.
"""


def _stub_embedder():
    """SearchCore.__init__ instantiates an embedder; replace with a stub
    so the test never loads SentenceTransformer."""

    e = MagicMock()
    e.embed.return_value = np.zeros((1, 384))
    return e


async def _seed_skill(db_path: Path, *, name: str, skill_md_path: Path,
                     description: str = "Stored skill description.") -> None:
    db = MetadataDB(db_path)
    await db.initialize()
    await db.save_tool(ToolRecord(
        tool_id=f"petstore-skills::skill::{name}",
        server_id="petstore-skills",
        name=name,
        description=description,
        item_type="skill",
        annotations={
            "path": str(skill_md_path),
            "allowed_tools": ["getInventory", "placeOrder", "getOrderById"],
        },
    ))


# ---------------------------------------------------------------------------
# SearchCore layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_skill_by_name_returns_full_body():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "index").mkdir()
        skill_md = base / "manage-pet-orders" / "v1" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text(SKILL_BODY, encoding="utf-8")

        await _seed_skill(
            base / "index" / "metadata.db",
            name="manage-pet-orders",
            skill_md_path=skill_md,
        )

        cfg = MCPSemanticGatewayConfig()
        core = SearchCore(cfg, base)
        core.embedder = _stub_embedder()

        skill = await core.get_skill_by_name("manage-pet-orders")
        assert skill is not None
        assert skill["name"] == "manage-pet-orders"
        assert skill["allowed_tools"] == [
            "getInventory", "placeOrder", "getOrderById",
        ]
        assert "## Procedure" in skill["body_markdown"]
        assert "getOrderById" in skill["body_markdown"]


@pytest.mark.asyncio
async def test_get_skill_by_name_returns_none_for_unknown():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "index").mkdir()
        # Empty DB — no skills indexed.
        db = MetadataDB(base / "index" / "metadata.db")
        await db.initialize()

        cfg = MCPSemanticGatewayConfig()
        core = SearchCore(cfg, base)
        core.embedder = _stub_embedder()

        skill = await core.get_skill_by_name("does-not-exist")
        assert skill is None


@pytest.mark.asyncio
async def test_get_skill_by_name_handles_missing_file_on_disk():
    """DB knows about the skill but the SKILL.md was deleted out from
    under us — return the metadata with an empty body rather than crash."""

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "index").mkdir()
        skill_md = base / "ghost" / "v1" / "SKILL.md"  # never created

        await _seed_skill(
            base / "index" / "metadata.db",
            name="ghost-skill",
            skill_md_path=skill_md,
        )

        cfg = MCPSemanticGatewayConfig()
        core = SearchCore(cfg, base)
        core.embedder = _stub_embedder()

        skill = await core.get_skill_by_name("ghost-skill")
        assert skill is not None
        assert skill["body_markdown"] == ""


# ---------------------------------------------------------------------------
# Router layer
# ---------------------------------------------------------------------------


async def _build_router(base: Path) -> ToolRouter:
    cfg = MCPSemanticGatewayConfig()
    registry = ToolRegistry(base / "index" / "metadata.db")
    await registry.initialize()
    core = SearchCore(cfg, base, registry=registry)
    core.embedder = _stub_embedder()
    return ToolRouter(
        cfg,
        registry=registry,
        openapi_executor=OpenAPIExecutor(cfg),
        search_core=core,
    )


@pytest.mark.asyncio
async def test_router_get_skill_returns_body_in_content_block():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "index").mkdir()
        skill_md = base / "manage-pet-orders" / "v1" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text(SKILL_BODY, encoding="utf-8")

        await _seed_skill(
            base / "index" / "metadata.db",
            name="manage-pet-orders",
            skill_md_path=skill_md,
        )

        router = await _build_router(base)
        result = await router.call(
            "mcp_semantic_gateway_get_skill",
            {"name": "manage-pet-orders"},
        )

        assert "isError" not in result
        text = result["content"][0]["text"]
        assert "## Procedure" in text
        assert "getOrderById" in text
        # Structured content carries the parsed shape too.
        assert result["structuredContent"]["name"] == "manage-pet-orders"
        assert "getInventory" in result["structuredContent"]["allowed_tools"]


@pytest.mark.asyncio
async def test_router_get_skill_validation_error_when_name_missing():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "index").mkdir()
        db = MetadataDB(base / "index" / "metadata.db")
        await db.initialize()

        router = await _build_router(base)
        result = await router.call("mcp_semantic_gateway_get_skill", {})
        assert result.get("isError") is True
        err = result["structuredContent"]["error"]
        assert err["kind"] == "validation_error"


@pytest.mark.asyncio
async def test_router_get_skill_returns_not_found_for_unknown_name():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "index").mkdir()
        db = MetadataDB(base / "index" / "metadata.db")
        await db.initialize()

        router = await _build_router(base)
        result = await router.call(
            "mcp_semantic_gateway_get_skill",
            {"name": "missing-skill"},
        )
        assert result.get("isError") is True
        err = result["structuredContent"]["error"]
        assert err["kind"] == "skill_not_found"
        assert err["name"] == "missing-skill"
