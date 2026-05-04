"""Phase I — Skill package validation.

Three deterministic passes over a :class:`SkillPackage` emitted by Phase H
(skill synthesis). No LLM calls. The validator's primary job is catching
the dominant LLM failure mode -- invented (hallucinated) tool names --
without an Atlas.

Public surface
--------------

* :class:`SkillReference` -- one auxiliary file under ``references/``.
* :class:`SkillPackage` -- the structured payload Phase H emits and Phase J
  writes to disk. Defined here for now; Phase H will import this shape.
* :class:`ValidationFailure` -- one structured pass failure.
* :class:`ValidationReport` -- aggregate result over all three passes.
* :func:`validate_skill` -- the entrypoint.

Validation passes
-----------------

``spec-conformance``
    ``name`` matches ``^[a-z][a-z0-9-]{1,63}$``; ``description`` length
    within configured bounds; ``body_markdown`` does NOT begin with a
    YAML frontmatter delimiter (Phase H is responsible for that, the
    body is procedural prose only).

``tool-grounding``
    Every entry in ``tool_dependencies`` and every backticked identifier
    of length >= 3 in ``body_markdown`` resolves into the harvested tool
    name set. ``tool_dependencies`` must be non-empty.

``length-bounds``
    ``body_markdown`` length within configured bounds; each
    ``references[*].content`` length within configured bounds.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


__all__ = [
    "SkillReference",
    "SkillPackage",
    "ValidationFailure",
    "ValidationReport",
    "validate_skill",
    "PassName",
]


# ---------------------------------------------------------------------------
# Skill package shape (Phase H will mirror / import this)
# ---------------------------------------------------------------------------


class SkillReference(BaseModel):
    """One auxiliary reference file emitted under ``references/``."""

    filename: str
    content: str


class SkillPackage(BaseModel):
    """Structured skill payload prior to on-disk SKILL.md assembly.

    Field-level constraints are intentionally loose so that the validator
    -- not pydantic -- is the source of truth for validity. The validator
    must be able to inspect malformed packages and return a structured
    report.
    """

    skill_id: str
    name: str
    description: str
    body_markdown: str
    tool_dependencies: list[str] = Field(default_factory=list)
    references: list[SkillReference] = Field(default_factory=list)

    # provenance
    server_id: str
    source_hash: str
    cluster_id: str
    cluster_hash: str
    use_case_ids: list[str] = Field(default_factory=list)
    generated_by: str
    generated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


PassName = Literal["spec-conformance", "tool-grounding", "length-bounds"]

_ALL_PASSES: tuple[PassName, ...] = (
    "spec-conformance",
    "tool-grounding",
    "length-bounds",
)


class ValidationFailure(BaseModel):
    """One structured failure with a machine-friendly reason key."""

    pass_name: PassName
    reason: str
    details: dict = Field(default_factory=dict)


class ValidationReport(BaseModel):
    """Aggregate result across all three deterministic passes."""

    passed: bool
    failures: list[ValidationFailure] = Field(default_factory=list)
    passes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

# Backticked identifiers in markdown body. The validator treats any
# backticked identifier of length >= 3 as a candidate tool reference.
# Real tool names usually clear that bar; common false-positive
# identifiers (``id``, ``it``) sit just below it.
_TOOL_REF_PATTERN = re.compile(r"`([a-zA-Z_][a-zA-Z0-9_-]*)`")
_MIN_REFERENCED_NAME_LEN = 3


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_skill(
    package: SkillPackage,
    harvested_tool_names: set[str] | list[str],
    *,
    description_min_chars: int = 50,
    description_max_chars: int = 600,
    body_min_chars: int = 100,
    body_max_chars: int = 8000,
    reference_min_chars: int = 50,
    reference_max_chars: int = 12000,
) -> ValidationReport:
    """Run all three deterministic passes on ``package``.

    All passes are evaluated even when one fails so the report enumerates
    every distinct problem in a single call.
    """

    harvested: set[str] = set(harvested_tool_names)
    failures: list[ValidationFailure] = []

    failures.extend(
        _check_spec_conformance(
            package,
            description_min_chars=description_min_chars,
            description_max_chars=description_max_chars,
        )
    )
    failures.extend(_check_tool_grounding(package, harvested))
    failures.extend(
        _check_length_bounds(
            package,
            body_min_chars=body_min_chars,
            body_max_chars=body_max_chars,
            reference_min_chars=reference_min_chars,
            reference_max_chars=reference_max_chars,
        )
    )

    failed_passes = {f.pass_name for f in failures}
    passes = [p for p in _ALL_PASSES if p not in failed_passes]

    return ValidationReport(
        passed=len(failures) == 0,
        failures=failures,
        passes=passes,
    )


# ---------------------------------------------------------------------------
# Pass implementations
# ---------------------------------------------------------------------------


def _check_spec_conformance(
    package: SkillPackage,
    *,
    description_min_chars: int,
    description_max_chars: int,
) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []

    if not _NAME_PATTERN.fullmatch(package.name):
        failures.append(
            ValidationFailure(
                pass_name="spec-conformance",
                reason="name_format",
                details={"name": package.name},
            )
        )

    desc_len = len(package.description)
    if not (description_min_chars <= desc_len <= description_max_chars):
        failures.append(
            ValidationFailure(
                pass_name="spec-conformance",
                reason="description_length_out_of_bounds",
                details={
                    "length": desc_len,
                    "min": description_min_chars,
                    "max": description_max_chars,
                },
            )
        )

    # Phase H assembles SKILL.md by combining frontmatter (built from
    # package fields) with ``body_markdown``. If the body itself starts
    # with ``---\n`` Phase H mis-emitted; flag it before it produces
    # double frontmatter on disk.
    if package.body_markdown.startswith("---\n"):
        failures.append(
            ValidationFailure(
                pass_name="spec-conformance",
                reason="body_starts_with_frontmatter",
                details={},
            )
        )

    return failures


def _check_tool_grounding(
    package: SkillPackage,
    harvested: set[str],
) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []

    if not package.tool_dependencies:
        failures.append(
            ValidationFailure(
                pass_name="tool-grounding",
                reason="empty_tool_dependencies",
                details={},
            )
        )

    # tool_dependencies is the authoritative declaration; any name there
    # that does not resolve in the harvested catalog is a hard failure.
    declared = set(package.tool_dependencies)
    unresolved_declared = sorted(declared - harvested)

    if unresolved_declared:
        failures.append(
            ValidationFailure(
                pass_name="tool-grounding",
                reason="tool_name_unresolved",
                details={"unresolved": unresolved_declared},
            )
        )

    # Body backticks legitimately wrap parameter names (e.g., `petId`),
    # JSON keys, paths, and short variable names. A body-backticked
    # identifier is flagged ONLY when it is "obviously tool-shaped":
    # length >= 8 AND contains an underscore (snake_case). That pattern
    # captures the dominant LLM hallucination (`github_invent_tool`) while
    # not flagging parameter names like `petId` or short variables.
    body_candidates = {
        name
        for name in _TOOL_REF_PATTERN.findall(package.body_markdown)
        if len(name) >= 8
        and "_" in name
        and name not in harvested
        and name not in declared
    }
    if body_candidates:
        failures.append(
            ValidationFailure(
                pass_name="tool-grounding",
                reason="tool_name_unresolved",
                details={"unresolved": sorted(body_candidates)},
            )
        )

    return failures


def _check_length_bounds(
    package: SkillPackage,
    *,
    body_min_chars: int,
    body_max_chars: int,
    reference_min_chars: int,
    reference_max_chars: int,
) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []

    body_len = len(package.body_markdown)
    if not (body_min_chars <= body_len <= body_max_chars):
        failures.append(
            ValidationFailure(
                pass_name="length-bounds",
                reason="body_length_out_of_bounds",
                details={
                    "length": body_len,
                    "min": body_min_chars,
                    "max": body_max_chars,
                },
            )
        )

    for index, reference in enumerate(package.references):
        ref_len = len(reference.content)
        if not (reference_min_chars <= ref_len <= reference_max_chars):
            failures.append(
                ValidationFailure(
                    pass_name="length-bounds",
                    reason="reference_length_out_of_bounds",
                    details={
                        "index": index,
                        "filename": reference.filename,
                        "length": ref_len,
                        "min": reference_min_chars,
                        "max": reference_max_chars,
                    },
                )
            )

    return failures
