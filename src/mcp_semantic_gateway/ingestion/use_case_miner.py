"""Phase C — Use-case mining.

Given a per-server harvested tool list, group it into deterministic
chunks (Phase B) and run a per-chunk LLM extraction (Phase A) to derive
``UseCaseRecord`` entries that describe agent-actionable tasks the
tools enable.

Records are validated, persisted to ``MetadataDB`` (Phase C migration),
and observability events are emitted via the ``EventEmitter`` (Phase D).

Public surface
--------------

* :class:`UseCaseRecord` — pydantic model persisted by :class:`MetadataDB`.
* :class:`UseCaseMinerConfig` — knobs (model, chunk size, concurrency,
  validation bounds).
* :class:`UseCaseMiner` — orchestrator. ``mine_source`` runs over an
  entire upstream surface; ``mine_chunk`` runs one LLM call.
* :data:`USE_CASE_EXTRACTION_TOOL` — the ``ToolSpec`` registered with
  the LLM for structured-output extraction.
* :data:`SYSTEM_PROMPT_V1`, :data:`USER_PROMPT_TEMPLATE_V1` — versioned
  prompt templates. Prompt-version bumps invalidate per-chunk cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from mcp_semantic_gateway.ingestion.chunker import (
    ToolChunk,
    chunk_tools,
    compute_source_hash_mcp,
    compute_source_hash_openapi,
)
from mcp_semantic_gateway.ingestion.observability import EventEmitter, Stage
from mcp_semantic_gateway.llm.base import LLMProvider, Message, ToolSpec
from mcp_semantic_gateway.storage.metadata_db import MetadataDB


__all__ = [
    "UseCaseRecord",
    "UseCaseMinerConfig",
    "UseCaseMiner",
    "USE_CASE_EXTRACTION_TOOL",
    "SYSTEM_PROMPT_V1",
    "USER_PROMPT_TEMPLATE_V1",
]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class UseCaseRecord(BaseModel):
    """A validated, content-hashed use case persisted under
    ``use_cases`` and embedded into the vector store."""

    id: str = Field(..., description="uc-<server>-<chunk_short>-<seq>")
    server_id: str
    source_hash: str
    chunk_id: str
    description: str = Field(..., min_length=50, max_length=400)
    linked_tool_names: list[str] = Field(..., min_length=1)
    prerequisites: list[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    generated_by: str = Field(..., description="<model_id>@<prompt_version>")
    generated_at: datetime
    use_case_hash: str


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class UseCaseMinerConfig(BaseModel):
    """Tunable parameters for :class:`UseCaseMiner`."""

    model_id: str
    prompt_version: str = "v1"
    chunk_size: int = 12
    max_concurrency: int = 4
    description_min_chars: int = 50
    description_max_chars: int = 400


# ---------------------------------------------------------------------------
# LLM tool schema (structured-output extraction target)
# ---------------------------------------------------------------------------


USE_CASE_EXTRACTION_TOOL = ToolSpec(
    name="emit_use_cases",
    description=(
        "Emit one or more use case records derived from the supplied "
        "tool catalog."
    ),
    input_schema={
        "type": "object",
        "required": ["use_cases"],
        "properties": {
            "use_cases": {
                "type": "array",
                "minItems": 0,
                "items": {
                    "type": "object",
                    "required": [
                        "description",
                        "linked_tool_names",
                        "confidence",
                    ],
                    "properties": {
                        "description": {
                            "type": "string",
                            "minLength": 50,
                            "maxLength": 400,
                            "description": (
                                "A 1-3 sentence description of the use case "
                                "in agent vocabulary. Describe a real-world "
                                "task an agent could accomplish using ONLY "
                                "tools from the supplied chunk."
                            ),
                        },
                        "linked_tool_names": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                            "description": (
                                "Tool names from the supplied chunk that "
                                "this use case uses. Must reference tools "
                                "verbatim."
                            ),
                        },
                        "prerequisites": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Natural-language preconditions (e.g., "
                                "'user must be authenticated', 'repo must "
                                "exist'). Optional."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": (
                                "How confident you are this use case is "
                                "realistic and grounded. 0.9+ for clear "
                                "cases; lower for inferred."
                            ),
                        },
                    },
                },
            }
        },
    },
)


# ---------------------------------------------------------------------------
# Prompts (versioned — bump prompt_version to invalidate per-chunk cache)
# ---------------------------------------------------------------------------


SYSTEM_PROMPT_V1 = """You are an expert API analyst. Given a list of API tools (with names, descriptions, and schemas), identify the high-level USE CASES the tools enable for an AI agent.

