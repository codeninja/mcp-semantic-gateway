"""Tests for the ``mcp-semantic-gateway doctor`` diagnostic command.

Each test exercises one check in :mod:`mcp_semantic_gateway.cli.doctor`
against a temp gateway home, asserts the expected status and that a
remediation hint is present when the check fails.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcp_semantic_gateway.cli import doctor as doctor_module
from mcp_semantic_gateway.cli.main import app as cli_app
from mcp_semantic_gateway.config.loader import load_config


def _write_config(home: Path, body: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _result_for(report: doctor_module.DoctorReport, name: str):
    matches = [r for r in report.results if r.name == name]
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# config-file check
# ---------------------------------------------------------------------------


def test_missing_config_fails_with_remediation(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    report = doctor_module.DoctorReport()
    cfg = doctor_module._check_config_loadable(report)
    assert cfg is None
    res = _result_for(report, "config-file")
    assert res.status == "fail"
    assert res.remediation is not None
    assert "init" in res.remediation


def test_invalid_toml_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(tmp_path, "this is = not = valid = toml = {")
    report = doctor_module.DoctorReport()
    cfg = doctor_module._check_config_loadable(report)
    assert cfg is None
    assert _result_for(report, "config-file").status == "fail"


def test_valid_empty_config_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(tmp_path, "[servers]\n")
    report = doctor_module.DoctorReport()
    cfg = doctor_module._check_config_loadable(report)
    assert cfg is not None
    assert _result_for(report, "config-file").status == "pass"


# ---------------------------------------------------------------------------
# index check
# ---------------------------------------------------------------------------


def test_missing_index_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(tmp_path, "[servers]\n")
    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_index_present(report, cfg)
    assert _result_for(report, "index-metadata-db").status == "fail"
    assert _result_for(report, "index-vector-db").status == "fail"


def test_index_present_with_zero_rows_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(tmp_path, "[servers]\n")

    # Create the metadata DB with the expected schema but no rows.
    from mcp_semantic_gateway.storage.metadata_db import MetadataDB

    metadata_path = tmp_path / "index" / "metadata.db"
    metadata_path.parent.mkdir(parents=True)
    asyncio.run(MetadataDB(metadata_path).initialize())
    (tmp_path / "index" / "vectors.db").write_bytes(b"")  # placeholder

    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_index_present(report, cfg)
    assert _result_for(report, "index-metadata-db").status == "warn"
    assert _result_for(report, "index-vector-db").status == "pass"


# ---------------------------------------------------------------------------
# auth env vars
# ---------------------------------------------------------------------------


def test_missing_auth_env_var_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    monkeypatch.delenv("PETSTORE_API_KEY", raising=False)
    _write_config(
        tmp_path,
        """
[servers.petstore]
type = "openapi"
url = "https://example.invalid/openapi.json"

[servers.petstore.auth]
type = "api_key"
header_name = "X-API-Key"
api_key_env = "PETSTORE_API_KEY"
""",
    )
    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_auth_env_vars(report, cfg)
    res = _result_for(report, "auth-env:petstore")
    assert res.status == "fail"
    assert "PETSTORE_API_KEY" in res.detail
    assert "PETSTORE_API_KEY" in (res.remediation or "")


def test_present_auth_env_var_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    monkeypatch.setenv("PETSTORE_API_KEY", "sk-test")
    _write_config(
        tmp_path,
        """
[servers.petstore]
type = "openapi"
url = "https://example.invalid/openapi.json"

[servers.petstore.auth]
type = "bearer"
bearer_token_env = "PETSTORE_API_KEY"
""",
    )
    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_auth_env_vars(report, cfg)
    assert _result_for(report, "auth-env:petstore").status == "pass"


# ---------------------------------------------------------------------------
# skill paths
# ---------------------------------------------------------------------------


def test_missing_skill_path_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        """
[servers.local-skills]
type = "skill"
path = "/nonexistent/skills/dir"
""",
    )
    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_skill_paths(report, cfg)
    res = _result_for(report, "skill-path:local-skills")
    assert res.status == "fail"
    assert res.remediation is not None


def test_skill_dir_with_skill_md_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    skills_dir = tmp_path / "skills" / "petstore"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("---\nname: x\n---\nbody", encoding="utf-8")

    _write_config(
        tmp_path,
        f"""
[servers.local-skills]
type = "skill"
path = "{skills_dir}"
""",
    )
    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_skill_paths(report, cfg)
    assert _result_for(report, "skill-path:local-skills").status == "pass"


def test_skill_dir_with_no_skill_md_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    skills_dir = tmp_path / "empty-skills"
    skills_dir.mkdir()

    _write_config(
        tmp_path,
        f"""
[servers.local-skills]
type = "skill"
path = "{skills_dir}"
""",
    )
    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_skill_paths(report, cfg)
    assert _result_for(report, "skill-path:local-skills").status == "warn"


# ---------------------------------------------------------------------------
# route metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_metadata_missing_for_openapi_tool_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        """
[servers.petstore]
type = "openapi"
url = "https://example.invalid/openapi.json"
""",
    )
    from mcp_semantic_gateway.storage.metadata_db import MetadataDB, ToolRecord

    metadata_path = tmp_path / "index" / "metadata.db"
    metadata_path.parent.mkdir(parents=True)
    db = MetadataDB(metadata_path)
    await db.initialize()
    # Save tool without route_metadata.
    await db.save_tool(
        ToolRecord(
            tool_id="petstore::tool::listPets",
            server_id="petstore",
            name="listPets",
            description="List pets",
            item_type="tool",
        )
    )

    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_route_metadata(report, cfg)
    res = _result_for(report, "route-metadata")
    assert res.status == "fail"
    assert "listPets" in res.detail


@pytest.mark.asyncio
async def test_route_metadata_present_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        """
[servers.petstore]
type = "openapi"
url = "https://example.invalid/openapi.json"
""",
    )
    from mcp_semantic_gateway.storage.metadata_db import MetadataDB, ToolRecord

    metadata_path = tmp_path / "index" / "metadata.db"
    metadata_path.parent.mkdir(parents=True)
    db = MetadataDB(metadata_path)
    await db.initialize()
    await db.save_tool(
        ToolRecord(
            tool_id="petstore::tool::listPets",
            server_id="petstore",
            name="listPets",
            description="List pets",
            item_type="tool",
            route_metadata={"method": "GET", "path": "/pets"},
        )
    )

    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_route_metadata(report, cfg)
    assert _result_for(report, "route-metadata").status == "pass"


# ---------------------------------------------------------------------------
# OpenAPI reachability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openapi_reachable_skipped_with_no_network_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        """
[servers.petstore]
type = "openapi"
url = "https://example.invalid/openapi.json"
""",
    )
    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    await doctor_module._check_openapi_reachable(report, cfg, skip_network=True)
    assert _result_for(report, "openapi-reachable:petstore").status == "skip"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_doctor_cli_exits_nonzero_on_failures(monkeypatch, tmp_path):
    """End-to-end: a brand-new home dir has no config and should produce a
    non-zero exit and a remediation pointing at `init`."""

    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli_app, ["doctor", "--no-network"])
    assert result.exit_code == 1
    assert "init" in result.output


def test_doctor_cli_json_output_carries_results(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(tmp_path, "[servers]\n")
    runner = CliRunner()
    result = runner.invoke(cli_app, ["doctor", "--no-network", "--json"])
    payload = json.loads(result.output)
    assert "results" in payload
    assert "failures" in payload
    names = [r["name"] for r in payload["results"]]
    assert "config-file" in names
