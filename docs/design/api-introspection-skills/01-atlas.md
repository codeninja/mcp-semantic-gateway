# 01 — The Atlas

The Atlas is the unified, source-agnostic representation of an ingested API.
Every downstream component (use-case generation, skill synthesis, retrieval,
discriminator, surgical updates) reads from the Atlas and never from a raw
source file. New source types extend the system by writing a Source Adapter;
nothing else in the pipeline changes.

## Goals

- One representation, regardless of whether the source is OpenAPI, a class
  reference XML, a Sphinx site, an LSP dump, or a `.d.ts` file.
- Rich enough to capture the structural relationships that make non-naive
  retrieval possible (inheritance, signals, lifecycle hooks, references).
- Content-addressable at every level so caching and surgical updates fall
  out naturally.
- Versioned per source so skills can pin to the API revision they were
  generated against.

## Entity model

```
Entity {
    id:              string         # stable, source-scoped (e.g. "godot.RigidBody3D.apply_impulse")
    source_id:       string         # foreign key to Source
    snapshot_id:     string         # foreign key to AtlasSnapshot
    kind:            EntityKind     # see below
    parent_id:       string?        # for nested kinds (method-of-class, property-of-class)
    namespace:       string?        # module / package / tag
    name:            string
    qualified_name:  string         # display form
    signature:       string?        # method/function signature, if applicable
    params:          Param[]
    return_type:     TypeRef?
    doc:             string         # canonical description (extracted, normalized)
    examples:        Example[]      # extracted code/usage examples
    deprecated:      bool
    since_version:   string?
    content_hash:    string         # sha256 over name + signature + params + return_type + doc
}
```

### EntityKind

A closed set across all source types. Adding a kind is a breaking change to
the Atlas schema and requires every adapter to declare a mapping.

```
class | interface | struct | enum | function | method | property |
constructor | signal | event | endpoint | parameter_group | module | namespace
```

Adapters MAY produce a subset; consumers MUST handle absent kinds gracefully.
For OpenAPI: `endpoint`, `parameter_group`, `module` (for tags). For Godot:
`class`, `method`, `property`, `signal`, `enum`, `constructor`. For
TypeScript: `interface`, `function`, `module`.

### Param and TypeRef

```
Param {
    name:         string
    type:         TypeRef
    default:      string?           # textual default; not eval'd
    required:     bool
    doc:          string?
}

TypeRef {
    raw:          string            # e.g. "Vector3", "Array[Node]", "Optional[int]"
    resolved_id:  string?           # entity id if the type names another Atlas entity
    cardinality:  one | many | optional
}
```

`resolved_id` is the bridge that lets the retrieval layer follow type
relationships. When the synthesizer references a method, it can pull in the
referenced types' entities automatically.

## Edges

Relationships between entities are stored as typed edges. Edges are
first-class so graph-aware retrieval and surgical updates can traverse them.

```
Edge {
    snapshot_id:   string
    src_id:        string
    dst_id:        string
    kind:          EdgeKind
}
```

### EdgeKind

```
extends         # subclass → superclass
implements      # class → interface
contains        # class → method/property; module → function
emits           # class → signal
returns         # method → return type
accepts         # method → parameter type
calls           # method → method (when statically determinable)
overrides       # subclass method → superclass method
references     # doc cross-reference (lower confidence)
example_of      # example → entity it illustrates
```

Adapters produce the edges they can derive deterministically. Edges with
lower confidence (e.g. `references` from prose) are flagged and weighted
lower in graph traversal.

## Source Adapter contract

Every source type implements a Source Adapter. The contract is small and
strictly typed.

```python
class SourceAdapter(Protocol):
    source_kind: str                          # "openapi" | "godot-xml" | "sphinx" | ...

    async def acquire(self, config: SourceConfig) -> RawSnapshot:
        """Fetch / clone / download. Idempotent. Caches on content hash."""

    async def parse(self, raw: RawSnapshot) -> AtlasSnapshot:
        """Transform raw bytes into Atlas entities + edges. Deterministic."""

    def partition_hint(self, snapshot: AtlasSnapshot) -> PartitionMap:
        """Group entity ids into clusters that should become section boundaries
        in generated skill packages. See Section partitioning below."""

    def detect_version(self, raw: RawSnapshot) -> SourceVersion:
        """Extract semantic version (or content-hash fallback) from the source."""
```

