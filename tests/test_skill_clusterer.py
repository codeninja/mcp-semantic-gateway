"""Tests for `ingestion/skill_clusterer.py` — Phase G."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mcp_semantic_gateway.ingestion.skill_clusterer import (
    UseCaseCluster,
    cluster_use_cases,
)
from mcp_semantic_gateway.ingestion.use_case_miner import UseCaseRecord


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Deterministic embedder that maps each input string to a preset vector."""

    def __init__(self, mapping: dict[str, list[float]]):
        self._mapping = mapping

    def embed(self, texts):
        return [self._mapping[t] for t in texts]


_EPOCH = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _mk_record(
    *,
    seq: int,
    description: str,
    linked_tools: list[str],
    use_case_hash: str | None = None,
    server_id: str = "srv-test",
    source_hash: str = "src-hash-aaaa",
) -> UseCaseRecord:
    """Build a minimal valid :class:`UseCaseRecord`.

    Descriptions in tests must satisfy the >=50 char min — pad as
    needed by the caller. ``use_case_hash`` defaults to a deterministic
    derivation from ``seq``.
    """

    h = use_case_hash if use_case_hash is not None else f"hash-{seq:04d}"
    return UseCaseRecord(
        id=f"uc-{server_id}-grp-{seq}",
        server_id=server_id,
        source_hash=source_hash,
        chunk_id=f"chunk-{server_id}-grp-{seq // 5}",
        description=description,
        linked_tool_names=linked_tools,
        prerequisites=[],
        confidence=0.9,
        generated_by="model-x@v1",
        generated_at=_EPOCH,
        use_case_hash=h,
    )


def _pad(text: str) -> str:
    """Pad to the 50-char minimum description length."""
    if len(text) >= 50:
        return text
    return (text + " " + "x" * 60)[:80].strip().ljust(50, "x")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_identical_descriptions_cluster_together():
    desc = _pad("Search GitHub issues and label them based on triage rules.")
    embedder = FakeEmbedder({desc: [1.0, 0.0, 0.0]})

    records = [
        _mk_record(seq=0, description=desc, linked_tools=["t1"]),
        _mk_record(seq=1, description=desc, linked_tools=["t1", "t2"]),
        _mk_record(seq=2, description=desc, linked_tools=["t3"]),
    ]

    clusters = cluster_use_cases(records, embedder=embedder, threshold=0.78)

    assert len(clusters) == 1
    assert len(clusters[0].use_case_ids) == 3


def test_orthogonal_descriptions_stay_split():
    d1 = _pad("Search GitHub issues across repos and triage them.")
    d2 = _pad("Manage Slack channels and post threaded replies.")
    d3 = _pad("Format JSON output and pretty-print structured data.")

    embedder = FakeEmbedder(
        {
            d1: [1.0, 0.0, 0.0],
            d2: [0.0, 1.0, 0.0],
            d3: [0.0, 0.0, 1.0],
        }
    )

    records = [
        _mk_record(seq=0, description=d1, linked_tools=["gh"]),
        _mk_record(seq=1, description=d2, linked_tools=["slack"]),
        _mk_record(seq=2, description=d3, linked_tools=["fmt"]),
    ]

    clusters = cluster_use_cases(records, embedder=embedder, threshold=0.78)
    assert len(clusters) == 3


def test_threshold_tuning_is_monotonic():
    # Three vectors with varying cosine similarities so threshold matters.
    d1 = _pad("desc one alpha")
    d2 = _pad("desc two beta")
    d3 = _pad("desc three gamma")

    embedder = FakeEmbedder(
        {
            d1: [1.0, 0.0],
            d2: [0.95, 0.3122498999199199],   # cos ~0.95 with d1
            d3: [0.7, 0.7142136],              # cos ~0.7 with d1
        }
    )

    records = [
        _mk_record(seq=0, description=d1, linked_tools=["a"]),
        _mk_record(seq=1, description=d2, linked_tools=["b"]),
        _mk_record(seq=2, description=d3, linked_tools=["c"]),
    ]

    counts = []
    for thr in [0.5, 0.6, 0.7, 0.8, 0.9, 0.99]:
        # rebuild embedder per call (no state between)
        e = FakeEmbedder(
            {
                d1: [1.0, 0.0],
                d2: [0.95, 0.3122498999199199],
                d3: [0.7, 0.7142136],
            }
        )
        clusters = cluster_use_cases(records, embedder=e, threshold=thr)
        counts.append(len(clusters))

    # Non-decreasing.
    for prev, curr in zip(counts, counts[1:]):
        assert curr >= prev, f"cluster count decreased: {counts}"
    # And the extremes really do differ for this fixture.
    assert counts[0] < counts[-1]


