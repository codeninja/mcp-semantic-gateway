# 03 — Storage and Retrieval

The current gateway uses hnswlib for vector search and SQLite for metadata.
This works for the existing tool/prompt/skill index but is not enough for
the new pipeline: we need hybrid (sparse + dense) search, payload filtering
on multiple axes, multi-collection routing, and a re-ranker stage. Decision
10 is to migrate the vector store to **Qdrant**.

## Why Qdrant

- Native hybrid retrieval (sparse + dense + filter) in a single query.
- First-class payload filtering — `source`, `source_version`, `status`,
  `tenant_id`, `kind` are direct predicates, not bolt-ons.
- Embedded mode (in-process) for development and tests; server mode for
  production. Same client API.
- Fast and operationally simple compared to Weaviate; substantially more
  capable than Chroma on hybrid and filtering.
- Python client is mature; payload schemas are typed.

Trade-off accepted: we lose the simplicity of hnswlib. We gain
first-class hybrid retrieval, which is required for the API-symbol-keyword
case discussed during planning (e.g., a query that quotes `RigidBody3D`
verbatim).

## Collections

Three collections, each tuned for its retrieval role.

### `skills` (primary)

The retrieval layer hits this collection first. One point per *published*
skill version.

```
Point {
    id:         "<source>:<source_major>:<skill_id>:<skill_version>"
    vector:     dense embedding of SKILL.md frontmatter description (+ HyDE-rewritten variant, see below)
    sparse:     BM25 sparse vector over description + skill name
    payload:    {
        skill_id, skill_version, source, source_version, source_major,
        atlas_snapshot_id, status, lineage_root, parent_version,
        partition_groups: [...],
        feedback_score: float,
        published_at: timestamp,
        tenant_id: string?           # nullable; null = global
    }
}
```

Filters used at query time:

- `status == "published"` (always)
- `source IN [user_configured_sources]`
- `source_major == <pinned_major>` if the requesting tenant has pinned to a major
- `tenant_id IN [<query_tenant>, null]`

### `use_cases` (synthesized intent corpus)

One point per use case. Use cases are generated during ingest (see
[05](05-synthesis-and-validation.md)) and are the connective tissue between
agent queries and skills.

```
Point {
    id:         "<source>:<source_major>:<use_case_id>"
    vector:     dense embedding of use case description
    sparse:     BM25 sparse vector over use case + linked entity qualified_names
    payload:    {
        use_case_id, source, source_version, source_major,
        linked_skill_ids: [...],     # may be empty if no skill exists yet (gap)
        linked_entity_ids: [...],
        cluster_id: string,
        confidence: float
    }
}
```

The retrieval layer queries `use_cases` in parallel with `skills`. A
matched use case yields its `linked_skill_ids`; the planner deduplicates
and merges with direct skill matches. Use cases without linked skills
become *gap signals* — they tell the planner "this is a known use case but
nothing satisfies it," which is actionable for the agent.

### `entities` (Atlas fallback)

One point per Atlas entity. Used when both `skills` and `use_cases` miss,
to give the planner *something* to return — raw API surface that the agent
can improvise from.

```
Point {
    id:         "<source>:<entity_id>:<snapshot_id>"
    vector:     dense embedding of (qualified_name + signature + doc)
    sparse:     BM25 sparse vector emphasizing qualified_name
    payload:    {
        entity_id, kind, source, source_version, snapshot_id,
        qualified_name, parent_id, partition_group: string
    }
}
```

This collection rebuilds entirely on each Atlas snapshot. It is large
(thousands to tens of thousands of points per source) but cheap to maintain
because entity hashes are already computed.

## Hybrid retrieval

For each collection, every query is a hybrid query:

1. Embed the (possibly HyDE-rewritten) query text → dense vector.
2. Tokenize the original query → sparse BM25 vector.
3. Issue a single Qdrant query with both vectors and a fusion score.
4. Apply payload filters (decided by tenant context, source pinning, status).

Qdrant's `Query` API supports this directly; we don't build it from scratch.

### HyDE rewrite

Before embedding, an LLM expands the agent's query into a hypothetical answer
paragraph (HyDE). The embedded text is the hypothetical answer, not the
question. This bridges the vocabulary gap between "I want my character to
jump" (agent vocab) and "apply_impulse with Vector3.UP" (API vocab).

The HyDE rewrite is a small, cached LLM call. Cache key is
`hash(query_text + source_set + source_majors)`.

### Re-ranker

Optional second stage: pull top-50 from each collection, run a cross-encoder
re-ranker (or a tiny LLM-as-judge call) to top-10. Re-ranking is the
single largest quality lever after hybrid retrieval. v1 ships with a
configurable cross-encoder via sentence-transformers (e.g.,
`cross-encoder/ms-marco-MiniLM-L-6-v2`), with a `disable_reranker = true`
flag for latency-sensitive deployments.

## Graph-aware expansion

Once a skill or use case is selected, the planner expands the result set
by traversing the Atlas graph for *related* entities the agent will likely
need. Cheap: SQLite already holds the edges.

Traversal rules:

- Starting nodes: every `entity_id` in the skill's `atlas_dependencies`.
- Edges followed: `extends`, `contains`, `emits`, `returns`, `accepts`.
- Depth: 1 by default; 2 if the result set is small.
- Result: an annotated bundle of supplementary entities the planner can
  surface as "you'll also want to know about X."

This is GraphRAG-shaped but bounded — we never traverse to populate the
retrieval candidates, only to enrich an already-selected result.

## Auth pass-through

Skills don't carry credentials. They reference auth by name. The chain:

