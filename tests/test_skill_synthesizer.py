"""Phase H — Skill synthesis tests (stub LLM)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Allow importing the StubLLM helper that lives at tests/_stub_llm.py.
sys.path.insert(0, str(Path(__file__).parent))
from _stub_llm import StubLLM  # noqa: E402

from mcp_semantic_gateway.ingestion.observability import EventEmitter, SynthesisRun
from mcp_semantic_gateway.ingestion.skill_clusterer import UseCaseCluster
from mcp_semantic_gateway.ingestion.skill_synthesizer import (
    SkillSynthesizer,
    SkillSynthesizerConfig,
    slugify_skill_id,
)
from mcp_semantic_gateway.ingestion.use_case_miner import UseCaseRecord
from mcp_semantic_gateway.llm.base import LLMResponse, UsageStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(uc_id: str, *, server="srv", source="srchash", tools=("toolA",)) -> UseCaseRecord:
    return UseCaseRecord(
        id=uc_id,
        server_id=server,
        source_hash=source,
        chunk_id="chunk-srv-grp-0",
        description=(
            "Demo description for the synthesizer tests, long enough to satisfy "
            "the validator's lower bound."
        ),
        linked_tool_names=list(tools),
        prerequisites=[],
        confidence=0.9,
        generated_by="stub@v1",
        generated_at=datetime.now(timezone.utc),
        use_case_hash="uchash-" + uc_id,
    )


def _make_cluster(cluster_id, source, *, members):
    return UseCaseCluster(
        cluster_id=cluster_id,
        server_id="srv",
        source_hash=source,
        use_case_ids=[m.id for m in members],
        tool_name_union=sorted({t for m in members for t in m.linked_tool_names}),
        centroid_description=members[0].description,
        cluster_hash=f"clusterhash-{cluster_id}",
    )


def _make_response(*, name="github-issue-triage", deps=("toolA",), body_extra="") -> LLMResponse:
    body = (
        "## When to use this skill\n\n"
        "Use this skill to do the demo task in tests.\n\n"
        "## Procedure\n\n"
        f"1. Call `{deps[0]}` to start the workflow.\n"
        + body_extra
        + "\n\nThis is enough body content to satisfy minimum length bounds.\n"
        + "Add more text here to ensure the body length passes validation cleanly.\n"
        * 3
    )
    args = {
        "name": name,
        "description": (
            "Walk through a demo workflow that exercises the synthesizer end to "
            "end so tests can assert behaviour."
        ),
        "body_markdown": body,
        "tool_dependencies": list(deps),
    }
    return LLMResponse(
        tool_name="emit_skill_package",
        tool_arguments=args,
        text=None,
        stop_reason="tool_use",
        usage=UsageStats(input_tokens=100, output_tokens=50),
    )


@pytest.fixture
def emitter(tmp_path):
    run = SynthesisRun.new(server_ids=["srv"])
    log_path = tmp_path / "synth.jsonl"
    diag_dir = tmp_path / "diagnostics"
    em = EventEmitter(run=run, log_path=log_path, diagnostics_dir=diag_dir, progress=False)
    em.__enter__()
    yield em
    em.__exit__(None, None, None)


@pytest.fixture
def harvested_tools():
    return [
        {"name": "toolA", "description": "First tool", "inputSchema": {"required": []}},
        {"name": "toolB", "description": "Second tool", "inputSchema": {"required": ["x"]}},
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_writes_skill_md_and_meta(tmp_path, emitter, harvested_tools):
    rec = _make_record("uc-1")
    cluster = _make_cluster("cluster-srv-0", rec.source_hash, members=[rec])
    llm = StubLLM([_make_response(name="demo-skill", deps=("toolA",))])

    cfg = SkillSynthesizerConfig(model_id="stub-model-v1", project_root=tmp_path)
    syn = SkillSynthesizer(llm=llm, emitter=emitter, config=cfg)

    result = await syn.synthesize_for_source(
        server_id="srv",
        source_hash=rec.source_hash,
        clusters=[cluster],
        records_by_id={rec.id: rec},
        harvested_tools=harvested_tools,
    )

    assert len(result.written) == 1
    assert not result.rejected
    skill_md = Path(result.written[0])
    assert skill_md.exists()
    content = skill_md.read_text()
    assert content.startswith("---\n")
    assert "name: demo-skill" in content
    meta_path = skill_md.parent / ".meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta["cluster"]["cluster_hash"] == cluster.cluster_hash
    assert meta["generation"]["model"] == "stub-model-v1"
    assert "spec-conformance" in meta["generation"]["validation_passes"]


@pytest.mark.asyncio
async def test_hallucinated_tool_name_rejected(tmp_path, emitter, harvested_tools):
    rec = _make_record("uc-1")
    cluster = _make_cluster("cluster-srv-0", rec.source_hash, members=[rec])
    # The LLM makes up a tool name that isn't in the harvested set.
    llm = StubLLM([_make_response(name="bad-skill", deps=("doesNotExist",))])

    cfg = SkillSynthesizerConfig(model_id="stub-model-v1", project_root=tmp_path)
    syn = SkillSynthesizer(llm=llm, emitter=emitter, config=cfg)

    result = await syn.synthesize_for_source(
        server_id="srv",
        source_hash=rec.source_hash,
        clusters=[cluster],
        records_by_id={rec.id: rec},
        harvested_tools=harvested_tools,
    )

    assert not result.written
    assert len(result.rejected) == 1
    reasons = [r["reason"] for r in result.rejected[0]["reasons"]]
    assert "tool_name_unresolved" in reasons


@pytest.mark.asyncio
async def test_cache_hit_skips_llm_call(tmp_path, emitter, harvested_tools):
    rec = _make_record("uc-1")
    cluster = _make_cluster("cluster-srv-0", rec.source_hash, members=[rec])

    # First run writes the skill.
    llm = StubLLM([_make_response(name="cached-skill", deps=("toolA",))])
    cfg = SkillSynthesizerConfig(model_id="stub-model-v1", project_root=tmp_path)
    syn = SkillSynthesizer(llm=llm, emitter=emitter, config=cfg)
    first = await syn.synthesize_for_source(
        server_id="srv",
        source_hash=rec.source_hash,
        clusters=[cluster],
        records_by_id={rec.id: rec},
        harvested_tools=harvested_tools,
    )
    assert len(first.written) == 1

    # Second run with an EMPTY stub: any LLM call would raise.
    llm2 = StubLLM([])
    syn2 = SkillSynthesizer(llm=llm2, emitter=emitter, config=cfg)
    second = await syn2.synthesize_for_source(
        server_id="srv",
        source_hash=rec.source_hash,
        clusters=[cluster],
        records_by_id={rec.id: rec},
        harvested_tools=harvested_tools,
    )
    assert not second.written
    assert len(second.cached) == 1
    assert llm2.calls == []  # confirm zero LLM calls


@pytest.mark.asyncio
async def test_multiple_clusters_synthesize_in_parallel(tmp_path, emitter, harvested_tools):
    r1 = _make_record("uc-a", tools=("toolA",))
    r2 = _make_record("uc-b", tools=("toolB",))
    c1 = _make_cluster("cluster-srv-0", r1.source_hash, members=[r1])
    c2 = _make_cluster("cluster-srv-1", r1.source_hash, members=[r2])

    llm = StubLLM(
        [
            _make_response(name="skill-a", deps=("toolA",)),
            _make_response(name="skill-b", deps=("toolB",)),
        ]
    )
    cfg = SkillSynthesizerConfig(model_id="stub-model-v1", project_root=tmp_path, max_concurrency=2)
    syn = SkillSynthesizer(llm=llm, emitter=emitter, config=cfg)
    result = await syn.synthesize_for_source(
        server_id="srv",
        source_hash=r1.source_hash,
        clusters=[c1, c2],
        records_by_id={r1.id: r1, r2.id: r2},
        harvested_tools=harvested_tools,
    )
    assert len(result.written) == 2


def test_slugify_skill_id_basics():
    assert slugify_skill_id("GitHub Issue Triage") == "github-issue-triage"
    assert slugify_skill_id("--bad--name--") == "bad-name"
    # leading non-alpha gets prefixed
    assert slugify_skill_id("3-things") == "s-3-things"
