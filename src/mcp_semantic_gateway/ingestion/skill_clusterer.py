"""Phase G — Use-case clustering.

Group semantically-similar :class:`UseCaseRecord` entries into
:class:`UseCaseCluster` records that downstream skill synthesis (Phase
H) turns into one skill each.

The algorithm is deterministic given a deterministic embedder:

1. Embed all use-case descriptions via :class:`LocalEmbedder`.
2. Greedy agglomerative clustering in input order. Each record either
   attaches to the highest-similarity existing cluster (cosine
   similarity >= ``threshold``) or opens a new cluster. Ties resolve to
   the earlier-created cluster.
3. The cluster centroid is updated as the running mean of member
   vectors.
4. The medoid (the member whose embedding is closest to the centroid)
   supplies ``centroid_description``. Singletons report their own
   description.
5. ``cluster_hash`` is the SHA-256 of the JSON-encoded sorted list of
   member ``use_case_hash`` values.

Cross-source clustering is out of scope for v1: all input records must
share the same ``(server_id, source_hash)``; otherwise ``ValueError``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence

import numpy as np
from pydantic import BaseModel, Field

from mcp_semantic_gateway.ingestion.embedder import LocalEmbedder
from mcp_semantic_gateway.ingestion.use_case_miner import UseCaseRecord


__all__ = ["UseCaseCluster", "cluster_use_cases"]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class UseCaseCluster(BaseModel):
    """A semantically coherent group of :class:`UseCaseRecord` entries."""

    cluster_id: str = Field(..., description="cluster-<server>-<seq>")
    server_id: str
    source_hash: str
    use_case_ids: list[str]
    tool_name_union: list[str]
    centroid_description: str
    cluster_hash: str


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _compute_cluster_hash(use_case_hashes: Sequence[str]) -> str:
    blob = json.dumps(sorted(use_case_hashes), separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _dedup_preserve_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cluster_use_cases(
    records: list[UseCaseRecord],
    *,
    embedder: LocalEmbedder | None = None,
    threshold: float = 0.78,
) -> list[UseCaseCluster]:
    """Group ``records`` by cosine similarity over description embeddings.

    Records must all share the same ``(server_id, source_hash)``.
    Returns clusters in assignment order with contiguous 0-indexed
    sequence numbers in ``cluster_id``.
    """

    if not records:
        return []

    # All records must share (server_id, source_hash).
    server_id = records[0].server_id
    source_hash = records[0].source_hash
    for r in records[1:]:
        if r.server_id != server_id or r.source_hash != source_hash:
            raise ValueError(
                "cluster_use_cases requires records to share "
                "(server_id, source_hash); got mismatched values"
            )

    if embedder is None:
        embedder = LocalEmbedder("all-MiniLM-L6-v2")

    descriptions = [r.description for r in records]
    raw_vectors = embedder.embed(descriptions)
    vectors = [np.asarray(v, dtype=np.float64) for v in raw_vectors]

    # Per-cluster accumulators.
    cluster_member_indices: list[list[int]] = []
    cluster_centroids: list[np.ndarray] = []

    for idx, vec in enumerate(vectors):
        best_cluster = -1
        best_sim = -1.0
        for ci, centroid in enumerate(cluster_centroids):
            sim = _cosine(vec, centroid)
            # Strictly greater preserves "earlier cluster wins on ties".
            if sim > best_sim:
                best_sim = sim
                best_cluster = ci

        if best_cluster >= 0 and best_sim >= threshold:
            cluster_member_indices[best_cluster].append(idx)
            members = cluster_member_indices[best_cluster]
            stacked = np.stack([vectors[i] for i in members], axis=0)
            cluster_centroids[best_cluster] = stacked.mean(axis=0)
        else:
            cluster_member_indices.append([idx])
            cluster_centroids.append(vec.copy())

    clusters: list[UseCaseCluster] = []
    for seq, member_idxs in enumerate(cluster_member_indices):
        members = [records[i] for i in member_idxs]
        member_vectors = [vectors[i] for i in member_idxs]
        centroid = cluster_centroids[seq]

        # Medoid: member whose vector has highest cosine similarity to the
        # centroid. Ties resolve to the earlier member.
        best_member_pos = 0
        best_member_sim = -1.0
        for pos, mvec in enumerate(member_vectors):
            sim = _cosine(mvec, centroid)
            if sim > best_member_sim:
                best_member_sim = sim
                best_member_pos = pos
        medoid_record = members[best_member_pos]

        tool_union: list[str] = []
        for m in members:
            tool_union.extend(m.linked_tool_names)
        tool_union = _dedup_preserve_order(tool_union)

        cluster_hash = _compute_cluster_hash(
            [m.use_case_hash for m in members]
        )

        clusters.append(
            UseCaseCluster(
                cluster_id=f"cluster-{server_id}-{seq}",
                server_id=server_id,
                source_hash=source_hash,
                use_case_ids=[m.id for m in members],
                tool_name_union=tool_union,
                centroid_description=medoid_record.description,
                cluster_hash=cluster_hash,
            )
        )

    return clusters
