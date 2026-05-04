"""Forge engine tests.

The golden test pins the structured ``route_metadata`` shape that the
``OpenAPIExecutor`` will consume. To regenerate the golden after an
intentional schema change::

    UPDATE_FORGE_GOLDEN=1 pytest tests/test_forge.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from mcp_semantic_gateway.ingestion.forge import ForgeEngine

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL_SPEC = FIXTURES / "forge_minimal.yaml"
GOLDEN = FIXTURES / "forge_minimal_golden.json"


def _forge_minimal() -> list:
    spec = yaml.safe_load(MINIMAL_SPEC.read_text())
    return ForgeEngine.forge_tools(spec)


def test_forge_minimal_golden() -> None:
    actual = _forge_minimal()

    if os.environ.get("UPDATE_FORGE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        return

    expected = json.loads(GOLDEN.read_text())
    # Round-trip ``actual`` through json so dict iteration order matches.
    actual_normalized = json.loads(json.dumps(actual, sort_keys=True))
    assert actual_normalized == expected, (
        "Forge output drifted. Re-run with UPDATE_FORGE_GOLDEN=1 if the change "
        "is intentional."
    )


def test_forge_emits_three_tools() -> None:
    tools = _forge_minimal()
    names = {t["name"] for t in tools}
    assert names == {"getUser", "updateUser", "search"}


def test_forge_resolves_parameter_ref() -> None:
    tools = _forge_minimal()
    get_user = next(t for t in tools if t["name"] == "getUser")
    params = {p["name"]: p for p in get_user["_route_metadata"]["parameters"]}
    # X-Trace-Id was declared as a $ref to components.parameters.TraceId.
    assert "X-Trace-Id" in params
    assert params["X-Trace-Id"]["in"] == "header"
    assert params["X-Trace-Id"]["schema"] == {"type": "string"}


def test_forge_resolves_body_ref_and_inlines_object() -> None:
    tools = _forge_minimal()
    update_user = next(t for t in tools if t["name"] == "updateUser")
    body = update_user["_route_metadata"]["request_body"]
    assert body["content_type"] == "application/json"
    assert body["required"] is True
    assert body["input_style"] == "inline"
    assert body["schema"]["properties"]["name"] == {"type": "string"}
    # The inlined body keys appear in the public inputSchema alongside path
    # params, so callers see one flat argument object.
    props = update_user["inputSchema"]["properties"]
    assert {"id", "name", "age"}.issubset(props)
    assert "id" in update_user["inputSchema"]["required"]
    assert "name" in update_user["inputSchema"]["required"]


def test_forge_path_parameter_is_required_even_without_required_flag() -> None:
    tools = _forge_minimal()
    update_user = next(t for t in tools if t["name"] == "updateUser")
    # The path-item-level ``id`` parameter merges into the operation.
    id_param = next(
        p for p in update_user["_route_metadata"]["parameters"] if p["name"] == "id"
    )
    assert id_param["in"] == "path"
    assert id_param["required"] is True


def test_forge_operation_servers_override_spec_servers() -> None:
    tools = _forge_minimal()
    update_user = next(t for t in tools if t["name"] == "updateUser")
    assert update_user["_route_metadata"]["servers"] == [
        "https://users.api.example.com/v2"
    ]
    # getUser has no operation-level servers, so it inherits the spec-level one.
    get_user = next(t for t in tools if t["name"] == "getUser")
    assert get_user["_route_metadata"]["servers"] == ["https://api.example.com/v1"]


def test_forge_captures_success_response_content_type() -> None:
    tools = _forge_minimal()
    get_user = next(t for t in tools if t["name"] == "getUser")
    resp = get_user["_route_metadata"]["success_response"]
    assert resp["status"] == "200"
    assert resp["content_type"] == "application/json"
    # The $ref to User was resolved.
    assert resp["schema"]["properties"]["name"] == {"type": "string"}


def test_forge_carries_security_requirements() -> None:
    tools = _forge_minimal()
    get_user = next(t for t in tools if t["name"] == "getUser")
    assert get_user["_route_metadata"]["security"] == [{"api_key": []}]
    update_user = next(t for t in tools if t["name"] == "updateUser")
    assert update_user["_route_metadata"]["security"] == []


def test_forge_preserves_array_param_explode() -> None:
    tools = _forge_minimal()
    get_user = next(t for t in tools if t["name"] == "getUser")
    include = next(
        p for p in get_user["_route_metadata"]["parameters"] if p["name"] == "include"
    )
    # ``explode: true`` matters for query serialization (?include=a&include=b).
    assert include["explode"] is True
    assert include["schema"]["type"] == "array"


def test_forge_input_schema_does_not_leak_internal_keys() -> None:
    tools = _forge_minimal()
    for t in tools:
        # Only these keys are part of the MCP tool wire shape; ``_route_metadata``
        # is internal and stripped on the way to ``tools/list``.
        assert set(t["inputSchema"]).issubset({"type", "properties", "required"})
        for prop in t["inputSchema"]["properties"].values():
            # Param descriptions used to be polluted with ``[query] ...``; verify
            # location prefixes are gone.
            desc = prop.get("description", "")
            assert "[query]" not in desc
            assert "[path]" not in desc
            assert "[header]" not in desc


def test_forge_petstore_does_not_crash() -> None:
    """Smoke test against the real-world petstore spec.

    Petstore exercises ``$ref`` chains, security, multiple content types,
    path-item params, and form-encoded bodies. We don't pin a golden here --
    just that the forge produces a non-empty list of well-formed tools.
    """

    spec = yaml.safe_load((FIXTURES / "petstore.yaml").read_text())
    tools = ForgeEngine.forge_tools(spec)
    assert len(tools) >= 15
    for t in tools:
        assert t["name"]
        assert isinstance(t["_route_metadata"], dict)
        assert t["_route_metadata"]["method"] in {
            "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"
        }
        assert t["_route_metadata"]["path_template"].startswith("/")