A use case describes a real-world task — not a single tool call. Examples: "Triage GitHub issues by searching, labeling, and closing stale ones." "Summarize a Notion page after fetching it by URL."

Rules:
- Use cases must be agent-actionable in 1-2 sentences.
- Reference tools VERBATIM by name from the supplied chunk.
- Multi-step use cases are more valuable than single-tool ones.
- Confidence reflects how realistic and grounded the use case is.
- If the chunk contains tools that don't combine into any meaningful use case, emit fewer or zero records — don't fabricate.
- Output via the emit_use_cases tool. Do not output prose.
"""


USER_PROMPT_TEMPLATE_V1 = """Source: {server_id}
Group: {group_label}
Tools in this chunk:

{tool_catalog}

Identify the use cases this chunk of tools enables. Emit them via the emit_use_cases tool."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _required_params(tool: dict) -> str:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    required = schema.get("required") or []
    if not required:
        return "(none)"
    return ", ".join(str(r) for r in required)


def _render_tool_catalog(tools: list[dict]) -> str:
    """Render the per-chunk numbered tool catalog used in the user prompt."""

    lines: list[str] = []
    for i, tool in enumerate(tools, start=1):
        name = tool.get("name", "(unnamed)")
        desc = _truncate(str(tool.get("description") or ""), 240)
        req = _required_params(tool)
        lines.append(f"{i}. {name}")
        lines.append(f"   Description: {desc}")
        lines.append(f"   Required parameters: {req}")
    return "\n".join(lines)


def _chunk_short(chunk_id: str, server_id: str) -> str:
    """Distinguishing tail of a chunk_id, used in UseCaseRecord.id.

    ``chunk_id`` has the shape ``chunk-<server_id>-<group>-<idx>``. We
    strip the ``chunk-<server_id>-`` prefix to retain the
    group-and-index suffix that disambiguates chunks within a server
    (otherwise every chunk's "last segment" is just ``0``)."""

    prefix = f"chunk-{server_id}-"
    if chunk_id.startswith(prefix):
        return chunk_id[len(prefix):]
    if chunk_id.startswith("chunk-"):
        return chunk_id[len("chunk-"):]
    return chunk_id