1. MCP source config (existing `config.toml`) declares auth per source:
   ```toml
   [servers.stripe]
   type = "openapi"
   url = "https://api.stripe.com/openapi.json"
   auth = { type = "bearer", env = "STRIPE_API_KEY" }
   ```
2. The Source Adapter's `tool_requirements` map declares which Atlas entity
   kinds need auth.
3. The synthesizer marks the skill's `.meta.json` with `requires_auth: ["stripe"]`.
4. At execution time, the gateway's executor reads `STRIPE_API_KEY` from
   the configured location and injects it into the outbound request.
5. Skills that don't require auth (e.g., a Godot skill that only generates
   local files) have an empty `requires_auth` list and skip the chain.

Sources without auth declarations work transparently — adapters that don't
emit `requires_auth` produce skills without it.

## Filter design — payload index plan

Qdrant supports payload indexes for fast filtering. We declare:

| Field | Index type | Reason |
|---|---|---|
| `source` | keyword | Filter by configured sources |
| `source_major` | integer | Major-version pinning |
| `status` | keyword | Always-on `published` filter |
| `tenant_id` | keyword | Multi-tenant overlay |
| `partition_groups` | keyword (array) | Restrict to specific API regions |
| `feedback_score` | float | Range filter for "trusted skills only" mode |

Adding a payload field later is a no-op rebuild on Qdrant; adding an index
is a one-time migration.

## SQLite schema additions

Beyond the existing `metadata_db.py` tables, the new pipeline needs:

```sql
CREATE TABLE sources (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,          -- 'openapi' | 'godot-xml' | ...
    config_json     TEXT NOT NULL,
    last_ingest_at  TIMESTAMP
);

CREATE TABLE atlas_snapshots (
    id              TEXT PRIMARY KEY,
    source_id       TEXT REFERENCES sources(id),
    source_version  TEXT NOT NULL,
    source_major    INTEGER NOT NULL,
    raw_hash        TEXT NOT NULL,
    snapshot_hash   TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE entities (
    id              TEXT NOT NULL,
    snapshot_id     TEXT REFERENCES atlas_snapshots(id),
    kind            TEXT NOT NULL,
    parent_id       TEXT,
    qualified_name  TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    PRIMARY KEY (id, snapshot_id)
);

CREATE TABLE edges (
    snapshot_id     TEXT REFERENCES atlas_snapshots(id),
    src_id          TEXT NOT NULL,
    dst_id          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, src_id, dst_id, kind)
);

CREATE TABLE use_cases (
    id              TEXT PRIMARY KEY,
    snapshot_id     TEXT REFERENCES atlas_snapshots(id),
    description     TEXT NOT NULL,
    cluster_id      TEXT,
    embedding_id    TEXT                    -- Qdrant point id reference
);

CREATE TABLE use_case_entities (
    use_case_id     TEXT REFERENCES use_cases(id),
    entity_id       TEXT NOT NULL,
    PRIMARY KEY (use_case_id, entity_id)
);

CREATE TABLE skills (
    skill_id        TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    source_major    INTEGER NOT NULL,
    skill_version   INTEGER NOT NULL,
    lineage_root    INTEGER NOT NULL,
    parent_version  INTEGER,
    snapshot_id     TEXT REFERENCES atlas_snapshots(id),
    status          TEXT NOT NULL,
    package_path    TEXT NOT NULL,          -- relative to project root
    description     TEXT NOT NULL,
    feedback_score  REAL DEFAULT 0,
    published_at    TIMESTAMP,
    PRIMARY KEY (skill_id, source_id, source_major, skill_version)
);

CREATE TABLE skill_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id        TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    source_major    INTEGER NOT NULL,
    skill_version   INTEGER NOT NULL,
    tenant_id       TEXT,
    signal          TEXT NOT NULL,          -- 'positive' | 'negative' | 'note'
    section_path    TEXT,                   -- optional: target a specific section
    notes           TEXT,
    created_at      TIMESTAMP NOT NULL
);
```

The existing `tools` / `prompts` tables remain unchanged.

## Retrieval flow (consolidated)

```
agent query
  │
  ├─→ HyDE rewrite (cached) ──┐
  │                            │
  │                            ▼
  ├─→ embed dense              ┐
  ├─→ tokenize sparse           │ payload filters: source, major, status, tenant
  │                            ▼
  ├─→ qdrant.use_cases (top-50)  ─┐
  ├─→ qdrant.skills    (top-50)  ─┤  RRF fusion
  ├─→ qdrant.entities  (top-50)  ─┘
  │
  ├─→ cross-encoder rerank (top-10)
  │
  ├─→ graph-aware expansion (Atlas edges, depth 1)
  │
  └─→ planner (consume) → response
```

## Open implementation choices

- **Embedding model.** Current code uses `all-MiniLM-L6-v2` (384-dim, fast,
  decent quality). For the new collections we may want a larger or more
  recent model (e.g., `bge-small-en-v1.5` at 384-dim, or `bge-base-en-v1.5`
  at 768-dim). Decision deferred until we benchmark on Godot data.
- **Sparse model for BM25 sparse vectors.** Qdrant supports both classic
  BM25 and learned sparse models (SPLADE-style). Classic BM25 is
  sufficient for v1; SPLADE is a Phase-2+ optimization.
- **Reranker hosting.** sentence-transformers cross-encoder runs locally
  but adds latency and memory. An LLM-as-judge reranker is more flexible
  but costs API calls per query. v1 default: local cross-encoder; LLM-judge
  available as a per-query opt-in.
