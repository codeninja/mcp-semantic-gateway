"""Observability layer for the use-case synthesis pipeline (Phase D).

Defines:

* :class:`SynthesisRun` — identity for a single mining/synthesis run.
* :class:`Stage` — string-typed enum of all stage names emitted by the
  use-case mining and skill-synthesis stages.
* :class:`EventEmitter` — JSONL writer + Rich progress bars + diagnostics
  writer + tally aggregator + run-summary renderer.

This layer is intentionally *not* feature-flagged: every synth run emits
events. Callers can disable the live progress UI with ``progress=False``
(e.g., in tests or non-interactive environments); the JSONL log and
diagnostics writer remain durable in either mode.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

__all__ = ["Stage", "SynthesisRun", "EventEmitter"]


# ---------------------------------------------------------------------------
# Stage names
# ---------------------------------------------------------------------------


class Stage(str, Enum):
    """All stage names emitted by the synthesis pipeline.

    Mining stages drive Phase C (use-case mining); skill stages drive a
    later phase. They are enumerated together here so the JSONL schema
    is stable across phases.
    """

    # Use-case mining
    MINING_STARTED = "mining_started"
    CACHE_HIT = "cache_hit"
    CHUNK_STARTED = "chunk_started"
    CHUNK_COMPLETED = "chunk_completed"
    CHUNK_FAILED = "chunk_failed"
    RECORD_REJECTED = "record_rejected"
    MINING_COMPLETED = "mining_completed"
    MINING_FAILED = "mining_failed"

    # Skill synthesis (downstream phases)
    CLUSTERING_STARTED = "clustering_started"
    CLUSTERING_COMPLETED = "clustering_completed"
    SKILL_SYNTHESIS_STARTED = "skill_synthesis_started"
    SKILL_CACHE_HIT = "skill_cache_hit"
    SKILL_DUP_SUPPRESSED = "skill_dup_suppressed"
    SKILL_SYNTHESIZED = "skill_synthesized"
    SKILL_VALIDATED = "skill_validated"
    SKILL_REJECTED = "skill_rejected"
    SKILL_WRITTEN = "skill_written"
    SYNTHESIS_COMPLETED = "synthesis_completed"
    SYNTHESIS_FAILED = "synthesis_failed"


# ---------------------------------------------------------------------------
# SynthesisRun
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return current UTC time as a tz-aware datetime."""

    return datetime.now(timezone.utc)


def _format_ts(dt: datetime) -> str:
    """Format a datetime as ISO-8601 with millisecond precision and ``Z``."""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class SynthesisRun:
    """Identity for a single synthesis run."""

    run_id: str
    started_at: datetime
    server_ids: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, server_ids: list[str] | None = None) -> "SynthesisRun":
        """Construct a new run with a generated id and current timestamp."""

        return cls(
            run_id=f"run-{uuid.uuid4().hex[:8]}",
            started_at=_utcnow(),
            server_ids=list(server_ids or []),
        )


# ---------------------------------------------------------------------------
# EventEmitter
# ---------------------------------------------------------------------------


def _format_duration(seconds: float) -> str:
    """Format an elapsed duration like ``4m 12s`` / ``42s`` / ``1h 3m``."""

    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_cost(cost: float | None) -> str:
    """Render a USD cost; ``n/a`` when None."""

    if cost is None:
        return "n/a"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.3f}"


