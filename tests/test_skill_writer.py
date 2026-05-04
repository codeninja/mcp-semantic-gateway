"""Tests for the Phase J skill writer."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcp_semantic_gateway.ingestion.skill_writer import (
    SkillPackage,
    SkillReference,
    write_diagnostic,
    write_package,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_package(
    *,
    skill_id: str = "github-issue-triage",
    server_id: str = "github-api",
    source_hash: str = "sha256:ab12cd34ef56789012345678abcdef0123456789abcdef0123456789abcdef01",
    cluster_id: str = "cluster-github-api-3",
    cluster_hash: str = "sha256:cluster-hash-aaa",
    tool_dependencies: list[str] | None = None,
    references: list[SkillReference] | None = None,
    description: str = (
        "Search, label, comment, and close GitHub issues based on triage rules.\n"
        "Combines repo issue search with bulk label and comment operations."
    ),
    body_markdown: str = (
        "## When to use this skill\n\n"
        "Use when triaging GitHub issues across one or more repositories.\n\n"
        "## Procedure\n\n"
        "1. Find candidate issues with `github_search_issues`.\n"
        "2. Apply labels with `github_add_labels`.\n"
    ),
    generated_by: str = "claude-sonnet-4-6@v1",
    generated_at: datetime | None = None,
) -> SkillPackage:
    return SkillPackage(
        skill_id=skill_id,
        name=skill_id,
        description=description,
        body_markdown=body_markdown,
        tool_dependencies=tool_dependencies
        if tool_dependencies is not None
        else [
            "github_search_issues",
            "github_add_labels",
            "github_create_comment",
            "github_close_issue",
        ],
        references=references or [],
        server_id=server_id,
        source_hash=source_hash,
        cluster_id=cluster_id,
        cluster_hash=cluster_hash,
        use_case_ids=["uc-github-api-issues-0-1", "uc-github-api-issues-2-0"],
        generated_by=generated_by,
        generated_at=generated_at
        or datetime(2026, 5, 4, 10, 24, 11, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Layout & frontmatter
# ---------------------------------------------------------------------------


def test_layout_matches_design(tmp_path: Path):
    pkg = _make_package()
    v1_dir = write_package(pkg, project_root=tmp_path)

    expected_short = "ab12cd34ef56"  # first 12 chars of source_hash sans 'sha256:'
    expected_v1 = (
        tmp_path
        / ".mcp_semantic_gateway"
        / "skills"
        / "github-api"
        / expected_short
        / "github-issue-triage"
        / "v1"
    )
    assert v1_dir == expected_v1
    assert (expected_v1 / "SKILL.md").is_file()
    assert (expected_v1 / ".meta.json").is_file()


def test_frontmatter_shape(tmp_path: Path):
    pkg = _make_package()
    v1_dir = write_package(pkg, project_root=tmp_path)
    text = (v1_dir / "SKILL.md").read_text(encoding="utf-8")

    assert text.startswith("---\n")
    # Frontmatter must close with a `---` line followed by a blank line, then body.
    parts = text.split("\n---\n", 1)
    assert len(parts) == 2, "frontmatter not delimited by --- line"
    after_frontmatter = parts[1]
    # Body follows after a blank line then content.
    assert after_frontmatter.startswith("\n"), "missing blank line between frontmatter and body"
    # Single trailing newline.
    assert text.endswith("\n")
    assert not text.endswith("\n\n\n")


def test_allowed_tools_omitted_when_empty(tmp_path: Path):
    pkg = _make_package(tool_dependencies=[])
    v1_dir = write_package(pkg, project_root=tmp_path)
    text = (v1_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "allowed-tools" not in text


def test_allowed_tools_preserves_order(tmp_path: Path):
    pkg = _make_package(tool_dependencies=["zeta_tool", "alpha_tool", "mu_tool"])
    v1_dir = write_package(pkg, project_root=tmp_path)
    text = (v1_dir / "SKILL.md").read_text(encoding="utf-8")
    z = text.find("zeta_tool")
    a = text.find("alpha_tool")
    m = text.find("mu_tool")
    assert 0 < z < a < m


def test_multiline_description_indented(tmp_path: Path):
    pkg = _make_package(description="Line one.\nLine two with detail.")
    v1_dir = write_package(pkg, project_root=tmp_path)
    text = (v1_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "description: |\n  Line one.\n  Line two with detail." in text


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def test_references_written(tmp_path: Path):
    refs = [SkillReference(filename="api.md", content="# API reference\nDetails go here.")]
    pkg = _make_package(references=refs)
    v1_dir = write_package(pkg, project_root=tmp_path)
    ref_path = v1_dir / "references" / "api.md"
    assert ref_path.is_file()
    assert ref_path.read_text(encoding="utf-8") == "# API reference\nDetails go here."


def test_references_dir_absent_when_no_references(tmp_path: Path):
    pkg = _make_package()
    v1_dir = write_package(pkg, project_root=tmp_path)
    assert not (v1_dir / "references").exists()


def test_rewrite_prunes_stale_references(tmp_path: Path):
    """Idempotent rewrite must keep references/ in sync with the package's
    declared content. A reference removed from a later package version is
    deleted from disk; a removed-entirely set deletes the directory."""

    refs_v1 = [
        SkillReference(filename="api.md", content="api content text body padding"),
        SkillReference(filename="examples.md", content="example content text body padding"),
    ]
    pkg = _make_package(references=refs_v1)
    v1_dir = write_package(pkg, project_root=tmp_path)
    refs_dir = v1_dir / "references"
    assert (refs_dir / "api.md").is_file()
    assert (refs_dir / "examples.md").is_file()

    # Rewrite with one reference removed.
    refs_v2 = [SkillReference(filename="api.md", content="api content text body padding")]
    pkg_v2 = _make_package(references=refs_v2)
    write_package(pkg_v2, project_root=tmp_path)
    assert (refs_dir / "api.md").is_file()
    assert not (refs_dir / "examples.md").exists()

    # Rewrite with NO references — directory should be removed entirely.
    pkg_v3 = _make_package()
    write_package(pkg_v3, project_root=tmp_path)
    assert not refs_dir.exists()


# ---------------------------------------------------------------------------
# .meta.json
# ---------------------------------------------------------------------------


def test_meta_json_round_trip(tmp_path: Path):
    pkg = _make_package()
    v1_dir = write_package(
        pkg,
        project_root=tmp_path,
        validation_passes=["spec-conformance", "tool-grounding", "length-bounds"],
    )
    meta = json.loads((v1_dir / ".meta.json").read_text(encoding="utf-8"))

    assert meta["schema_version"] == 1
    assert meta["skill_id"] == pkg.skill_id
    assert meta["skill_version"] == 1
    assert meta["status"] == "published"
    assert meta["tool_dependencies"] == pkg.tool_dependencies
    assert meta["source"]["server_id"] == pkg.server_id
    assert meta["source"]["source_hash"] == pkg.source_hash
    assert meta["source"]["source_hash_short"] == "ab12cd34ef56"
    assert meta["cluster"]["cluster_id"] == pkg.cluster_id
    assert meta["cluster"]["cluster_hash"] == pkg.cluster_hash
    assert meta["cluster"]["use_case_ids"] == pkg.use_case_ids
    assert meta["generation"]["model"] == "claude-sonnet-4-6"
    assert meta["generation"]["prompt_version"] == "v1"
    assert meta["generation"]["validation_passes"] == [
        "spec-conformance",
        "tool-grounding",
        "length-bounds",
    ]


def test_validation_passes_propagated(tmp_path: Path):
    pkg = _make_package()
    v1_dir = write_package(pkg, project_root=tmp_path, validation_passes=["only-one"])
    meta = json.loads((v1_dir / ".meta.json").read_text(encoding="utf-8"))
    assert meta["generation"]["validation_passes"] == ["only-one"]


def test_validation_passes_default_empty(tmp_path: Path):
    pkg = _make_package()
    v1_dir = write_package(pkg, project_root=tmp_path)
    meta = json.loads((v1_dir / ".meta.json").read_text(encoding="utf-8"))
    assert meta["generation"]["validation_passes"] == []


def test_generated_by_split_no_at(tmp_path: Path):
    pkg = _make_package(generated_by="just-a-model-name")
    v1_dir = write_package(pkg, project_root=tmp_path)
    meta = json.loads((v1_dir / ".meta.json").read_text(encoding="utf-8"))
    assert meta["generation"]["model"] == "just-a-model-name"
    assert meta["generation"]["prompt_version"] == "v1"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_overwrite_byte_stable(tmp_path: Path):
    pkg = _make_package()
    v1_dir = write_package(pkg, project_root=tmp_path)
    skill_md_first = (v1_dir / "SKILL.md").read_bytes()
    meta_first = (v1_dir / ".meta.json").read_bytes()

    v1_dir2 = write_package(pkg, project_root=tmp_path)
    assert v1_dir2 == v1_dir
    assert (v1_dir / "SKILL.md").read_bytes() == skill_md_first
    assert (v1_dir / ".meta.json").read_bytes() == meta_first


# ---------------------------------------------------------------------------
# Collision suffixing
# ---------------------------------------------------------------------------


def test_collision_suffixing(tmp_path: Path):
    pkg_a = _make_package(skill_id="foo", cluster_hash="sha256:cluster-A")
    pkg_b = _make_package(
        skill_id="foo",
        cluster_id="cluster-github-api-9",
        cluster_hash="sha256:cluster-B",
    )

    v1_a = write_package(pkg_a, project_root=tmp_path)
    v1_b = write_package(pkg_b, project_root=tmp_path)

    assert v1_a.parent.name == "foo"
    assert v1_b.parent.name == "foo-2"

    # Original is untouched.
    meta_a = json.loads((v1_a / ".meta.json").read_text(encoding="utf-8"))
    assert meta_a["cluster"]["cluster_hash"] == "sha256:cluster-A"
    meta_b = json.loads((v1_b / ".meta.json").read_text(encoding="utf-8"))
    assert meta_b["cluster"]["cluster_hash"] == "sha256:cluster-B"


def test_collision_third_skill_gets_suffix_3(tmp_path: Path):
    pkgs = [
        _make_package(skill_id="foo", cluster_hash=f"sha256:cluster-{i}")
        for i in range(3)
    ]
    dirs = [write_package(p, project_root=tmp_path) for p in pkgs]
    assert [d.parent.name for d in dirs] == ["foo", "foo-2", "foo-3"]


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_no_tmp_files_remain(tmp_path: Path):
    pkg = _make_package(
        references=[SkillReference(filename="api.md", content="ref content")]
    )
    write_package(pkg, project_root=tmp_path)
    leftovers = list(tmp_path.rglob("*.tmp"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# Diagnostic writer
# ---------------------------------------------------------------------------


def test_write_diagnostic(tmp_path: Path):
    pkg = _make_package(skill_id="bad-skill")
    reasons = [
        {"pass": "tool-grounding", "tool": "nonexistent_tool", "where": "body"},
        {"pass": "length-bounds", "field": "description", "len": 12},
    ]
    diag_path = write_diagnostic(pkg, project_root=tmp_path, reasons=reasons)

    expected = (
        tmp_path
        / ".mcp_semantic_gateway"
        / "skills"
        / pkg.server_id
        / "ab12cd34ef56"
        / "bad-skill.diagnostic.json"
    )
    assert diag_path == expected
    assert diag_path.is_file()
    payload = json.loads(diag_path.read_text(encoding="utf-8"))
    assert payload["status"] == "rejected"
    assert payload["skill_id"] == "bad-skill"
    assert payload["reasons"] == reasons
    # Diagnostic landing should not interfere with a future successful skill.
    assert not (diag_path.parent / "bad-skill").exists()


def test_diagnostic_atomic(tmp_path: Path):
    pkg = _make_package(skill_id="bad-skill")
    write_diagnostic(pkg, project_root=tmp_path, reasons=[{"pass": "x"}])
    assert list(tmp_path.rglob("*.tmp")) == []
