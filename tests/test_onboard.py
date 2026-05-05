"""Tests for the ``mcp-semantic-gateway onboard`` CLI command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcp_semantic_gateway.cli import onboard as onboard_mod
from mcp_semantic_gateway.cli.main import app


runner = CliRunner()


def test_iter_bundled_skills_yields_all_collections():
    skills = list(onboard_mod.iter_bundled_skills("all"))
    names = {(s.collection, s.name) for s in skills}

    # Consumer collection
    assert ("consumer", "mcp-gateway-getting-started") in names
    assert ("consumer", "mcp-gateway-configure-sources") in names
    assert ("consumer", "mcp-gateway-discover-by-intent") in names
    assert ("consumer", "mcp-gateway-synthesize-skills") in names

    # Development collection
    assert ("development", "mcp-gateway-dev-setup") in names
    assert ("development", "mcp-gateway-run-tests") in names
    assert ("development", "mcp-gateway-release-publish") in names
    assert ("development", "mcp-gateway-add-source-type") in names


def test_iter_bundled_skills_consumer_filter():
    consumer_only = list(onboard_mod.iter_bundled_skills("consumer"))
    assert all(s.collection == "consumer" for s in consumer_only)
    assert len(consumer_only) >= 4


def test_iter_bundled_skills_development_filter():
    dev_only = list(onboard_mod.iter_bundled_skills("development"))
    assert all(s.collection == "development" for s in dev_only)
    assert len(dev_only) >= 4


def test_iter_bundled_skills_rejects_unknown_collection():
    with pytest.raises(ValueError):
        list(onboard_mod.iter_bundled_skills("bogus"))


def test_provider_table_has_all_four_providers():
    assert set(onboard_mod.PROVIDERS) == {"claude", "codex", "opencode", "pi"}


def test_provider_default_paths_match_documented_locations():
    home = Path.home()
    p = onboard_mod.PROVIDERS
    assert p["claude"].user_dir == home / ".claude" / "skills"
    assert p["claude"].project_dir == Path(".claude") / "skills"
    assert p["codex"].user_dir == home / ".agents" / "skills"
    assert p["codex"].project_dir == Path(".agents") / "skills"
    assert p["opencode"].user_dir == home / ".config" / "opencode" / "skills"
    assert p["opencode"].project_dir == Path(".opencode") / "skills"
    assert p["pi"].user_dir == home / ".pi" / "agent" / "skills"
    assert p["pi"].project_dir == Path(".pi") / "skills"


def test_onboard_with_target_installs_all_skills(tmp_path: Path):
    result = runner.invoke(
        app, ["onboard", "claude", "--target", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    skill_files = list(tmp_path.rglob("SKILL.md"))
    assert len(skill_files) == 8
    # Sample one to confirm content was actually written
    first = skill_files[0].read_text(encoding="utf-8")
    assert first.startswith("---\n")
    assert "name:" in first
    assert "description:" in first


def test_onboard_consumer_only_filters_collection(tmp_path: Path):
    result = runner.invoke(
        app,
        ["onboard", "claude", "--target", str(tmp_path), "--include", "consumer"],
    )
    assert result.exit_code == 0, result.output
    skill_dirs = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert all(name.startswith("mcp-gateway-") for name in skill_dirs)
    # No development-only skill should land in this tree
    assert "mcp-gateway-release-publish" not in skill_dirs
    assert "mcp-gateway-getting-started" in skill_dirs


def test_onboard_dry_run_writes_nothing(tmp_path: Path):
    result = runner.invoke(
        app,
        ["onboard", "claude", "--target", str(tmp_path), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert list(tmp_path.iterdir()) == []
    assert "would-install" in result.output


def test_onboard_skips_existing_without_force(tmp_path: Path):
    # First install
    runner.invoke(app, ["onboard", "claude", "--target", str(tmp_path)])

    # Modify one skill so we can detect overwrite
    sentinel = tmp_path / "mcp-gateway-getting-started" / "SKILL.md"
    sentinel.write_text("USER-EDITED", encoding="utf-8")

    # Re-run without --force — should skip and preserve our edit
    result = runner.invoke(app, ["onboard", "claude", "--target", str(tmp_path)])
    assert result.exit_code == 0
    assert "skipped" in result.output
    assert sentinel.read_text(encoding="utf-8") == "USER-EDITED"


def test_onboard_force_overwrites(tmp_path: Path):
    runner.invoke(app, ["onboard", "claude", "--target", str(tmp_path)])
    sentinel = tmp_path / "mcp-gateway-getting-started" / "SKILL.md"
    sentinel.write_text("USER-EDITED", encoding="utf-8")

    result = runner.invoke(
        app, ["onboard", "claude", "--target", str(tmp_path), "--force"]
    )
    assert result.exit_code == 0
    assert "overwritten" in result.output
    body = sentinel.read_text(encoding="utf-8")
    assert body != "USER-EDITED"
    assert body.startswith("---\n")


def test_onboard_unknown_provider_errors(tmp_path: Path):
    result = runner.invoke(
        app, ["onboard", "ghost-agent", "--target", str(tmp_path)]
    )
    assert result.exit_code != 0


def test_onboard_no_provider_prints_provider_table():
    result = runner.invoke(app, ["onboard"])
    assert result.exit_code != 0
    out = result.output
    assert "claude" in out
    assert "codex" in out
    assert "opencode" in out
    assert "pi" in out


def test_onboard_list_providers_flag():
    result = runner.invoke(app, ["onboard", "--list-providers"])
    assert result.exit_code == 0
    assert "claude" in result.output
    assert "codex" in result.output


def test_onboard_list_skills_flag():
    result = runner.invoke(app, ["onboard", "--list-skills"])
    assert result.exit_code == 0
    assert "mcp-gateway-getting-started" in result.output
    assert "mcp-gateway-dev-setup" in result.output


def test_onboard_project_scope_writes_to_cwd_relative_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`--project` must resolve targets against cwd, not $HOME."""

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["onboard", "claude", "--project"])
    assert result.exit_code == 0, result.output
    landed = (
        tmp_path
        / ".claude"
        / "skills"
        / "mcp-gateway-getting-started"
        / "SKILL.md"
    )
    assert landed.is_file()
    # codex --project should write to .agents/skills/ instead
    result = runner.invoke(app, ["onboard", "codex", "--project"])
    assert result.exit_code == 0
    assert (tmp_path / ".agents" / "skills").is_dir()


def test_every_bundled_skill_has_valid_frontmatter():
    """Every bundled SKILL.md must start with YAML frontmatter that names + describes."""

    for skill in onboard_mod.iter_bundled_skills("all"):
        body = (skill.source / "SKILL.md").read_text(encoding="utf-8")
        assert body.startswith("---\n"), f"{skill.name} missing frontmatter"
        # Find end of frontmatter
        rest = body[4:]
        end_idx = rest.find("\n---\n")
        assert end_idx != -1, f"{skill.name} frontmatter not closed"
        frontmatter = rest[:end_idx]
        assert f"name: {skill.name}" in frontmatter, (
            f"{skill.name} frontmatter name does not match dir"
        )
        assert "description:" in frontmatter, f"{skill.name} missing description"
