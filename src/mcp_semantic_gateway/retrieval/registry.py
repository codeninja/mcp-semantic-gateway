"""Tool registry: name-aware lookup over ``metadata.db``.

The registry computes **canonical** tool names according to the collision
policy below, then exposes lookup APIs that ``tools/call`` (in both the HTTP
server and the stdio proxy) uses to resolve a tool name to its source server
and execution metadata.

Naming rules (see ``proxy.namespace_collisions``):

* Server IDs are split on non-alphanumeric runs to produce a short prefix.
  A single-word server ID becomes its first 3 characters lowercased; a
  multi-word server ID becomes the first letter of each word, capped at 3.
* When a raw tool name is unique across servers, it is exposed as-is.
* When a raw name collides across servers, every colliding entry receives
  the prefix as ``{prefix}.{raw_name}``.
* When ``proxy.namespace_collisions`` is True, *every* tool is namespaced,
  even unique ones.
* All canonical names are clamped to 64 characters to satisfy MCP tool-name
  limits. If clamping causes additional collisions, *every* member of the
  collision group receives a deterministic ``.1`` / ``.2`` / ... suffix
  that itself fits within 64.

The registry is built in-memory at startup. Re-indexing requires
re-instantiating the registry; we don't yet hot-reload.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mcp_semantic_gateway.storage.metadata_db import MetadataDB, ToolRecord

MAX_TOOL_NAME = 64
NAMESPACE_SEP = "."
SUFFIX_SEP = "."


@dataclass(frozen=True)
class ToolHandle:
    """Resolved tool entry. ``canonical_name`` is what we expose to MCP
    clients; ``raw_name`` is the upstream's original name."""

    tool_id: str
    server_id: str
    canonical_name: str
    raw_name: str
    item_type: str
    description: Optional[str]
    input_schema: Optional[dict]
    annotations: Optional[dict]
    route_metadata: Optional[dict]


# ---------------------------------------------------------------------------
# Pure-function helpers (exposed for unit testing)
# ---------------------------------------------------------------------------


def derive_namespace_prefix(server_id: str) -> str:
    """Compute a short namespace prefix from a server ID.

    Splits on non-alphanumeric. A single word yields its first 3 letters; two
    or more words yield the first letter of each, capped at 3.
    """

    words = [w for w in re.split(r"[^a-zA-Z0-9]+", server_id) if w]
    if not words:
        raise ValueError(
            f"server_id {server_id!r} has no alphanumeric characters; "
            "cannot derive a namespace prefix"
        )
    if len(words) == 1:
        return words[0][:3].lower()
    return "".join(w[0] for w in words[:3]).lower()


