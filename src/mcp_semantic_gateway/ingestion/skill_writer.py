"""Skill package writer (Phase J).

Writes agent-skills-spec-conformant skill packages to the project-local
cache layout:

    <project_root>/<output_dir>/skills/<server_id>/<source_hash[:12]>/
        <skill-id>/v1/
            SKILL.md
            .meta.json
            references/      # only when references != []
                <filename>

All file writes are atomic (temp file + os.replace) so a partial write
cannot be observed by `Collector.collect_skills()`.

This module is intentionally self-contained: the Phase I validator is
being built in parallel and Phase H will reconcile the canonical
`SkillPackage` definition. The shape defined here is a SUPERSET of the
fields the writer needs.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Canonical SkillPackage lives in skill_validator.py. Re-export here so
# existing call sites (and tests) that import from skill_writer still work.
from mcp_semantic_gateway.ingestion.skill_validator import (  # noqa: F401
    SkillPackage,
    SkillReference,
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _strip_hash_prefix(source_hash: str) -> str:
    """Strip a leading algorithm prefix like ``sha256:`` if present."""
    if ":" in source_hash:
        return source_hash.split(":", 1)[1]
    return source_hash


def _short_hash(source_hash: str) -> str:
    return _strip_hash_prefix(source_hash)[:12]


def _server_root(project_root: Path, output_dir: str, server_id: str, source_hash: str) -> Path:
    return project_root / output_dir / "skills" / server_id / _short_hash(source_hash)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write *data* to *target* atomically via a sibling .tmp file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    # Write fully then replace; os.replace is atomic on POSIX/NTFS.
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def _atomic_write_text(target: Path, text: str) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# SKILL.md rendering
# ---------------------------------------------------------------------------


def _render_skill_md(package: SkillPackage) -> str:
    """Render the SKILL.md text deterministically.

    YAML frontmatter is hand-emitted (no YAML lib) so that byte-for-byte
    output is stable across runs and Python versions:

      - description is always a literal block scalar (``|``) with each
        body line indented 2 spaces.
      - allowed-tools is omitted entirely when tool_dependencies is empty.
      - tool_dependencies order is preserved verbatim.
      - The file ends with exactly one trailing newline.
    """
    lines: list[str] = ["---", f"name: {package.name}", "description: |"]
    desc_lines = package.description.split("\n") if package.description else [""]
    for dl in desc_lines:
        lines.append(f"  {dl}" if dl else "  ")

    if package.tool_dependencies:
        lines.append("allowed-tools:")
        for tool in package.tool_dependencies:
            lines.append(f"  - {tool}")

    lines.append("---")
    lines.append("")  # blank line between frontmatter and body

    body = package.body_markdown.rstrip("\n")
    rendered = "\n".join(lines) + "\n" + body + "\n"
    return rendered


# ---------------------------------------------------------------------------
# .meta.json rendering
# ---------------------------------------------------------------------------


def _format_generated_at(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    # Normalise to UTC and emit with trailing Z (ISO-8601, no microseconds for stability).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _split_generated_by(generated_by: str) -> tuple[str, str]:
    if "@" in generated_by:
        model, prompt_version = generated_by.split("@", 1)
        return model, prompt_version
    return generated_by, "v1"


def _render_meta_json(
    package: SkillPackage,
    *,
    validation_passes: list[str],
) -> bytes:
    """Build the .meta.json payload.

    Field order follows the design doc exactly. We rely on Python 3.12's
    insertion-ordered dicts plus ``json.dumps`` without ``sort_keys`` to
    keep that order on disk.
    """
    model, prompt_version = _split_generated_by(package.generated_by)
    payload: dict = {
        "schema_version": 1,
        "skill_id": package.skill_id,
        "skill_version": 1,
        "source": {
            "server_id": package.server_id,
            "source_hash": package.source_hash,
            "source_hash_short": _short_hash(package.source_hash),
        },
        "cluster": {
            "cluster_id": package.cluster_id,
            "cluster_hash": package.cluster_hash,
            "use_case_ids": list(package.use_case_ids),
        },
        "tool_dependencies": list(package.tool_dependencies),
        "generation": {
            "model": model,
            "prompt_version": prompt_version,
            "generated_at": _format_generated_at(package.generated_at),
            "validation_passes": list(validation_passes),
        },
        "status": "published",
    }
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Collision handling
# ---------------------------------------------------------------------------


def _existing_cluster_hash(meta_path: Path) -> Optional[str]:
    """Return the cluster_hash recorded in an existing .meta.json, or None.

    None means: file missing, unparseable, or no cluster_hash field. Callers
    treat None as "this slot is occupied by something we can't reason
    about" -> suffix and move on (never overwrite).
    """
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cluster = data.get("cluster")
    if not isinstance(cluster, dict):
        return None
    ch = cluster.get("cluster_hash")
    return ch if isinstance(ch, str) else None


def _resolve_skill_dir(server_root: Path, skill_id: str, cluster_hash: str) -> tuple[Path, bool]:
    """Resolve the on-disk <skill-id>/ dir, suffixing on hard collisions.

    Returns (skill_dir, is_idempotent_rewrite).

    - If <skill-id>/v1/SKILL.md is absent: use the bare slot.
    - If present AND .meta.json records the SAME cluster_hash: idempotent
      rewrite — same slot, overwrite atomically.
    - Otherwise (different cluster_hash, or meta missing/corrupt): try
      <skill-id>-2, -3, ... until a free or matching slot is found.
    """
    candidate = server_root / skill_id
    skill_md = candidate / "v1" / "SKILL.md"
    if not skill_md.exists():
        return candidate, False

    existing = _existing_cluster_hash(candidate / "v1" / ".meta.json")
    if existing == cluster_hash:
        return candidate, True

    suffix = 2
    while True:
        cand = server_root / f"{skill_id}-{suffix}"
        cand_skill_md = cand / "v1" / "SKILL.md"
        if not cand_skill_md.exists():
            return cand, False
        existing = _existing_cluster_hash(cand / "v1" / ".meta.json")
        if existing == cluster_hash:
            return cand, True
        suffix += 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_package(
    package: SkillPackage,
    *,
    project_root: Path,
    output_dir: str = ".mcp_semantic_gateway",
    validation_passes: list[str] | None = None,
) -> Path:
    """Write *package* to disk under the project-local cache layout.

    Returns the absolute path to the ``v1/`` directory.

    Atomicity: every file (SKILL.md, .meta.json, each reference) is
    written to a sibling ``.tmp`` file and then ``os.replace``-d into
    place. The collector therefore never sees a half-written SKILL.md.
    """
    server_root = _server_root(project_root, output_dir, package.server_id, package.source_hash)
    skill_dir, _is_rewrite = _resolve_skill_dir(server_root, package.skill_id, package.cluster_hash)
    v1_dir = skill_dir / "v1"
    v1_dir.mkdir(parents=True, exist_ok=True)

    skill_md_text = _render_skill_md(package)
    _atomic_write_text(v1_dir / "SKILL.md", skill_md_text)

    meta_bytes = _render_meta_json(package, validation_passes=validation_passes or [])
    _atomic_write_bytes(v1_dir / ".meta.json", meta_bytes)

    refs_dir = v1_dir / "references"
    desired_filenames = {ref.filename for ref in package.references}

    # Prune stale reference files from a previous rewrite: anything in
    # references/ that the new package does NOT declare must be removed.
    # Keeps disk content == package.references content even when an
    # idempotent rewrite shrinks or changes the reference set.
    if refs_dir.exists():
        for existing in refs_dir.iterdir():
            if existing.is_file() and existing.name not in desired_filenames:
                try:
                    existing.unlink()
                except OSError:
                    pass
        # If the new package has no references at all, drop the empty dir
        # so collectors never see a phantom references/ folder.
        if not desired_filenames:
            try:
                refs_dir.rmdir()
            except OSError:
                pass

    if package.references:
        refs_dir.mkdir(parents=True, exist_ok=True)
        for ref in package.references:
            _atomic_write_text(refs_dir / ref.filename, ref.content)

    return v1_dir


def write_diagnostic(
    package: SkillPackage,
    *,
    project_root: Path,
    output_dir: str = ".mcp_semantic_gateway",
    reasons: list[dict],
) -> Path:
    """Write a ``<skill-id>.diagnostic.json`` for a rejected package.

    The diagnostic lives as a sibling of the (would-be) ``<skill-id>/``
    directory — i.e. under
    ``<output_dir>/skills/<server_id>/<source_hash[:12]>/<skill-id>.diagnostic.json``
    — so a failed skill does not pollute a ``<skill-id>/`` slot that a
    later successful generation might want to occupy.
    """
    server_root = _server_root(project_root, output_dir, package.server_id, package.source_hash)
    server_root.mkdir(parents=True, exist_ok=True)
    target = server_root / f"{package.skill_id}.diagnostic.json"

    payload = {
        "schema_version": 1,
        "skill_id": package.skill_id,
        "source": {
            "server_id": package.server_id,
            "source_hash": package.source_hash,
            "source_hash_short": _short_hash(package.source_hash),
        },
        "cluster": {
            "cluster_id": package.cluster_id,
            "cluster_hash": package.cluster_hash,
            "use_case_ids": list(package.use_case_ids),
        },
        "tool_dependencies": list(package.tool_dependencies),
        "reasons": list(reasons),
        "status": "rejected",
    }
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(target, data)
    return target


__all__ = [
    "SkillPackage",
    "SkillReference",
    "write_package",
    "write_diagnostic",
]
