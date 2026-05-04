"""Phase H — Skill synthesis.

For each :class:`UseCaseCluster`, call the configured LLM with a forced
``emit_skill_package`` tool call and assemble a :class:`SkillPackage`.
The validator (Phase I) gates publication; the writer (Phase J) lands
the package on disk.

Caching: a project-local cache hit happens when a previously-written
``.meta.json`` records the same
``(server_id, source_hash, cluster_hash, model_id, prompt_version)``.
That tuple is the durable cache key per design S-9.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field

from mcp_semantic_gateway.ingestion.observability import EventEmitter, Stage
from mcp_semantic_gateway.ingestion.skill_clusterer import UseCaseCluster
from mcp_semantic_gateway.ingestion.skill_validator import (
    SkillPackage,
    SkillReference,
    ValidationReport,
    validate_skill,
)
from mcp_semantic_gateway.ingestion.skill_writer import (
    write_diagnostic,
    write_package,
)
from mcp_semantic_gateway.ingestion.use_case_miner import UseCaseRecord
from mcp_semantic_gateway.llm.base import LLMProvider, Message, ToolSpec


__all__ = [
    "SkillSynthesizerConfig",
    "SkillSynthesizer",
    "SKILL_EMISSION_TOOL",
    "SYSTEM_PROMPT_V1",
    "USER_PROMPT_TEMPLATE_V1",
]


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


SKILL_EMISSION_TOOL = ToolSpec(
    name="emit_skill_package",
    description=(
        "Emit one agent-skills-spec-conformant skill package for the supplied "
        "cluster. Reference tool names verbatim from the harvested catalog; "
        "any unknown name will be rejected by deterministic validation."
    ),
    input_schema={
        "type": "object",
        "required": ["name", "description", "body_markdown", "tool_dependencies"],
        "properties": {
            "name": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9-]{1,63}$",
                "description": "Kebab-case skill id, 2-64 chars, must match the regex.",
            },
            "description": {
                "type": "string",
                "minLength": 50,
                "maxLength": 600,
                "description": "1-3 sentences, agent-vocabulary, summarising what the skill does.",
            },
            "body_markdown": {
                "type": "string",
                "minLength": 100,
                "description": (
                    "Procedural markdown body. Reference tool names with single "
                    "backticks. Sections like 'When to use this skill' and "
                    "'Procedure' are expected. Do NOT emit YAML frontmatter — "
                    "the writer assembles it from the other fields."
                ),
            },
            "tool_dependencies": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": "Tool names from the harvested catalog the skill calls.",
            },
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["filename", "content"],
                    "properties": {
                        "filename": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
                "description": "Optional auxiliary reference files.",
            },
        },
    },
)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


SYSTEM_PROMPT_V1 = """You are an expert API analyst writing agent skills.

Given a CLUSTER of related USE CASES (real-world tasks) plus the CATALOG of available tools, produce ONE agent-skills-specification skill that bundles the cluster's use cases into a coherent, procedural workflow an AI agent can follow.

Hard rules:
- Reference tool names VERBATIM from the supplied catalog. Any name you invent will be rejected.
- The body must be procedural markdown — sections like "When to use this skill" and "Procedure".
- Do NOT include YAML frontmatter; the writer assembles it from the other fields.
- Backtick tool names in the body so they're easy to scan.
- description must be 50-600 chars, name must be lowercase kebab-case (2-64 chars).
- tool_dependencies must list every tool the skill actually invokes.
- Output only via the emit_skill_package tool; no prose responses.

Quality bar: a competent agent reading the skill should be able to execute the workflow end-to-end without needing to consult the underlying API spec.
"""


USER_PROMPT_TEMPLATE_V1 = """Source: {server_id}
Cluster: {cluster_id} ({n_use_cases} use case(s))

Use cases in this cluster:
{use_case_block}

Available tools (catalog):
{tool_catalog}