def canonicalize_names(
    entries: List[Tuple[str, str]],
    *,
    force_namespace: bool = False,
) -> Dict[Tuple[str, str], str]:
    """Map ``(server_id, raw_name)`` to canonical name.

    Deterministic: the same input list always yields the same output mapping,
    regardless of dict iteration order, because we sort collision groups by
    ``(server_id, raw_name)`` before applying suffixes.
    """

    if not entries:
        return {}

    # Pass 1: count raw-name collisions across servers.
    raw_counts = Counter(name for _, name in entries)

    # Pass 2: tentative canonical names with namespacing + 64-char clamp.
    tentative: Dict[Tuple[str, str], str] = {}
    for sid, name in entries:
        if force_namespace or raw_counts[name] > 1:
            prefix = derive_namespace_prefix(sid)
            n = f"{prefix}{NAMESPACE_SEP}{name}"
        else:
            n = name
        if len(n) > MAX_TOOL_NAME:
            n = n[:MAX_TOOL_NAME]
        tentative[(sid, name)] = n

    # Pass 3: resolve post-clamp collisions with deterministic ``.N`` suffix.
    by_canon: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for k, v in tentative.items():
        by_canon[v].append(k)

    final: Dict[Tuple[str, str], str] = {}
    for canon, keys in by_canon.items():
        if len(keys) == 1:
            final[keys[0]] = canon
            continue
        # Stable order: sort by (server_id, raw_name). Every member of the
        # collision group receives a ``.N`` suffix starting at 1, so the
        # collision is visible in every name (no implicit "winner").
        keys.sort()
        for i, k in enumerate(keys, start=1):
            suffix = f"{SUFFIX_SEP}{i}"
            budget = MAX_TOOL_NAME - len(suffix)
            final[k] = canon[:budget] + suffix
    return final


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Async name-resolution registry over the persisted ``tools`` table."""

    def __init__(self, db_path: Path, *, force_namespace: bool = False):
        self.db = MetadataDB(db_path)
        self.force_namespace = force_namespace
        self._canon_to_tool_id: Dict[str, str] = {}
        self._raw_to_tool_ids: Dict[str, List[str]] = defaultdict(list)
        self._tool_id_to_canon: Dict[str, str] = {}
        # ``(server_id, raw_name) -> canonical`` for O(1) reverse lookup.
        # Used by SearchCore to rewrite ``tools/list`` names per row.
        self._server_raw_to_canon: Dict[Tuple[str, str], str] = {}
        self._initialized = False

    async def initialize(self) -> None:
        rows = await self.db.list_tool_summaries()
        # Filter to executable items. Skills and prompts are not callable as
        # ``tools/call`` targets, so they don't participate in canonical
        # naming. (They still live in the same table for embedding / discovery.)
        tool_rows = [r for r in rows if (r[3] or "tool") == "tool"]
        entries = [(r[1], r[2]) for r in tool_rows]
        canonical = canonicalize_names(entries, force_namespace=self.force_namespace)

        self._canon_to_tool_id.clear()
        self._raw_to_tool_ids.clear()
        self._tool_id_to_canon.clear()
        self._server_raw_to_canon.clear()

        for tool_id, server_id, name, _item_type in tool_rows:
            canon = canonical[(server_id, name)]
            self._canon_to_tool_id[canon] = tool_id
            self._tool_id_to_canon[tool_id] = canon
            self._raw_to_tool_ids[name].append(tool_id)
            self._server_raw_to_canon[(server_id, name)] = canon

        self._initialized = True

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("ToolRegistry.initialize() has not been called")

    async def resolve(self, name: str) -> Optional[ToolHandle]:
        """Look up a tool by canonical name, falling back to raw name when
        unambiguous. Returns ``None`` on miss or ambiguous raw lookup."""

        self._ensure_initialized()

        tool_id = self._canon_to_tool_id.get(name)
        if tool_id is None:
            candidates = self._raw_to_tool_ids.get(name, [])
            if len(candidates) == 1:
                tool_id = candidates[0]
            else:
                # Either zero matches or ambiguous raw-name lookup; bail out.
                return None

        record = await self.db.get_tool_by_id(tool_id)
        if record is None:
            return None
        return _record_to_handle(record, self._tool_id_to_canon[tool_id])

    async def list_handles(self) -> List[ToolHandle]:
        """Hydrate every executable tool, with canonical names applied.

        Performs a single bulk DB query so listing N tools costs O(1)
        connections instead of N.
        """

        self._ensure_initialized()
        tool_ids = list(self._tool_id_to_canon.keys())
        records = await self.db.list_tools_by_ids(tool_ids)
        return [
            _record_to_handle(r, self._tool_id_to_canon[r.tool_id])
            for r in records
            if r.tool_id in self._tool_id_to_canon
        ]

    def canonical_for(self, server_id: str, raw_name: str) -> Optional[str]:
        """Synchronous reverse lookup keyed by ``(server_id, raw_name)``.

        Hot path: called once per row by ``SearchCore`` when rewriting
        names in ``tools/list``. Backed by an O(1) dict built in
        :meth:`initialize`.
        """

        self._ensure_initialized()
        return self._server_raw_to_canon.get((server_id, raw_name))


def _record_to_handle(record: ToolRecord, canonical_name: str) -> ToolHandle:
    return ToolHandle(
        tool_id=record.tool_id,
        server_id=record.server_id or "",
        canonical_name=canonical_name,
        raw_name=record.name,
        item_type=record.item_type or "tool",
        description=record.description,
        input_schema=record.input_schema,
        annotations=record.annotations,
        route_metadata=record.route_metadata,
    )
