"""Tests for `ingestion/chunker.py` and the forge tag-plumbing change."""

from __future__ import annotations

from mcp_semantic_gateway.ingestion.chunker import (
    ToolChunk,
    chunk_tools,
    compute_chunk_hash,
    compute_source_hash_mcp,
    compute_source_hash_openapi,
)
from mcp_semantic_gateway.ingestion.forge import ForgeEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openapi_tool(
    name: str,
    *,
    path: str | None = None,
    tags: list[str] | None = None,
    description: str = "",
) -> dict:
    annotations: dict = {"source": "openapi"}
    if path is not None:
        annotations["path"] = path
    if tags is not None:
        annotations["tags"] = list(tags)
    return {
        "name": name,
        "description": description or f"desc-{name}",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": annotations,
    }


def _make_mcp_tool(name: str, *, description: str = "") -> dict:
    return {
        "name": name,
        "description": description or f"desc-{name}",
        "inputSchema": {"type": "object", "properties": {}},
    }


# ---------------------------------------------------------------------------
# 1. Tagged OpenAPI -> tag grouping
# ---------------------------------------------------------------------------


def test_tagged_openapi_groups_by_tag():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Tagged API", "version": "1.0.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "summary": "List users",
                    "tags": ["users"],
                },
                "post": {
                    "operationId": "createUser",
                    "summary": "Create a user",
                    "tags": ["users"],
                },
            },
            "/repos": {
                "get": {
                    "operationId": "listRepos",
                    "summary": "List repos",
                    "tags": ["repos"],
                },
            },
        },
    }
    tools = ForgeEngine.forge_tools(spec)
    assert len(tools) == 3
    # All tools have tags after Phase B forge change.
    assert all(t["annotations"]["tags"] for t in tools)

    chunks = chunk_tools(
        tools,
        server_id="srv",
        source_hash="src-hash",
        source_kind="openapi",
    )
    labels = sorted(c.group_label for c in chunks)
    assert labels == ["repos", "users"]
    repos = next(c for c in chunks if c.group_label == "repos")
    users = next(c for c in chunks if c.group_label == "users")
    assert {t["name"] for t in repos.tools} == {"listRepos"}
    assert {t["name"] for t in users.tools} == {"listUsers", "createUser"}
    assert repos.chunk_id == "chunk-srv-repos-0"
    assert users.chunk_id == "chunk-srv-users-0"


# ---------------------------------------------------------------------------
# 2. Untagged OpenAPI -> path-prefix grouping
# ---------------------------------------------------------------------------


def test_untagged_openapi_groups_by_path_prefix():
    tools = [
        _make_openapi_tool("listUsers", path="/users", tags=[]),
        _make_openapi_tool("getUser", path="/users/{id}", tags=[]),
        _make_openapi_tool("listRepos", path="/repos", tags=[]),
        _make_openapi_tool("getRepo", path="/repos/{id}", tags=[]),
    ]
    chunks = chunk_tools(
        tools,
        server_id="srv",
        source_hash="src-hash",
        source_kind="openapi",
    )
    labels = sorted(c.group_label for c in chunks)
    assert labels == ["repos", "users"]
    users = next(c for c in chunks if c.group_label == "users")
    repos = next(c for c in chunks if c.group_label == "repos")
    assert {t["name"] for t in users.tools} == {"listUsers", "getUser"}
    assert {t["name"] for t in repos.tools} == {"listRepos", "getRepo"}


# ---------------------------------------------------------------------------
# 3. Oversized group splits into multiple chunks
# ---------------------------------------------------------------------------


def test_oversized_tag_group_splits():
    chunk_size = 12
    overflow = 5
    n = chunk_size + overflow
    tools = [
        _make_openapi_tool(f"users_{i}", path=f"/users/{i}", tags=["users"])
        for i in range(n)
    ]
    chunks = chunk_tools(
        tools,
        server_id="srv",
        source_hash="src-hash",
        source_kind="openapi",
        chunk_size=chunk_size,
    )
    assert len(chunks) == 2
    assert chunks[0].chunk_id == "chunk-srv-users-0"
    assert chunks[1].chunk_id == "chunk-srv-users-1"
    assert len(chunks[0].tools) == chunk_size
    assert len(chunks[1].tools) == overflow
    # Input order preserved within chunks.
    assert [t["name"] for t in chunks[0].tools] == [f"users_{i}" for i in range(chunk_size)]
    assert [t["name"] for t in chunks[1].tools] == [
        f"users_{i}" for i in range(chunk_size, n)
    ]