def test_cluster_hash_is_deterministic_and_changes_with_membership():
    desc = _pad("identical description for hash determinism check.")
    embedder = FakeEmbedder({desc: [1.0, 0.0]})

    r0 = _mk_record(
        seq=0, description=desc, linked_tools=["t1"], use_case_hash="hA"
    )
    r1 = _mk_record(
        seq=1, description=desc, linked_tools=["t1"], use_case_hash="hB"
    )
    r2 = _mk_record(
        seq=2, description=desc, linked_tools=["t2"], use_case_hash="hC"
    )

    c_first = cluster_use_cases([r0, r1], embedder=embedder, threshold=0.5)
    c_again = cluster_use_cases([r0, r1], embedder=embedder, threshold=0.5)
    c_more = cluster_use_cases([r0, r1, r2], embedder=embedder, threshold=0.5)

    assert len(c_first) == 1
    assert len(c_again) == 1
    assert len(c_more) == 1
    assert c_first[0].cluster_hash == c_again[0].cluster_hash
    assert c_first[0].cluster_hash != c_more[0].cluster_hash


def test_cluster_id_seq_is_contiguous_from_zero():
    descs = [_pad(f"unique description number {i} alpha beta") for i in range(4)]
    # Each vector orthogonal so each becomes its own cluster.
    vectors = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    embedder = FakeEmbedder(dict(zip(descs, vectors)))

    records = [
        _mk_record(seq=i, description=descs[i], linked_tools=[f"t{i}"])
        for i in range(4)
    ]
    clusters = cluster_use_cases(records, embedder=embedder, threshold=0.78)

    assert [c.cluster_id for c in clusters] == [
        "cluster-srv-test-0",
        "cluster-srv-test-1",
        "cluster-srv-test-2",
        "cluster-srv-test-3",
    ]


def test_tool_name_union_is_deduplicated():
    desc = _pad("identical description so all members cluster together.")
    embedder = FakeEmbedder({desc: [1.0, 0.0]})

    records = [
        _mk_record(seq=0, description=desc, linked_tools=["a", "b"]),
        _mk_record(seq=1, description=desc, linked_tools=["b", "c"]),
        _mk_record(seq=2, description=desc, linked_tools=["c", "a", "d"]),
    ]
    clusters = cluster_use_cases(records, embedder=embedder, threshold=0.5)
    assert len(clusters) == 1
    union = clusters[0].tool_name_union
    assert sorted(union) == ["a", "b", "c", "d"]
    # Deduplicated (no duplicates).
    assert len(union) == len(set(union))


def test_centroid_description_is_a_member_description():
    # Build three semantically-similar but distinct descriptions; pick
    # vectors so that the medoid is unambiguously the second member.
    d_far_a = _pad("description A which sits far from the geometric center.")
    d_medoid = _pad("description M which sits at the geometric center hub.")
    d_far_b = _pad("description B which sits opposite of A and not central.")

    embedder = FakeEmbedder(
        {
            d_far_a: [1.0, 0.0],
            d_medoid: [0.7071067811865476, 0.7071067811865476],
            d_far_b: [0.0, 1.0],
        }
    )

    records = [
        _mk_record(seq=0, description=d_far_a, linked_tools=["x"]),
        _mk_record(seq=1, description=d_medoid, linked_tools=["x"]),
        _mk_record(seq=2, description=d_far_b, linked_tools=["x"]),
    ]
    clusters = cluster_use_cases(records, embedder=embedder, threshold=0.3)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.centroid_description in {d_far_a, d_medoid, d_far_b}
    # Medoid is the symmetric center of the three vectors.
    assert cluster.centroid_description == d_medoid


def test_singleton_cluster_uses_its_own_description():
    desc = _pad("a single use case that nobody else clusters with at all.")
    embedder = FakeEmbedder({desc: [1.0, 0.0]})

    records = [_mk_record(seq=0, description=desc, linked_tools=["t"])]
    clusters = cluster_use_cases(records, embedder=embedder, threshold=0.78)

    assert len(clusters) == 1
    assert clusters[0].centroid_description == desc
    assert clusters[0].use_case_ids == [records[0].id]


def test_mismatched_server_or_source_raises():
    d1 = _pad("desc one for clustering")
    d2 = _pad("desc two for clustering")

    embedder = FakeEmbedder({d1: [1.0, 0.0], d2: [0.0, 1.0]})

    r0 = _mk_record(
        seq=0, description=d1, linked_tools=["t"], server_id="srv-A"
    )
    r1 = _mk_record(
        seq=1, description=d2, linked_tools=["t"], server_id="srv-B"
    )
    with pytest.raises(ValueError):
        cluster_use_cases([r0, r1], embedder=embedder, threshold=0.78)

    r2 = _mk_record(
        seq=2,
        description=d1,
        linked_tools=["t"],
        server_id="srv-A",
        source_hash="other-hash",
    )
    r3 = _mk_record(
        seq=3,
        description=d2,
        linked_tools=["t"],
        server_id="srv-A",
        source_hash="src-hash-aaaa",
    )
    with pytest.raises(ValueError):
        cluster_use_cases([r2, r3], embedder=embedder, threshold=0.78)


def test_empty_input_returns_empty_list():
    assert cluster_use_cases([], threshold=0.78) == []
