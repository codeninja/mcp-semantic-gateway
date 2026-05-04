"""Chunking of harvested tools for use-case mining.

Groups tools (from OpenAPI specs or live MCP servers) into deterministic,
content-addressed chunks suitable for per-chunk LLM calls. Each chunk
carries stable identifiers (`chunk_id`, `chunk_hash`) and the source-level
hash of the upstream surface so downstream callers can short-circuit on
unchanged inputs.

This module is self-contained: stdlib + pydantic only. It is consumed by
`ingestion.use_case_miner` (Phase C) and `ingestion.observability`
(Phase D); neither dependency is imported here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Iterable, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Public schema
# ---------------------------------------------------------------------------


class ToolChunk(BaseModel):
    """A deterministic group of harvested tool dicts ready for one LLM call."""

    chunk_id: str = Field(..., description="chunk-<server>-<group>-<idx>")
    server_id: str
    source_hash: str = Field(..., description="sha256 of the full upstream source")
    chunk_hash: str = Field(..., description="sha256 of the canonical (sorted) tool list")
    group_label: str = Field(..., description="tag name, path-prefix, name-prefix, or 'mixed'")
    tools: list[dict] = Field(..., description="full harvested tool dicts in input order")


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def _canonical_dump(obj: object) -> bytes:
    """Stable JSON serialization for hashing.

    Sort keys + tight separators avoid whitespace and ordering drift.
    """

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_source_hash_openapi(spec: dict) -> str:
    """Canonical sha256 of an OpenAPI spec dict."""

    return _sha256_hex(_canonical_dump(spec))


def compute_source_hash_mcp(tools: list[dict]) -> str:
    """Canonical sha256 of an MCP tool list keyed on (name, description).

    Sorted by `name` so upstream reordering does not invalidate the hash.
    """

    pairs = sorted(
        ((t.get("name", ""), t.get("description", "")) for t in tools),
        key=lambda p: p[0],
    )
    return _sha256_hex(_canonical_dump(pairs))


def compute_chunk_hash(tools: list[dict]) -> str:
    """Canonical sha256 over a tool list, normalized by sorting on `name`.

    The chunk's tool order is preserved on the `ToolChunk.tools` field;
    only the *hash* uses a sorted view so the value is stable across
    upstream reordering.
    """

    sorted_tools = sorted(tools, key=lambda t: t.get("name", ""))
    return _sha256_hex(_canonical_dump(sorted_tools))


# ---------------------------------------------------------------------------
# Slugification + chunk id construction
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9_-]")


def _slugify(label: str) -> str:
    if not label:
        return "mixed"
    lowered = label.lower()
    return _SLUG_RE.sub("_", lowered) or "mixed"


def _chunk_id(server_id: str, group_label: str, idx: int) -> str:
    return f"chunk-{server_id}-{_slugify(group_label)}-{idx}"


# ---------------------------------------------------------------------------
# Grouping rules
# ---------------------------------------------------------------------------


def _tool_tags(tool: dict) -> list[str]:
    annotations = tool.get("annotations") or {}
    tags = annotations.get("tags") or []
    return [t for t in tags if isinstance(t, str) and t]


def _tool_path_prefix(tool: dict) -> str | None:
    annotations = tool.get("annotations") or {}
    path = annotations.get("path")
    if not isinstance(path, str) or not path:
        return None
    # Strip leading slashes and split.
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    return segments[0]


def _group_openapi(tools: list[dict]) -> list[tuple[str, list[dict]]]:
    """Return ordered (group_label, tools) pairs for OpenAPI chunking."""

    # Precedence 1: tag grouping when ANY tool has tags.
    any_tagged = any(_tool_tags(t) for t in tools)
    if any_tagged:
        return _group_by_tag(tools)

    # Precedence 2: path-prefix grouping when ANY tool has a path.
    any_pathed = any(_tool_path_prefix(t) for t in tools)
    if any_pathed:
        return _group_by_path_prefix(tools)

    # Precedence 3: ordered fixed-size — single mixed bucket.
    return [("mixed", list(tools))]


def _group_by_tag(tools: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group OpenAPI tools by their first (alphabetically) tag.

    Tools without tags fall into 'mixed', mirroring the lone-tool rule.
    Returns groups in deterministic order: alphabetically by label, with
    'mixed' last so it stays out of the way of named groups.
    """

    groups: dict[str, list[dict]] = defaultdict(list)
    for tool in tools:
        tags = _tool_tags(tool)
        if not tags:
            groups["mixed"].append(tool)
            continue
        label = sorted(tags)[0]
        groups[label].append(tool)
    return _ordered_groups(groups)


