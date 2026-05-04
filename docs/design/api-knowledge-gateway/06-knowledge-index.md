# 06 — Knowledge Index

The knowledge-index module exposes the Atlas + patterns + use cases as
a hybrid retrieval surface. It is the engine behind the MCP knowledge
tools ([07](07-knowledge-mcp-surface.md)), behind the planner
([09](09-task-decomposition.md)), and behind any future consumer that
needs semantic access to the knowledge layer.

This module inherits decisions I-1 (Qdrant), I-2 (content-addressed
hashing), N-9 (entity collection ships before patterns).

## Collections

Three collections ship in v1. Skills, when added, get a fourth managed
by the skill-gen module (not knowledge-index).

### `entities`

One point per Atlas entity. Largest collection by point count. Used
when query intent is direct API lookup.

```
Point {
    id:      "<source>:<entity_id>:<snapshot_id>"
    vector:  dense embedding of (qualified_name + signature + normalized doc)
    sparse:  BM25 over (qualified_name + signature)
    payload: {
        entity_id, kind, source, source_version, source_major,
        snapshot_id, qualified_name, parent_id,
        partition_group, deprecated, since_version
    }
}
```

Ships in Phase 2. The knowledge surface is queryable on Day 1 of
ingest, returning entity hits, before any pattern mining runs.

### `patterns`

One point per non-superseded `Pattern`. Used when query intent involves
how-to or implementation-style questions.

```
Point {
    id:      "<source>:<pattern_id>"
    vector:  dense embedding of pattern.description
    sparse:  BM25 over (description + participant qualified_names)
    payload: {
        pattern_id, kind, determinism, source, source_version, source_major,
        snapshot_id, participant_entity_ids: [...], partition_groups: [...],
        confidence, generated_at
    }
}
```

Ships in Phase 4. Until then, queries return entity hits only; once
the patterns collection is online, the MCP surface fuses results from
both collections (see "Retrieval flow" below). Each collection is an
additive capability — adding patterns does not change how entity
queries work.

### `use_cases`

Synthesized intents — the bridge between agent vocabulary and API
vocabulary. Use cases with linked patterns/skills serve as the
high-confidence retrieval path; use cases without linked patterns
serve as **gap signals**.

```
Point {
    id:      "<source>:<use_case_id>"
    vector:  dense embedding of use case description
    sparse:  BM25 over (description + linked entity qualified_names)
    payload: {
        use_case_id, source, source_version, source_major,
        snapshot_id, linked_entity_ids: [...], linked_pattern_ids: [...],
        linked_skill_ids: [...] | null, cluster_id, confidence
    }
}
```

Ships in Phase 4 alongside patterns.

## Hybrid retrieval

Every query against every collection is hybrid:

1. **Dense vector** — embedding of HyDE-rewritten query (or original
   if HyDE disabled).
2. **Sparse vector** — BM25 tokens of the original query.
3. **Payload filters** — `source`, `source_major`, `snapshot_id` (when
   pinned), `kind`, `tenant_id`, `partition_groups`.
4. **Score fusion** — Qdrant's RRF (Reciprocal Rank Fusion) or
   weighted sum. Default RRF.

Returns `top_k` ranked results per collection, deduplicated and
re-ranked across collections by the MCP surface.

## HyDE

HyDE (Hypothetical Document Embeddings) bridges agent vocabulary →
API vocabulary. The query "I want my character to jump" embeds poorly
against the API vocabulary "apply_impulse with Vector3.UP". A small
LLM call rewrites the query into a hypothetical answer paragraph, and
the answer's embedding is what queries the dense index.

```python
class HyDERewriter:
    async def rewrite(self, query: str, *, source_set: set[str]) -> str: ...
```

Cache key: `hash(query_text + sorted(source_set))`. TTL 24h.

For very short queries (< 5 tokens), HyDE materially helps. For
already-API-vocabulary queries (`RigidBody3D.apply_impulse`), HyDE adds
noise — a heuristic detects API-name-like queries and skips HyDE,
embedding the original query directly.

## Reranking

