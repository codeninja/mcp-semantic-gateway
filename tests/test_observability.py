"""Tests for the synthesis observability layer (Phase D).

Covers:

* Well-formed JSONL records with required core fields.
* Aggregation of ``usage`` across many ``chunk_completed`` events.
* ``record_rejected`` reason counter feeding the summary breakdown.
* ``cache_hit`` source-level tally.
* Diagnostic file write + JSON roundtrip.
* ``progress=False`` mode: no Rich Progress constructed; JSONL still durable.
* JSONL parses line-by-line.
* Summary handles zero rejections cleanly.
* Summary renders ``n/a`` when no events carry ``estimated_cost_usd``.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

from mcp_semantic_gateway.ingestion.observability import (
    EventEmitter,
    Stage,
    SynthesisRun,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_paths():
    """Yield (log_path, diagnostics_dir) under a fresh temp dir."""

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        yield root / "logs" / "synthesis.jsonl", root / "diagnostics"


@pytest.fixture
def run() -> SynthesisRun:
    return SynthesisRun.new(["github-api"])


def _emitter(run: SynthesisRun, log_path: Path, diag_dir: Path) -> EventEmitter:
    """Build an emitter with progress disabled for clean test output."""

    return EventEmitter(run, log_path, diag_dir, progress=False)


def _read_lines(log_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_synthesis_run_new_generates_id_and_timestamp() -> None:
    r = SynthesisRun.new(["a", "b"])
    assert re.match(r"^run-[0-9a-f]{8}$", r.run_id), r.run_id
    assert r.started_at.tzinfo is not None
    assert r.server_ids == ["a", "b"]


def test_emit_produces_well_formed_jsonl_record(tmp_paths, run) -> None:
    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        ev.emit(
            Stage.MINING_STARTED,
            server_id="github-api",
            total_chunks=3,
        )

    records = _read_lines(log_path)
    assert len(records) == 1
    rec = records[0]

    # Required core fields
    assert rec["stage"] == "mining_started"
    assert rec["run_id"] == run.run_id
    assert isinstance(rec["ts"], str)
    # ts: ISO-8601 with millisecond precision and Z suffix
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", rec["ts"]
    ), rec["ts"]

    # Passthrough kwargs survived
    assert rec["server_id"] == "github-api"
    assert rec["total_chunks"] == 3


def test_emit_aggregates_usage_across_chunk_completed_events(tmp_paths, run) -> None:
    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        ev.emit(
            Stage.CHUNK_COMPLETED,
            server_id="github-api",
            chunk_id="chunk-1",
            use_cases_emitted=4,
            usage={
                "input_tokens": 1000,
                "output_tokens": 200,
                "cached_input_tokens": 800,
                "estimated_cost_usd": 0.001,
            },
        )
        ev.emit(
            Stage.CHUNK_COMPLETED,
            server_id="github-api",
            chunk_id="chunk-2",
            use_cases_emitted=3,
            usage={
                "input_tokens": 500,
                "output_tokens": 100,
                "cached_input_tokens": 200,
                "estimated_cost_usd": 0.0005,
            },
        )

        assert ev.tokens_in == 1500
        assert ev.tokens_out == 300
        assert ev.cached_tokens_in == 1000
        assert ev.use_cases_emitted == 7
        assert ev.chunks_completed == 2
        assert ev.total_cost_usd == pytest.approx(0.0015)


def test_record_rejected_reason_counter_in_summary(tmp_paths, run) -> None:
    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        ev.emit(Stage.MINING_STARTED, server_id="github-api", total_chunks=1)
        ev.emit(
            Stage.RECORD_REJECTED,
            server_id="github-api",
            chunk_id="chunk-1",
            reason="tool_name_unresolved",
        )
        ev.emit(
            Stage.RECORD_REJECTED,
            server_id="github-api",
            chunk_id="chunk-1",
            reason="tool_name_unresolved",
        )
        ev.emit(
            Stage.RECORD_REJECTED,
            server_id="github-api",
            chunk_id="chunk-1",
            reason="length_out_of_bounds",
        )

        assert ev.use_cases_rejected == 3
        assert ev.rejection_reasons["tool_name_unresolved"] == 2
        assert ev.rejection_reasons["length_out_of_bounds"] == 1

        summary = ev.render_summary()

    assert "Use cases rejected:       3" in summary
    assert "tool_name_unresolved=2" in summary
    assert "length_out_of_bounds=1" in summary


def test_cache_hit_increments_source_count_and_surfaces_in_summary(
    tmp_paths, run
) -> None:
    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        ev.emit(Stage.CACHE_HIT, server_id="github-api", use_cases_emitted=12)
        ev.emit(Stage.CACHE_HIT, server_id="other-api", use_cases_emitted=5)

        assert ev.cache_hits == 2
        assert ev.use_cases_emitted == 17
        summary = ev.render_summary()

    assert "Cache hits:               2" in summary
    assert "Use cases emitted:        17" in summary


def test_write_diagnostic_writes_file_at_expected_path(tmp_paths, run) -> None:
    log_path, diag_dir = tmp_paths
    payload = {
        "stage": "chunk_failed",
        "chunk_id": "chunk-x",
        "prompt": "hello",
        "model_response": "world",
        "validation_errors": ["missing_tool"],
        "chunk_hash": "abcdef",
    }
    with _emitter(run, log_path, diag_dir) as ev:
        path = ev.write_diagnostic("chunk-x", payload)

    expected = diag_dir / run.run_id / "chunk-x.json"
    assert path == expected
    assert expected.exists()
    roundtripped = json.loads(expected.read_text())
    assert roundtripped == payload


def test_progress_false_does_not_construct_progress_but_writes_jsonl(
    tmp_paths, run
) -> None:
    log_path, diag_dir = tmp_paths
    ev = EventEmitter(run, log_path, diag_dir, progress=False)
    try:
        # Confirm no Rich Progress / Console were constructed.
        assert ev._progress is None
        assert ev._console is None

        ev.emit(Stage.MINING_STARTED, server_id="github-api", total_chunks=1)
        ev.emit(Stage.CHUNK_STARTED, server_id="github-api", chunk_id="c1")
        ev.emit(
            Stage.CHUNK_COMPLETED,
            server_id="github-api",
            chunk_id="c1",
            use_cases_emitted=2,
            usage={"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0},
        )
    finally:
        ev.close()

    records = _read_lines(log_path)
    assert [r["stage"] for r in records] == [
        "mining_started",
        "chunk_started",
        "chunk_completed",
    ]


def test_jsonl_file_parses_line_by_line(tmp_paths, run) -> None:
    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        for i in range(5):
            ev.emit(
                Stage.CHUNK_COMPLETED,
                server_id="s",
                chunk_id=f"c{i}",
                use_cases_emitted=1,
                usage={
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cached_input_tokens": 0,
                },
            )

    raw = log_path.read_text().splitlines()
    assert len(raw) == 5
    for line in raw:
        rec = json.loads(line)  # would raise if any line is malformed
        assert rec["run_id"] == run.run_id
        assert rec["stage"] == "chunk_completed"


def test_summary_handles_zero_rejections_cleanly(tmp_paths, run) -> None:
    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        ev.emit(Stage.MINING_STARTED, server_id="github-api", total_chunks=1)
        ev.emit(
            Stage.CHUNK_COMPLETED,
            server_id="github-api",
            chunk_id="c1",
            use_cases_emitted=2,
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_input_tokens": 50,
            },
        )
        summary = ev.render_summary()

    assert "Use cases rejected:       0" in summary
    # No empty parentheses, no trailing whitespace tail.
    assert "()" not in summary
    rejected_line = next(
        line for line in summary.splitlines() if "Use cases rejected" in line
    )
    assert not rejected_line.rstrip().endswith("(")
    assert "(" not in rejected_line


def test_summary_renders_n_a_when_no_cost_present(tmp_paths, run) -> None:
    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        ev.emit(
            Stage.CHUNK_COMPLETED,
            server_id="s",
            chunk_id="c1",
            use_cases_emitted=1,
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_input_tokens": 0,
                # No estimated_cost_usd field at all.
            },
        )
        assert ev.total_cost_usd is None
        summary = ev.render_summary()

    assert "Estimated cost:           n/a" in summary


def test_summary_includes_skill_section_when_skills_present(tmp_paths, run) -> None:
    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        ev.emit(Stage.SKILL_WRITTEN, skill_id="skill-1")
        ev.emit(Stage.SKILL_WRITTEN, skill_id="skill-2")
        ev.emit(Stage.SKILL_CACHE_HIT, skill_id="skill-3")
        ev.emit(Stage.SKILL_REJECTED, skill_id="skill-4", reason="tool_grounding")
        summary = ev.render_summary()

    assert "Skills generated:        2" in summary
    assert "Skills cached:           1" in summary
    assert "Skills rejected:         1" in summary
    assert "tool_grounding=1" in summary


def test_emit_accepts_string_stage_name(tmp_paths, run) -> None:
    """Stage may be passed as a raw string (not just the enum)."""

    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        ev.emit("chunk_started", server_id="s", chunk_id="c1")

    records = _read_lines(log_path)
    assert records[0]["stage"] == "chunk_started"


def test_emit_does_not_let_caller_clobber_core_fields(tmp_paths, run) -> None:
    """Caller-supplied ``ts`` / ``run_id`` must not override emitter-set values."""

    log_path, diag_dir = tmp_paths
    with _emitter(run, log_path, diag_dir) as ev:
        ev.emit(
            Stage.CHUNK_STARTED,
            server_id="s",
            chunk_id="c1",
            ts="bogus",
            run_id="run-evilevil",
        )

    rec = _read_lines(log_path)[0]
    assert rec["run_id"] == run.run_id
    assert rec["stage"] == "chunk_started"
    assert rec["ts"] != "bogus"