def _group_by_path_prefix(tools: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group OpenAPI tools by the first segment of `annotations.path`.

    Path-less tools (or those whose first segment is unique) fall through
    to 'mixed' so single-occurrence prefixes don't pollute group labels.
    """

    # First pass: tally first-segment occurrences.
    counts: dict[str, int] = defaultdict(int)
    for tool in tools:
        seg = _tool_path_prefix(tool)
        if seg is not None:
            counts[seg] += 1

    groups: dict[str, list[dict]] = defaultdict(list)
    for tool in tools:
        seg = _tool_path_prefix(tool)
        if seg is None or counts[seg] < 2:
            groups["mixed"].append(tool)
        else:
            groups[seg].append(tool)
    return _ordered_groups(groups)


def _group_mcp(tools: list[dict]) -> list[tuple[str, list[dict]]]:
    """Return ordered (group_label, tools) pairs for live MCP chunking."""

    return _group_by_name_prefix(tools)


def _tokenize(name: str) -> list[str]:
    if not name:
        return []
    return [tok for tok in re.split(r"[_\-]", name) if tok]


def _group_by_name_prefix(tools: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group MCP tools by the longest delimiter-bounded prefix shared by >=2 tools.

    Tools with no shared prefix fall into 'mixed'.
    """

    token_lists = [_tokenize(t.get("name", "")) for t in tools]

    # Count how many tools share each token-prefix (length >= 1).
    prefix_counts: dict[tuple[str, ...], int] = defaultdict(int)
    for toks in token_lists:
        for length in range(1, len(toks)):  # exclude full name (length == len(toks))
            prefix_counts[tuple(toks[:length])] += 1

    # For each tool, pick the longest prefix shared by >=2 tools.
    assignments: list[tuple[str, ...] | None] = []
    for toks in token_lists:
        chosen: tuple[str, ...] | None = None
        for length in range(len(toks) - 1, 0, -1):
            prefix = tuple(toks[:length])
            if prefix_counts.get(prefix, 0) >= 2:
                chosen = prefix
                break
        assignments.append(chosen)

    # A prefix only counts if at least two tools were actually assigned to it
    # (a longer shared prefix may have stolen them).
    label_counts: dict[tuple[str, ...], int] = defaultdict(int)
    for assignment in assignments:
        if assignment is not None:
            label_counts[assignment] += 1

    groups: dict[str, list[dict]] = defaultdict(list)
    for tool, assignment in zip(tools, assignments):
        if assignment is None or label_counts[assignment] < 2:
            groups["mixed"].append(tool)
        else:
            label = "_".join(assignment)
            groups[label].append(tool)

    return _ordered_groups(groups)


def _ordered_groups(groups: dict[str, list[dict]]) -> list[tuple[str, list[dict]]]:
    """Stable group ordering: named groups alphabetically, 'mixed' last."""

    named = sorted((k for k in groups if k != "mixed"))
    ordered: list[tuple[str, list[dict]]] = [(k, groups[k]) for k in named]
    if "mixed" in groups and groups["mixed"]:
        ordered.append(("mixed", groups["mixed"]))
    return ordered


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def chunk_tools(
    tools: list[dict],
    *,
    server_id: str,
    source_hash: str,
    source_kind: Literal["openapi", "mcp"],
    chunk_size: int = 12,
) -> list[ToolChunk]:
    """Group `tools` into deterministic `ToolChunk` records.

    Args:
        tools: harvested tool dicts (from `Collector.collect_all()`).
        server_id: stable upstream server identifier.
        source_hash: sha256 of the full upstream source. Caller computes
            via `compute_source_hash_openapi` / `compute_source_hash_mcp`.
        source_kind: 'openapi' selects tag/path/mixed precedence;
            'mcp' selects name-prefix/mixed precedence.
        chunk_size: maximum tools per chunk; oversized groups are split.

    Returns:
        A list of `ToolChunk` records in stable order.
    """

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    if not tools:
        return []

    if source_kind == "openapi":
        groups = _group_openapi(tools)
    elif source_kind == "mcp":
        groups = _group_mcp(tools)
    else:  # pragma: no cover - guarded by Literal
        raise ValueError(f"Unknown source_kind: {source_kind!r}")

    chunks: list[ToolChunk] = []
    for group_label, group_tools in groups:
        for idx, slice_ in enumerate(_chunk_slices(group_tools, chunk_size)):
            chunks.append(
                ToolChunk(
                    chunk_id=_chunk_id(server_id, group_label, idx),
                    server_id=server_id,
                    source_hash=source_hash,
                    chunk_hash=compute_chunk_hash(slice_),
                    group_label=group_label,
                    tools=list(slice_),
                )
            )
    return chunks


def _chunk_slices(items: list[dict], size: int) -> Iterable[list[dict]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
