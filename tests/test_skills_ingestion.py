import pytest
from mcp_semantic_gateway.ingestion.collector import Collector
from mcp_semantic_gateway.config.models import MCPSemanticGatewayConfig, ServerConfig, SourceType

@pytest.fixture
def skills_dir(tmp_path):
    skill_a = tmp_path / "skill_a"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text("# Python Expert\nExpert in writing clean Python code.")
    
    skill_b = tmp_path / "skill_b"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text("# Rust Guru\nLow-level performance optimization specialist.")
    
    return tmp_path

@pytest.mark.asyncio
async def test_skills_ingestion(skills_dir):
    config = MCPSemanticGatewayConfig(
        servers={
            "my-skills": ServerConfig(
                type=SourceType.SKILL,
                path=str(skills_dir),
                enabled=True
            )
        }
    )
    
    collector = Collector(config)
    items = await collector.collect_all()
    
    # Assertions
    skills = [i for i in items if i["_item_type"] == "skill"]
    assert len(skills) == 2
    
    python_skill = next(s for s in skills if s["name"] == "Python Expert")
    assert "clean Python code" in python_skill["description"]
    assert "skill_a" in python_skill["annotations"]["path"]
    
    rust_skill = next(s for s in skills if s["name"] == "Rust Guru")
    assert "Low-level" in rust_skill["description"]

    print("\n[SUCCESS] Skills ingestion test passed.")


@pytest.mark.asyncio
async def test_skills_ingestion_parses_yaml_frontmatter(tmp_path):
    """Generated skills carry agent-skills-spec frontmatter; the collector
    must surface ``name``/``description`` from the YAML block, not from
    the legacy line-1 heuristic."""

    sd = tmp_path / "github-issue-triage" / "v1"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\n"
        "name: github-issue-triage\n"
        "description: |\n"
        "  Search, label, comment, and close GitHub issues based on triage rules.\n"
        "allowed-tools:\n"
        "  - github_search_issues\n"
        "  - github_add_labels\n"
        "---\n\n"
        "## When to use this skill\n\n"
        "When triaging incoming issues.\n"
    )

    config = MCPSemanticGatewayConfig(
        servers={
            "generated": ServerConfig(
                type=SourceType.SKILL,
                path=str(tmp_path),
                enabled=True,
            )
        }
    )
    items = await Collector(config).collect_all()
    skills = [i for i in items if i["_item_type"] == "skill"]
    assert len(skills) == 1
    s = skills[0]
    assert s["name"] == "github-issue-triage"
    assert "Search, label, comment" in s["description"]
    # Body content is included so the embedder gets full surface area.
    assert "When to use this skill" in s["description"]
    assert s["annotations"]["allowed_tools"] == [
        "github_search_issues",
        "github_add_labels",
    ]


@pytest.mark.asyncio
async def test_skills_ingestion_handles_non_string_yaml_values(tmp_path):
    """Regression: YAML can legally parse `name: 123` as int. The collector
    must coerce to str so the whole skill isn't dropped."""

    sd = tmp_path / "weird-skill" / "v1"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\n"
        "name: 123\n"
        "description: 456\n"
        "---\n\nbody text\n"
    )
    config = MCPSemanticGatewayConfig(
        servers={"weird": ServerConfig(type=SourceType.SKILL, path=str(tmp_path), enabled=True)}
    )
    items = await Collector(config).collect_all()
    skills = [i for i in items if i["_item_type"] == "skill"]
    assert len(skills) == 1
    assert skills[0]["name"] == "123"
    assert "456" in skills[0]["description"]


@pytest.mark.asyncio
async def test_skills_ingestion_legacy_fallback(tmp_path):
    """SKILL.md without frontmatter falls back to line-1-as-name."""

    (tmp_path / "skill_legacy").mkdir()
    (tmp_path / "skill_legacy" / "SKILL.md").write_text(
        "# Legacy Skill\nDescription on subsequent lines."
    )
    config = MCPSemanticGatewayConfig(
        servers={
            "legacy": ServerConfig(
                type=SourceType.SKILL,
                path=str(tmp_path),
                enabled=True,
            )
        }
    )
    items = await Collector(config).collect_all()
    skills = [i for i in items if i["_item_type"] == "skill"]
    assert len(skills) == 1
    assert skills[0]["name"] == "Legacy Skill"
    assert "Description on subsequent lines" in skills[0]["description"]
    # No allowed_tools surfaced for legacy.
    assert "allowed_tools" not in skills[0]["annotations"]