Adapters MUST be deterministic given the same `RawSnapshot`. Non-determinism
breaks content hashing and surgical updates.

## Partition hints

The skill synthesizer slices a generated skill package into sections. Each
section's dependency set is the Atlas entities it covers; when any of those
hashes change, only that section is re-synthesized. The Source Adapter
controls how the API surface partitions, because what counts as a "natural
unit" varies by source.

```python
PartitionMap = dict[str, list[str]]   # group_id -> entity_ids
```

Recommended partition strategies per source kind:

| Source kind | Partition strategy | Group_id format |
|---|---|---|
| OpenAPI | by tag, fallback to first path segment | `tag:billing`, `path:/users` |
| Godot XML | by class | `class:RigidBody3D` |
| Sphinx (Python) | by module | `module:pandas.io.sql` |
| TypeScript `.d.ts` | by module / namespace | `module:@stripe/stripe-js` |
| LSP dump | by file/package | `pkg:net/http` |

A skill that touches three classes will produce three `references/<class>.md`
files plus examples partitioned by which class(es) they exercise. SKILL.md
procedure steps are tagged in `.meta.json` with the group(s) they belong to.

This rule is the answer to "how does section granularity work for any API":
the Source Adapter declares the natural unit; the synthesizer slices along
those boundaries.

## Atlas snapshots

```
AtlasSnapshot {
    id:               string         # uuid
    source_id:        string
    source_version:   string         # "4.4.1", or content hash if unversioned
    source_major:     int            # parsed from source_version; resets lineage
    raw_hash:         string         # hash of acquired source bytes
    entity_count:     int
    edge_count:       int
    created_at:       datetime
    adapter_version:  string         # which adapter version produced this
}
```

Snapshots are immutable. Re-running ingest against an unchanged source is
a cache hit on `raw_hash` and produces no new snapshot. Re-running against
a changed source produces a new snapshot; the prior one is retained.

The `source_major` field is what determines whether skills can carry
lineage forward (minor/patch bump) or must be cold-regenerated (major bump).
For sources without semantic versioning, the adapter declares its own
"major" boundary policy (e.g. content-hash distance threshold, or always-
treat-as-minor for monotonically additive APIs).

## Hashing

Three hash levels, each computed deterministically:

- **Entity hash** — `sha256(canonical_json({name, qualified_name, signature, params, return_type, doc, deprecated}))`. Stable across ingest runs.
- **Group hash** — `sha256(sorted(entity_hashes_in_group))`. Surgical-update key for a section.
- **Snapshot hash** — `sha256(sorted(entity_hashes) + sorted(edge_serializations))`. Fingerprint of an entire Atlas snapshot.

Doc text is normalized before hashing (whitespace collapse, link
canonicalization) so cosmetic doc changes don't trigger churn.

## Storage

Atlas data lives in two stores:

- **SQLite** (`.mcp_semantic_gateway/atlas.db`) — relational tables for
  `sources`, `atlas_snapshots`, `entities`, `edges`, `partitions`. Schema
  migrations follow the same pattern as the existing `metadata_db.py`.
- **Qdrant collections** — entity-level embeddings (for fallback when no
  use-case or skill matches the query) and use-case-level embeddings (for
  the primary retrieval path). See [03](03-storage-and-retrieval.md).

SQLite is the source of truth for entity content; Qdrant is a derived
index. Rebuilding Qdrant from SQLite must always be possible.

## Open implementation choices (acknowledged, not yet decided)

- **Doc normalization depth.** How aggressively to strip Markdown / RST
  formatting before hashing. Too little → cosmetic churn; too much → losing
  semantically meaningful structure (e.g. parameter tables).
- **Entity ID stability across renames.** If `Foo.bar()` renames to
  `Foo.baz()` between source versions, current model treats it as a delete
  + add. A future rename-detector could improve surgical-update yield, but
  is out of scope for v1.