class EventEmitter:
    """Append-only JSONL emitter with progress UI and tally aggregation.

    Use as a context manager — the JSONL file handle and Rich progress
    UI are torn down on ``__exit__``.

    Example::

        run = SynthesisRun.new(["github-api"])
        with EventEmitter(run, log_path, diagnostics_dir) as ev:
            ev.emit("mining_started", server_id="github-api", total_chunks=3)
            ...
            print(ev.render_summary())
    """

    def __init__(
        self,
        run: SynthesisRun,
        log_path: Path,
        diagnostics_dir: Path,
        *,
        progress: bool = True,
    ) -> None:
        self.run = run
        self.log_path = Path(log_path)
        self.diagnostics_dir = Path(diagnostics_dir)
        self._progress_enabled = progress

        # JSONL writer state
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle: TextIO = self.log_path.open("a", encoding="utf-8")

        # Tallies (mining)
        self.servers_seen: set[str] = set()
        self.chunks_started: int = 0
        self.chunks_completed: int = 0
        self.chunks_failed: int = 0
        self.cache_hits: int = 0
        self.use_cases_emitted: int = 0
        self.use_cases_rejected: int = 0
        self.rejection_reasons: Counter[str] = Counter()
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self.cached_tokens_in: int = 0
        self.total_cost_usd: float | None = None

        # Tallies (skills, downstream)
        self.skills_written: int = 0
        self.skills_rejected: int = 0
        self.skills_cached: int = 0
        self.skill_rejection_reasons: Counter[str] = Counter()

        # Rich progress UI
        self._console: Console | None = None
        self._progress: Progress | None = None
        self._server_tasks: dict[str, int] = {}
        self._chunk_tasks: dict[str, int] = {}

        if self._progress_enabled:
            self._console = Console()
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                TextColumn(
                    "[dim]tok in/out: {task.fields[tokens_in]:,}/"
                    "{task.fields[tokens_out]:,}  uc: {task.fields[uc]}"
                ),
                console=self._console,
                transient=False,
            )
            self._progress.start()

    # ---- context manager ----------------------------------------------------

    def __enter__(self) -> "EventEmitter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop progress UI and close the JSONL handle. Idempotent."""

        if self._progress is not None:
            try:
                self._progress.stop()
            except Exception:  # pragma: no cover - defensive
                pass
            self._progress = None
        if self._log_handle is not None and not self._log_handle.closed:
            try:
                self._log_handle.flush()
                self._log_handle.close()
            except Exception:  # pragma: no cover - defensive
                pass

    # ---- emit ---------------------------------------------------------------

    def emit(self, stage: str, **fields: Any) -> None:
        """Append one JSONL record and update tallies / UI.

        ``stage`` may be a :class:`Stage` enum or the raw string. All
        other keyword arguments are merged into the record verbatim;
        ``ts``, ``run_id``, and ``stage`` are always set by the emitter
        and override any caller-supplied values.
        """

        stage_str = stage.value if isinstance(stage, Stage) else str(stage)

        record: dict[str, Any] = {
            "ts": _format_ts(_utcnow()),
            "run_id": self.run.run_id,
            "stage": stage_str,
        }
        # Merge passthrough fields, but never let them clobber the core trio.
        for key, val in fields.items():
            if key in {"ts", "run_id", "stage"}:
                continue
            record[key] = val

        line = json.dumps(record, default=_json_default, ensure_ascii=False)
        self._log_handle.write(line + "\n")
        self._log_handle.flush()

        self._aggregate(stage_str, fields)
        self._advance_progress(stage_str, fields)

    # ---- diagnostics --------------------------------------------------------

    def write_diagnostic(self, chunk_id: str, payload: dict[str, Any]) -> Path:
        """Write a per-chunk diagnostic JSON file.

        Returns the path written. Creates parent directories as needed.
        """

        run_dir = self.diagnostics_dir / self.run.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{chunk_id}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, default=_json_default, indent=2, ensure_ascii=False)
        return path

    # ---- aggregation --------------------------------------------------------

    def _aggregate(self, stage: str, fields: dict[str, Any]) -> None:
        server_id = fields.get("server_id")
        if server_id:
            self.servers_seen.add(str(server_id))

        if stage == Stage.MINING_STARTED.value:
            pass
        elif stage == Stage.CACHE_HIT.value:
            self.cache_hits += 1
            # Cache-hit may carry pre-counted use_cases; surface them.
            uc = fields.get("use_cases_emitted")
            if isinstance(uc, int):
                self.use_cases_emitted += uc
        elif stage == Stage.CHUNK_STARTED.value:
            self.chunks_started += 1
        elif stage == Stage.CHUNK_COMPLETED.value:
            self.chunks_completed += 1
            uc = fields.get("use_cases_emitted")
            if isinstance(uc, int):
                self.use_cases_emitted += uc
            ucr = fields.get("use_cases_rejected")
            if isinstance(ucr, int):
                self.use_cases_rejected += ucr
            self._absorb_usage(fields.get("usage"))
        elif stage == Stage.CHUNK_FAILED.value:
            self.chunks_failed += 1
            self._absorb_usage(fields.get("usage"))
        elif stage == Stage.RECORD_REJECTED.value:
            self.use_cases_rejected += 1
            reason = fields.get("reason")
            if reason:
                self.rejection_reasons[str(reason)] += 1
        elif stage == Stage.SKILL_WRITTEN.value:
            self.skills_written += 1
        elif stage == Stage.SKILL_CACHE_HIT.value:
            self.skills_cached += 1
        elif stage == Stage.SKILL_REJECTED.value:
            self.skills_rejected += 1
            reason = fields.get("reason")
            if reason:
                self.skill_rejection_reasons[str(reason)] += 1

    def _absorb_usage(self, usage: Any) -> None:
        """Fold a ``usage`` payload (dict or pydantic-like) into tallies."""

        if usage is None:
            return
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        elif hasattr(usage, "__dict__") and not isinstance(usage, dict):
            usage = dict(usage.__dict__)
        if not isinstance(usage, dict):
            return

        ti = usage.get("input_tokens")
        to = usage.get("output_tokens")
        ci = usage.get("cached_input_tokens")
        cost = usage.get("estimated_cost_usd")

        if isinstance(ti, int):
            self.tokens_in += ti
        if isinstance(to, int):
            self.tokens_out += to
        if isinstance(ci, int):
            self.cached_tokens_in += ci
        if isinstance(cost, (int, float)):
            self.total_cost_usd = (self.total_cost_usd or 0.0) + float(cost)

    # ---- Rich progress ------------------------------------------------------

    def _advance_progress(self, stage: str, fields: dict[str, Any]) -> None:
        if self._progress is None:
            return

        server_id = fields.get("server_id")

        if stage == Stage.MINING_STARTED.value and server_id:
            total = int(fields.get("total_chunks", 0) or 0)
            tid = self._progress.add_task(
                description=f"{server_id}",
                total=total or None,
                tokens_in=self.tokens_in,
                tokens_out=self.tokens_out,
                uc=self.use_cases_emitted,
            )
            self._server_tasks[str(server_id)] = tid

        elif stage == Stage.CHUNK_STARTED.value:
            chunk_id = fields.get("chunk_id")
            if chunk_id is not None:
                tid = self._progress.add_task(
                    description=f"  {chunk_id}",
                    total=1,
                    tokens_in=self.tokens_in,
                    tokens_out=self.tokens_out,
                    uc=self.use_cases_emitted,
                )
                self._chunk_tasks[str(chunk_id)] = tid

        elif stage in (Stage.CHUNK_COMPLETED.value, Stage.CHUNK_FAILED.value):
            chunk_id = fields.get("chunk_id")
            if chunk_id is not None:
                tid = self._chunk_tasks.pop(str(chunk_id), None)
                if tid is not None:
                    self._progress.update(
                        tid,
                        completed=1,
                        tokens_in=self.tokens_in,
                        tokens_out=self.tokens_out,
                        uc=self.use_cases_emitted,
                    )
                    self._progress.remove_task(tid)
            if server_id and str(server_id) in self._server_tasks:
                self._progress.update(
                    self._server_tasks[str(server_id)],
                    advance=1,
                    tokens_in=self.tokens_in,
                    tokens_out=self.tokens_out,
                    uc=self.use_cases_emitted,
                )

        elif stage == Stage.MINING_COMPLETED.value and server_id:
            tid = self._server_tasks.get(str(server_id))
            if tid is not None:
                self._progress.update(
                    tid,
                    tokens_in=self.tokens_in,
                    tokens_out=self.tokens_out,
                    uc=self.use_cases_emitted,
                )

        elif stage == Stage.CACHE_HIT.value and server_id:
            # Refresh outer task (if it exists) so totals update.
            tid = self._server_tasks.get(str(server_id))
            if tid is not None:
                self._progress.update(
                    tid,
                    tokens_in=self.tokens_in,
                    tokens_out=self.tokens_out,
                    uc=self.use_cases_emitted,
                )

    # ---- summary ------------------------------------------------------------

    def render_summary(self) -> str:
        """Render the end-of-run summary block (multi-line string)."""

        elapsed = (_utcnow() - _ensure_aware(self.run.started_at)).total_seconds()
        sources_processed = len(self.servers_seen) or len(self.run.server_ids)

        # Use-case rejection breakdown
        if self.rejection_reasons:
            uc_breakdown = ", ".join(
                f"{reason}={count}"
                for reason, count in sorted(self.rejection_reasons.items())
            )
            uc_rejected_line = (
                f"  Use cases rejected:       {self.use_cases_rejected} "
                f"({uc_breakdown})"
            )
        else:
            uc_rejected_line = (
                f"  Use cases rejected:       {self.use_cases_rejected}"
            )

        # Cached %
        if self.tokens_in > 0:
            cached_pct = round(self.cached_tokens_in / self.tokens_in * 100)
            cached_line = (
                f"  Cached tokens in:         {self.cached_tokens_in:,} "
                f"({cached_pct}%)"
            )
        else:
            cached_line = f"  Cached tokens in:         {self.cached_tokens_in:,}"

        diag_path = self.diagnostics_dir / self.run.run_id
        try:
            diag_display = os.fspath(diag_path)
        except TypeError:  # pragma: no cover - Path always supports fspath
            diag_display = str(diag_path)

        lines = [
            f"Synthesis run {self.run.run_id} complete ({_format_duration(elapsed)})",
            f"  Sources processed:        {sources_processed}",
            f"  Chunks executed:          {self.chunks_completed}",
            f"  Cache hits:               {self.cache_hits}",
            f"  Use cases emitted:        {self.use_cases_emitted}",
            uc_rejected_line,
            f"  Tokens in / out:          {self.tokens_in:,} / {self.tokens_out:,}",
            cached_line,
            f"  Estimated cost:           {_format_cost(self.total_cost_usd)}",
            f"  Diagnostics:              {diag_display}",
        ]

        if self.skills_written or self.skills_rejected or self.skills_cached:
            lines.append(f"  Skills generated:        {self.skills_written}")
            lines.append(f"  Skills cached:           {self.skills_cached}")
            if self.skill_rejection_reasons:
                skill_breakdown = ", ".join(
                    f"{reason}={count}"
                    for reason, count in sorted(self.skill_rejection_reasons.items())
                )
                lines.append(
                    f"  Skills rejected:         {self.skills_rejected} "
                    f"({skill_breakdown})"
                )
            else:
                lines.append(f"  Skills rejected:         {self.skills_rejected}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _json_default(obj: Any) -> Any:
    """JSON fallback for non-native types (datetime, pydantic models, sets)."""

    if isinstance(obj, datetime):
        return _format_ts(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