Emit ONE skill package via the emit_skill_package tool that bundles these use cases."""


def _format_use_case_block(records: list[UseCaseRecord]) -> str:
    lines: list[str] = []
    for i, r in enumerate(records, start=1):
        tools = ", ".join(r.linked_tool_names) or "(none listed)"
        lines.append(f"{i}. {r.description}\n   tools: {tools}")
    return "\n".join(lines)


def _format_tool_catalog(tools: list[dict]) -> str:
    lines: list[str] = []
    for tool in tools:
        name = tool.get("name", "")
        desc = (tool.get("description") or "").strip()
        if len(desc) > 240:
            desc = desc[:237] + "..."
        required = (tool.get("inputSchema") or {}).get("required") or []
        req_block = ", ".join(required) if required else "(none)"
        lines.append(
            f"- {name}\n    description: {desc}\n    required params: {req_block}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cache lookup (project-local, reads .meta.json files)
# ---------------------------------------------------------------------------


def _meta_glob_for_source(
    project_root: Path,
    output_dir: str,
    server_id: str,
    source_hash: str,
) -> list[Path]:
    short = source_hash.split(":", 1)[-1][:12]
    root = project_root / output_dir / "skills" / server_id / short
    if not root.exists():
        return []
    return list(root.glob("*/v1/.meta.json"))


def _load_existing_cache_keys(
    project_root: Path,
    output_dir: str,
    server_id: str,
    source_hash: str,
) -> dict[tuple[str, str, str], Path]:
    """Return ``{(cluster_hash, model, prompt_version): meta_path}``."""

    out: dict[tuple[str, str, str], Path] = {}
    for meta_path in _meta_glob_for_source(project_root, output_dir, server_id, source_hash):
        try:
            data = json.loads(meta_path.read_text())
            cluster_hash = data.get("cluster", {}).get("cluster_hash")
            generation = data.get("generation", {})
            model = generation.get("model")
            prompt_version = generation.get("prompt_version")
            if cluster_hash and model and prompt_version:
                out[(cluster_hash, model, prompt_version)] = meta_path
        except (OSError, json.JSONDecodeError):
            continue
    return out


# ---------------------------------------------------------------------------
# Config + entrypoint
# ---------------------------------------------------------------------------


_KEBAB_PATTERN = re.compile(r"[^a-z0-9-]+")


def slugify_skill_id(name: str) -> str:
    s = name.strip().lower()
    s = _KEBAB_PATTERN.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "skill"
    if not s[0].isalpha():
        s = "s-" + s
    return s[:64]


class SkillSynthesizerConfig(BaseModel):
    model_id: str
    prompt_version: str = "v1"
    max_concurrency: int = 4
    description_min_chars: int = 50
    description_max_chars: int = 600
    body_min_chars: int = 100
    body_max_chars: int = 8000
    output_dir: str = ".mcp_semantic_gateway"
    project_root: Path = Field(default_factory=Path.cwd)


class SkillSynthesisResult(BaseModel):
    written: list[str] = Field(default_factory=list)         # paths
    rejected: list[dict] = Field(default_factory=list)
    cached: list[str] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)


class SkillSynthesizer:
    """Drive cluster → skill package → validate → write, with caching."""

    def __init__(
        self,
        llm: LLMProvider,
        emitter: EventEmitter,
        config: SkillSynthesizerConfig,
    ) -> None:
        self._llm = llm
        self._emitter = emitter
        self._config = config

    async def synthesize_for_source(
        self,
        *,
        server_id: str,
        source_hash: str,
        clusters: list[UseCaseCluster],
        records_by_id: dict[str, UseCaseRecord],
        harvested_tools: list[dict],
    ) -> SkillSynthesisResult:
        emitter = self._emitter
        cfg = self._config

        emitter.emit(
            Stage.CLUSTERING_COMPLETED,
            server_id=server_id,
            cluster_count=len(clusters),
            singleton_count=sum(1 for c in clusters if len(c.use_case_ids) == 1),
        )

        if not clusters:
            return SkillSynthesisResult()

        cache_keys = _load_existing_cache_keys(
            project_root=cfg.project_root,
            output_dir=cfg.output_dir,
            server_id=server_id,
            source_hash=source_hash,
        )

        result = SkillSynthesisResult()
        sem = asyncio.Semaphore(cfg.max_concurrency)
        harvested_names = {t["name"] for t in harvested_tools if "name" in t}

        async def _one(cluster: UseCaseCluster) -> None:
            async with sem:
                key = (cluster.cluster_hash, cfg.model_id, cfg.prompt_version)
                emitter.emit(
                    Stage.SKILL_SYNTHESIS_STARTED,
                    server_id=server_id,
                    cluster_id=cluster.cluster_id,
                    cluster_hash=cluster.cluster_hash,
                )
                if key in cache_keys:
                    emitter.emit(
                        Stage.SKILL_CACHE_HIT,
                        server_id=server_id,
                        cluster_id=cluster.cluster_id,
                        meta_path=str(cache_keys[key]),
                    )
                    result.cached.append(str(cache_keys[key]))
                    return

                start = time.monotonic()
                try:
                    package = await self._synthesize_one(
                        cluster=cluster,
                        records_by_id=records_by_id,
                        harvested_tools=harvested_tools,
                        server_id=server_id,
                        source_hash=source_hash,
                    )
                except Exception as exc:
                    emitter.emit(
                        Stage.SKILL_REJECTED,
                        server_id=server_id,
                        cluster_id=cluster.cluster_id,
                        reason="synthesis_error",
                        error=repr(exc),
                        duration_ms=int((time.monotonic() - start) * 1000),
                    )
                    result.errors.append(
                        {"cluster_id": cluster.cluster_id, "error": repr(exc)}
                    )
                    return

                emitter.emit(
                    Stage.SKILL_SYNTHESIZED,
                    server_id=server_id,
                    cluster_id=cluster.cluster_id,
                    skill_id=package.skill_id,
                    tool_count=len(package.tool_dependencies),
                )

                report = validate_skill(
                    package,
                    harvested_names,
                    description_min_chars=cfg.description_min_chars,
                    description_max_chars=cfg.description_max_chars,
                    body_min_chars=cfg.body_min_chars,
                    body_max_chars=cfg.body_max_chars,
                )
                emitter.emit(
                    Stage.SKILL_VALIDATED,
                    server_id=server_id,
                    cluster_id=cluster.cluster_id,
                    skill_id=package.skill_id,
                    passed=report.passed,
                    passes=list(report.passes),
                )

                if not report.passed:
                    reasons = [
                        {
                            "pass_name": f.pass_name,
                            "reason": f.reason,
                            "details": f.details,
                        }
                        for f in report.failures
                    ]
                    diag_path = write_diagnostic(
                        package,
                        project_root=cfg.project_root,
                        output_dir=cfg.output_dir,
                        reasons=reasons,
                    )
                    emitter.emit(
                        Stage.SKILL_REJECTED,
                        server_id=server_id,
                        cluster_id=cluster.cluster_id,
                        skill_id=package.skill_id,
                        reasons=[r["reason"] for r in reasons],
                        diagnostic_path=str(diag_path),
                    )
                    result.rejected.append(
                        {
                            "cluster_id": cluster.cluster_id,
                            "skill_id": package.skill_id,
                            "reasons": reasons,
                        }
                    )
                    return

                v1_dir = write_package(
                    package,
                    project_root=cfg.project_root,
                    output_dir=cfg.output_dir,
                    validation_passes=list(report.passes),
                )
                emitter.emit(
                    Stage.SKILL_WRITTEN,
                    server_id=server_id,
                    cluster_id=cluster.cluster_id,
                    skill_id=package.skill_id,
                    file_path=str(v1_dir / "SKILL.md"),
                    tool_count=len(package.tool_dependencies),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
                result.written.append(str(v1_dir / "SKILL.md"))

        await asyncio.gather(*[_one(c) for c in clusters], return_exceptions=False)

        emitter.emit(
            Stage.SYNTHESIS_COMPLETED,
            server_id=server_id,
            skills_written=len(result.written),
            skills_rejected=len(result.rejected),
            skills_cached=len(result.cached),
            errors=len(result.errors),
        )
        return result

    # ----- per-cluster ------------------------------------------------------

    async def _synthesize_one(
        self,
        *,
        cluster: UseCaseCluster,
        records_by_id: dict[str, UseCaseRecord],
        harvested_tools: list[dict],
        server_id: str,
        source_hash: str,
    ) -> SkillPackage:
        records = [records_by_id[uid] for uid in cluster.use_case_ids if uid in records_by_id]

        # Restrict the catalog to tools referenced in the cluster (union)
        # plus a reasonable buffer of remaining tools, capped to keep the
        # prompt size sane.
        union = set(cluster.tool_name_union)
        primary = [t for t in harvested_tools if t.get("name") in union]
        secondary = [
            t for t in harvested_tools if t.get("name") not in union
        ][:24]
        catalog_tools = primary + secondary

        user_prompt = USER_PROMPT_TEMPLATE_V1.format(
            server_id=server_id,
            cluster_id=cluster.cluster_id,
            n_use_cases=len(records),
            use_case_block=_format_use_case_block(records),
            tool_catalog=_format_tool_catalog(catalog_tools),
        )

        messages = [
            Message(role="system", content=SYSTEM_PROMPT_V1),
            Message(role="user", content=user_prompt),
        ]

        response = await self._llm.call(
            messages,
            tools=[SKILL_EMISSION_TOOL],
            force_tool="emit_skill_package",
            max_tokens=3072,
        )

        if not response.tool_arguments:
            raise ValueError(
                f"LLM returned no tool_arguments for cluster {cluster.cluster_id}"
            )

        args = response.tool_arguments
        name = args.get("name") or slugify_skill_id(cluster.cluster_id)
        skill_id = slugify_skill_id(name)

        references_raw: Iterable[dict] = args.get("references") or []
        references = [
            SkillReference(filename=r["filename"], content=r["content"])
            for r in references_raw
            if isinstance(r, dict) and "filename" in r and "content" in r
        ]

        return SkillPackage(
            skill_id=skill_id,
            name=name,
            description=args.get("description", ""),
            body_markdown=args.get("body_markdown", ""),
            tool_dependencies=list(args.get("tool_dependencies", []) or []),
            references=references,
            server_id=server_id,
            source_hash=source_hash,
            cluster_id=cluster.cluster_id,
            cluster_hash=cluster.cluster_hash,
            use_case_ids=list(cluster.use_case_ids),
            generated_by=f"{self._config.model_id}@{self._config.prompt_version}",
            generated_at=datetime.now(timezone.utc),
        )
