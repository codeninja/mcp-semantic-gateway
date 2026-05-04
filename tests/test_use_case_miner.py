"""Tests for `ingestion/use_case_miner.py` — Phase C."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcp_semantic_gateway.ingestion.observability import (
    EventEmitter,
    Stage,
    SynthesisRun,
)
from mcp_semantic_gateway.ingestion.use_case_miner import (
    USE_CASE_EXTRACTION_TOOL,
    UseCaseMiner,
    UseCaseMinerConfig,
    UseCaseRecord,
)
from mcp_semantic_gateway.llm.base import LLMResponse, UsageStats
from mcp_semantic_gateway.storage.metadata_db import MetadataDB

from _stub_llm import StubLLM  # type: ignore[import-not-found]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_response(use_cases: list[dict]) -> LLMResponse:
    return LLMResponse(
        tool_name=USE_CASE_EXTRACTION_TOOL.name,
        tool_arguments={"use_cases": use_cases},
        text=None,
        stop_reason="tool_use",
        usage=UsageStats(input_tokens=100, output_tokens=50, cached_input_tokens=0),
    )


def _make_minimal_openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Sample API", "version": "1.0.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "summary": "List all users",
                    "tags": ["users"],
                }
            },
            "/users/{id}": {
                "get": {
                    "operationId": "getUser",
                    "summary": "Get user by id",
                    "tags": ["users"],
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                }
            },
        },
    }


def _harvest(spec: dict) -> list[dict]:
    from mcp_semantic_gateway.ingestion.forge import ForgeEngine

    return ForgeEngine.forge_tools(spec)


@pytest.fixture
def tmp_paths(tmp_path: Path):
    db_path = tmp_path / "metadata.db"
    log_path = tmp_path / "logs" / "synthesis.jsonl"
    diag_dir = tmp_path / "diagnostics"
    return db_path, log_path, diag_dir


def _read_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line]


async def _make_db(db_path: Path) -> MetadataDB:
    db = MetadataDB(db_path)
    await db.initialize()
    return db


def _emitter(run: SynthesisRun, log_path: Path, diag_dir: Path) -> EventEmitter:
    return EventEmitter(run, log_path, diag_dir, progress=False)


def _good_use_case(
    description: str = "Look up users by id and list all users in the system to support an agent's user-discovery flow.",
    linked: list[str] | None = None,
    prerequisites: list[str] | None = None,
    confidence: float = 0.92,
) -> dict:
    return {
        "description": description,
        "linked_tool_names": list(linked or ["listUsers", "getUser"]),
        "prerequisites": list(prerequisites or []),
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 1. End-to-end with stub LLM, OpenAPI spec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_openapi_persists_records(tmp_paths):
    db_path, log_path, diag_dir = tmp_paths
    db = await _make_db(db_path)
    spec = _make_minimal_openapi_spec()
    tools = _harvest(spec)

    uc1 = _good_use_case(
        description="List all users and then look up a single user by id when more detail is needed.",
        linked=["listUsers", "getUser"],
        confidence=0.95,
    )
    uc2 = _good_use_case(
        description="Discover users by enumerating the directory before drilling down to one record by its id.",
        linked=["listUsers"],
        confidence=0.8,
    )
    stub = StubLLM([_mk_response([uc1, uc2])])

    run = SynthesisRun.new(["sample-api"])
    with _emitter(run, log_path, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub.model_id),
        )
        accepted = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
        )

    assert len(accepted) == 2
    persisted = await db.list_use_cases_for_source(
        "sample-api", accepted[0].source_hash
    )
    assert len(persisted) == 2
    harvested_names = {t["name"] for t in tools}
    for rec in persisted:
        for n in rec.linked_tool_names:
            assert n in harvested_names
        assert isinstance(rec, UseCaseRecord)
        assert re.fullmatch(r"[0-9a-f]{64}", rec.use_case_hash), rec.use_case_hash
        assert rec.id.startswith(f"uc-sample-api-")
        assert rec.generated_by == f"{stub.model_id}@v1"

    events = _read_events(log_path)
    stages_seen = {e["stage"] for e in events}
    assert Stage.MINING_STARTED.value in stages_seen
    assert Stage.CHUNK_STARTED.value in stages_seen
    assert Stage.CHUNK_COMPLETED.value in stages_seen
    assert Stage.MINING_COMPLETED.value in stages_seen


# ---------------------------------------------------------------------------
# 2. Cache hit on re-run — no LLM call on the second pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_on_rerun(tmp_paths):
    db_path, log_path, diag_dir = tmp_paths
    db = await _make_db(db_path)
    spec = _make_minimal_openapi_spec()
    tools = _harvest(spec)

    # First run: stub returns one valid use case.
    stub_first = StubLLM([_mk_response([_good_use_case()])])
    run1 = SynthesisRun.new(["sample-api"])
    with _emitter(run1, log_path, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub_first,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub_first.model_id),
        )
        first = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
        )
    assert len(first) == 1
    assert len(stub_first.calls) == 1

    # Second run: stub will RAISE on any call (responses=[]). If the
    # source-level cache is honored we never call the stub, so no
    # exception is raised.
    stub_second = StubLLM([])
    log_path2 = log_path.parent / "synthesis2.jsonl"
    run2 = SynthesisRun.new(["sample-api"])
    with _emitter(run2, log_path2, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub_second,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub_second.model_id),
        )
        second = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
        )
    assert len(stub_second.calls) == 0
    assert len(second) == 1
    assert second[0].use_case_hash == first[0].use_case_hash

    events = _read_events(log_path2)
    assert any(e["stage"] == Stage.CACHE_HIT.value for e in events)


# ---------------------------------------------------------------------------
# 3. Hallucinated tool name -> rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hallucinated_tool_name_rejected(tmp_paths):
    db_path, log_path, diag_dir = tmp_paths
    db = await _make_db(db_path)
    spec = _make_minimal_openapi_spec()
    tools = _harvest(spec)

    bad = _good_use_case(linked=["listUsers", "deleteUserNuke"])
    stub = StubLLM([_mk_response([bad])])
    run = SynthesisRun.new(["sample-api"])
    with _emitter(run, log_path, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub.model_id),
        )
        accepted = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
        )
    assert accepted == []
    rows = await db.list_use_cases()
    assert rows == []
    events = _read_events(log_path)
    rejections = [
        e for e in events if e["stage"] == Stage.RECORD_REJECTED.value
    ]
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "tool_name_unresolved"


# ---------------------------------------------------------------------------
# 4. Short description -> rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_description_rejected(tmp_paths):
    db_path, log_path, diag_dir = tmp_paths
    db = await _make_db(db_path)
    spec = _make_minimal_openapi_spec()
    tools = _harvest(spec)

    bad = {
        "description": "tooshort01",
        "linked_tool_names": ["listUsers"],
        "confidence": 0.9,
    }
    stub = StubLLM([_mk_response([bad])])
    run = SynthesisRun.new(["sample-api"])
    with _emitter(run, log_path, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub.model_id),
        )
        accepted = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
        )
    assert accepted == []
    events = _read_events(log_path)
    rejections = [
        e for e in events if e["stage"] == Stage.RECORD_REJECTED.value
    ]
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "length_out_of_bounds"


# ---------------------------------------------------------------------------
# 5. Out-of-range confidence -> rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_out_of_range_rejected(tmp_paths):
    db_path, log_path, diag_dir = tmp_paths
    db = await _make_db(db_path)
    spec = _make_minimal_openapi_spec()
    tools = _harvest(spec)

    bad = _good_use_case(confidence=1.5)
    stub = StubLLM([_mk_response([bad])])
    run = SynthesisRun.new(["sample-api"])
    with _emitter(run, log_path, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub.model_id),
        )
        accepted = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
        )
    assert accepted == []
    events = _read_events(log_path)
    rejections = [
        e for e in events if e["stage"] == Stage.RECORD_REJECTED.value
    ]
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "confidence_out_of_range"


# ---------------------------------------------------------------------------
# 6. Multiple use cases from one chunk -> sequential seq in id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_use_cases_sequential_seq(tmp_paths):
    db_path, log_path, diag_dir = tmp_paths
    db = await _make_db(db_path)
    spec = _make_minimal_openapi_spec()
    tools = _harvest(spec)

    uc1 = _good_use_case(
        description="First use case description that is comfortably above the fifty-character lower bound.",
        linked=["listUsers"],
    )
    uc2 = _good_use_case(
        description="Second use case description that is also above the lower bound but distinct from the first.",
        linked=["getUser"],
    )
    uc3 = _good_use_case(
        description="Third use case combining both endpoints to drill down from list to a specific user record.",
        linked=["listUsers", "getUser"],
    )
    stub = StubLLM([_mk_response([uc1, uc2, uc3])])
    run = SynthesisRun.new(["sample-api"])
    with _emitter(run, log_path, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub.model_id),
        )
        accepted = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
        )
    assert len(accepted) == 3
    seqs = sorted(int(r.id.rsplit("-", 1)[-1]) for r in accepted)
    assert seqs == [0, 1, 2]
    # All under the same chunk_id.
    assert len({r.chunk_id for r in accepted}) == 1


# ---------------------------------------------------------------------------
# 7. Per-chunk cache short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_chunk_cache_short_circuit(tmp_paths):
    db_path, log_path, diag_dir = tmp_paths
    db = await _make_db(db_path)
    spec = _make_minimal_openapi_spec()
    tools = _harvest(spec)

    # Build chunks with the same logic the miner uses, then mark each
    # chunk_hash as already cached so the miner's mine_source must
    # short-circuit per-chunk and emit zero records (and never call LLM).
    from mcp_semantic_gateway.ingestion.chunker import (
        chunk_tools,
        compute_source_hash_openapi,
    )

    source_hash = compute_source_hash_openapi(spec)
    chunks = chunk_tools(
        tools,
        server_id="sample-api",
        source_hash=source_hash,
        source_kind="openapi",
        chunk_size=12,
    )
    cfg = UseCaseMinerConfig(model_id="stub-model-v1")
    for c in chunks:
        await db.mark_chunk_cached(c, cfg.model_id, cfg.prompt_version)

    # No use_cases yet for this source_hash, so source-cache is False;
    # miner must still proceed into mine_chunk where the per-chunk cache
    # diverts it. Provide an empty StubLLM that would raise if invoked.
    stub = StubLLM([])
    run = SynthesisRun.new(["sample-api"])
    with _emitter(run, log_path, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub,
            db=db,
            emitter=ev,
            config=cfg,
        )
        accepted = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
        )
    assert len(stub.calls) == 0
    assert accepted == []
    events = _read_events(log_path)
    completed = [
        e for e in events if e["stage"] == Stage.CHUNK_COMPLETED.value
    ]
    assert completed and all(e.get("cached") is True for e in completed)


# ---------------------------------------------------------------------------
# 8. Dry run -> no LLM, no persistence, dry_run=True on CHUNK_COMPLETED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_no_llm_no_persist(tmp_paths):
    db_path, log_path, diag_dir = tmp_paths
    db = await _make_db(db_path)
    spec = _make_minimal_openapi_spec()
    tools = _harvest(spec)

    stub = StubLLM([])
    run = SynthesisRun.new(["sample-api"])
    with _emitter(run, log_path, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub.model_id),
        )
        accepted = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
            dry_run=True,
        )
    assert accepted == []
    assert len(stub.calls) == 0
    rows = await db.list_use_cases()
    assert rows == []

    events = _read_events(log_path)
    completed = [
        e for e in events if e["stage"] == Stage.CHUNK_COMPLETED.value
    ]
    assert completed and all(e.get("dry_run") is True for e in completed)


# ---------------------------------------------------------------------------
# 9. Source-hash determinism across key reordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_hash_determinism_keys_reordered(tmp_paths):
    db_path, log_path, diag_dir = tmp_paths
    db = await _make_db(db_path)
    spec = _make_minimal_openapi_spec()
    # Reorder top-level keys.
    reordered = {
        "paths": spec["paths"],
        "info": {
            "version": spec["info"]["version"],
            "title": spec["info"]["title"],
        },
        "openapi": spec["openapi"],
    }
    tools = _harvest(spec)

    stub_first = StubLLM([_mk_response([_good_use_case()])])
    run1 = SynthesisRun.new(["sample-api"])
    with _emitter(run1, log_path, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub_first,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub_first.model_id),
        )
        await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=spec,
        )

    # Same content, different key order -> source-cache must hit.
    stub_second = StubLLM([])
    log_path2 = log_path.parent / "synthesis2.jsonl"
    run2 = SynthesisRun.new(["sample-api"])
    with _emitter(run2, log_path2, diag_dir) as ev:
        miner = UseCaseMiner(
            llm=stub_second,
            db=db,
            emitter=ev,
            config=UseCaseMinerConfig(model_id=stub_second.model_id),
        )
        second = await miner.mine_source(
            server_id="sample-api",
            harvested_tools=tools,
            source_kind="openapi",
            spec=reordered,
        )
    assert len(stub_second.calls) == 0
    assert len(second) == 1
    events = _read_events(log_path2)
    assert any(e["stage"] == Stage.CACHE_HIT.value for e in events)
