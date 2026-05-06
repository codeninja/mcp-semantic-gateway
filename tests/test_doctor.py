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


def test_disabled_openapi_server_skips_auth_check(monkeypatch, tmp_path):
    """A server marked `enabled = false` is ignored by the collector and
    router; doctor should not flag missing env vars for it."""

    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    monkeypatch.delenv("PETSTORE_API_KEY", raising=False)
    _write_config(
        tmp_path,
        """
[servers.petstore]
type = "openapi"
url = "https://example.invalid/openapi.json"
enabled = false

[servers.petstore.auth]
type = "api_key"
header_name = "X-API-Key"
api_key_env = "PETSTORE_API_KEY"
""",
    )
    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_auth_env_vars(report, cfg)
    # No check produced for the disabled server.
    assert _result_for(report, "auth-env:petstore") is None


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


def test_disabled_skill_server_is_skipped(monkeypatch, tmp_path):
    """Disabled skill sources must not surface failures from doctor."""

    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        """
[servers.local-skills]
type = "skill"
path = "/nonexistent/skills/dir"
enabled = false
""",
    )
    cfg = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_skill_paths(report, cfg)
    assert _result_for(report, "skill-path:local-skills") is None


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
async def test_disabled_openapi_server_skips_route_metadata_check(
    monkeypatch, tmp_path
):
    """Stale rows belonging to disabled OpenAPI servers should not cause
    route-metadata failures — the operator already turned the server off."""

    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        """
[servers.petstore]
type = "openapi"
url = "https://example.invalid/openapi.json"
enabled = false
""",
    )
    from mcp_semantic_gateway.storage.metadata_db import MetadataDB, ToolRecord

    metadata_path = tmp_path / "index" / "metadata.db"
    metadata_path.parent.mkdir(parents=True)
    db = MetadataDB(metadata_path)
    await db.initialize()
    # Stale row from a previous run where the server was enabled.
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
    # No openapi-server check was produced because petstore is disabled.
    assert _result_for(report, "route-metadata") is None


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
    # Single failure (config-file) → exit code 1. Documented contract is
    # "exit code = number of failures".
    assert result.exit_code == 1
    assert "init" in result.output


def test_doctor_cli_exit_code_equals_failure_count(monkeypatch, tmp_path):
    """A config that exists but lacks an index produces 2 failures
    (index-metadata-db + index-vector-db). Exit code must match."""

    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(tmp_path, "[servers]\n")
    runner = CliRunner()
    result = runner.invoke(cli_app, ["doctor", "--no-network"])
    assert result.exit_code == 2, result.output


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


# ---------------------------------------------------------------------------
# orphan-diagnostics check
# ---------------------------------------------------------------------------


def _seed_orphan_diagnostic(skills_root: Path, server: str, hash_short: str, skill_id: str) -> Path:
    target_dir = skills_root / server / hash_short
    target_dir.mkdir(parents=True, exist_ok=True)
    f = target_dir / f"{skill_id}.diagnostic.json"
    f.write_text('{"status":"rejected"}', encoding="utf-8")
    return f


def test_orphan_diagnostics_warns_when_present_and_no_fix(monkeypatch, tmp_path):
    """No --fix: warn with remediation hint, file untouched."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(tmp_path, "[servers]\n")

    skills_root = tmp_path / ".mcp_semantic_gateway" / "skills"
    orphan = _seed_orphan_diagnostic(skills_root, "github", "205eec12a36e", "bad-skill")

    config = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_orphan_diagnostics(report, config, fix=False)

    res = _result_for(report, "orphan-diagnostics")
    assert res is not None
    assert res.status == "warn"
    assert "1 legacy" in res.detail
    assert res.remediation and "--fix" in res.remediation
    assert orphan.is_file(), "warn-only must not delete files"


def test_orphan_diagnostics_removes_files_with_fix(monkeypatch, tmp_path):
    """--fix: delete every legacy-location *.diagnostic.json under the
    synth output and any type=skill source path. New-layout diagnostics
    living inside <skill-id>/diagnostic.json must NOT be matched."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))

    extra_skill_root = tmp_path / "external-skills"
    _write_config(
        tmp_path,
        f'[servers.lib]\ntype = "skill"\npath = "{extra_skill_root}"\n',
    )

    synth_root = tmp_path / ".mcp_semantic_gateway" / "skills"
    o1 = _seed_orphan_diagnostic(synth_root, "github", "205eec12a36e", "skill-a")
    o2 = _seed_orphan_diagnostic(synth_root, "stitch", "abc123def456", "skill-b")
    o3 = _seed_orphan_diagnostic(extra_skill_root, "lib", "111111111111", "skill-c")

    new_loc = synth_root / "github" / "205eec12a36e" / "skill-d" / "diagnostic.json"
    new_loc.parent.mkdir(parents=True, exist_ok=True)
    new_loc.write_text('{"status":"rejected"}', encoding="utf-8")

    config = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_orphan_diagnostics(report, config, fix=True)

    res = _result_for(report, "orphan-diagnostics")
    assert res is not None
    assert res.status == "pass", res.detail
    assert "Removed 3" in res.detail
    assert not o1.exists()
    assert not o2.exists()
    assert not o3.exists()
    assert new_loc.is_file(), "new-layout diagnostic must not be removed"


def test_orphan_diagnostics_pass_when_clean(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_SEMANTIC_GATEWAY_HOME", str(tmp_path))
    _write_config(tmp_path, "[servers]\n")
    config = load_config(tmp_path / "config.toml")
    report = doctor_module.DoctorReport()
    doctor_module._check_orphan_diagnostics(report, config, fix=False)
    res = _result_for(report, "orphan-diagnostics")
    assert res is not None and res.status == "pass"