Optional cross-encoder rerank. After per-collection top-50 retrieval
and cross-collection fusion, a cross-encoder scores each (query,
result) pair and reduces to final top-K. Default model:
`cross-encoder/ms-marco-MiniLM-L-6-v2`. Disable per-deployment if
latency-sensitive.

A future LLM-judge reranker is available as a per-query opt-in for
agents willing to pay extra latency for quality.

## Graph-aware expansion

Once a candidate result is selected, the index can return supplementary
entities the agent will likely need. This is *enrichment*, not
*candidate generation*: graph expansion never changes which results
are returned, only what additional context accompanies them.

```python
class GraphExpander:
    async def expand(
        self,
        seeds: list[str],            # entity_ids
        *,
        depth: int = 1,
        edge_kinds: set[EdgeKind] = ...,
        max_results: int = 25,
    ) -> list[EntityHit]: ...
```

Edges traversed by default: `extends`, `contains`, `emits`, `returns`,
`accepts`, `participates_in`. Pattern participation edges are
particularly valuable — expanding from an entity to the patterns it
participates in surfaces "you'll also want to know how this is
typically used."

## Filters

All filters are payload predicates. Indexed fields:

| Field | Type | Used by |
|---|---|---|
| `source` | keyword | source pinning |
| `source_major` | int | major-version pinning |
| `snapshot_id` | keyword | reproducibility (pin to a snapshot) |
| `kind` | keyword | entity-kind / pattern-kind / use-case filter |
| `partition_groups` | keyword (array) | scope to API region |
| `tenant_id` | keyword | multi-tenant overlay |
| `deprecated` | bool | exclude deprecated entities |
| `confidence` | float | range filter for "trusted patterns only" |
| `determinism` | keyword | deterministic-only / no-LLM-induced filtering |

The `determinism` filter is what lets cautious deployments query
patterns mined deterministically only, ignoring LLM-induced patterns.

## Retrieval flow

```
agent query
  │
  ├─→ HyDE rewrite (cached, optional) ──┐
  │                                      │
  ├─→ embed dense                        │
  ├─→ tokenize sparse                    │ payload filters
  │                                      ▼
  ├─→ qdrant.entities  (top-50) ─┐
  ├─→ qdrant.patterns  (top-50) ─┤  RRF fusion
  ├─→ qdrant.use_cases (top-50) ─┘
  │
  ├─→ cross-encoder rerank (top-K)
  │
  ├─→ graph expansion (depth 1) ─→ supplementary
  │
  └─→ result bundle: {primary, supplementary, gaps?}
```

Skills, when present, query a parallel `skills` collection in the
skill-gen module and contribute to fusion identically.

## Storage layout

Embedded Qdrant for development:

```
.mcp_semantic_gateway/
├── qdrant/                              # embedded mode storage
│   ├── entities/
│   ├── patterns/
│   └── use_cases/
└── ...
```

Server mode for production: same client API, different connection
URL. Migrations between modes are a re-index from SQLite (which
remains the source of truth).

## Embedding model selection

Default: `bge-small-en-v1.5` (384-dim). Fast, decent quality, good
licensing. Per-deployment overridable via config. v1 standardizes one
model; multi-model collections are post-v1.

For the API-symbol-keyword case (queries that quote `RigidBody3D`
verbatim), the BM25 sparse vector carries the exact match; the dense
vector handles semantic match. Hybrid + RRF gets both right.

## What knowledge-index does NOT do

- It does not produce embeddings of generated content (skills, plans).
  Those are owned by the producing module.
- It does not compute pattern confidence — that comes from the miner.
- It does not gate by retrieval fitness — fitness is a discriminator
  concern in skill-gen.
- It does not make tool-handler decisions — the MCP surface composes.

## Open implementation choices

- **Sparse model upgrade.** Classic BM25 in v1; SPLADE-style learned
  sparse vectors in v2 if quality suffers.
- **Multi-vector per point for HyDE.** The prior design noted Qdrant
  supports multiple named vectors per point. We can store
  api-vocab + agent-vocab embeddings under one point; revisit when
  measurable.
- **Re-index throughput.** Atlas snapshot rebuilds re-embed entities.
  Embedding ~30k entities is minutes, not hours, at default batch
  size; budget for faster batches if Godot-scale snapshots show
  throughput issues.