# ---------------------------------------------------------------------------
# 4. Live MCP name-prefix grouping with a lone tool
# ---------------------------------------------------------------------------


def test_mcp_name_prefix_groups_with_lone_tool():
    tools = [
        _make_mcp_tool("gh_issue_open"),
        _make_mcp_tool("gh_issue_close"),
        _make_mcp_tool("slack_post"),
        _make_mcp_tool("slack_dm"),
        _make_mcp_tool("lone_tool"),
    ]
    chunks = chunk_tools(
        tools,
        server_id="srv",
        source_hash="src-hash",
        source_kind="mcp",
    )
    labels = [c.group_label for c in chunks]
    # Named groups in alpha order, 'mixed' last.
    assert labels == ["gh_issue", "slack", "mixed"]

    gh = chunks[0]
    sl = chunks[1]
    mx = chunks[2]
    assert {t["name"] for t in gh.tools} == {"gh_issue_open", "gh_issue_close"}
    assert {t["name"] for t in sl.tools} == {"slack_post", "slack_dm"}
    assert [t["name"] for t in mx.tools] == ["lone_tool"]
    assert mx.group_label == "mixed"


# ---------------------------------------------------------------------------
# 5. Source-hash determinism — key order should not matter
# ---------------------------------------------------------------------------


def test_source_hash_openapi_is_key_order_independent():
    spec_a = {
        "openapi": "3.0.0",
        "info": {"title": "X", "version": "1"},
        "paths": {"/a": {"get": {"operationId": "getA"}}},
    }
    spec_b = {
        "paths": {"/a": {"get": {"operationId": "getA"}}},
        "info": {"version": "1", "title": "X"},
        "openapi": "3.0.0",
    }
    assert compute_source_hash_openapi(spec_a) == compute_source_hash_openapi(spec_b)


def test_source_hash_mcp_is_order_independent():
    tools_a = [
        {"name": "alpha", "description": "first"},
        {"name": "beta", "description": "second"},
    ]
    tools_b = list(reversed(tools_a))
    assert compute_source_hash_mcp(tools_a) == compute_source_hash_mcp(tools_b)


# ---------------------------------------------------------------------------
# 6. Chunk-hash determinism — tool order should not matter
# ---------------------------------------------------------------------------


def test_chunk_hash_is_tool_order_independent():
    tools_a = [
        _make_mcp_tool("alpha"),
        _make_mcp_tool("beta"),
        _make_mcp_tool("gamma"),
    ]
    tools_b = [tools_a[2], tools_a[0], tools_a[1]]
    assert compute_chunk_hash(tools_a) == compute_chunk_hash(tools_b)


# ---------------------------------------------------------------------------
# 7. chunk_id determinism — re-running yields identical ids and hashes
# ---------------------------------------------------------------------------


def test_chunk_tools_is_deterministic_across_runs():
    tools = [
        _make_openapi_tool("listUsers", path="/users", tags=["users"]),
        _make_openapi_tool("createUser", path="/users", tags=["users"]),
        _make_openapi_tool("listRepos", path="/repos", tags=["repos"]),
    ]
    kwargs = dict(
        server_id="srv",
        source_hash="src-hash",
        source_kind="openapi",
        chunk_size=12,
    )
    a = chunk_tools(tools, **kwargs)
    b = chunk_tools(tools, **kwargs)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert [c.chunk_hash for c in a] == [c.chunk_hash for c in b]
    # And every chunk validates as a ToolChunk.
    for c in a:
        assert isinstance(c, ToolChunk)


# ---------------------------------------------------------------------------
# 8. Forge tag plumbing
# ---------------------------------------------------------------------------


def test_forge_propagates_operation_tags():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "summary": "Tagged op",
                    "tags": ["foo", "bar"],
                }
            }
        },
    }
    tools = ForgeEngine.forge_tools(spec)
    assert len(tools) == 1
    assert tools[0]["annotations"]["tags"] == ["foo", "bar"]


def test_forge_defaults_tags_to_empty_list_when_absent():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/x": {
                "get": {"operationId": "getX", "summary": "Untagged op"}
            }
        },
    }
    tools = ForgeEngine.forge_tools(spec)
    assert tools[0]["annotations"]["tags"] == []
