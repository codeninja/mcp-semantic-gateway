# 03 — Atlas

The Atlas is the source-of-truth representation of any ingested API.
The model is largely inherited from the prior design
([../api-introspection-skills/01-atlas.md](../api-introspection-skills/01-atlas.md)).
This document records the deltas and the consolidated shape.

## What carries forward unchanged

- Entity / Edge / TypeRef / Param model.
- Closed `EntityKind` enum (with one addition, see below).
- Closed `EdgeKind` enum (with additions, see below).
- Per-entity content hashing.
- Per-snapshot fingerprint hashing.
- SQLite as source of truth; Qdrant as derived index.
- Doc text normalization before hashing.

## Deltas

### `EntityKind` — adds `pattern`

```
class | interface | struct | enum | function | method | property |
constructor | signal | event | endpoint | parameter_group | module |
namespace | pattern        ← new
```

`pattern` is materialized by the pattern-mining module
([05](05-pattern-mining.md)). It lives in the Atlas because patterns
cite entities by id and inherit Atlas snapshot identity. Pattern bodies
have their own structured payload (subkinds, links, evidence) that
parsers don't produce — only miners do.

### `EdgeKind` — adds three

```
extends | implements | contains | emits | returns | accepts |
calls | overrides | references | example_of |
participates_in     ← new: entity ↔ pattern
co_occurs_with      ← new: deterministic co-occurrence in examples (lightweight)
sequenced_with      ← new: ordering relation observed in examples
```

`participates_in` is the primary edge between an entity and a pattern.
`co_occurs_with` and `sequenced_with` are *raw* edges produced by
deterministic miners; they are inputs to higher-confidence patterns.
The miner records them so the same evidence isn't re-derived later.

### Snapshot pattern count

`AtlasSnapshot` gains a `pattern_count` field for telemetry; it is not
load-bearing.

```python
class AtlasSnapshot(BaseModel):
    id: str
    source_id: str
    source_version: str
    source_major: int
    raw_hash: str
    snapshot_hash: str
    entity_count: int
    edge_count: int
    pattern_count: int = 0          # NEW
    parser_version: str             # renamed from adapter_version
    created_at: datetime
```

The naming change `adapter_version → parser_version` follows the
sources/adapters refactor ([02](02-sources.md)). Migration is a one-line
rename in the SQLite schema.

## Hash hierarchy

Restated for centrality:

```
entity_hash    = sha256(canonical_json(name, qualified_name, signature, params, return_type, doc, deprecated))
group_hash     = sha256(sorted(entity_hashes_in_group))
snapshot_hash  = sha256(sorted(entity_hashes) + sorted(edge_serializations))
pattern_hash   = sha256(canonical_json(kind, participants, evidence_summary))
```

`pattern_hash` is the cache key for surgical updates of patterns:
entity hashes change → patterns referencing those entities are
invalidated. A pattern with no changed participants is a fast-forward.

A `semantic_hash` variant excludes cosmetic doc-only fields. Surgical
updates compare semantic hashes for change classification while
recording full hashes for diagnostics.

## Storage

```
.mcp_semantic_gateway/
├── atlas.db                                # SQLite source of truth
├── atlas/
│   └── <source>/
│       └── <source-version>/
│           ├── snapshot.json               # full snapshot, content-addressed
│           ├── raw/                        # cached raw acquisition
│           └── patterns/                   # mined patterns, by pattern_id
└── ...
```

Patterns live in their own subdirectory because they have their own
generation lineage and may be regenerated independently of the snapshot
that produced them.

## SQLite tables

Mirrors the prior schema with the additions:

```sql
CREATE TABLE atlas_snapshots (
    id              TEXT PRIMARY KEY,
    source_id       TEXT REFERENCES sources(id),
    source_version  TEXT NOT NULL,
    source_major    INTEGER NOT NULL,
    raw_hash        TEXT NOT NULL,
    snapshot_hash   TEXT NOT NULL,
    parser_version  TEXT NOT NULL,
    entity_count    INTEGER NOT NULL,
    edge_count      INTEGER NOT NULL,
    pattern_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP NOT NULL
);

CREATE TABLE entities (
    id              TEXT NOT NULL,
    snapshot_id     TEXT REFERENCES atlas_snapshots(id),
    kind            TEXT NOT NULL,
    parent_id       TEXT,
    qualified_name  TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    semantic_hash   TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    PRIMARY KEY (id, snapshot_id)
);

CREATE TABLE edges (
    snapshot_id     TEXT REFERENCES atlas_snapshots(id),
    src_id          TEXT NOT NULL,
    dst_id          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    evidence_json   TEXT,                    -- per-edge metadata, e.g. example file + lineno
    confidence      REAL DEFAULT 1.0,
    PRIMARY KEY (snapshot_id, src_id, dst_id, kind)
);

CREATE TABLE patterns (
    id              TEXT PRIMARY KEY,
    snapshot_id     TEXT REFERENCES atlas_snapshots(id),
    kind            TEXT NOT NULL,           -- 'co-occurrence' | 'sequence' | 'idiom' | 'constraint' | 'use-case'
    determinism     TEXT NOT NULL,           -- 'deterministic' | 'statistical' | 'llm-induced'
    description     TEXT NOT NULL,
    payload_json    TEXT NOT NULL,           -- structured pattern body
    pattern_hash    TEXT NOT NULL,
    confidence      REAL NOT NULL,
    discriminator_passes TEXT NOT NULL,      -- json array of pass names
    generated_at    TIMESTAMP NOT NULL
);

CREATE TABLE pattern_participants (
    pattern_id      TEXT REFERENCES patterns(id),
    entity_id       TEXT NOT NULL,
    role            TEXT,                    -- 'subject' | 'collaborator' | 'precondition' | 'postcondition'
    PRIMARY KEY (pattern_id, entity_id)
);
```

The `evidence_json` column on `edges` is what lets pattern miners cite
their sources without re-walking the corpus.

## Atlas as a small library

The `atlas` module exports a thin Python API on top of the schema:

```python
class AtlasRepository:
    async def write_snapshot(self, snapshot: AtlasSnapshot) -> None: ...
    async def read_snapshot(self, snapshot_id: str) -> AtlasSnapshot: ...
    async def latest_snapshot(self, source_id: str) -> AtlasSnapshot | None: ...

    async def read_entity(self, entity_id: str, snapshot_id: str) -> Entity: ...
    async def list_entities(self, snapshot_id: str, *, kind: EntityKind | None = None) -> Iterable[Entity]: ...
    async def neighbors(self, entity_id: str, snapshot_id: str, edge_kinds: set[EdgeKind]) -> list[Edge]: ...

    async def read_pattern(self, pattern_id: str) -> Pattern: ...
    async def list_patterns(self, snapshot_id: str, *, kind: PatternKind | None = None) -> Iterable[Pattern]: ...
    async def patterns_for_entity(self, entity_id: str) -> list[Pattern]: ...

    async def diff_snapshots(self, prior_id: str, current_id: str) -> SnapshotDiff: ...
```

`SnapshotDiff` is the input to the update pipeline
([08](08-caching-and-updates.md)).

## What `atlas` does NOT do

- It does not run parsers — parsers write to the repository through the
  ingestion orchestrator.
- It does not embed — embeddings are a knowledge-index concern.
- It does not call LLMs.
- It does not know about patterns *as a generation pipeline*; it stores
  what miners produce.

## Open implementation choices

- **Pattern payload schema.** The `payload_json` column is loosely
  typed in v1; we ship JSON-Schemas per `PatternKind` for validation
  but do not enforce at the SQL layer. Tighten in v2.
- **Edge confidence semantics.** `confidence` is parser-supplied for
  derived edges (e.g., `references` from prose). Miners may produce
  edges with confidence < 1.0; consumers must respect it.
