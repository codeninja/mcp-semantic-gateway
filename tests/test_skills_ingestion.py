import pytest
import os
from pathlib import Path
from mcp_semantic_gateway.ingestion.collector import Collector
from mcp_semantic_gateway.config.models import MCP Semantic GatewayConfig, ServerConfig, SourceType

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
    config = MCP Semantic GatewayConfig(
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