def _compute_use_case_hash(
    *,
    server_id: str,
    source_hash: str,
    description: str,
    linked_tool_names: list[str],
) -> str:
    payload = {
        "server_id": server_id,
        "source_hash": source_hash,
        "description": description,
        "linked_tool_names": sorted(linked_tool_names),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Miner
# ---------------------------------------------------------------------------


class UseCaseMiner:
    """Orchestrator for use-case mining over a single server's surface."""

    def __init__(
        self,
        llm: LLMProvider,
        db: MetadataDB,
        emitter: EventEmitter,
        config: UseCaseMinerConfig,
    ) -> None:
        self.llm = llm
        self.db = db
        self.emitter = emitter
        self.config = config

    # ---------------- public API ----------------

    async def mine_source(
        self,
        *,
        server_id: str,
        harvested_tools: list[dict],
        source_kind: Literal["openapi", "mcp"],
        spec: dict | None = None,
        dry_run: bool = False,
    ) -> list[UseCaseRecord]:
        """Mine all use cases for a single source.

        Steps (verbatim from design):
          1. Compute source_hash.
          2. If source-cached, emit CACHE_HIT, return persisted records.
          3. Build chunks.
          4. Emit MINING_STARTED.
          5. Run mine_chunk per chunk under a concurrency semaphore;
             ``return_exceptions=True`` so one chunk failure doesn't
             abort the run.
          6. Emit MINING_COMPLETED.
        """

        started = time.monotonic()

        # 1. source_hash
        if source_kind == "openapi":
            if spec is None:
                raise ValueError(
                    "spec is required when source_kind='openapi'"
                )
            source_hash = compute_source_hash_openapi(spec)
        elif source_kind == "mcp":
            source_hash = compute_source_hash_mcp(harvested_tools)
        else:  # pragma: no cover — guarded by Literal
            raise ValueError(f"Unknown source_kind: {source_kind!r}")

        # 2. Source-level cache short-circuit (skip when dry_run so the
        #    caller can always preview a fresh chunking).
        if not dry_run and await self.db.is_source_cached(
            server_id, source_hash
        ):
            cached = await self.db.list_use_cases_for_source(
                server_id, source_hash
            )
            self.emitter.emit(
                Stage.CACHE_HIT,
                server_id=server_id,
                source_hash=source_hash,
                use_cases_emitted=len(cached),
            )
            return cached

        # 3. Build chunks.
        try:
            chunks = chunk_tools(
                harvested_tools,
                server_id=server_id,
                source_hash=source_hash,
                source_kind=source_kind,
                chunk_size=self.config.chunk_size,
            )
        except Exception as exc:
            self.emitter.emit(
                Stage.MINING_FAILED,
                server_id=server_id,
                error=str(exc),
            )
            raise

        total_tools = sum(len(c.tools) for c in chunks)

        # 4. MINING_STARTED.
        self.emitter.emit(
            Stage.MINING_STARTED,
            server_id=server_id,
            source_hash=source_hash,
            total_chunks=len(chunks),
            total_tools=total_tools,
            dry_run=dry_run,
        )

        # 5. Concurrent chunk mining.
        sem = asyncio.Semaphore(max(1, self.config.max_concurrency))

        async def _run_chunk(chunk: ToolChunk):
            async with sem:
                return await self.mine_chunk(chunk, dry_run=dry_run)

        try:
            results = await asyncio.gather(
                *(_run_chunk(c) for c in chunks),
                return_exceptions=True,
            )
        except Exception as exc:  # pragma: no cover — gather absorbs
            self.emitter.emit(
                Stage.MINING_FAILED,
                server_id=server_id,
                error=str(exc),
            )
            raise

        accepted: list[UseCaseRecord] = []
        for r in results:
            if isinstance(r, BaseException):
                # Per-chunk failure already emitted CHUNK_FAILED + diagnostic.
                continue
            chunk_accepted, _chunk_rejected = r
            accepted.extend(chunk_accepted)

        # 6. MINING_COMPLETED.
        duration_ms = int((time.monotonic() - started) * 1000)
        self.emitter.emit(
            Stage.MINING_COMPLETED,
            server_id=server_id,
            source_hash=source_hash,
            total_chunks=len(chunks),
            total_tools=total_tools,
            total_use_cases=len(accepted),
            tokens_in=self.emitter.tokens_in,
            tokens_out=self.emitter.tokens_out,
            cached_tokens_in=self.emitter.cached_tokens_in,
            duration_ms=duration_ms,
        )
        return accepted

    async def mine_chunk(
        self,
        chunk: ToolChunk,
        *,
        dry_run: bool = False,
    ) -> tuple[list[UseCaseRecord], list[dict]]:
        """Run one LLM extraction over a single chunk.

        Returns ``(accepted_records, rejected_records_with_reasons)``.
        Per-chunk cache hits return ``([], [])``; the caller can rely on
        the source-level cache (or DB queries) to materialize records
        from earlier runs.
        """

        chunk_started = time.monotonic()

        # 1. CHUNK_STARTED
        self.emitter.emit(
            Stage.CHUNK_STARTED,
            server_id=chunk.server_id,
            chunk_id=chunk.chunk_id,
            group_label=chunk.group_label,
            tool_count=len(chunk.tools),
            chunk_hash=chunk.chunk_hash,
        )

        try:
            # 2. Per-chunk cache check (skip on dry_run so we still surface
            #    the chunk in observability without persistence).
            if not dry_run and await self.db.is_chunk_cached(
                chunk.chunk_hash,
                self.config.model_id,
                self.config.prompt_version,
            ):
                self.emitter.emit(
                    Stage.CHUNK_COMPLETED,
                    server_id=chunk.server_id,
                    chunk_id=chunk.chunk_id,
                    group_label=chunk.group_label,
                    tool_count=len(chunk.tools),
                    use_cases_emitted=0,
                    use_cases_rejected=0,
                    cached=True,
                    duration_ms=int((time.monotonic() - chunk_started) * 1000),
                )
                return ([], [])

            # 3. Dry-run path: log a CHUNK_COMPLETED and bail without LLM.
            if dry_run:
                self.emitter.emit(
                    Stage.CHUNK_COMPLETED,
                    server_id=chunk.server_id,
                    chunk_id=chunk.chunk_id,
                    group_label=chunk.group_label,
                    tool_count=len(chunk.tools),
                    use_cases_emitted=0,
                    use_cases_rejected=0,
                    dry_run=True,
                    duration_ms=int((time.monotonic() - chunk_started) * 1000),
                )
                return ([], [])

            # 4. LLM call.
            messages = self._build_messages(chunk)
            response = await self.llm.call(
                messages,
                tools=[USE_CASE_EXTRACTION_TOOL],
                force_tool=USE_CASE_EXTRACTION_TOOL.name,
                max_tokens=2048,
            )

            # 5/6/7. Parse + validate.
            raw_records = []
            args = response.tool_arguments or {}
            if isinstance(args, dict):
                raw_records = list(args.get("use_cases") or [])

            accepted: list[UseCaseRecord] = []
            rejected: list[dict] = []
            chunk_tool_names = {
                t.get("name") for t in chunk.tools if t.get("name")
            }

            generated_by = (
                f"{self.config.model_id}@{self.config.prompt_version}"
            )

            for raw in raw_records:
                rejection = self._validate_raw(raw, chunk_tool_names)
                if rejection is not None:
                    self.emitter.emit(
                        Stage.RECORD_REJECTED,
                        server_id=chunk.server_id,
                        chunk_id=chunk.chunk_id,
                        reason=rejection["reason"],
                        offending=rejection.get("offending"),
                        raw=raw,
                    )
                    rejected.append({"raw": raw, **rejection})
                    continue

                description = raw["description"]
                linked = list(raw["linked_tool_names"])
                prerequisites = list(raw.get("prerequisites") or [])
                confidence = float(raw["confidence"])

                seq = len(accepted)
                short = _chunk_short(chunk.chunk_id, chunk.server_id)
                uc_id = f"uc-{chunk.server_id}-{short}-{seq}"

                use_case_hash = _compute_use_case_hash(
                    server_id=chunk.server_id,
                    source_hash=chunk.source_hash,
                    description=description,
                    linked_tool_names=linked,
                )

                record = UseCaseRecord(
                    id=uc_id,
                    server_id=chunk.server_id,
                    source_hash=chunk.source_hash,
                    chunk_id=chunk.chunk_id,
                    description=description,
                    linked_tool_names=linked,
                    prerequisites=prerequisites,
                    confidence=confidence,
                    generated_by=generated_by,
                    generated_at=_utcnow(),
                    use_case_hash=use_case_hash,
                )
                accepted.append(record)

            # 8. Persist accepted records + mark chunk cached.
            for rec in accepted:
                await self.db.save_use_case(rec)
            await self.db.mark_chunk_cached(
                chunk,
                self.config.model_id,
                self.config.prompt_version,
            )

            # 9. CHUNK_COMPLETED.
            duration_ms = int((time.monotonic() - chunk_started) * 1000)
            self.emitter.emit(
                Stage.CHUNK_COMPLETED,
                server_id=chunk.server_id,
                chunk_id=chunk.chunk_id,
                group_label=chunk.group_label,
                tool_count=len(chunk.tools),
                use_cases_emitted=len(accepted),
                use_cases_rejected=len(rejected),
                rejection_reasons=[r["reason"] for r in rejected],
                usage=response.usage.model_dump(),
                duration_ms=duration_ms,
            )

            return accepted, rejected

        except Exception as exc:
            # 10. Diagnostic + CHUNK_FAILED then re-raise so mine_source
            #     can tally via gather(return_exceptions=True).
            duration_ms = int((time.monotonic() - chunk_started) * 1000)
            try:
                self.emitter.write_diagnostic(
                    chunk.chunk_id,
                    {
                        "chunk_id": chunk.chunk_id,
                        "chunk_hash": chunk.chunk_hash,
                        "server_id": chunk.server_id,
                        "group_label": chunk.group_label,
                        "tool_count": len(chunk.tools),
                        "tool_names": [
                            t.get("name") for t in chunk.tools
                        ],
                        "model_id": self.config.model_id,
                        "prompt_version": self.config.prompt_version,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception:  # pragma: no cover — diagnostic best-effort
                pass

            self.emitter.emit(
                Stage.CHUNK_FAILED,
                server_id=chunk.server_id,
                chunk_id=chunk.chunk_id,
                group_label=chunk.group_label,
                tool_count=len(chunk.tools),
                error=str(exc),
                error_type=type(exc).__name__,
                duration_ms=duration_ms,
            )
            raise

    # ---------------- internal ----------------

    def _build_messages(self, chunk: ToolChunk) -> list[Message]:
        catalog = _render_tool_catalog(chunk.tools)
        user = USER_PROMPT_TEMPLATE_V1.format(
            server_id=chunk.server_id,
            group_label=chunk.group_label,
            tool_catalog=catalog,
        )
        return [
            Message(role="system", content=SYSTEM_PROMPT_V1),
            Message(role="user", content=user),
        ]

    def _validate_raw(
        self,
        raw: dict,
        chunk_tool_names: set[str],
    ) -> dict | None:
        """Return a rejection dict, or ``None`` if the record is valid."""

        if not isinstance(raw, dict):
            return {
                "reason": "malformed_record",
                "offending": {"type": type(raw).__name__},
            }

        description = raw.get("description")
        linked = raw.get("linked_tool_names")
        confidence = raw.get("confidence")

        if not isinstance(description, str) or not (
            self.config.description_min_chars
            <= len(description)
            <= self.config.description_max_chars
        ):
            length = len(description) if isinstance(description, str) else None
            return {
                "reason": "length_out_of_bounds",
                "offending": {"description_length": length},
            }

        if not isinstance(linked, list) or len(linked) == 0:
            return {
                "reason": "empty_tool_names",
                "offending": {"linked_tool_names": linked},
            }

        unresolved = [
            n for n in linked if not isinstance(n, str) or n not in chunk_tool_names
        ]
        if unresolved:
            return {
                "reason": "tool_name_unresolved",
                "offending": {"unresolved": unresolved},
            }

        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            return {
                "reason": "confidence_out_of_range",
                "offending": {"confidence": confidence},
            }
        if not (0.0 <= conf <= 1.0):
            return {
                "reason": "confidence_out_of_range",
                "offending": {"confidence": conf},
            }

        return None
