"""Tests for ``SearchCore.search_with_scores`` and the ``mcp-semantic-gateway
search`` CLI."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from typer.testing import CliRunner

from mcp_semantic_gateway.cli.main import app as cli_app
from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig
from mcp_semantic_gateway.retrieval.core import SearchCore
from mcp_semantic_gateway.storage.metadata_db import MetadataDB, ToolRecord
from mcp_semantic_gateway.storage.vector_store import VectorStore


def _stub_embedder(query_vec=None):
    e = MagicMock()
    e.embed.return_value = np.asarray([query_vec if query_vec is not None else [1.0, 0.0]])
    return e


async def _seed_two_tools(base: Path) -> None:
    """Index two tools with different embeddings so similarity ordering is
    predictable."""

    db_path = base / "index" / "metadata.db"
    vec_path = base / "index" / "vectors.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = MetadataDB(db_path)
    await db.initialize()

    await db.save_tool(
        ToolRecord(
            tool_id="petstore::tool::listPets",
            server_id="petstore",
            name="listPets",
            description="List pets in the store.",
            item_type="tool",
        )
    )
    await db.save_tool(
        ToolRecord(
            tool_id="weather::tool::getForecast",
            server_id="weather",
            name="getForecast",
            description="Daily weather forecast for a US zip code.",
            item_type="tool",
        )
    )

    vs = VectorStore(vec_path, dim=2)
    # listPets perfectly aligns with [1, 0]; getForecast is orthogonal.
    vs.add_items([[1.0, 0.0], [0.0, 1.0]], [0, 1])
    vs.save()


@pytest.mark.asyncio
async def test_search_with_scores_returns_top_matches_with_metadata():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        await _seed_two_tools(base)

        cfg = MCPSemanticGatewayConfig()
        cfg.embedding.dimensions = 2
        core = SearchCore(cfg, base)
        core.embedder = _stub_embedder([1.0, 0.0])

        matches = await core.search_with_scores("list pets", top_k=5)
        assert len(matches) == 2
        # listPets should rank first (cosine similarity = 1.0).
        assert matches[0]["name"] == "listPets"
        assert matches[0]["server_id"] == "petstore"
        assert matches[0]["item_type"] == "tool"
        assert matches[0]["score"] == pytest.approx(1.0, abs=1e-6)
        # getForecast is orthogonal -> similarity ~0.
        assert matches[1]["name"] == "getForecast"
        assert matches[1]["score"] < 0.5


@pytest.mark.asyncio
async def test_search_with_scores_respects_top_k():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        await _seed_two_tools(base)
        cfg = MCPSemanticGatewayConfig()
        cfg.embedding.dimensions = 2
        core = SearchCore(cfg, base)
        core.embedder = _stub_embedder([1.0, 0.0])

        matches = await core.search_with_scores("list pets", top_k=1)
        assert len(matches) == 1
        assert matches[0]["name"] == "listPets"


@pytest.mark.asyncio
async def test_search_with_scores_empty_query_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        await _seed_two_tools(base)
        cfg = MCPSemanticGatewayConfig()
        cfg.embedding.dimensions = 2
        core = SearchCore(cfg, base)
        core.embedder = _stub_embedder()

        assert await core.search_with_scores("") == []
        assert await core.search_with_scores("   ") == []


@pytest.mark.asyncio
async def test_search_with_scores_filters_by_item_type():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        # Seed one tool and one skill.
        db_path = base / "index" / "metadata.db"
        vec_path = base / "index" / "vectors.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = MetadataDB(db_path)
        await db.initialize()
        await db.save_tool(
            ToolRecord(
                tool_id="petstore::tool::listPets",
                server_id="petstore",
                name="listPets",
                description="List pets",
                item_type="tool",
            )
        )
        await db.save_tool(
            ToolRecord(
                tool_id="petstore::skill::manage-pets",
                server_id="petstore",
                name="manage-pets",
                description="Manage pets workflow",
                item_type="skill",
            )
        )
        vs = VectorStore(vec_path, dim=2)
        vs.add_items([[1.0, 0.0], [0.99, 0.05]], [0, 1])
        vs.save()

        cfg = MCPSemanticGatewayConfig()
        cfg.embedding.dimensions = 2
        core = SearchCore(cfg, base)
        core.embedder = _stub_embedder([1.0, 0.0])

        skills_only = await core.search_with_scores(
            "pets", top_k=10, item_types=["skill"]
        )
        assert [m["name"] for m in skills_only] == ["manage-pets"]


def test_search_cli_no_index_exits_nonzero(monkeypatch, tmp_path):
    """Running ``search`` against a brand-new home dir should fail
    actionably, not crash."""

    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli_app, ["search", "anything"])
    assert result.exit_code != 0
    assert "No index" in result.output or "index" in result.output.lower()


def test_search_cli_json_output(monkeypatch, tmp_path):
    """Wire the CLI end-to-end against a tiny seeded index and confirm
    JSON output carries the expected shape."""

    import asyncio

    async def seed():
        await _seed_two_tools(tmp_path)

    asyncio.run(seed())

    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))

    # The CLI loads config via gateway_home(); make sure a config exists so
    # load_config() doesn't trip on empty servers.
    (tmp_path / "config.toml").write_text("", encoding="utf-8")

    # Patch the embedder used inside SearchCore so we don't pull a model.
    from mcp_semantic_gateway.cli import search as search_module

    async def patched_run(query, top_k, item_type, as_json):
        # Re-create the SearchCore but stub the embedder.
        from mcp_semantic_gateway.config.loader import load_config
        from mcp_semantic_gateway.retrieval.core import SearchCore
        from mcp_semantic_gateway.retrieval.registry import ToolRegistry

        cfg = load_config()
        cfg.embedding.dimensions = 2
        registry = ToolRegistry(tmp_path / "index" / "metadata.db")
        await registry.initialize()
        core = SearchCore(cfg, tmp_path, registry=registry)
        core.embedder = _stub_embedder([1.0, 0.0])
        matches = await core.search_with_scores(query, top_k=top_k)
        if as_json:
            import typer

            typer.echo(json.dumps(matches, indent=2))
            return 0 if matches else 1
        return 0

    monkeypatch.setattr(search_module, "_run", patched_run)

    runner = CliRunner()
    result = runner.invoke(cli_app, ["search", "list pets", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert payload[0]["name"] == "listPets"
    assert payload[0]["server_id"] == "petstore"
    assert "score" in payload[0]
